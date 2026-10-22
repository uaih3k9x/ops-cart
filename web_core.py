#!/usr/bin/env python3
"""web_core: 网页版 Kiosk 的可测核心层. HTTP 之外的逻辑都在这.

- 来源过滤: tailnet / 本机永远放行; 局域网看开关, 且推车必须设了 PIN
- 配置: /opt/pxe/web.conf ({"lan_open": bool, "port": int})
- 会话: 内存里的随机令牌, 闲置超时, 走 HttpOnly cookie
- 状态 JSON: 复用 kiosk_core 的采集函数, 单项出错不拖垮整份
"""

import ipaddress
import json
import os
import secrets
import time

import atomicfile
import kiosk_core as kc

WEB_CONF = "/opt/pxe/web.conf"
DEFAULT_WEB_CONF = {"lan_open": False, "port": 8088}
COOKIE = "cart_sid"

_TRUSTED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128",
    "100.64.0.0/10",            # tailnet (CGNAT 段)
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


def client_allowed(ip, lan_open, pin_set):
    kind = client_kind(ip)
    if kind == "trusted":
        return True
    return kind == "lan" and lan_open and pin_set


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
    def __init__(self, ttl_s=12 * 3600):
        self.ttl_s, self._seen = ttl_s, {}

    def issue(self, now=None):
        tok = secrets.token_urlsafe(32)
        self._seen[tok] = time.time() if now is None else now
        return tok

    def check(self, tok, now=None):
        now = time.time() if now is None else now
        last = self._seen.get(tok)
        if last is None or now - last > self.ttl_s:
            self._seen.pop(tok, None)
            return False
        self._seen[tok] = now
        return True

    def revoke(self, tok):
        self._seen.pop(tok, None)


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

def mouse_chunks(dx, dy, wheel):
    """大位移拆成多帧 int8; 滚轮夹到 ±1."""
    dx, dy = int(dx), int(dy)
    wheel = max(-1, min(1, int(wheel)))
    out = []
    while True:
        sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
        out.append((sx, sy, wheel))
        dx, dy, wheel = dx - sx, dy - sy, 0
        if not (dx or dy):
            return out


def mouse_buttons(b):
    return int(b) & 0x07


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
