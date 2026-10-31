#!/usr/bin/env python3
"""mcp_serial: 串口写路径的 MCP 工具 + 车机确认 / 急停命令行 (#21). 规则全在 serial_write_core, 这里只管 IO.

工具 (cart_mcp 注册): lease_acquire / lease_renew / lease_release / send_command (行级命令) /
  send_key (回车 / 翻页空格 / Ctrl+C) / pending_list (只读) / estop (Agent 也能自停; 解除不给 Agent).
发送一律走 conbroker send (以 serial.conf 的 user 身份; 人 3 秒内敲过键它自己排队), 不直开串口.

状态: /run/ops-cart/serial-write.json (root 0600; 租约、挂起、在途、急停), 每次操作 flock 一把锁
  "读 -> 改 -> 原子写"; stdio 每个 Agent 一个进程、HTTP 多线程、车机命令行, 都经同一个文件.
  重启推车 /run 清空 = 租约、挂起、急停全部清掉 (挂起的命令不会在重启后冒出来).
日志: /var/log/ops-cart/serial-slot<N>.log, 每槽位一份 JSONL (时间、来源、谁、命令、闸门结果、回显摘要、
  确认 / 急停事件); 急停 / 解除 / 确认 / 拒绝另在 events.log 记一行摘要 + 日志路径.

车机命令行 (#23 的触摸屏确认框之前的确认通道):
    sudo python3 /opt/pxe/cart_mcp.py pending              看挂起的和最近结果
    sudo python3 /opt/pxe/cart_mcp.py confirm <编号>        确认发送 (只认推车本机终端, 要敲 yes)
    sudo python3 /opt/pxe/cart_mcp.py reject <编号>         拒绝 (哪都能拒)
    sudo python3 /opt/pxe/cart_mcp.py estop [槽位]          急停, 不给槽位 = 全部 (哪都能停)
    sudo python3 /opt/pxe/cart_mcp.py resume <槽位|all>     解除急停 (只认推车本机终端)
    sudo python3 /opt/pxe/cart_mcp.py requests             执行网页投来的急停 / 拒绝 (cart-serial-req 单元调)
"本机" = 不是经 sshd / tailscaled 进来的 (查祖先进程), 且 stdin 是终端. 车机上"退出到终端"就是本机.

确认 UI (#23): 车机 / 网页 / 副屏都用 peek() 只读看状态 (不加锁不写盘); 车机 (root, 本机) 点确认走
send_confirmed(), 和命令行同一条路. 网页 (ops-web) 写不了状态文件, 只能经 write_request() 往
/run/ops-cart/serial-req/ 投"停"方向的请求 (急停 / 拒绝), 由 root 的 cart-serial-req 单元 process_requests() 执行.
"""

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time

import atomicfile
import cartlog
import confirm_core as cc
import lamps_core
import mcp_core as mc
import serial_core
import serial_write_core as sw

STATE_PATH = "/run/ops-cart/serial-write.json"
ESTOP_FLAG = "serial-estop.json"   # 和状态文件同目录; conbroker 默认读 /run/ops-cart/serial-estop.json
REQ_DIR = "/run/ops-cart/serial-req"   # 网页投请求的目录 (ops-web 0700, tmpfiles 建), cart-serial-req.path 盯着
REQ_POLL_S = 0.2                # 半截请求文件隔多久再看
REQ_ROUNDS = 10                 # 最多看几轮 (= 等半截文件写完最多 2 秒)
LOG_DIR = cartlog.LOG_DIR
OUTPUT_MAX = 64 * 1024          # 回给 Agent 的设备输出上限
TAIL_LINES = 8                  # 发前看几行认厂商 / 提示符 / 名字
SEND_TIMEOUT = 30
SEND_TIMEOUT_MAX = 300
KEY_TIMEOUT = 10
SUMMARY_EVS = ("estop", "resume", "confirm", "reject")


def log_path(log_dir, slot):
    return os.path.join(log_dir, f"serial-slot{slot}.log")


# ------------------------------------------------------------ 状态文件 + 执行绪日志

class Store:
    """run(fn): 加锁 -> 读状态 -> fn(book, now) -> 存 (fn 抛异常也存: tick 清掉的过期要落盘) -> 写日志."""

    def __init__(self, path=STATE_PATH, log_dir=LOG_DIR, clock=time.time, event=cartlog.event):
        self.path, self.log_dir, self.clock, self.event = path, log_dir, clock, event

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return "\0"                     # 在但读不了: 按坏了算 -> 全部急停

    def run(self, fn):
        with self._locked():
            now = self.clock()
            book = sw.Book.from_text(self._read(), now)
            try:
                return fn(book, now)
            finally:
                atomicfile.write_text(self.path, book.dumps(), mode=0o600)
                self._flag(book)
                self._log(book.events)

    def _flag(self, book):
        """给 conbroker 的公开急停标记 (0644, 只有槽位号): 排队等人停手的那条, 写串口前它看这个丢掉."""
        stopped = [n for n in sw.SLOTS if book.stopped(n)]
        doc = {"slots": stopped, "all": len(stopped) == len(sw.SLOTS)}
        try:
            atomicfile.write_text(os.path.join(os.path.dirname(self.path), ESTOP_FLAG), json.dumps(doc), mode=0o644)
        except OSError:
            pass

    def _log(self, events):
        for rec in events:
            path = log_path(self.log_dir, rec["slot"])
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:     # 每行开一次: logrotate copytruncate 后照样写对
                    f.write(sw.log_line(rec) + "\n")
            except OSError:
                pass
            if rec["ev"] in SUMMARY_EVS and self.event is not None:
                self.event("serial-write", cartlog.fields(ev=rec["ev"], slot=rec["slot"], who=rec.get("who"),
                                                          id=rec.get("id"), log=path))

    def view(self, slot=None):
        return self.run(lambda b, now: b.view(now, slot))


def peek(path=STATE_PATH, clock=time.time):
    """只读看一眼 (车机 / 网页 / 副屏): 不加锁不写盘, 过期的在内存里算掉. 读不了 / 读不懂 = 全部急停.
    写状态的一方都是原子改名, 不加锁也读不到半个文件."""
    now = clock()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError):
        return cc.stopped_view("读不到状态文件")
    return sw.Book.from_text(text, now).view(now)


def writers(store):
    """{槽位: 写入者 或 None} 给 list_threads; 读不到状态就全是 None, 不抛."""
    try:
        return {s["slot"]: s["writer"] for s in store.view()["slots"]}
    except Exception:                   # noqa: BLE001  列执行绪不能因为这个挂
        return {n: None for n in sw.SLOTS}


# ------------------------------------------------------------ conbroker send

def conbroker_send(dev, text, timeout, raw=False, run=subprocess.run):
    """-> (status, 输出, 说明). status: sent / refused (conbroker 发前把关没发) / timeout (发了没等到提示符) / error.
    最后一道检查: 普通行再过一遍 check_line, raw 只收固定特殊键; 不合格不起子进程."""
    if raw:
        if text not in {k["text"] for k in sw.KEYS.values() if k["raw"]}:
            raise sw.Refused("bad", "raw 只收固定特殊键")
    elif text != "":
        sw.check_line(text)
    args = serial_core._conbroker_cmd() + ["send", dev, text, "-t", str(int(timeout))] + (["--raw"] if raw else [])
    try:
        r = run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        return "timeout", "", "conbroker send 子进程超时 (可能还在等人停手, 已交出去的收不回来)"
    except OSError as e:
        return "error", "", f"conbroker send 跑不起来: {e}"
    out = (r.stdout or "").replace("\r", "").rstrip("\n")
    if r.returncode != 0:
        return "error", out, serial_core.serve_error(r.returncode, r.stderr)
    if out.startswith("[conbroker: 拒绝发送"):
        return "refused", out, out.splitlines()[0]
    last = out.rsplit("\n", 1)[-1]
    if last.startswith("[conbroker:") and "没等到提示符" in last:
        return "timeout", out, last
    return "sent", out, None


# ------------------------------------------------------------ MCP: 谁在调 / 工具注解

_ctx = threading.local()


def current_who():
    return getattr(_ctx, "who", None) or "-"


class Server(mc.Server):
    """mc.Server 只把参数给工具; 写工具要知道"谁" (租约、日志), 经线程局部变量带进去."""

    def _call(self, rid, params, who):
        _ctx.who = who
        try:
            return super()._call(rid, params, who)
        finally:
            _ctx.who = None


class Tool(mc.Tool):
    def __init__(self, name, description, params, fn, read_only=False, destructive=False):
        super().__init__(name, description, params, fn)
        self.read_only, self.destructive = read_only, destructive

    def spec(self):
        d = super().spec()
        d["annotations"] = {"readOnlyHint": self.read_only, "destructiveHint": self.destructive,
                            "openWorldHint": False}
        return d


SLOT = {"type": "integer", "minimum": 1, "maximum": 4, "required": True, "description": "操作头槽位 1-4"}
LEASE = {"type": "string", "maxLength": 64, "required": True, "description": "lease_acquire 给的令牌"}


def build_tools(store, slots, tail, send, rules=sw.load_rules, who=current_who):
    """slots() -> lamps_core.slot_status 那样的 4 项; tail(dev, n) -> 文本 (出错抛 mc.ToolError);
    send(dev, text, timeout, raw) -> (status, 输出, 说明); rules() -> load_rules 的结果. 测试里全换成假的."""

    def _slot(n):
        s = slots()[n - 1]
        if not s["present"]:
            raise mc.ToolError(f"槽位 {n} 没插线 ({s['dev']} 不在)")
        if not s["baud"]:
            raise mc.ToolError(f"槽位 {n} 没有在跑的 conbroker 服务, 请人在推车串口面板上开服务")
        return s

    def _run(fn, slot=None, kind=None, cmd=None):
        """Refused -> ToolError; 给了 slot 的顺手把拒绝记进执行绪日志."""
        def go(b, now):
            try:
                return fn(b, now)
            except sw.Refused as e:
                if slot is not None:
                    b.log_refused(slot, who(), kind, cmd, e.code, str(e), now)
                raise
        try:
            return store.run(go)
        except sw.Refused as e:
            raise mc.ToolError(str(e)) from None
        except OSError as e:
            raise mc.ToolError(f"写状态文件失败, 不发: {e}") from None

    def _dispatch(n, dev, kind, cmd, gate, lease, timeout, name, text, raw):
        me = who()
        if gate["verdict"] != "pass":
            it = _run(lambda b, now: b.submit(n, lease, me, kind, cmd, gate, now, timeout, name), n, kind, cmd)
            return [mc.json_content({
                "status": "pending", "id": it["id"], "slot": n, "cmd": cmd, "reason": gate["reason"],
                "danger": gate["danger"],
                "hint": "已挂起, 一个字节都没发. 要人在推车上确认才发; 用 pending_list 看结果. "
                        "等的时候继续 lease_renew, 租约过期这条就作废."})]
        it = _run(lambda b, now: b.begin(n, lease, me, kind, cmd, gate, now, timeout, name), n, kind, cmd)
        t0 = time.monotonic()
        try:
            status, out, note = send(dev, text, timeout, raw)
        except sw.Refused as e:
            status, out, note = "error", "", str(e)
        except Exception as e:          # noqa: BLE001  在途标记一定要收掉
            status, out, note = "error", "", f"{type(e).__name__}: {e}"
        ms_ = int((time.monotonic() - t0) * 1000)
        store.run(lambda b, now: b.finish(it["id"], {"status": status, "echo": out, "ms": ms_, "note": note}, now))
        res = {"status": status, "id": it["id"], "slot": n, "cmd": cmd, "gate": gate,
               "output": out if len(out) <= OUTPUT_MAX else out[:OUTPUT_MAX] + f"\n…(截断, 共 {len(out)} 字)"}
        if note:
            res["note"] = note
        return [mc.json_content(res)]

    def lease_acquire(a):
        _slot(a["slot"])
        res = _run(lambda b, now: b.acquire(a["slot"], who(), now, ttl=a["ttl_s"]))
        res["hint"] = f"每 {res['ttl_s'] // 2} 秒左右 lease_renew 一次; 用完 lease_release. 令牌别给别人."
        return [mc.json_content(res)]

    def lease_renew(a):
        return [mc.json_content(_run(lambda b, now: b.renew(a["slot"], a["lease"], now)))]

    def lease_release(a):
        _run(lambda b, now: b.release(a["slot"], a["lease"], now))
        return [mc.json_content({"slot": a["slot"], "released": True})]

    def send_command(a):
        n, cmd = a["slot"], a["command"]
        s = _slot(n)
        try:
            sw.check_line(cmd)
        except sw.Refused as e:
            _run(lambda b, now: b.log_refused(n, who(), "line", cmd, e.code, str(e), now))
            raise mc.ToolError(str(e)) from None
        try:
            text = tail(s["dev"], TAIL_LINES)
        except mc.ToolError:
            text = ""                   # 看不到设备现在停在哪: 认不出厂商 -> 挂起
        vendor, at = sw.slot_vendor(n, rules(), text)
        gate = sw.classify(cmd, vendor, at, rules())
        return _dispatch(n, s["dev"], "line", cmd, gate, a["lease"], a["timeout_s"], mc.thread_name(text),
                         cmd, False)

    def send_key(a):
        n, key = a["slot"], a["key"]
        s = _slot(n)
        gate = sw.classify_key(key)
        try:
            name = mc.thread_name(tail(s["dev"], TAIL_LINES))
        except mc.ToolError:
            name = None
        k = sw.KEYS[key]
        return _dispatch(n, s["dev"], "key", key, gate, a["lease"], KEY_TIMEOUT, name, k["text"], k["raw"])

    def pending_list(a):
        return [mc.json_content(_run(lambda b, now: b.view(now, a.get("slot"))))]

    def estop(a):
        slot = a.get("slot")
        dropped = _run(lambda b, now: b.estop(slot, who(), "agent", now))
        return [mc.json_content({"stopped": list(sw.SLOTS) if slot is None else [slot],
                                 "dropped": [d["id"] for d in dropped],
                                 "note": "已撤销写租约、丢弃挂起的命令, 没往设备发任何东西. 解除要人在推车本机上操作."})]

    return [
        Tool("lease_acquire", "申请某个槽位的写租约 (一条执行绪同时只有一个写入者). 返回令牌, 发命令时带上. "
             "租约有期限要续期, 过期自动释放, 挂着的命令一起作废. 急停中拿不到.",
             {"slot": SLOT, "ttl_s": {"type": "integer", "minimum": sw.LEASE_TTL_MIN_S,
                                      "maximum": sw.LEASE_TTL_MAX_S, "default": sw.LEASE_TTL_S,
                                      "description": "期限秒数"}}, lease_acquire),
        Tool("lease_renew", "续写租约 (从现在起再给一个期限). 过期了不能续, 要重新申请.",
             {"slot": SLOT, "lease": LEASE}, lease_renew),
        Tool("lease_release", "用完交还写租约; 自己还挂着等确认的命令一起作废.",
             {"slot": SLOT, "lease": LEASE}, lease_release),
        Tool("send_command", "往某个槽位的串口发一行命令 (自动补回车, 只收一行可打印 ASCII, 不给原始字节). "
             "过闸门: 认得出厂商、设备停在提示符上的只读命令 (华为 display… / 思科 show…, 全称小写) 直接发, "
             "返回设备输出; 其余一律挂起 (status=pending) 等人在推车上确认, 带 save/reboot/reset/format/delete "
             "等高危词的永远要人确认. 人 3 秒内敲过键会排队. 要先 lease_acquire.",
             {"slot": SLOT, "command": {"type": "string", "maxLength": sw.LINE_MAX, "required": True,
                                        "description": "一行命令, 不含换行"},
              "lease": LEASE,
              "timeout_s": {"type": "integer", "minimum": 1, "maximum": SEND_TIMEOUT_MAX, "default": SEND_TIMEOUT,
                            "description": "等提示符的秒数"}},
             send_command, destructive=True),
        Tool("send_key", "发一个固定特殊键: enter (回车) / space (翻页空格) 直接发; ctrl_c (打断) 要人确认. "
             "需要别的按键 (BootROM 菜单、按任意键) 请人在推车屏幕上操作.",
             {"slot": SLOT, "key": {"type": "string", "enum": sorted(sw.KEYS), "required": True,
                                    "description": "哪个键"},
              "lease": LEASE}, send_key, destructive=True),
        Tool("pending_list", "看各槽位的写入者 / 急停 / 在途, 挂起等人确认的命令, 和最近的结果 "
             "(人确认后发出的命令输出摘要在 history 里, 完整输出用 read_output). 只读.",
             {"slot": {"type": "integer", "minimum": 1, "maximum": 4, "description": "只看这个槽位, 不给 = 全部"}},
             pending_list, read_only=True),
        Tool("estop", "急停: 撤销写租约、丢弃挂起的命令, 之后谁都不能写这个槽位, 直到人在推车上解除. "
             "只停发送, 不往设备发任何东西. 不给 slot = 全部槽位. 觉得自己要出错时就用它.",
             {"slot": {"type": "integer", "minimum": 1, "maximum": 4, "description": "槽位; 不给 = 全部"}},
             estop),
    ]


def real_slots():
    return lamps_core.slot_status(serial_core.data_dir(serial_core.serial_conf_read()))


def real_tools(tail, store=None):
    """cart_mcp 用: tail 用它的 conbroker_tail."""
    return build_tools(store or Store(), slots=real_slots, tail=tail, send=conbroker_send)


# ------------------------------------------------------------ 人确认后发送 / 网页请求

def send_confirmed(store, iid, by, slots, send):
    """人确认 #iid -> 标在途 -> 经 conbroker 发 -> finish. -> (条目, status, 输出, 说明).
    Book 不让 (急停 / 租约没了 / 在途 / 不在挂起) 抛 sw.Refused, 一个字节都不发. 会阻塞到发完, 车机放后台线程调."""
    it = store.run(lambda b, now: b.confirm(iid, by, now))
    try:
        dev = slots()[it["slot"] - 1]["dev"]
        if it["kind"] == "key":
            text, raw = sw.KEYS[it["cmd"]]["text"], sw.KEYS[it["cmd"]]["raw"]
        else:
            text, raw = it["cmd"], False
    except Exception as e:              # noqa: BLE001  在途标记一定要收掉
        dev, err = None, f"{type(e).__name__}: {e}"
    t0 = time.monotonic()
    if dev is None:
        status, output, note = "error", "", err
    else:
        try:
            status, output, note = send(dev, text, it["timeout"], raw)
        except Exception as e:          # noqa: BLE001
            status, output, note = "error", "", f"{type(e).__name__}: {e}"
    ms_ = int((time.monotonic() - t0) * 1000)
    store.run(lambda b, now: b.finish(it["id"], {"status": status, "echo": output, "ms": ms_, "note": note}, now))
    return it, status, output, note


def write_request(req, req_dir=None):
    """网页 (ops-web) 投一个请求 (confirm_core.make_request 的结果): 新文件 O_EXCL、不跟链接、一次 write 写完."""
    path = os.path.join(req_dir or REQ_DIR, f"{time.time_ns()}-{secrets.token_hex(4)}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(req, ensure_ascii=False).encode())
    finally:
        os.close(fd)
    return path


def _remove(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _take_request(store, path, now, force):
    """处理一个请求文件 -> 一句结果; 还没写完 (空 / 半截 JSON, 刚建的) 返回 None 等下一轮.
    先删再执行: 执行出错也不会被单元反复拉起来重做."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:                # 链接 (ELOOP) / 已经没了
        _remove(path)
        return f"不认: 打不开 ({e.strerror})"
    try:
        st = os.fstat(fd)
        raw = os.read(fd, cc.REQ_MAX + 1) if stat.S_ISREG(st.st_mode) else None
    finally:
        os.close(fd)
    if raw is None:
        _remove(path)
        return "不认: 不是普通文件"
    try:
        req = cc.parse_request(raw.decode("utf-8"), now)
    except ValueError as e:
        try:
            json.loads(raw)
            partial = False
        except ValueError:
            partial = len(raw) <= cc.REQ_MAX
        if partial and not force and now - st.st_mtime < REQ_POLL_S * REQ_ROUNDS:
            return None
        _remove(path)
        return f"不认: {e}"
    _remove(path)
    try:
        return store.run(lambda b, t: cc.apply_request(b, req, t))
    except sw.Refused as e:
        return f"不行: {e}"


def process_requests(store, req_dir=None, clock=None, sleep=time.sleep):
    """root 的 cart-serial-req 单元调: 目录里的请求逐个执行并删掉, 直到目录空. -> [(文件名, 结果)].
    只认急停 / 拒绝、一分钟内的 (confirm_core.parse_request); 链接不跟, 目录和读不懂的一律删掉,
    目录不空单元就会被反复拉起."""
    req_dir, clock = req_dir or REQ_DIR, clock or store.clock
    out = []
    for rnd in range(REQ_ROUNDS + 1):
        try:
            names = sorted(os.listdir(req_dir))
        except OSError:
            return out
        if not names:
            break
        wait = False
        for name in names:
            res = _take_request(store, os.path.join(req_dir, name), clock(), force=rnd == REQ_ROUNDS)
            if res is None:
                wait = True
            else:
                out.append((name, res))
        if wait:
            sleep(REQ_POLL_S)
    return out


# ------------------------------------------------------------ 本机判定

REMOTE_COMMS = {"sshd", "sshd-session", "sshd-auth", "tailscaled", "dropbear", "mosh-server", "cockpit-ws",
                "cockpit-bridge"}
_SSH_ENV = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


def read_stat(pid, path_fmt="/proc/{pid}/stat"):
    """/proc/<pid>/stat -> (comm, ppid); 读不到 None. comm 可能带空格和括号, 按最后一个 ')' 切."""
    try:
        with open(path_fmt.format(pid=pid)) as f:
            raw = f.read()
        head, _, rest = raw.rpartition(")")
        return head.split("(", 1)[1], int(rest.split()[1])
    except (OSError, IndexError, ValueError):
        return None


def local_human(env, pid, stat=read_stat, tty=False):
    """是不是推车本机终端上的人 -> (是否, 说明). 拿不准一律"不是"."""
    hit = next((k for k in _SSH_ENV if env.get(k)), None)
    if hit:
        return False, f"环境里有 {hit}, 是经 ssh 进来的"
    if not tty:
        return False, "stdin 不是终端, 要人在推车终端上交互确认"
    for _ in range(64):
        st = stat(pid)
        if st is None:
            return False, f"读不到进程 {pid} 的 /proc, 认不出是不是本机"
        comm, ppid = st
        if comm in REMOTE_COMMS:
            return False, f"是经 {comm} 进来的 (远程)"
        if ppid <= 0:
            return True, "本机终端"
        pid = ppid
    return False, "进程链太长, 认不出"


# ------------------------------------------------------------ 车机命令行

CLI_CMDS = ("pending", "confirm", "reject", "estop", "resume", "requests")


def _print_view(v, out):
    for s in v["slots"]:
        bits = [f"槽位 {s['slot']}:"]
        bits.append(f"急停 (by {s['estop']['by']} {s['estop']['t']})" if s["estop"] else "正常")
        bits.append(f"写入者 {s['writer']} 剩 {s['expires_in']}s" if s["writer"] else "无写入者")
        if s["inflight"]:
            bits.append(f"在途 #{s['inflight']['id']} {s['inflight']['cmd']!r}")
        print(" ".join(bits), file=out)
    print("挂起等确认:" if v["pending"] else "挂起等确认: 无", file=out)
    for p in v["pending"]:
        warn = f" [高危: {', '.join(p['danger'])}]" if p.get("danger") else ""
        print(f"  #{p['id']} 槽位 {p['slot']} {p['kind']} {p['cmd']!r} 来自 {p['who']} ({p['age_s']}s 前){warn}"
              f"\n      {p.get('reason')}", file=out)
    for h in v["history"][-5:]:
        print(f"  最近 #{h['id']} 槽位 {h['slot']} {h['cmd']!r} -> {h['status']}"
              f"{' (' + h['why'] + ')' if h.get('why') else ''}", file=out)


def cli(argv, store=None, send=None, slots=None, local=None, stdin=None, out=None, env=None):
    store = store or Store()
    send = send or conbroker_send
    slots = slots or real_slots
    env = os.environ if env is None else env
    stdin = stdin or sys.stdin
    out = out or sys.stdout
    if local is None:
        local = lambda: local_human(env, os.getpid(), read_stat, tty=stdin.isatty())    # noqa: E731
    ap = argparse.ArgumentParser(prog="cart_mcp.py", description="串口写路径: 车机上确认 / 拒绝 / 急停 / 解除 (#21)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending", help="看挂起的命令和各槽位状态")
    sub.add_parser("confirm", help="确认发送 (只认推车本机终端)").add_argument("id", type=int)
    p = sub.add_parser("reject", help="拒绝")
    p.add_argument("id", type=int)
    p.add_argument("--why", default=None)
    sub.add_parser("estop", help="急停; 不给槽位 = 全部").add_argument("slot", nargs="?", type=int, choices=sw.SLOTS)
    sub.add_parser("resume", help="解除急停 (只认推车本机终端)").add_argument("target", choices=["1", "2", "3", "4", "all"])
    sub.add_parser("requests", help="执行网页投来的急停 / 拒绝 (cart-serial-req 单元调)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    by = f"human:{env.get('SUDO_USER') or env.get('USER') or '?'}@local"

    def run(fn):
        try:
            return store.run(fn)
        except sw.Refused as e:
            print(f"不行: {e}", file=out)
            return None

    if a.cmd == "pending":
        _print_view(store.view(), out)
        return 0
    if a.cmd == "estop":
        dropped = store.run(lambda b, now: b.estop(a.slot, by.replace("@local", ""), "human", now))
        print(f"已急停 {'全部槽位' if a.slot is None else f'槽位 {a.slot}'}; 丢弃挂起 {[d['id'] for d in dropped]}; "
              "没往设备发任何东西", file=out)
        return 0
    if a.cmd == "requests":
        for name, res in process_requests(store):
            print(f"{name}: {res}", file=out)
        return 0
    if a.cmd == "reject":
        if run(lambda b, now: b.reject(a.id, by.replace("@local", ""), now, why=a.why) or True) is None:
            return 1
        print(f"已拒绝 #{a.id}", file=out)
        return 0
    ok, why = local()
    if not ok:
        print(f"{a.cmd} 只认推车本机终端上的人: {why}", file=out)
        return 1
    if a.cmd == "resume":
        store.run(lambda b, now: b.resume(None if a.target == "all" else int(a.target), by, now))
        print(f"已解除 {'全部槽位' if a.target == 'all' else '槽位 ' + a.target} 的急停; Agent 要重新申请租约", file=out)
        return 0
    # confirm
    item = next((p for p in store.view()["pending"] if p["id"] == a.id), None)
    if item is None:
        print(f"没有挂起的 #{a.id}", file=out)
        return 1
    warn = f"\n  !!! 高危词: {', '.join(item['danger'])}" if item.get("danger") else ""
    print(f"槽位 {item['slot']} ({item.get('name') or '名字未知'}) 来自 {item['who']}:\n  {item['kind']} {item['cmd']!r}"
          f"\n  {item.get('reason')}{warn}\n确认发送? 输入 yes: ", end="", file=out)
    out.flush()
    if stdin.readline().strip() != "yes":
        print("没输 yes, 不发 (还挂着)", file=out)
        return 1
    try:
        it, status, output, note = send_confirmed(store, a.id, by, slots, send)
    except sw.Refused as e:
        print(f"不行: {e}", file=out)
        return 1
    print(f"#{it['id']} -> {status}{' (' + note + ')' if note else ''}\n{sw.echo_summary(output)}", file=out)
    return 0 if status == "sent" else 1


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
