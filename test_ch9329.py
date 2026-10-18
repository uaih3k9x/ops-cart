"""ch9329-tool 协议层测试: 帧构建/校验/键表/组合键. 串口部分实机验证."""


import pytest

import ch9329


def test_checksum_matches_real_device_frame():
    # 实机 GET_INFO 应答: 57 ab 00 c1 01 e4 a8
    assert ch9329.checksum(bytes.fromhex("57 ab 00 c1 01 e4")) == 0xA8


def test_get_info_frame():
    frame = ch9329.build_frame(ch9329.CMD_GET_INFO)
    assert frame == bytes.fromhex("57 ab 00 01 00 03")  # 校验含帧头: 57+AB+00+01+00 & 0xFF


def test_send_data_frame_single_key_a():
    report, unknown = ch9329.key_report(["a"])
    assert unknown == []
    frame = ch9329.build_frame(ch9329.CMD_SEND_DATA, report)
    # 报告: 00 00 04 00 00 00 00 00 (modifier,保留,code*6)
    assert report == bytes([0x00, 0x00, 0x04]) + b"\x00" * 5
    # 帧: 57 ab 00 02 08 <report> <sum>
    body = bytes([0x00, 0x02, 0x08]) + report
    assert frame == b"\x57\xab" + body + bytes([ch9329.checksum(b"\x57\xab" + body)])


def test_uppercase_uses_shift():
    report, _ = ch9329.key_report(["A"])
    assert report[0] == 0x02        # LShift
    assert report[2] == 0x04        # a 键位


def test_symbol_shift_mapping():
    report, _ = ch9329.key_report(["!"])
    assert report[0] == 0x02
    assert report[2] == 0x1E        # 1 键位 + shift
    report, _ = ch9329.key_report(["?"])
    assert (report[0], report[2]) == (0x02, 0x38)


def test_modifier_and_key_combo():
    report, unknown = ch9329.key_report(["ctrl", "alt", "delete"])
    assert unknown == []
    assert report[0] == 0x01 | 0x04          # ctrl|alt
    assert report[2] == ch9329.KEYS["delete"][0]


def test_six_key_limit():
    with pytest.raises(ValueError):
        ch9329.key_report(list("abcdefg"))


def test_unknown_key_rejected():
    _, unknown = ch9329.key_report(["sausage"])
    assert unknown == ["sausage"]


def test_all_printable_ascii_typable():
    """type_text 能打 US 布局所有可打印 ASCII."""
    import string
    for ch in string.printable:
        if ch in "\n\r\t\x0b\x0c":
            continue
        assert ch in ch9329.KEYS, f"缺字符 {ch!r}"


def test_dedup_same_code():
    # shift+a + A(也是shift+a) 不应重复占键位
    report, _ = ch9329.key_report(["A", "a"])
    assert report[2] == 0x04 and report[3] == 0x00


def test_mouse_report_frame():
    frame = ch9329.build_frame(ch9329.CMD_SEND_DATA,
                               bytes([0x01, 0x00, 0x05, 0x00, 0x00]))
    body = bytes([0x00, 0x02, 0x05, 0x01, 0x00, 0x05, 0x00, 0x00])
    assert frame == b"\x57\xab" + body + bytes([ch9329.checksum(b"\x57\xab" + body)])
    assert len(frame) == 11


def test_mouse_i8_clamps():
    """大位移要截到 int8 范围, 负数用两补码."""
    class FakeDev:
        def __init__(self):
            self.sent = []
        def _send_raw(self, frame):
            self.sent.append(frame)
            return b""
    import types
    dev = FakeDev()
    ch9329.Ch9329.send_mouse(dev, buttons=2, dx=300, dy=-300, wheel=1)
    rep = dev.sent[0][5:10]   # 帧头2+addr+cmd+len = 偏移5
    assert rep[0] == 0x02                     # 右键
    assert rep[1] == 127                      # dx 截 127
    assert rep[2] == 0x81                     # dy -127 -> 0x81
    assert rep[3] == 0x01                     # 滚轮
    assert rep[4] == 0x00
