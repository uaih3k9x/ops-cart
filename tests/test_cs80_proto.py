"""cs80_proto 测试: 帧编码 / 拆包 / 应答解码 / RGB565 / 脏区 / 段码屏 / 灯. 纯函数, 不碰设备.

字节样例都按 Monita 协议文档 (docs/protocol.md、docs/segmap.md, 固件 v10) 手算, 不取自它的参考实现.
"""

import struct
import time

import pytest

import cs80_proto as p


# ---- 帧格式: A5 5A | type | seq | u16 len | payload | u32 crc(type..payload)

def test_crc_is_crc32_ieee():
    assert p.crc(b"123456789") == 0xCBF43926            # 协议文档给的校验值


def test_encode_ping_bytes():
    assert p.encode(p.PING, 5) == bytes.fromhex("a5 5a 7f 05 00 00 3d ad d3 14")


def test_encode_hello_bytes():
    assert p.encode(p.HELLO, 0) == bytes.fromhex("a5 5a 01 00 00 00 79 b8 f8 99")


def test_encode_seq_wraps_and_length_is_little_endian():
    f = p.encode(p.SEG, 256 + 3, b"\x00" * 72)
    assert f[:6] == bytes.fromhex("a5 5a 10 03 48 00") and len(f) == 6 + 72 + 4


def test_hello_payload_time_and_signed_zone():
    assert p.hello_payload(0x12345678, 540) == bytes.fromhex("78 56 34 12 1c 02")
    assert p.hello_payload(0x12345678, -240) == bytes.fromhex("78 56 34 12 10 ff")


def test_local_tz_minutes_uses_gmtoff():
    now = time.time()
    assert p.tz_minutes(now) == time.localtime(now).tm_gmtoff // 60


# ---- 拆设备回包

def _ack(for_type, status=0, seq=1, counters=(0,) * 9):
    return p.encode(p.ACK, seq, struct.pack("<BBH9I", for_type, status, 0, *counters))


def test_parser_handles_split_and_glued_frames():
    a, b = _ack(p.PING, seq=1), _ack(p.SEG, seq=2)
    ps = p.Parser()
    data = a + b
    got = ps.feed(data[:3]) + ps.feed(data[3:17]) + ps.feed(data[17:])
    assert [(t, s) for t, s, _ in got] == [(p.ACK, 1), (p.ACK, 2)]


def test_parser_skips_garbage_and_bad_crc_one_byte_at_a_time():
    good = _ack(p.PING, seq=9)
    bad = bytearray(_ack(p.SEG, seq=8))
    bad[-1] ^= 0xFF
    got = p.Parser().feed(b"\x00\xa5\x11" + bytes(bad) + good)
    assert [(t, s) for t, s, _ in got] == [(p.ACK, 9)]


def test_parser_rejects_absurd_length_and_resyncs():
    junk = bytes.fromhex("a5 5a fe 01 ff ff")         # len 65535: 设备回包不可能这么长
    got = p.Parser().feed(junk + _ack(p.PING, seq=3))
    assert [(t, s) for t, s, _ in got] == [(p.ACK, 3)]


def test_parse_hello_ack():
    raw = bytes.fromhex("0000 0a00 2003 0005 01 48 b300 0080 0000")
    h = p.parse_hello_ack(raw)
    assert h == {"proto_ver": 0, "fw_ver": 10, "width": 800, "height": 1280, "pixfmt": 1,
                 "seg_bytes": 72, "caps": 0xB3, "max_payload": 32768}
    assert p.hello_problem(h) is None


@pytest.mark.parametrize("field,value", [("proto_ver", 1), ("width", 480), ("height", 800),
                                         ("pixfmt", 2), ("max_payload", 1031)])
def test_hello_problem_rejects_incompatible(field, value):
    h = p.parse_hello_ack(bytes.fromhex("0000 0a00 2003 0005 01 48 b300 0080 0000"))
    h[field] = value
    assert p.hello_problem(h)


def test_parse_ack_counters_and_errors():
    raw = struct.pack("<BBH9I", p.PRESENT, 0, 0, 100, 5, 1, 2, 80, 1, 3, 999, 1234)
    a = p.parse_ack(raw)
    assert a["for_type"] == p.PRESENT and a["status"] == 0 and a["rects"] == 80
    assert p.ack_errors(a) == 1 + 2 + 3 and a["extra"] == 1234


def test_parse_key():
    k = p.parse_key(bytes.fromhex("03 02 0700 e8030000"))
    assert k == {"key": 3, "ev": 2, "n": 7, "uptime": 1000}


# ---- RGB565 / RECT

def test_rgb565_little_endian_red_high_bits():
    from PIL import Image
    img = Image.new("RGB", (3, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (0, 255, 0))
    img.putpixel((2, 0), (0, 0, 255))
    assert p.rgb565(img) == bytes.fromhex("00f8 e007 1f00")


def test_rgb565_matches_bitwise_formula_on_all_levels():
    from PIL import Image
    img = Image.new("RGB", (256, 1))
    for v in range(256):
        img.putpixel((v, 0), (v, 255 - v, (v * 7) % 256))
    out = p.rgb565(img)
    for v in range(256):
        r, g, b = v, 255 - v, (v * 7) % 256
        want = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        assert struct.unpack_from("<H", out, v * 2)[0] == want


def test_rect_payload_header_and_exact_length():
    from PIL import Image
    img = Image.new("RGB", (800, 1280), (255, 0, 0))
    pl = p.rect_payload(img, (1, 2, 1, 1))
    assert pl == bytes.fromhex("0100 0200 0100 0100 00f8")
    big = p.rect_payload(img, (0, 16, 800, 16))
    assert len(big) == 8 + 800 * 16 * 2


# ---- 脏区

def _blank(color=(0, 0, 0)):
    from PIL import Image
    return Image.new("RGB", (800, 1280), color)


def test_dirty_none_when_same():
    a = _blank()
    assert p.dirty_rects(a, a.copy(), 32768) == []


def test_dirty_single_pixel_one_tile():
    a, b = _blank(), _blank()
    b.putpixel((100, 40), (0, 0, 1))             # 只差蓝色最低位也算脏
    assert p.dirty_rects(a, b, 32768) == [(96, 32, 32, 16)]


def test_dirty_full_frame_is_80_bands():
    rects = p.dirty_rects(None, _blank(), 32768)
    assert len(rects) == 80 and all(r[2] == 800 and r[3] == 16 for r in rects)
    assert sum(r[2] * r[3] for r in rects) == 800 * 1280


def test_dirty_splits_to_max_payload_and_covers_band():
    rects = p.dirty_rects(None, _blank(), 16384)
    assert all(8 + w * h * 2 <= 16384 for _x, _y, w, h in rects)
    band0 = sorted(r for r in rects if r[1] == 0)
    assert band0[0][0] == 0 and sum(r[2] for r in band0) == 800
    assert all(a[0] + a[2] == b[0] for a, b in zip(band0, band0[1:]))


def test_max_rect_width():
    assert p.max_rect_w(32768) == 800
    assert p.max_rect_w(16384) == 480         # (16384-8)//32 = 511 -> 向下对齐 32
    assert p.max_rect_w(1032) == 32
    assert p.max_rect_w(1031) == 0


# ---- 段码屏: fb[18*com + seg/8]; 第 k 列 fb[k] f/g, fb[18+k] a/b, fb[36+k] e/c, fb[54+k] d

def test_seg_digit_one_and_eight_bits():
    fb = p.seg_encode(main="1     ")
    assert fb[18] == 0x08 and fb[36] == 0x08 and sum(fb) == 0x10
    fb = p.seg_encode(main="8     ")
    assert (fb[0], fb[18], fb[36], fb[54]) == (0x88, 0x88, 0x88, 0x80)


def test_seg_dash_is_g_only_and_columns():
    fb = p.seg_encode(small="-     ", yuan=" -")
    assert fb[8] == 0x08 and fb[15] == 0x08 and sum(fb) == 0x10


def test_seg_icons():
    fb = p.seg_encode(icons={"MAIN_COLON2", "MAIN_COLON4", "BAT_FRAME", "BAT_D", "CHARGE",
                             "WIFI0", "WIFI3", "WIFI_X"})
    want = bytearray(72)
    for i, m in ((55, 0x08), (57, 0x08), (70, 0x80), (34, 0x80), (52, 0x80),
                 (61, 0x20), (25, 0x08), (43, 0x20)):
        want[i] |= m
    assert bytes(fb) == bytes(want)


def test_seg_is_72_bytes():
    assert len(p.seg_encode()) == 72 == p.SEG_BYTES


# ---- 段码屏内容 seg_state(snap, view, now)

NOW = time.mktime((2026, 11, 19, 9, 5, 7, 0, 0, -1))


def _digits(fb, cols):
    rev = {v: k for k, v in p.SEG_FONT.items()}
    out = ""
    for k in cols:
        m = 0
        for idx, bit, seg in ((k, 0x80, 5), (k, 0x08, 6), (18 + k, 0x80, 0), (18 + k, 0x08, 1),
                              (36 + k, 0x80, 4), (36 + k, 0x08, 2), (54 + k, 0x80, 3)):
            if fb[idx] & bit:
                m |= 1 << seg
        out += rev.get(m, "?")
    return out


def _icon(fb, name):
    i, m = p.SEG_ICONS[name]
    return bool(fb[i] & m)


def _view(**over):
    v = {"net": "on", "checks": {"ok": 3, "warn": 0, "fail": 0, "problems": []},
         "ups_alert": None, "serve": None, "agent": None, "stale": False}
    v.update(over)
    return v


def test_seg_state_clock_ups_percent_and_icons():
    snap = {"ups": {"percent": 60, "status": "OL", "state": "charging"}}
    fb = p.seg_state(snap, _view(), NOW)
    assert _digits(fb, range(6)) == "090507"
    assert _icon(fb, "MAIN_COLON2") and _icon(fb, "MAIN_COLON4")
    assert _digits(fb, range(8, 14)) == "    60"
    assert not _icon(fb, "CLK_COLON_A")
    assert _icon(fb, "BAT_FRAME") and _icon(fb, "CHARGE")
    cells = [_icon(fb, "BAT_" + c) for c in "ABCD"]
    assert cells == [False, True, True, True]              # ceil(60/25) = 3 格, 从负极端 (右, D) 起亮
    assert all(_icon(fb, "WIFI%d" % i) for i in range(4)) and not _icon(fb, "WIFI_X")
    assert _digits(fb, (14, 15)) == "  "


def test_seg_state_full_and_empty_battery():
    fb = p.seg_state({"ups": {"percent": 100, "status": "OL"}}, _view(), NOW)
    assert [_icon(fb, "BAT_" + c) for c in "ABCD"] == [True] * 4
    assert _digits(fb, range(8, 14)) == "   100"
    fb = p.seg_state({"ups": {"percent": 0, "status": "OB"}}, _view(), NOW)
    assert [_icon(fb, "BAT_" + c) for c in "ABCD"] == [False] * 4 and _icon(fb, "BAT_FRAME")


def test_seg_state_on_battery_shows_minutes_left_no_bolt():
    snap = {"ups": {"percent": 40, "status": "OB DISCHRG", "runtime": 1260}}
    fb = p.seg_state(snap, _view(), NOW)
    assert _digits(fb, range(8, 14)) == "    21" and not _icon(fb, "CHARGE")
    snap = {"ups": {"percent": 40, "status": "OB"}}               # 没有 runtime 就还显示电量
    assert _digits(p.seg_state(snap, _view(), NOW), range(8, 14)) == "    40"


def test_seg_state_no_ups_blank_battery_and_small_clock():
    fb = p.seg_state({}, _view(), NOW)
    assert _digits(fb, range(8, 14)) == "      " and not _icon(fb, "BAT_FRAME")


def test_seg_state_problem_count_caps_at_99():
    fb = p.seg_state({}, _view(checks={"ok": 0, "warn": 2, "fail": 1}), NOW)
    assert _digits(fb, (14, 15)) == " 3"
    fb = p.seg_state({}, _view(checks={"ok": 0, "warn": 150, "fail": 1}), NOW)
    assert _digits(fb, (14, 15)) == "99"


@pytest.mark.parametrize("net,levels,x", [("on", 4, False), ("warn", 2, True), ("off", 0, True),
                                          ("down", 0, True)])
def test_seg_state_wifi(net, levels, x):
    fb = p.seg_state({}, _view(net=net), NOW)
    assert [_icon(fb, "WIFI%d" % i) for i in range(4)] == [i < levels for i in range(4)]
    assert _icon(fb, "WIFI_X") is x


def test_seg_state_stale_small_clock_dashes():
    fb = p.seg_state({"ups": {"percent": 50}}, _view(stale=True), NOW)
    assert _digits(fb, range(6)) == "090507" and _digits(fb, range(8, 14)) == "------"


def test_seg_state_boot_view_only_clock():
    """开机自检页的 view 没有 checks / ups_alert / serve / agent / net, 不能炸, 只显示时钟."""
    boot = {"boot": True, "title": "OPS-CART", "version": "", "clock": "09:05", "lines": [],
            "verdict": None, "stale": False}
    fb = p.seg_state({"ups": {"percent": 50, "status": "OL"}}, boot, NOW)
    want = p.seg_encode(main="090507", icons={"MAIN_COLON2", "MAIN_COLON4"})
    assert fb == want


# ---- 灯

def test_led_payloads():
    assert p.led_payload("alert") == bytes.fromhex("01 02 f401 fa00 0000")     # 红闪 500 / 250
    assert p.led_payload("warn") == bytes.fromhex("03 01 0000 0000 0000")      # 红 + 绿 常亮
    assert p.led_payload("ok") == bytes.fromhex("02 01 0000 0000 0000")
    assert p.led_payload("selftest") == bytes.fromhex("04 01 0000 0000 0000")  # 蓝常亮, 不闪


def test_led_state_boot_view_is_selftest():
    boot = {"boot": True, "lines": [], "stale": False}
    assert p.led_state({}, boot) == "selftest"
    assert p.led_state({}, _view(stale=True)) == "selftest"


@pytest.mark.parametrize("over,want", [
    ({}, "ok"),
    ({"checks": {"ok": 1, "warn": 1, "fail": 0}}, "warn"),
    ({"checks": {"ok": 1, "warn": 1, "fail": 1}}, "alert"),
    ({"ups_alert": {"level": "warn"}}, "warn"),
    ({"ups_alert": {"level": "bad"}}, "alert"),
    ({"serve": {"level": "warn"}}, "warn"),
    ({"serve": {"level": "bad"}}, "alert"),
    ({"agent": {"text": "Agent 待确认 1", "level": "warn"}}, "warn"),
    ({"agent": {"text": "Agent 急停 2", "level": "bad"}}, "alert"),
])
def test_led_state_priority(over, want):
    assert p.led_state({}, _view(**over)) == want


def test_led_state_tolerates_missing_keys():
    assert p.led_state({}, {}) == "ok"
