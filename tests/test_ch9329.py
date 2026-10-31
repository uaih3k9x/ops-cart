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
    """相对鼠标走 CMD_SEND_MS_REL_DATA(0x05), 数据 5 字节且首字节固定 0x01.
    2026-10-21 实机: 之前误用 0x02(键盘通道)发 5 字节, 一动鼠标目标机就乱输入."""
    frame = ch9329.build_frame(ch9329.CMD_SEND_MS_REL, bytes([0x01, 0x00, 0x05, 0x00, 0x00]))
    body = bytes([0x00, 0x05, 0x05, 0x01, 0x00, 0x05, 0x00, 0x00])
    assert frame == b"\x57\xab" + body + bytes([ch9329.checksum(b"\x57\xab" + body)])


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
    f = dev.sent[0]
    assert f[3] == 0x05 and f[4] == 5         # 相对鼠标命令, 5 字节
    rep = f[5:10]                             # 帧头2+addr+cmd+len = 偏移5
    assert rep[0] == 0x01                     # 相对鼠标固定首字节
    assert rep[1] == 0x02                     # 右键
    assert rep[2] == 127                      # dx 截 127
    assert rep[3] == 0x81                     # dy -127 -> 0x81
    assert rep[4] == 0x01                     # 滚轮


def test_send_report_raw_keyboard_frame():
    """按住/松开同步要直接发 8 字节键盘报告, 不走 send_keys 的按下即抬起."""
    class FakeDev:
        def __init__(self):
            self.sent = []
        def _send_raw(self, frame):
            self.sent.append(frame)
            return b""
    dev = FakeDev()
    rep = bytes([0x04, 0, 0x2B, 0, 0, 0, 0, 0])
    ch9329.Ch9329.send_report(dev, rep)
    assert dev.sent == [ch9329.build_frame(ch9329.CMD_SEND_DATA, rep)]


def test_abs_mouse_frame_and_clamp():
    """绝对鼠标: CMD_SEND_MS_ABS=0x04, 数据 [0x02, 按键, xL, xH, yL, yH, 滚轮], 坐标 0~4095."""
    class FakeDev:
        def __init__(self):
            self.sent = []
        def _send_raw(self, frame):
            self.sent.append(frame)
            return b""
    dev = FakeDev()
    ch9329.Ch9329.send_mouse_abs(dev, 1, 2048, 5000, -3)
    f = dev.sent[0]
    assert f[3] == 0x04 and f[4] == 7
    assert f[5:12] == bytes([0x02, 0x01, 0x00, 0x08, 0xFF, 0x0F, 0xFD])   # y 夹到 4095, 滚轮 -3 原样


# ============================================================ 加固: 独占 / 拔线 / ACK / 键名表

import os

import termios


def _pty():
    master, slave = os.openpty()
    return master, slave, os.ttyname(slave)


def test_gone_is_oserror_subclass():
    assert issubclass(ch9329.Ch9329Gone, OSError)
    assert issubclass(ch9329.Ch9329Busy, OSError)


def test_second_open_of_same_port_is_busy():
    master, slave, path = _pty()
    try:
        a = ch9329.Ch9329(path, log=lambda *x: None)
        with pytest.raises(ch9329.Ch9329Busy):
            ch9329.Ch9329(path, log=lambda *x: None)
        a.close()
        ch9329.Ch9329(path, log=lambda *x: None).close()     # 关掉之后又能开
    finally:
        os.close(master)
        os.close(slave)


def test_send_after_unplug_raises_gone():
    master, slave, path = _pty()
    dev = ch9329.Ch9329(path, log=lambda *x: None)
    os.close(master)                                         # 拔线
    os.close(slave)
    with pytest.raises(ch9329.Ch9329Gone):
        for _ in range(3):                                   # 有的系统第一次 write 还能进缓冲
            dev.send_report(bytes(8))
    dev.close()


def test_accepts_open_fd_and_writes_frame():
    master, slave, path = _pty()
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        dev = ch9329.Ch9329(fd=fd, log=lambda *x: None, ack_timeout=0.01)
        dev.send_report(bytes(8))
        assert os.read(master, 64) == ch9329.build_frame(ch9329.CMD_SEND_DATA, bytes(8))
        dev.close()
    finally:
        os.close(master)
        os.close(slave)


def test_flush_only_drops_input(monkeypatch):
    calls = []
    monkeypatch.setattr(ch9329.termios, "tcflush", lambda fd, q: calls.append(q))
    dev = ch9329.Ch9329.__new__(ch9329.Ch9329)
    dev.fd, dev.ack_timeout = -1, 0
    monkeypatch.setattr(ch9329.os, "write", lambda fd, b: len(b))
    monkeypatch.setattr(ch9329.select, "select", lambda r, w, x, t: ([], [], []))
    dev._send_raw(b"x")
    assert calls == [termios.TCIFLUSH]


def test_termios_error_becomes_gone(monkeypatch):
    def boom(*a):
        raise termios.error(5, "Input/output error")
    monkeypatch.setattr(ch9329.termios, "tcflush", boom)
    dev = ch9329.Ch9329.__new__(ch9329.Ch9329)
    dev.fd, dev.ack_timeout = -1, 0
    with pytest.raises(ch9329.Ch9329Gone):
        dev._send_raw(b"x")


def test_unknown_baud_is_value_error_and_lookup_is_lazy():
    assert ch9329.baud_const(115200) == termios.B115200
    assert ch9329.baud_const(12345) is None


def test_ack_timeout_is_short():
    assert ch9329.ACK_TIMEOUT <= 0.1


def test_parse_ack():
    ok = ch9329.build_frame(0x82, b"\x00")                  # SEND_DATA 正常应答
    assert ch9329.parse_ack(ok) == {"cmd": 0x02, "ok": True, "data": b"\x00"}
    err = bytes.fromhex("57 ab 00 c1 01 e4 a8")             # 实机: 0xC0 位 = 出错应答
    assert ch9329.parse_ack(err) == {"cmd": 0x01, "ok": False, "data": b"\xe4"}
    assert ch9329.parse_ack(b"\x00\x00" + ok) == ch9329.parse_ack(ok)   # 前面有垃圾也找得到
    assert ch9329.parse_ack(ok[:-1] + b"\x00") is None      # 校验不对
    assert ch9329.parse_ack(ok[:4]) is None                 # 不完整
    assert ch9329.parse_ack(b"") is None


@pytest.mark.parametrize("name", ["win", "super", "gui"])
def test_win_is_gui_modifier(name):
    report, unknown = ch9329.key_report([name, "r"])
    assert unknown == [] and report[0] == 0x08 and report[2] == 0x15


def test_key_aliases_and_extra_keys():
    assert ch9329.KEYS["del"] == ch9329.KEYS["delete"]
    assert ch9329.KEYS["printscreen"][0] == 0x46
    assert ch9329.KEYS["scrolllock"][0] == 0x47
    assert ch9329.KEYS["pause"][0] == 0x48
    assert ch9329.KEYS["numlock"][0] == 0x53
    assert ch9329.KEYS["f13"][0] == 0x68 and ch9329.KEYS["f24"][0] == 0x73
    assert ch9329.KEYS["menu"][0] == 0x65


def test_type_text_rejects_whole_string_before_sending():
    class FakeDev:
        sent = []

        def send_keys(self, keys, press_ms=30):
            self.sent.append(keys)
    dev = FakeDev()
    with pytest.raises(ValueError) as e:
        ch9329.Ch9329.type_text(dev, "ab中cé")
    assert dev.sent == [] and "中" in str(e.value) and "é" in str(e.value)
    assert ch9329.untypable("ab\n\tc") == []


def test_mouse_chunks_shared_here():
    assert ch9329.mouse_chunks(300, -5, 3) == [(127, -5, 1), (127, 0, 0), (46, 0, 0)]
    assert ch9329.mouse_chunks(0, 0, 0) == [(0, 0, 0)]
    big = ch9329.mouse_chunks(10 ** 9, 0, 0)
    assert len(big) == ch9329.MOUSE_MAX // 127 + 1


# ------------------------------------------------------------ 命令行经键鼠代理 (#9)


class _FakeCh:
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        return lambda *a, **kw: self.log.append((name, a, kw)) or "ab cd"


def _cli(monkeypatch, argv):
    import kvm_broker
    log, opened = [], []

    def open_ch(port, src="agent", who="cli", baud=115200, sock_path=None):
        opened.append((port, src, who, baud))
        return _FakeCh(log)
    monkeypatch.setattr(kvm_broker, "open_ch", open_ch)
    ch9329.main(argv)
    return opened, log


def test_cli_goes_through_open_ch_as_agent_by_default(monkeypatch):
    opened, log = _cli(monkeypatch, ["--port", "/dev/ttyACM1", "type", "hi"])
    assert opened == [("/dev/ttyACM1", "agent", "cli", 115200)]
    assert log[0][0] == "type_text" and log[0][1] == ("hi",) and log[-1][0] == "close"


def test_cli_src_who_and_old_subcommands(monkeypatch):
    opened, log = _cli(monkeypatch, ["--src", "human", "--who", "ops", "combo", "ctrl", "alt", "del"])
    assert opened[0][1:3] == ("human", "ops")
    assert log[0][:2] == ("send_keys", (["ctrl", "alt", "del"],))
    _o, log = _cli(monkeypatch, ["hold", "500", "a"])
    assert log[0] == ("send_keys", (["a"],), {"press_ms": 500})


def test_cli_estop_and_resume(monkeypatch):
    _o, log = _cli(monkeypatch, ["estop", "--all"])
    assert log[0] == ("estop", (), {"all_devs": True})
    _o, log = _cli(monkeypatch, ["--src", "human", "resume"])
    assert log[0] == ("resume", (), {"all_devs": False})
