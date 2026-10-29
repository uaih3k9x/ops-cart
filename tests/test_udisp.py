"""udisp 协议层测试: 帧头布局 / 假 zlp 判定 / iProduct 解析. 真机用 udisp_send.py."""

import udisp


# 实机 (2026-10-29, ESP32-P4, 303a:2987) 的接口字符串
REAL_P4 = "esp32p4udisp0_R1280x800_Ejpg6_Fps46_Bl300000"
# README 里的示例 (T113 太极派)
REAL_T113 = "t113_R480x480_Ejpg6_Fps60_Bl300"


def test_header_is_16_bytes_le():
    h = udisp.build_header(udisp.TYPE_JPG, 0x1234, frame_id=5, x=1, y=2, width=0x400, height=0x200)
    assert len(h) == 16
    crc, ptype, cmd, x, y, w, hh, word = udisp.HEADER.unpack(h)
    assert (crc, ptype, cmd, x, y, w, hh) == (0, 3, 0, 1, 2, 0x400, 0x200)
    assert word & 0x3FF == 5
    assert word >> 10 == 0x1234


def test_frame_id_wraps_at_10_bits():
    _, _, _, _, _, _, _, word = udisp.HEADER.unpack(udisp.build_header(3, 1, frame_id=1024 + 7))
    assert word & 0x3FF == 7
    assert word >> 10 == 1


def test_payload_over_22_bits_rejected():
    try:
        udisp.build_header(3, 1 << 22)
    except ValueError:
        return
    raise AssertionError("超 22 位负载应抛 ValueError")


def test_build_frame_prepends_header():
    frame = udisp.build_frame(udisp.TYPE_JPG, b"\xff\xd8abc", frame_id=2, width=1280, height=800)
    assert frame[:2] == b"\x00\x00"
    assert frame[2] == 3 and frame[3] == 0
    assert frame[16:] == b"\xff\xd8abc"
    assert udisp.HEADER.unpack(frame[:16])[5:7] == (1280, 800)


def test_zlp_only_on_exact_multiple():
    assert udisp.needs_zlp(1024, 512)
    assert not udisp.needs_zlp(87756, 512)      # 实机那帧 87756 % 512 = 204
    assert not udisp.needs_zlp(100, 0)          # 端点包长读不到时不补


def test_zlp_frame_is_16_bytes_type_ff():
    z = udisp.zlp_frame()
    assert len(z) == 16
    assert z[2] == 0xFF
    assert udisp.HEADER.unpack(z)[7] == 0       # 负载长度 0


def test_parse_real_p4():
    cfg = udisp.parse_config(REAL_P4)
    assert (cfg["width"], cfg["height"]) == (1280, 800)
    assert cfg["enc"] == udisp.TYPE_JPG
    assert cfg["quality"] == 6
    assert cfg["fps"] == 46
    assert cfg["blimit"] == 300000


def test_parse_t113_example():
    cfg = udisp.parse_config(REAL_T113)
    assert (cfg["width"], cfg["height"]) == (480, 480)
    assert cfg["fps"] == 60 and cfg["blimit"] == 300   # Bl 原样返回, 单位见 docs


def test_parse_strips_nul_padding():
    # 实测描述符后面跟着一串 \x00
    cfg = udisp.parse_config(REAL_T113 + "\x00" * 40)
    assert cfg["width"] == 480


def test_parse_rgb_mode():
    cfg = udisp.parse_config("esp32udisp0_R320x240_Ergb16_Fps30")
    assert cfg["enc"] == udisp.TYPE_RGB565
    assert cfg["fps"] == 30


def test_parse_falls_back_to_defaults():
    for bad in ("", None, "gezi-lakala USB Display"):
        cfg = udisp.parse_config(bad)
        assert (cfg["width"], cfg["height"]) == (0, 0)
        assert cfg["enc"] == udisp.TYPE_JPG and cfg["quality"] == 5
        assert cfg["fps"] == 60 and cfg["blimit"] == 1920 * 1080 * 4


def test_rot90_rect_puts_top_left_at_bottom_left():
    # 横屏 1280x800, 左上角那一块 -> 竖屏左下角: x 在 800 的竖向上, y 贴近 1280 底边
    x2, y2, w2, h2 = udisp.rot90_rect(0, 0, 100, 50, 1280)
    assert (x2, y2, w2, h2) == (0, 1180, 50, 100)
    # 横屏右下角 -> 竖屏左上角
    assert udisp.rot90_rect(1180, 750, 100, 50, 1280) == (750, 0, 50, 100)
