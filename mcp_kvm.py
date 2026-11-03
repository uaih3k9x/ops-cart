"""MCP 工具: 看和动被测机 (#41). 键鼠 kvm_* / OCR screen_ocr / 网络唤醒 wol_send.

键鼠: 一把写租约 (/run/ops-cart/kvm-lease.json, root 0600), 有期限要续, 同时一个写入者, 认令牌不认名字; 全部急停 (kvm_broker
读的公开标记) 时收回, 解除后要重新申请. 发键只经键鼠代理、来源固定 agent, 代理不在就报错, 不退回直开串口.
只给整键和整行, 不给原始报告 / 鼠标 (同 kvm_broker_core.AGENT_OPS).
闸门 (#42): 打字按"从上次回车起的整行"查高危词, 高危组合键也算, 命中就挂起等车机上的人确认; Agent 没有确认工具.
secret=true 的内容不进审计和日志, 命中高危词直接拒绝. 这只是减速带: 推车看不懂屏幕, 鼠标 / 图形界面 / 脚本 / 编码都拦不住,
真正的兜底是人盯和急停.
日志 (#43): /var/log/ops-cart/kvm-thread.log, JSONL; 确认 / 拒绝 / 急停收回另在 events.log 记摘要.
这里没有一个工具能解锁 PXE, 也不碰串口写路径.
"""

import contextlib
import fcntl
import hmac
import json
import os
import secrets
import subprocess
import time

import atomicfile
import cart_ocr
import cartlog
import cart_snap
import kvm_broker
import kiosk_core as kc
import kvm_core
import mcp_core as mc
import mcp_serial as ms
import serial_write_core as sw
import wol_core

LEASE_PATH = kvm_broker.LEASE_PATH
LOG_PATH = os.path.join(cartlog.LOG_DIR, "kvm-thread.log")
TTL_S, TTL_MIN_S, TTL_MAX_S = 60, 10, 300
LAN1 = "eno1"                   # 魔术包只从装机口发: LAN2 / 无线上都是别人的机器
TEXT_MAX = 1000
HISTORY_MAX = 10
SUMMARY_EVS = {"confirm", "reject", "revoked"}
OCR_MIN_LINE = 40.0             # 低于这个置信度的行当噪声扔掉
OCR_MIN_MEAN = 60.0             # 平均低于这个: 明说读不出, 叫 Agent 看图
OCR_HINT = "OCR 读不出 (没信号、纯图形画面都这样; 退回 Tesseract 时彩色桌面也读不出), 用 screenshot 看图"
# 串口闸门的词表之外, 操作系统上常见的清盘 / 引导命令 (子串匹配: mkfs.ext4、Format-Volume 都算)
DANGER_SUB = ("diskpart", "bcdedit", "mkfs", "wipefs", "fdisk", "sgdisk", "parted", "shred", "dd if=", "cipher /w",
              "format-volume", "clear-disk", "remove-item", "reg delete")
DANGER_KEYS = {frozenset(k) for k in (("ctrl", "alt", "delete"), ("ctrl", "alt", "del"), ("alt", "f4"),
                                      ("alt", "sysrq"))}


def danger_of(text):
    """这行里的高危词. 往多拦的方向错: 比如普通文字里带 write 也挂起."""
    low = (text or "").lower()
    return sw.danger_words(text) + [w for w in DANGER_SUB if w in low]


def danger_keys(keys):
    return frozenset(k.lower() for k in keys) in DANGER_KEYS


def _shown(text, secret):
    return f"<secret {len(text)} 字符>" if secret else text


class LeaseStore:
    """键鼠状态: {next, lease, pending, line, sending_until, history}. 每次操作加锁、读、算过期和急停、存、写日志.
    文件读不懂 = 没有租约 (lease_allows 同理)."""

    def __init__(self, path=LEASE_PATH, clock=time.time, stopped=kvm_broker.serial_all_stopped,
                 log_path=None, event=None):
        self.path, self.clock, self.stopped = path, clock, stopped
        real = path == LEASE_PATH
        self.log_path = log_path or (LOG_PATH if real else None)
        self.event = event if event is not None else (cartlog.event if real else None)

    @contextlib.contextmanager
    def _tx(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = self.clock()
            st, evs = settle(read_state(self.path), now, self.stopped()), []
            evs.extend(st.pop("_evs"))
            try:
                yield st, now, evs
            finally:
                atomicfile.write_text(self.path, json.dumps(st), mode=0o600)
                self._log(evs, now)
        finally:
            os.close(fd)

    def _log(self, evs, now):
        for rec in evs:
            rec = dict(t=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), **rec)
            if self.log_path:
                try:
                    with open(self.log_path, "a", encoding="utf-8") as f:     # 每行开一次: logrotate copytruncate 后照样写对
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            if rec["ev"] in SUMMARY_EVS and self.event is not None:
                self.event("kvm", cartlog.fields(ev=rec["ev"], who=rec.get("who"), id=rec.get("id"), log=self.log_path))

    def _need(self, st, token):
        if self.stopped():
            raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除, 之后重新申请租约")
        lease = st["lease"]
        if not lease or not hmac.compare_digest(str(lease.get("token", "")), str(token)):
            raise mc.ToolError("没有有效的键鼠写租约 (过期了? 急停收回了?), 先 kvm_lease_acquire")
        return lease

    def acquire(self, who, ttl):
        ttl = int(max(TTL_MIN_S, min(TTL_MAX_S, ttl)))
        with self._tx() as (st, now, evs):
            if self.stopped():
                raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除")
            if st["lease"]:             # 认令牌不认名字: stdio 下两个 Agent 可能是同一个用户
                raise mc.ToolError(f"键鼠写租约在 {st['lease']['who']} 手里, 还剩 {int(st['lease']['until'] - now)} 秒")
            st["lease"] = {"token": secrets.token_urlsafe(16), "who": who, "until": now + ttl, "ttl": ttl}
            st["line"] = ""
            evs.append({"ev": "lease_acquire", "src": "agent", "who": who, "ttl": ttl})
            return {"lease": st["lease"]["token"], "ttl_s": ttl, "hint": "用完 kvm_lease_release; 令牌别给别人."}

    def renew(self, token):
        with self._tx() as (st, now, _evs):
            lease = self._need(st, token)
            lease["until"] = now + lease.get("ttl", TTL_S)
            return {"expires_in": int(lease["until"] - now)}

    def release(self, token):
        with self._tx() as (st, _now, evs):
            lease = self._need(st, token)
            evs.append({"ev": "lease_release", "src": "agent", "who": lease["who"]})
            _drop_all(st, evs, "租约交还")
            st["lease"], st["line"] = None, ""
            return {"released": True}

    def gate(self, token, kind, keys=(), text="", enter=False, secret=False, serial=None):
        """-> None 直接发 (已标在发); 挂起时 -> 回给 Agent 的说明. secret 命中高危词抛 ToolError."""
        with self._tx() as (st, now, evs):
            who = self._need(st, token)["who"]
            if st["pending"]:           # 键盘是一条流: 接着发的话, 人确认后顺序就乱了
                raise mc.ToolError(f"有 {len(st['pending'])} 条在等确认, 处理完才能接着发; 一个键都没发. 用 kvm_status 看结果")
            shown = "+".join(keys) if kind == "key" else _shown(text, secret)
            line, why = st["line"], None
            if kind == "key":
                hits = ["+".join(keys)] if danger_keys(keys) else []
                why = "高危组合键, 要人确认" if hits else None
                full = line
            else:
                full = (line + text).rsplit("\n", 1)[-1]
                hits = danger_of(full)
                why = f"这一行带高危词 {'、'.join(hits)}, 要人确认" if hits else None
            if hits and secret:
                evs.append({"ev": "refused", "src": "agent", "who": who, "text": shown})   # 命中哪个词也不记, 算泄露
                raise mc.ToolError("secret 内容带高危词, 不能挂起给人看, 已拒绝; 一个键都没发")
            if hits:
                item = {"id": st["next"], "kind": kind, "keys": list(keys), "text": text, "enter": bool(enter),
                        "who": who, "t": now, "reason": why, "danger": hits, "line": full, "serial": serial}
                st["next"] += 1
                st["pending"].append(item)
                evs.append({"ev": "pending", "src": "agent", "who": who, "id": item["id"], "text": shown,
                            "danger": hits})
                return {"status": "pending", "id": item["id"], "reason": why, "danger": hits,
                        "hint": "已挂起, 一个键都没发. 要人在推车上确认才发; 用 kvm_status 看结果. 等的时候继续 "
                                "kvm_lease_renew, 租约过期这条就作废."}
            if kind == "type" and not secret:
                st["line"] = full[-TEXT_MAX:]
            if enter or (kind == "key" and [k.lower() for k in keys] == ["enter"]):
                st["line"] = ""
            st["sending_until"] = now + max(2.0, len(text) / 20)
            return None

    def finish(self, who, kind, shown, ok, why=None):
        with self._tx() as (st, _now, evs):
            st["sending_until"] = 0
            evs.append({"ev": "send", "src": "agent", "who": who, "kind": kind, "text": shown, "ok": ok, "why": why})

    def take(self, iid, by):
        """人确认 -> 取出那条, 不在 / 急停 / 租约没了都抛 ValueError, 一个键都不发."""
        with self._tx() as (st, _now, evs):
            if self.stopped():
                raise ValueError("全部急停中, 先解除")
            item = next((p for p in st["pending"] if p["id"] == iid), None)
            if item is None:
                raise ValueError(f"#{iid} 不在等确认 (租约过期或急停时作废)")
            st["pending"].remove(item)
            line = "" if item["enter"] else item["line"]
            st["line"] = line[-TEXT_MAX:]
            evs.append({"ev": "confirm", "src": "human", "who": by, "id": iid, "text": _item_text(item)})
            _remember(st, item, "confirmed", by)
            return item

    def reject(self, iid, by, why="人拒绝"):
        with self._tx() as (st, _now, evs):
            item = next((p for p in st["pending"] if p["id"] == iid), None)
            if item is None:
                raise ValueError(f"#{iid} 不在等确认")
            st["pending"].remove(item)
            evs.append({"ev": "reject", "src": "human", "who": by, "id": iid, "text": _item_text(item), "why": why})
            _remember(st, item, "rejected", by)

    def view(self):
        with self._tx() as (st, now, _evs):
            return public_view(st, now, self.stopped())


def _item_text(item):
    return "+".join(item["keys"]) if item["kind"] == "key" else item["text"] + (" + 回车" if item["enter"] else "")


def _remember(st, item, result, by):
    st["history"] = (st["history"] + [{"id": item["id"], "what": _item_text(item), "result": result, "by": by}])[-HISTORY_MAX:]


def _drop_all(st, evs, why):
    for item in st["pending"]:
        evs.append({"ev": "drop", "src": "system", "who": item["who"], "id": item["id"], "text": _item_text(item), "why": why})
        _remember(st, item, "dropped", why)
    st["pending"] = []


def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    lease = doc.get("lease") if isinstance(doc.get("lease"), dict) else None
    if lease and not isinstance(lease.get("until"), (int, float)):
        lease = None
    return {"next": doc["next"] if isinstance(doc.get("next"), int) else 1, "lease": lease,
            "pending": [p for p in doc.get("pending") or () if isinstance(p, dict)] if isinstance(doc.get("pending"), list) else [],
            "line": doc["line"] if isinstance(doc.get("line"), str) else "",
            "sending_until": doc["sending_until"] if isinstance(doc.get("sending_until"), (int, float)) else 0,
            "history": doc["history"] if isinstance(doc.get("history"), list) else []}


def settle(st, now, stopped):
    """算掉过期和急停: 租约没了, 挂着的一起作废. 带 _evs 给调用方写日志."""
    evs = []
    lease = st["lease"]
    if lease and stopped:
        evs.append({"ev": "revoked", "src": "system", "who": lease.get("who"), "why": "全部急停"})
        _drop_all(st, evs, "全部急停")
        st["lease"], st["line"] = None, ""
    elif lease and lease["until"] <= now:
        evs.append({"ev": "lease_expired", "src": "system", "who": lease.get("who")})
        _drop_all(st, evs, "租约过期")
        st["lease"], st["line"] = None, ""
    elif not lease and st["pending"]:
        _drop_all(st, evs, "没有租约")
    st["_evs"] = evs
    return st


def public_view(st, now, stopped):
    """给 Agent 和界面看: 不含令牌. kiosk 能看挂着的内容; 副屏只拿 confirm_core 算出来的几个数."""
    lease = st["lease"]
    return {"writer": lease["who"] if lease else None,
            "expires_in": int(lease["until"] - now) if lease else None,
            "stopped": bool(stopped), "sending": st["sending_until"] > now,
            "pending": [{"id": p["id"], "what": _item_text(p), "who": p["who"], "t": p["t"], "reason": p["reason"],
                         "danger": p["danger"], "line": p["line"]} for p in st["pending"]],
            "history": st["history"]}


def peek(path=LEASE_PATH, clock=time.time, stopped=kvm_broker.serial_all_stopped):
    """只读看一眼 (车机 / 网页 / 副屏): 不加锁不写盘, 过期和急停在内存里算掉."""
    now = clock()
    halted = stopped()
    st = settle(read_state(path), now, halted)
    st.pop("_evs")
    return public_view(st, now, halted)


def human_action(store, op, arg, by, opener=None, ports=None):
    """车机上人的按钮 -> 一句结果: confirm (arg = 编号, 会发键, 放后台线程调) / reject. 调用方保证是本机人."""
    if op == "reject":
        store.reject(arg, by)
        return f"已拒绝键鼠 #{arg}"
    if op != "confirm":
        raise ValueError(f"不认识的动作 {op}")
    item = store.take(arg, by)
    online = (ports or kvm_core.ports)()
    port = online.get(item.get("serial")) if item.get("serial") else next(iter(online.values()), None)
    shown = _item_text(item)
    if not port:
        store.finish(item["who"], item["kind"], shown, False, "键鼠不在线")
        raise ValueError("键鼠不在线, 没发")
    try:
        ch = (opener or real_opener)(port, item["who"])
        try:
            if item["kind"] == "key":
                ch.send_keys(item["keys"])
            else:
                ch.type_text(item["text"])
                if item["enter"]:
                    ch.send_keys(["enter"])
        finally:
            ch.close()
    except (OSError, ValueError) as e:
        store.finish(item["who"], item["kind"], shown, False, str(e))
        raise ValueError(f"发键失败: {e}") from None
    store.finish(item["who"], item["kind"], shown, True)
    return f"已发键鼠 #{arg}: {shown}"


def ocr_view(lines):
    """cart_ocr 的行 -> {readable, mean_conf, text, lines}. 按字数加权; 噪声行扔掉, 读不出就明说."""
    good = [ln for ln in lines if ln["text"].strip() and ln["conf"] >= OCR_MIN_LINE]
    chars = sum(len(ln["text"]) for ln in good)
    mean = sum(ln["conf"] * len(ln["text"]) for ln in good) / chars if chars else 0.0
    out = {"readable": bool(good) and mean >= OCR_MIN_MEAN, "mean_conf": round(mean, 1),
           "text": "\n".join(ln["text"] for ln in good),
           "lines": [{"text": ln["text"], "x": ln["x"], "y": ln["y"]} for ln in good]}
    if not out["readable"]:
        out["hint"] = OCR_HINT
    return out


def redact_type(args):
    """审计里 secret 的内容只留长度."""
    if isinstance(args, dict) and args.get("secret") and isinstance(args.get("text"), str):
        return dict(args, text=_shown(args["text"], True))
    return args


def build_tools(store, ports, opener, ocr, wol, broker_up, who=ms.current_who):
    """ports() -> {序列号: 路径}; opener(port, who) -> 代理连接 (send_keys / type_text / close);
    ocr(dev|None, lang) -> (行, 哪块卡的说明, 引擎); wol(mac, iface); broker_up(port) -> bool. 测试里全换成假的."""

    def _port(serial):
        online = ports()
        if serial:
            if serial not in online:
                raise mc.ToolError(f"没有序列号 {serial} 的键鼠; 在线的: {', '.join(online) or '没有'}")
            return online[serial]
        if not online:
            raise mc.ToolError("没有在线的键鼠 (CH9329 没插?)")
        if len(online) > 1:
            raise mc.ToolError(f"有几块键鼠, 用 serial 指定: {', '.join(online)}")
        return next(iter(online.values()))

    def _send(a, kind, fn, keys=(), text="", enter=False, secret=False):
        held = store.gate(a["lease"], kind, keys=keys, text=text, enter=enter, secret=secret, serial=a.get("serial"))
        if held:
            return [mc.json_content(held)]
        shown = "+".join(keys) if kind == "key" else _shown(text, secret) + (" + 回车" if enter else "")
        me = who()
        try:
            port = _port(a.get("serial"))
            try:
                ch = opener(port, me)
            except (FileNotFoundError, ConnectionRefusedError):
                raise mc.ToolError("键鼠代理 (kvm-broker) 没在跑; 不直开串口, 请人在推车上开服务") from None
            except (OSError, ValueError) as e:
                raise mc.ToolError(f"键鼠打不开: {e}") from None
            try:
                fn(ch)
            except (OSError, ValueError) as e:
                raise mc.ToolError(f"发键失败: {e}") from None
            finally:
                ch.close()
        except mc.ToolError as e:
            store.finish(me, kind, shown, False, str(e))
            raise
        store.finish(me, kind, shown, True)
        return [mc.json_content({"sent": True, "hint": "发完等几秒, 用 screen_ocr 看结果"})]

    def kvm_status(_a):
        online = ports()
        up = any(broker_up(p) for p in online.values()) if online else None
        return [mc.json_content(dict(store.view(), all_stopped=store.stopped(), keyboards=sorted(online), broker=up))]

    def kvm_lease_acquire(a):
        return [mc.json_content(store.acquire(who(), a["ttl_s"]))]

    def kvm_lease_renew(a):
        return [mc.json_content(store.renew(a["lease"]))]

    def kvm_lease_release(a):
        return [mc.json_content(store.release(a["lease"]))]

    def kvm_key(a):
        keys = [k for k in a["keys"].split("+") if k]
        return _send(a, "key", lambda ch: ch.send_keys(keys), keys=keys)

    def kvm_type(a):
        secret = bool(a.get("secret"))

        def go(ch):
            ch.type_text(a["text"], secret=secret)
            if a.get("enter"):
                ch.send_keys(["enter"])
        return _send(a, "type", go, text=a["text"], enter=bool(a.get("enter")), secret=secret)

    def screen_ocr(a):
        dev = a.get("device")
        if dev and not dev.startswith("/dev/"):
            dev = "/dev/" + dev
        lines, label, engine = ocr(dev, a["lang"])
        return [mc.json_content(dict(ocr_view(lines), screen=label, engine=engine))]

    def wol_send(a):
        try:
            mac = wol_core.parse_mac(a["mac"])
            wol(mac, LAN1)
        except (ValueError, OSError) as e:
            raise mc.ToolError(f"魔术包发不出: {e}") from None
        return [mc.json_content({"mac": mac, "iface": LAN1, "note": "发了, 不保证醒: 要机器开了网络唤醒且关机后"
                                 "网口还亮. 看 pxe_clients 或 screen_ocr 确认"})]

    LEASE = {"type": "string", "required": True, "maxLength": 64, "description": "kvm_lease_acquire 给的令牌"}
    SERIAL = {"type": "string", "maxLength": 32, "description": "键鼠序列号, 只有一块时不用给"}
    T = ms.Tool
    return [
        T("kvm_status", "键鼠状态 (只读): 在线的键鼠序列号、代理在不在、写入者、全部急停、挂起等确认的和最近结果.",
          {}, kvm_status, read_only=True),
        T("kvm_lease_acquire", "申请键鼠写租约 (同时一个写入者), 返回令牌. 全部急停时不给.",
          {"ttl_s": {"type": "integer", "minimum": TTL_MIN_S, "maximum": TTL_MAX_S, "default": TTL_S,
                     "description": "期限秒"}}, kvm_lease_acquire),
        T("kvm_lease_renew", "续键鼠写租约. 过期了要重新申请.", {"lease": LEASE}, kvm_lease_renew),
        T("kvm_lease_release", "交还键鼠写租约.", {"lease": LEASE}, kvm_lease_release),
        T("kvm_key", "往被测机按键或组合键, 如 enter / esc / f12. 高危组合键 (ctrl+alt+delete 等) 挂起等人确认. "
          "人最近 3 秒动过键鼠会排队. 按之前先 screen_ocr 确认焦点对.",
          {"keys": {"type": "string", "required": True, "maxLength": 64, "pattern": r"[A-Za-z0-9_+-]+",
                    "description": "键名, 组合键用 + 连"}, "lease": LEASE, "serial": SERIAL},
          kvm_key, destructive=True),
        T("kvm_type", "往被测机打一段 ASCII (被测机输入法要在英文). 整行带高危词 (format、diskpart 等) 就挂起等人确认, "
          "可能误拦普通文字. 内容进审计日志, 密码给 secret=true. 敲之前先 screen_ocr 确认焦点对.",
          {"text": {"type": "string", "required": True, "maxLength": TEXT_MAX, "description": "要打的字"},
           "enter": {"type": "boolean", "description": "打完按回车"},
           "secret": {"type": "boolean", "description": "密码等: 内容不进审计和日志; 带高危词直接拒绝"},
           "lease": LEASE, "serial": SERIAL},
          kvm_type, destructive=True, redact=redact_type),
        T("screen_ocr", "读被测机屏幕上的字 (OCR, 带坐标), 比 screenshot 省得多, 看画面先用这个. readable=false 时再用 "
          "screenshot. engine 写明用的引擎; 可能有形近字错误, 命令和路径回车前要核对.",
          {"device": {"type": "string", "pattern": r"(/dev/)?video[0-9]{1,2}", "maxLength": 16,
                      "description": "采集设备, 默认正在预览的那块, 没预览就有信号的采集卡"},
           "lang": {"type": "string", "enum": ["eng", "eng+chi_sim"], "default": "eng",
                    "description": "只对退回 Tesseract 时有用; RapidOCR 中英都读"}},
          screen_ocr, read_only=True),
        T("wol_send", "往 LAN1 (装机口) 发网络唤醒魔术包. 不保证醒.",
          {"mac": {"type": "string", "required": True, "maxLength": 17,
                   "pattern": r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", "description": "目标 MAC"}}, wol_send),
    ]


# ------------------------------------------------------------ 真机

def describe_dev(dev):
    """哪块卡 + 有没有信号; 探测失败不影响截图."""
    try:
        return mc.snap_label(dev, kc.video_friendly(dev), kc.video_signal(dev))
    except Exception:                   # noqa: BLE001
        return mc.snap_label(dev, None, None)


def real_opener(port, who):
    return kvm_broker.BrokerCh(port, src="agent", who=who)


def broker_up(port):
    try:
        kvm_broker.BrokerCh(port, src="agent", who="status").close()
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:                     # 代理在, 设备忙 / 拔了
        return True


def real_ocr(dev, lang):
    path, why, used = cart_snap.grab(dev)
    if path is None:
        raise mc.ToolError(why)
    try:
        lines, engine = cart_ocr.ocr_best(path, lang)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise mc.ToolError(f"OCR 失败 (tesseract / convert): {e}") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return lines, describe_dev(used), engine


def real_tools(store=None):
    return build_tools(store or LeaseStore(), ports=kvm_core.ports, opener=real_opener, ocr=real_ocr,
                       wol=wol_core.send, broker_up=broker_up)
