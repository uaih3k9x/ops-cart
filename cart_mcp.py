#!/usr/bin/env python3
"""cart_mcp: 推车 MCP 服务 (issue #19). 协议和校验在 mcp_core; 串口写工具 (#21) 在 mcp_serial.

    sudo python3 /opt/pxe/cart_mcp.py stdio     # 本机 / ssh 上来的 Agent (ssh 本身就是认证)
    sudo python3 /opt/pxe/cart_mcp.py http      # tailnet: 只绑 127.0.0.1 和 tailnet 地址, 要 Bearer 令牌

档位 (#49): --profile readonly 只注册只看的工具 (会动手的根本不在); --profile act (默认) = 全部工具.
HTTP 两档各起一个进程: 动手档用 mcp.json 顶层 port/tokens, 只读档用 readonly.port (默认 8766) / readonly.tokens.

Claude Code 接法 (地址、令牌换成自己的):
    claude mcp add cart -- ssh cart sudo python3 /opt/pxe/cart_mcp.py stdio --profile act
    # 只读档要专用 ssh 身份 (sudoers + authorized_keys command= 同时锁死, 见 docs/DESIGN_agent_mcp.md 第十六节);
    # 别用上面那个能 sudo 跑动手档的身份:
    claude mcp add cart-ro -- ssh agent-ro@<推车地址>
    claude mcp add --transport http cart http://<推车 tailnet 地址>:8765/mcp --header "Authorization: Bearer <令牌>"

工具: list_threads (槽位 1-4) / read_output (走 conbroker tail, 不直开串口) / screenshot (cart_snap) /
      cart_status (/run/ops-cart/state.json) / pxe_status (只读).
PXE 解锁申请 (#24, mcp_pxe): pxe_request 只挂起, 人在车机上照抄确认码才解锁; pxe_retire 提前上锁.
写串口 (#21, mcp_serial): 写租约 + 闸门 (只读命令直发, 其余挂起等人确认) + 急停. 车机上确认 / 解除:
    sudo python3 /opt/pxe/cart_mcp.py pending | confirm <编号> | reject <编号> | estop [槽位] | resume <槽位|all>
要 root: 截图要采集卡和 mpv 的 socket, 读串口输出要 sudo -u <serial.conf 的 user> 跑 conbroker.
令牌和端口在 /etc/ops-cart/mcp.json (样例 examples/mcp.example.json), 每个请求重读, 加令牌不用重启.
审计每次调用一行 JSON (带档位), 落 /var/log/ops-cart/mcp-audit.log.
"""

import argparse
import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cart_snap
import dash_core
import kiosk_core as kc
import mcp_kvm
import lamps_core
import mcp_core as mc
import mcp_label
import mcp_pxe
import mcp_serial
import serial_core

MCP_CONF = "/etc/ops-cart/mcp.json"
DEFAULT_CONF = {"port": 8765, "tokens": {}, "hosts": []}
READ_LINES = 80                 # read_output 默认行数
NAME_TAIL = 30                  # 列执行绪时为认名字读多少行
OUTPUT_TAIL = 200                # 判断"有没有设备输出"多看多少行
RECENT_S = 3600                 # 多久内的输出算最近
TAIL_TIMEOUT = 10
MAX_IMAGE = 12 * 1024 * 1024    # 截图再大就是抓错东西了
MAX_BODY = 256 * 1024           # 只读工具的请求都很小
REBIND_S = 30                   # tailnet 地址晚到 (tailscaled 后起) 时多久再看一次
STATUS_KEYS = ("net", "lan2", "ups", "lldp", "slots", "kiosk", "rows", "temps", "fans", "uptime")
HERE = os.path.dirname(os.path.abspath(__file__))


def conf_read(path=MCP_CONF):
    cfg = dict(DEFAULT_CONF)
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            cfg.update(doc)
    except (OSError, ValueError):
        pass
    return cfg


# ------------------------------------------------------------ 工具

SLOT_PARAM = {"type": "integer", "minimum": 1, "maximum": 4, "required": True,
              "description": "操作头槽位 1-4 (对应 /dev/con-N)"}


def build_tools(slots, tail, snap, state, pxe, now=time.time, writer=lambda: {}):
    """slots() -> lamps_core.slot_status 那样的 4 项; tail(dev, n) -> 文本; snap(dev|None) -> (png, 哪块卡的说明);
    state() -> 快照 dict 或 None; pxe() -> dict; writer() -> {槽位: 写租约持有者}. 出错抛 mc.ToolError. 测试里全换成假的."""

    def _slot(n):
        s = slots()[n - 1]
        if not s["present"]:
            raise mc.ToolError(f"{mc.slot_label(n, None)} 没插线 ({s['dev']} 不在)")
        if not s["baud"]:
            raise mc.ToolError(f"{mc.slot_label(n, None)} 没有在跑的 conbroker 服务, 请人在推车串口面板上开服务")
        return s

    def list_threads(_a):
        out, writers = [], writer()
        for n, s in enumerate(slots(), 1):
            name, seen, age = None, None, None
            if s["present"] and s["baud"]:              # 转接头拔了 / 没服务: 名字清空, 不读旧日志
                try:
                    text = tail(s["dev"], NAME_TAIL)
                    name = mc.thread_name(text)
                    if not mc.device_output_seen(text):     # 尾巴全是接入 / 断开标记时往前看
                        text = tail(s["dev"], OUTPUT_TAIL)
                    age = mc.last_output_age(text, now(), s.get("mtime"))
                    seen = age is not None and age <= RECENT_S
                except mc.ToolError:
                    pass
            row = {"slot": n, "dev": s["dev"], "present": bool(s["present"]), "service": bool(s["baud"]),
                   "baud": s["baud"], "name": name, "label": mc.slot_label(n, name),
                   "recent_output": seen, "last_output_age_s": age, "writer": writers.get(n)}   # 写租约持有者 (#21)
            if seen is False:
                when = "日志里没有设备输出" if age is None else f"上次设备输出约 {age // 3600} 小时前"
                row["hint"] = f"{when}: 可能没接设备, 也可能只是没动静. 要确认就拿租约 send_key enter 看有没有回显"
            out.append(row)
        return [mc.json_content({"threads": out})]

    def read_output(a):
        s = _slot(a["slot"])
        text = tail(s["dev"], a["lines"])
        label = mc.slot_label(a["slot"], mc.thread_name(text))
        return [mc.text_content(f"[{label} {s['dev']} {s['baud']} 波特] 最近 {a['lines']} 行\n{text}")]

    def screenshot(a):
        dev = a.get("device")
        if dev and not dev.startswith("/dev/"):
            dev = "/dev/" + dev
        png, used = snap(dev)
        return [mc.image_content(png), mc.text_content(used)]

    def cart_status(_a):
        snap_doc = state()
        if not isinstance(snap_doc, dict):
            raise mc.ToolError("没有状态快照 (cart-dash 没在跑?)")
        t = now()
        out = {"age_s": round(t - snap_doc.get("ts", 0), 1), "stale": dash_core.stale(snap_doc, t)}
        out.update({k: snap_doc[k] for k in STATUS_KEYS if k in snap_doc})
        return [mc.json_content(out)]

    def pxe_status(_a):
        return [mc.json_content(pxe())]

    return [
        mc.Tool("list_threads", "列出串口操作头槽位 1-4 的执行绪: present = USB 转接头在 (串口探测不了对端, 不代表接了设备), "
                "recent_output = 一小时内有没有设备输出 (只看日志, 不探测), last_output_age_s = 大约多少秒前; 有没有在跑的 conbroker 服务、波特率、"
                "从提示符认出的设备名 (华为 <名>/[名]), 写入者 (写租约持有者). 名字只是标签, 以槽位号为准.",
                {}, list_threads, kind=mc.READONLY),
        mc.Tool("read_output", "读某个槽位串口最近 N 行输出 (经 conbroker tail 读它的日志, 不直接开串口). 只读.",
                {"slot": SLOT_PARAM,
                 "lines": {"type": "integer", "minimum": 1, "maximum": 500, "default": READ_LINES,
                           "description": "行数"}}, read_output, kind=mc.READONLY),
        mc.Tool("screenshot", "截被测机器当前画面 (HDMI 采集卡; 推车锁屏也拿得到), 返回 PNG. 很占上下文: 想读字先用 screen_ocr.",
                {"device": {"type": "string", "pattern": r"(/dev/)?video[0-9]{1,2}", "maxLength": 16,
                            "description": "采集设备, 默认正在预览的那块, 没预览就有信号的采集卡"}}, screenshot, kind=mc.READONLY),
        mc.Tool("cart_status", "推车状态快照 (cart-dash 写的 state.json): UPS、网络、LAN1/LAN2 的 LLDP 对端、"
                "操作头、锁屏、自检、温度风扇; stale=true 表示采集停了.", {}, cart_status, kind=mc.READONLY),
        mc.Tool("pxe_status", "PXE 装机口状态 (只读): 当前模式 (rescue/install/expired)、解锁的 MAC、到期时间、"
                "剩余秒数、用途 (rocky/winpe/clone-save/clone-restore)、守卫状态. 解锁用 pxe_request 申请, 人在推车上确认.",
                {}, pxe_status, kind=mc.READONLY),
    ]


# ------------------------------------------------------------ 真机读数

def real_slots():
    return lamps_core.slot_status(serial_core.data_dir(serial_core.serial_conf_read()))


def conbroker_tail(dev, n):
    """conbroker tail <口> -n N (以 serial.conf 的 user 身份); 失败抛 ToolError."""
    try:
        r = subprocess.run(serial_core._conbroker_cmd() + ["tail", dev, "-n", str(n)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=TAIL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise mc.ToolError(f"conbroker tail 跑不起来: {e}") from None
    if r.returncode != 0:
        raise mc.ToolError(f"conbroker tail 失败: {serial_core.serve_error(r.returncode, r.stderr)}")
    return r.stdout.rstrip("\n")


def real_snap(dev, readonly=False):
    path, why, used = cart_snap.grab(dev, readonly=readonly)     # 只读档限速更紧 (#49)
    if path is None:
        raise mc.ToolError(why)
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_IMAGE + 1)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if len(data) > MAX_IMAGE:
        raise mc.ToolError(f"截图太大 ({len(data)} 字节), 不发")
    return data, mcp_kvm.describe_dev(used)


def read_pxe(mode_path=kc.PXE_MODE, root=kc.TFTP_ROOT, guard_path=None, now=None):
    """pxe-mode.json + 那台 MAC 的菜单 (认用途) + 守卫状态. 只读, 不碰 tftp 里任何文件的内容以外的东西."""
    mode = kc.pxe_mode_read(mode_path, now)
    menu = None
    if mode.get("mac"):
        try:
            with open(os.path.join(root, kc.mac_cfg_name(mode["mac"])), errors="replace") as f:
                menu = f.read()
        except OSError:
            pass
    return mc.pxe_view(mode, menu, kc.pxe_guard_read(guard_path), now)


def real_tools(profile=mc.DEFAULT_PROFILE):
    """按档位挑好的工具; 档位不认识抛 ValueError."""
    ro = mc.check_profile(profile) == mc.READONLY       # 只读档: 各模块只给只读工具, 读状态走 peek (不加锁不写盘)
    store = mcp_serial.Store()
    snap = (lambda dev: real_snap(dev, readonly=True)) if ro else real_snap
    tools = (build_tools(slots=real_slots, tail=conbroker_tail, snap=snap, state=dash_core.read, pxe=read_pxe,
                         writer=lambda: mcp_serial.writers(store, readonly=ro))
             + mcp_serial.real_tools(conbroker_tail, store, readonly=ro)    # 写串口 (#21)
             + mcp_pxe.real_tools(readonly=ro)          # PXE 解锁申请 (#24): 只挂起, 人在车机上确认
             + mcp_kvm.real_tools(readonly=ro)          # 键鼠 / OCR / WOL (#41)
             + mcp_label.real_label_tools(readonly=ro))     # 标签 (#20)
    return mc.select_tools(tools, profile)                      # 只读档: 动手工具不注册 (#49)


def make_server(profile, audit):
    return mcp_serial.Server(real_tools(profile), audit=audit, version=version(), profile=profile)   # 写工具要知道"谁"


def version():
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "dev"


# ------------------------------------------------------------ stdio: 一行一条 JSON-RPC

def stdio_who(env):
    """审计里的 '谁': sudo 前的用户 + ssh 来源 (sudo 默认会清掉 SSH_CONNECTION, 那就只有用户)."""
    user = env.get("SUDO_USER") or env.get("USER") or "?"
    ssh = (env.get("SSH_CONNECTION") or "").split()
    return f"stdio:{user}@{ssh[0]}" if ssh else f"stdio:{user}"


def serve_stdio(server, who, stdin, stdout):
    """stdout 只走协议; 别的话一律写 stderr."""
    for line in iter(stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        out = server.handle_text(line, who)
        if out is not None:
            stdout.write(out + "\n")
            stdout.flush()


# ------------------------------------------------------------ HTTP (Streamable HTTP, 只回 application/json)

HTTP_TIMEOUT_S = 30         # 单次读写这么久没动静就断
HTTP_HEADER_DEADLINE_S = 10  # 读完请求头和请求体的整体时限: 每 <30 秒滴一个字节的慢连接也占不久 (#60)


def make_http_server(addr, port, server, conf=conf_read, audit=None, clock=time.monotonic):
    """一个监听地址一个 server. 每个请求: 来源/Host/Origin/令牌 (mc.http_check) -> 读体 -> server.handle_text.
    认证失败 (401/403) 的审计按 (档位, 状态码) 合并: 每秒最多一行, 带上中间没逐条记的次数 (#60)."""
    merge = mc.AuditMerge(clock)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ops-cart-mcp"
        timeout = HTTP_TIMEOUT_S
        _deadline = None

        def handle_one_request(self):
            t = threading.Timer(HTTP_HEADER_DEADLINE_S, self._cut)
            t.daemon = True
            self._deadline = t
            t.start()
            try:
                super().handle_one_request()
            finally:
                t.cancel()

        def _cut(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def _read_done(self):
            if self._deadline is not None:
                self._deadline.cancel()         # 请求读完了: 工具跑多久不归这个时限管

        def _reply(self, code, body=b"", ctype="application/json", extra=()):
            self._read_done()
            self.send_response(code)
            for k, v in extra:
                self.send_header(k, v)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _deny(self, code, why, extra=()):
            log, quiet, srcs = merge.note((server.profile, code, why), self.client_address[0])  # 403 按原因分开
            if audit is not None and log:
                more = f" (之前另有 {quiet} 次同类拒绝, 来自 {srcs} 个不同来源, 没逐条记)" if quiet else ""
                audit.write(mc.audit_line(f"-@{self.client_address[0]}", None, None, False,
                                          error=f"{code} {why}{more}", profile=server.profile))
            self._reply(code, json.dumps({"error": why}, ensure_ascii=False).encode(), extra=extra)

        def _gate(self):
            cfg = conf()
            code, who = mc.http_check(self.client_address[0], self.headers, mc.profile_tokens(cfg, server.profile),
                                      cfg.get("hosts"))       # 令牌按档位分开 (#49)
            if code:
                self._deny(code, who, extra=[("WWW-Authenticate", "Bearer")] if code == 401 else ())
                return None
            if self.path.split("?", 1)[0] != "/mcp":
                self._reply(404, b'{"error": "only /mcp"}')
                return None
            return who

        def do_POST(self):
            who = self._gate()
            if who is None:
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                return self._reply(413, b'{"error": "body too large"}')
            text = self.rfile.read(n).decode("utf-8", "replace")
            self._read_done()
            out = server.handle_text(text, who)
            if out is None:
                return self._reply(202)
            self._reply(200, out.encode())

        def do_GET(self):               # 不开服务端推送流 (SSE), 规范允许回 405
            if self._gate() is not None:
                self._reply(405, extra=[("Allow", "POST")])

        do_DELETE = do_GET

        def log_message(self, fmt, *args):
            pass

    class Srv(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            e = sys.exc_info()[1]
            if isinstance(e, OSError):          # 慢连接被时限掐断之类: 一行就够, 别把整段 traceback 刷进 journald
                print(f"cart_mcp: 连接 {client_address[0]} 断开: {type(e).__name__}", file=sys.stderr, flush=True)
                return
            super().handle_error(request, client_address)
        address_family = socket.AF_INET6 if ":" in addr else socket.AF_INET

    srv = Srv((addr, port), Handler)

    def flush_audit():
        if audit is not None:
            merge.flush(lambda key, q, n: audit.write(mc.audit_line(
                "-", None, None, False, error=f"{key[1]} {key[2]} (另有 {q} 次同类拒绝, 来自 {n} 个不同来源, 没逐条记)",
                profile=key[0])))
    srv.flush_audit = flush_audit
    return srv


def _ip_json():
    try:
        return subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def serve_http(server, port, audit, conf=conf_read):
    """只绑本机和 tailnet 地址, 不绑 0.0.0.0; tailscaled 后起时每 REBIND_S 秒补绑新出现的地址."""
    live = {}
    while True:
        for addr in mc.bind_addrs(_ip_json()):
            if addr in live:
                continue
            try:
                srv = make_http_server(addr, port, server, conf=conf, audit=audit)
            except OSError as e:
                print(f"cart_mcp: 绑不上 {addr}:{port}: {e}", file=sys.stderr, flush=True)
                continue
            live[addr] = srv
            atexit.register(srv.flush_audit)    # 合并审计的尾巴计数: 退出时写出, 平时每轮补绑时也写一次
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"cart_mcp: 监听 {addr}:{port}/mcp", file=sys.stderr, flush=True)
        for srv in live.values():
            srv.flush_audit()
        time.sleep(REBIND_S)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in mcp_serial.CLI_CMDS:        # 车机上确认 / 急停 / 解除 (#21)
        return mcp_serial.cli(argv)
    ap = argparse.ArgumentParser(description="ops-cart MCP 服务")
    ap.add_argument("mode", choices=["stdio", "http"])
    ap.add_argument("--profile", choices=list(mc.PROFILES), default=mc.DEFAULT_PROFILE,
                    help="readonly = 只注册只看的工具; act = 全部 (默认)")
    ap.add_argument("--port", type=int, help="HTTP 端口, 默认取 mcp.json 的 port (8765)")
    ap.add_argument("--conf", default=MCP_CONF, help="令牌/端口配置")
    ap.add_argument("--audit", default=mc.AUDIT_PATH, help="审计日志 (JSONL)")
    a = ap.parse_args(argv)
    slot = None
    if a.profile == mc.READONLY:                # 只读档: 跨进程槽位, 多开会话不能成倍放大限速 (#60)
        try:
            slot = mc.take_ro_slot()
        except mc.SlotBusy as e:
            print(f"cart_mcp: {e}", file=sys.stderr)
            return 3
    audit = mc.Audit(a.audit)
    server = make_server(a.profile, audit)
    atexit.register(server.flush_audit)         # 合并审计的尾巴计数退出时写出 (#60)
    for sig in (signal.SIGTERM, signal.SIGHUP):     # systemd 停服务 / ssh 断开: 走正常退出, atexit 才会跑
        try:
            signal.signal(sig, lambda *_a: sys.exit(0))
        except (ValueError, OSError):
            pass
    if a.mode == "stdio":
        for s in (sys.stdin, sys.stdout):
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8")
        serve_stdio(server, stdio_who(os.environ), sys.stdin, sys.stdout)
        return 0
    conf = lambda: conf_read(a.conf)        # noqa: E731
    serve_http(server, a.port or mc.profile_port(conf(), a.profile), audit, conf=conf)
    del slot                                    # 槽位跟进程走, 退出时内核放锁
    return 0


if __name__ == "__main__":
    sys.exit(main())
