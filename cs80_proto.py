#!/usr/bin/env python3
"""cs80_proto: Monita-CS80 副屏协议的最小编码器. 纯函数, 不碰设备.

按 Monita 仓库的协议文档 (docs/protocol.md / docs/segmap.md, 固件 v10) 独立实现, 只做 cart-dash 用到的消息
(HELLO / SEG / RECT / PRESENT / BACKLIGHT / LED / PING, 收 HELLO_ACK / ACK / KEY). 没有复制它的任何代码.

帧格式 (全小端): A5 5A | u8 type | u8 seq | u16 len | payload | u32 crc, crc = CRC-32/IEEE(type..payload).
"""

import struct
import time
import zlib

import ups_core

SYNC = b"\xa5\x5a"
HELLO, SEG, RECT, PRESENT, BACKLIGHT, LED, SLEEP, PING = 0x01, 0x10, 0x20, 0x21, 0x30, 0x31, 0x32, 0x7F
HELLO_ACK, KEY, ACK = 0x81, 0x90, 0xFE

CAP_KEYS, CAP_LED, CAP_SLEEP = 1 << 4, 1 << 5, 1 << 7
STATUS = {0: "OK", 1: "参数不对", 3: "不支持"}

PANEL_W, PANEL_H = 800, 1280
SEG_BYTES = 72
BAND, TILE = 16, 32                     # 脏区: 16 行一条带, 横向按 32 像素对齐
MAX_RX_LEN = 4096                       # 设备回包最长 40 B (ACK); 更长的一定是错位, 前进 1 字节重新找同步

KEY_MENU, KEY_PLUS, KEY_MINUS = 2, 3, 4
EV_DOWN, EV_UP, EV_LONG = 1, 2, 3


def crc(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def encode(ptype, seq, payload=b""):
    body = struct.pack("<BBH", ptype, seq & 0xFF, len(payload)) + bytes(payload)
    return SYNC + body + struct.pack("<I", crc(body))


def tz_minutes(now):
    """当地时区相对 UTC 的分钟数 (含夏令时), 按 now 那一刻算."""
    return time.localtime(now).tm_gmtoff // 60


def hello_payload(unix_time, tz_min):
    return struct.pack("<Ih", int(unix_time) & 0xFFFFFFFF, int(tz_min))


class Parser:
    """流式拆设备回包: feed(bytes) -> [(type, seq, payload)]. 坏帧 / 垃圾前进 1 字节重新找 A5 5A."""

    def __init__(self, max_len=MAX_RX_LEN):
        self.buf, self.max_len = bytearray(), max_len

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]        # 留最后一个字节: 可能是半个 A5 5A
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            ln = struct.unpack_from("<H", self.buf, 4)[0]
            if ln > self.max_len:
                del self.buf[:1]
                continue
            if len(self.buf) < 6 + ln + 4:
                return out
            body = bytes(self.buf[2:6 + ln])
            if struct.unpack_from("<I", self.buf, 6 + ln)[0] != crc(body):
                del self.buf[:1]
                continue
            del self.buf[:6 + ln + 4]
            out.append((body[0], body[1], body[4:]))


def parse_hello_ack(payload):
    f = struct.unpack_from("<HHHHBBHH", payload)
    keys = ("proto_ver", "fw_ver", "width", "height", "pixfmt", "seg_bytes", "caps", "max_payload")
    return dict(zip(keys, f))


def hello_problem(h):
    """HELLO_ACK 不兼容的原因; 能用返回 None. 不符就当打不开, 不硬缩放."""
    if h["proto_ver"] != 0:
        return "proto_ver %d" % h["proto_ver"]
    if (h["width"], h["height"]) != (PANEL_W, PANEL_H):
        return "尺寸 %dx%d" % (h["width"], h["height"])
    if h["pixfmt"] != 1:
        return "pixfmt %d" % h["pixfmt"]
    if max_rect_w(h["max_payload"]) < TILE:
        return "max_payload %d 放不下一个 %dx%d tile" % (h["max_payload"], TILE, BAND)
    return None


_ACK_KEYS = ("rx_bytes", "frames", "crc_err", "len_err", "rects", "presents", "bad_param", "uptime", "extra")


def parse_ack(payload):
    f = struct.unpack_from("<BBH9I", payload)
    a = {"for_type": f[0], "status": f[1]}
    a.update(zip(_ACK_KEYS, f[3:]))
    return a


def ack_errors(a):
    return a["crc_err"] + a["len_err"] + a["bad_param"]


def parse_key(payload):
    k, ev, n, up = struct.unpack_from("<BBHI", payload)
    return {"key": k, "ev": ev, "n": n, "uptime": up}


# ---------------------------------------------------------------- 像素

_LUT_HI_R = [v & 0xF8 for v in range(256)]
_LUT_HI_G = [v >> 5 for v in range(256)]
_LUT_LO_G = [(v & 0x1C) << 3 for v in range(256)]
_LUT_LO_B = [v >> 3 for v in range(256)]


def rgb565(img):
    """RGB 图 -> RGB565 小端字节 (R 在高 5 位). 用通道查表, 不逐像素循环."""
    from PIL import Image, ImageChops
    r, g, b = img.convert("RGB").split()
    hi = ImageChops.add(r.point(_LUT_HI_R), g.point(_LUT_HI_G))
    lo = ImageChops.add(g.point(_LUT_LO_G), b.point(_LUT_LO_B))
    return Image.merge("LA", (lo, hi)).tobytes()


def rect_payload(img, box):
    x, y, w, h = box
    return struct.pack("<4H", x, y, w, h) + rgb565(img.crop((x, y, x + w, y + h)))


def max_rect_w(max_payload, band=BAND, tile=TILE, width=PANEL_W):
    """一个 band 行高的 RECT 最多多宽 (按 tile 向下对齐). 0 = 一个 tile 都放不下."""
    w = max(0, (max_payload - 8) // (band * 2)) // tile * tile
    return min(w, width)


def dirty_rects(prev, img, max_payload):
    """和上一帧比, 返回要发的 [(x, y, w, h)]. prev 为 None (或尺寸不同) = 整帧.
    每 16 行一条带取变化范围, 横向对齐到 32; 超过 max_payload 横向切开."""
    from PIL import ImageChops
    W, H = img.size
    maxw = max_rect_w(max_payload, width=W)
    if maxw <= 0:
        raise ValueError("max_payload %d 太小" % max_payload)
    diff = None if prev is None or prev.size != img.size else ImageChops.difference(prev, img)
    out = []
    for y in range(0, H, BAND):
        h = min(BAND, H - y)
        if diff is None:
            x0, x1 = 0, W
        else:
            bb = diff.crop((0, y, W, y + h)).getbbox()
            if not bb:
                continue
            x0, x1 = bb[0] // TILE * TILE, min(W, -(-bb[2] // TILE) * TILE)
        for x in range(x0, x1, maxw):
            out.append((x, y, min(maxw, x1 - x), h))
    return out


# ---------------------------------------------------------------- 段码屏

SEG_FONT = {"0": 0x3F, "1": 0x06, "2": 0x5B, "3": 0x4F, "4": 0x66, "5": 0x6D, "6": 0x7D, "7": 0x07,
            "8": 0x7F, "9": 0x6F, "-": 0x40, " ": 0x00}
# 段 bit (a..g = bit0..6) -> (显存偏移相对第 k 列, 位)
_SEG_BITS = ((0, 18, 0x80), (1, 18, 0x08), (2, 36, 0x08), (3, 54, 0x80), (4, 36, 0x80), (5, 0, 0x80), (6, 0, 0x08))
SEG_ICONS = {
    "MAIN_COLON2": (55, 0x08), "MAIN_COLON4": (57, 0x08),
    "CLK_COLON_A": (63, 0x08), "CLK_COLON_B": (65, 0x08),
    "BAT_FRAME": (70, 0x80), "CHARGE": (52, 0x80),
    "BAT_A": (70, 0x40), "BAT_B": (16, 0x40), "BAT_C": (16, 0x80), "BAT_D": (34, 0x80),
    "WIFI0": (61, 0x20), "WIFI1": (7, 0x20), "WIFI2": (7, 0x08), "WIFI3": (25, 0x08), "WIFI_X": (43, 0x20),
}
MAIN_COLS, SMALL_COLS, YUAN_COLS = range(0, 6), range(8, 14), range(14, 16)


def seg_encode(main="", small="", yuan="", icons=()):
    """主区 6 位 / 小时钟 6 位 / 元88 两位 (数字、-、空格) + 图标名 -> 72 字节显存."""
    fb = bytearray(SEG_BYTES)
    for cols, text in ((MAIN_COLS, main), (SMALL_COLS, small), (YUAN_COLS, yuan)):
        for k, ch in zip(cols, text):
            m = SEG_FONT[ch]
            for bit, base, mask in _SEG_BITS:
                if m & (1 << bit):
                    fb[base + k] |= mask
    for name in icons:
        i, mask = SEG_ICONS[name]
        fb[i] |= mask
    return bytes(fb)


_WIFI = {"on": (4, False), "warn": (2, True)}


def seg_state(snap, view, now):
    """快照 + 版面 -> 段码屏显存. 主区是本机时钟 HH:MM:SS; 小时钟区放 UPS 电量 (走电池时剩余分钟);
    元88 放自检 FAIL + WARN 数; 电池图标看 UPS, WiFi 图标看网络. 开机自检页只显示时钟.
    view 可能是开机自检页 (缺 checks / net 等键), 一律 .get()."""
    snap, view = snap or {}, view or {}
    t = time.localtime(now)
    main = "%02d%02d%02d" % (t.tm_hour, t.tm_min, t.tm_sec)
    icons = {"MAIN_COLON2", "MAIN_COLON4"}
    if view.get("boot"):
        return seg_encode(main=main, icons=icons)
    ups = snap.get("ups") if isinstance(snap.get("ups"), dict) else None
    pct = _int(ups.get("percent")) if ups else None
    small = ""
    if view.get("stale"):
        small = "------"
    elif pct is not None:
        mins = _int(ups.get("runtime")) if ups_core.on_battery(ups) else None
        small = "%6d" % min(999999, (mins // 60) if mins is not None else max(0, pct))
    if ups:
        icons.add("BAT_FRAME")
        if not ups_core.on_battery(ups):
            icons.add("CHARGE")
        cells = 0 if pct is None else min(4, max(0, -(-pct // 25)))
        icons.update("BAT_" + c for c in "DCBA"[:cells])       # 正极在左: 从右 (D) 开始亮
    level, cross = _WIFI.get(view.get("net"), (0, True))
    icons.update("WIFI%d" % i for i in range(level))
    if cross:
        icons.add("WIFI_X")
    checks = view.get("checks") or {}
    n = (_int(checks.get("fail")) or 0) + (_int(checks.get("warn")) or 0)
    yuan = "%2d" % min(99, n) if n > 0 else ""
    return seg_encode(main=main, small=small, yuan=yuan, icons=icons)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 灯

LED_RED, LED_GREEN, LED_BLUE = 1, 2, 4
# 名字 -> (mask, mode, period_ms, on_ms). 自检中蓝常亮: 蓝闪是固件"主机走了"的心跳, 分不清
LEDS = {"selftest": (LED_BLUE, 1, 0, 0), "alert": (LED_RED, 2, 500, 250),
        "warn": (LED_RED | LED_GREEN, 1, 0, 0), "ok": (LED_GREEN, 1, 0, 0)}


def led_payload(state):
    mask, mode, period, on = LEDS[state]
    return struct.pack("<BBHHH", mask, mode, period, on, 0)


def led_state(snap, view):
    """灯三态 (+ 自检中): 自检 / 数据源停了 > 告警 > 注意 > 正常. 缺键按没数据."""
    view = view or {}
    if view.get("boot") or view.get("stale"):
        return "selftest"
    checks = view.get("checks") or {}
    levels = [(view.get(k) or {}).get("level") for k in ("ups_alert", "serve", "agent")]
    if checks.get("fail") or "bad" in levels:
        return "alert"
    if checks.get("warn") or "warn" in levels:
        return "warn"
    return "ok"
