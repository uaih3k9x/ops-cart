#!/usr/bin/env python3
"""web_core: 网页版 Kiosk 的可测核心层. HTTP 之外的逻辑都在这.

- 来源过滤: tailnet / 本机永远放行; 局域网看开关, 且推车必须设了 PIN; 装机口 (LAN1) 永远拒绝
- 配置: /opt/pxe/web.conf ({"lan_open": bool, "port": int, "deny_nets": [...]})
- 会话: 内存里的随机令牌, 闲置超时 + 绝对上限, 绑定签发时的 PIN, 走 HttpOnly cookie
- 请求校验: 键鼠事件先校验成干净的列表, 再碰 CH9329
- 状态 JSON: 复用 kiosk_core 的采集函数, 单项出错不拖垮整份
"""

import ipaddress
import json
import math
import os
import secrets
import threading
import time

import atomicfile
import kiosk_core as kc

WEB_CONF = "/opt/pxe/web.conf"
DEFAULT_WEB_CONF = {"lan_open": False, "port": 8088}
COOKIE = "cart_sid"

_TRUSTED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128",
    "100.64.0.0/10",            # tailnet (CGNAT 段)  public-safe-ignore
    "fd7a:115c:a1e0::/48",      # tailnet IPv6
)]


# ------------------------------------------------------------ 来源过滤

def client_kind(ip):
    """'trusted' (本机/tailnet) / 'lan' (其余) / None (解析不了)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if getattr(addr, "ipv4_mapped", None):       # ::ffff:a.b.c.d 双栈监听时会这样来
        addr = addr.ipv4_mapped
    return "trusted" if any(addr in n for n in _TRUSTED_NETS) else "lan"


def _in_nets(ip, nets):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    for n in nets:
        try:
            if addr in ipaddress.ip_network(n, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False


def source_reason(ip, lan_open, pin_set, deny_nets=(), local_ip=None):
    """放行返回 None, 否则返回给人看的拒绝原因.
    deny_nets = 装机口网段: 被测机插在 LAN1 上, 开了局域网开关也不许碰管理口;
    local_ip = 连接落在推车哪个地址上, 被测机手配了别的网段时靠它认出是从 LAN1 进来的."""
    kind = client_kind(ip)
    if kind is None:
        return "来源地址无法识别"
    if kind == "trusted":
        return None
    if _in_nets(ip, deny_nets) or (local_ip and _in_nets(local_ip, deny_nets)):
        return "装机口 (LAN1) 不提供管理访问"
    if not lan_open:
        return "未开放局域网访问"
    if not pin_set:
        return "推车未设 PIN, 局域网访问不可用"
    return None


def client_allowed(ip, lan_open, pin_set, deny_nets=()):
    return source_reason(ip, lan_open, pin_set, deny_nets) is None


def pin_state(path):
    """('set', 哈希) / ('unset', None) / ('unreadable', None). 读不到不能当成没设 (没设 = 不上锁)."""
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return "unset", None
    except (OSError, ValueError):
        return "unreadable", None
    stored = cfg.get("pin") if isinstance(cfg, dict) else None
    return ("set", stored) if stored else ("unset", None)


# ------------------------------------------------------------ 配置

def web_conf_read(path=WEB_CONF):
    cfg = dict(DEFAULT_WEB_CONF)
    try:
        cfg.update(json.load(open(path)))
    except (OSError, ValueError):
        pass
    return cfg


def web_conf_set(path=WEB_CONF, **kv):
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    cfg.update(kv)
    atomicfile.write_json(path, cfg)


# ------------------------------------------------------------ 会话

class Sessions:
    """令牌 -> {kind, pin_tag, born, seen}. 多线程 Handler 共用, 内部加锁.
    pin_tag 是签发时的 PIN 哈希: PIN 改了 (含从没设到设上) 旧令牌全部作废."""

    def __init__(self, ttl_s=12 * 3600, max_age_s=7 * 24 * 3600):
        self.ttl_s, self.max_age_s = ttl_s, max_age_s
        self._s, self._lock = {}, threading.Lock()

    def __len__(self):
        return len(self._s)

    def _alive(self, rec, now):
        return now - rec["seen"] <= self.ttl_s and now - rec["born"] <= self.max_age_s

    def issue(self, kind="trusted", pin_tag=None, now=None):
        now = time.time() if now is None else now
        tok = secrets.token_urlsafe(32)
        with self._lock:
            for t in [t for t, r in self._s.items() if not self._alive(r, now)]:
                del self._s[t]
            self._s[tok] = {"kind": kind, "pin_tag": pin_tag, "born": now, "seen": now}
        return tok

    def check(self, tok, now=None, pin_tag=None):
        now = time.time() if now is None else now
        with self._lock:
            rec = self._s.get(tok)
            if rec is None:
                return False
            if not self._alive(rec, now) or rec["pin_tag"] != pin_tag:
                del self._s[tok]
                return False
            rec["seen"] = now
            return True

    def kind(self, tok):
        rec = self._s.get(tok)
        return rec["kind"] if rec else None

    def revoke(self, tok):
        with self._lock:
            self._s.pop(tok, None)

    def revoke_kind(self, kind):
        with self._lock:
            gone = [t for t, r in self._s.items() if r["kind"] == kind]
            for t in gone:
                del self._s[t]
        return gone

    def revoke_all(self):
        with self._lock:
            gone = list(self._s)
            self._s.clear()
        return gone


def cookie_token(header):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and v:
            return v
    return None


# ------------------------------------------------------------ 状态 JSON

STATUS_KEYS = ("services", "nics", "misc", "temps", "fans", "version")


def default_status_sources(smart=lambda: None):
    fans_seen = set()
    return {
        "services": kc.services_status,
        "nics": kc.nics_status,
        "misc": kc.misc_status,
        "temps": lambda: kc.temps_status(smart=smart),
        "fans": lambda: kc.fans_to_show(kc.fans_status(), fans_seen),
        "version": kc.version_string,
    }


def status_payload(src, now=None):
    raw, errors = {}, {}
    for k in STATUS_KEYS:
        try:
            raw[k] = src[k]()
        except Exception as e:          # 单项坏了照样出其他项
            raw[k], errors[k] = None, f"{type(e).__name__}: {e}"
    return {
        "time": time.time() if now is None else now,
        "services": [{"name": n, "enabled": en, "active": ac}
                     for n, (en, ac) in (raw["services"] or {}).items()],
        "nics": raw["nics"] or [],
        "misc": raw["misc"] or {},
        "temps": [{"name": n, "c": c, "level": kc.temp_level(c)} for n, c in raw["temps"] or []],
        "fans": [{"name": n, "rpm": r} for n, r in raw["fans"] or []],
        "version": raw["version"] or "",
        "errors": errors,
    }


# ------------------------------------------------------------ 键盘: 浏览器 KeyboardEvent.code -> HID

MOD_BITS = {"ControlLeft": 0x01, "ShiftLeft": 0x02, "AltLeft": 0x04, "MetaLeft": 0x08,
            "ControlRight": 0x10, "ShiftRight": 0x20, "AltRight": 0x40, "MetaRight": 0x80}

USAGE = {f"Key{c}": 0x04 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
USAGE.update({f"Digit{c}": 0x1E + i for i, c in enumerate("1234567890")})
USAGE.update({f"F{i}": 0x39 + i for i in range(1, 13)})
USAGE.update({f"Numpad{c}": 0x59 + i for i, c in enumerate("123456789")})
USAGE.update({
    "Enter": 0x28, "Escape": 0x29, "Backspace": 0x2A, "Tab": 0x2B, "Space": 0x2C,
    "Minus": 0x2D, "Equal": 0x2E, "BracketLeft": 0x2F, "BracketRight": 0x30,
    "Backslash": 0x31, "Semicolon": 0x33, "Quote": 0x34, "Backquote": 0x35,
    "Comma": 0x36, "Period": 0x37, "Slash": 0x38, "CapsLock": 0x39,
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48, "Insert": 0x49,
    "Home": 0x4A, "PageUp": 0x4B, "Delete": 0x4C, "End": 0x4D, "PageDown": 0x4E,
    "ArrowRight": 0x4F, "ArrowLeft": 0x50, "ArrowDown": 0x51, "ArrowUp": 0x52,
    "NumLock": 0x53, "NumpadDivide": 0x54, "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56, "NumpadAdd": 0x57, "NumpadEnter": 0x58,
    "Numpad0": 0x62, "NumpadDecimal": 0x63, "IntlBackslash": 0x64, "ContextMenu": 0x65,
})


class KeyboardState:
    """按住/松开同步成 8 字节报告; 状态没变 (自动重复/未知键/超 6 键) 返回 None."""

    def __init__(self):
        self.mods, self.keys = 0, []

    def report(self):
        return bytes([self.mods, 0] + self.keys + [0] * (6 - len(self.keys)))

    def apply(self, code, down):
        if code in MOD_BITS:
            new = self.mods | MOD_BITS[code] if down else self.mods & ~MOD_BITS[code]
            if new == self.mods:
                return None
            self.mods = new
            return self.report()
        usage = USAGE.get(code)
        if usage is None:
            return None
        if down:
            if usage in self.keys or len(self.keys) >= 6:
                return None
            self.keys.append(usage)
        else:
            if usage not in self.keys:
                return None
            self.keys.remove(usage)
        return self.report()

    def release_all(self):
        self.mods, self.keys = 0, []
        return self.report()


# ------------------------------------------------------------ 鼠标 (相对位移, 跟 Tk 版同一条路)

MOUSE_MAX = 4096            # 一次位移上限 (像素): 再大也只是甩到屏幕边, 不值得在 115200 波特上发几十万帧


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def mouse_chunks(dx, dy, wheel):
    """大位移拆成多帧 int8 (先夹到 ±MOUSE_MAX); 滚轮夹到 ±1."""
    dx, dy = _clamp(int(dx), -MOUSE_MAX, MOUSE_MAX), _clamp(int(dy), -MOUSE_MAX, MOUSE_MAX)
    wheel = _clamp(int(wheel), -1, 1)
    out = []
    while True:
        sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
        out.append((sx, sy, wheel))
        dx, dy, wheel = dx - sx, dy - sy, 0
        if not (dx or dy):
            return out


def mouse_buttons(b):
    return int(b) & 0x07


# ------------------------------------------------------------ 请求校验 (拿 CH9329 之前做完)

HID_MAX_EVENTS = 256


def _num(v):
    """JSON 数字 -> int; bool / 字符串 / inf / nan 都不算."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"不是数字: {v!r}")
    return int(v)


def parse_hid_events(req):
    """{"events": [...]} -> 干净的事件列表; 任何一项不对整批拒绝 (ValueError)."""
    if not isinstance(req, dict) or not isinstance(req.get("events", []), list):
        raise ValueError("events 必须是列表")
    raw = req.get("events") or []
    if len(raw) > HID_MAX_EVENTS:
        raise ValueError(f"一次最多 {HID_MAX_EVENTS} 个事件")
    out = []
    for ev in raw:
        if not isinstance(ev, dict):
            raise ValueError("事件必须是对象")
        t = ev.get("t")
        if t == "k":
            code = ev.get("code")
            if not isinstance(code, str) or len(code) > 32:
                raise ValueError("键名不对")
            out.append({"t": "k", "code": code, "down": bool(ev.get("down"))})
        elif t == "m":
            out.append({"t": "m",
                        "dx": _clamp(_num(ev.get("dx", 0)), -MOUSE_MAX, MOUSE_MAX),
                        "dy": _clamp(_num(ev.get("dy", 0)), -MOUSE_MAX, MOUSE_MAX),
                        "b": mouse_buttons(_num(ev.get("b", 0))),
                        "w": _clamp(_num(ev.get("w", 0)), -1, 1)})
        elif t == "release":
            out.append({"t": "release"})
        else:
            raise ValueError(f"不认识的事件类型: {t!r}")
    return out


# ------------------------------------------------------------ 长连接: 定时复查鉴权

def relay(read, write, still_ok, clock=time.monotonic, every=2.0):
    """流式转发. read() 返回 bytes; b'' = 对端结束; None = 读超时 (只为了能复查).
    每隔 every 秒问一次 still_ok(): 退出登录 / 关局域网 / 改 PIN 之后流自己断."""
    last = clock()
    while True:
        chunk = read()
        if chunk == b"":
            return
        now = clock()
        if now - last >= every:
            last = now
            if not still_ok():
                return
        if chunk:
            write(chunk)


# ------------------------------------------------------------ 视频

def video_device(name, root="/dev"):
    import re
    if not re.fullmatch(r"video\d+", name or ""):
        return None
    path = f"{root}/{name}"
    return path if os.path.exists(path) else None


def video_cmd(dev, fps=15, quality=6):
    """采集卡 -> MJPEG 多段流 (multipart, 边界 'ffmpeg'), 浏览器 <img> 直接能放."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-i", dev,
            "-an", "-r", str(fps), "-q:v", str(quality),
            "-f", "mpjpeg", "pipe:1"]


# ------------------------------------------------------------ 串口 (conbroker)

def serial_ports(ddir):
    """在跑的 conbroker 服务: run/<name>.json 里有本地端口."""
    run = os.path.join(ddir, "run")
    try:
        names = sorted(os.listdir(run))
    except OSError:
        return []
    out = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            out.append({"name": fn[:-5], "port": int(json.load(open(os.path.join(run, fn)))["port"])})
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return out
