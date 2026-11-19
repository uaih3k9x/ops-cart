"""cs80_link 测试: 串口 raw 标志 / PTY 上整帧写完 / 会话 (HELLO、按 type+seq 等应答、KEY 分流、超时)."""

import os
import struct
import sys
import termios
import threading

import pytest

import cs80_link as cl
import cs80_proto as p
from cs80_fakedev import FakeDevice


# ---- raw: 手写 cfmakeraw (3.9 没有 tty.cfmakeraw, tty.setraw 不清 INLCR / IGNCR)

def test_raw_attrs_clears_everything_cfmakeraw_clears():
    full = 0xFFFFFFFF
    iflag, oflag, cflag, lflag, isp, osp, cc = cl.raw_attrs([full, full, full, full, 0, 0, [0] * 32])
    for f in ("IGNBRK", "BRKINT", "PARMRK", "ISTRIP", "INLCR", "IGNCR", "ICRNL", "IXON"):
        assert not iflag & getattr(termios, f), f
    assert not oflag & termios.OPOST
    for f in ("ECHO", "ECHONL", "ICANON", "ISIG", "IEXTEN"):
        assert not lflag & getattr(termios, f), f
    assert not cflag & termios.PARENB and cflag & termios.CSIZE == termios.CS8
    assert cflag & termios.CLOCAL and cflag & termios.CREAD
    assert cc[termios.VMIN] == 0 and cc[termios.VTIME] == 0


def _pty():
    master, slave = os.openpty()
    return master, os.ttyname(slave), slave


def test_port_on_pty_is_raw_no_echo_and_binary_clean():
    master, path, slave = _pty()
    try:
        port = cl.Port(path)
        data = bytes(range(256))
        port.write(data)
        got = b""
        while len(got) < 256:
            got += os.read(master, 4096)
        assert got == data                          # 0x0A / 0x0D 没被改写
        os.write(master, b"\x0d\x0a\xa5\x5a")
        assert port.read(1.0) == b"\x0d\x0a\xa5\x5a"
        port.write(b"x")
        assert os.read(master, 10) == b"x"          # 设备发来的字节没被回显回设备
        port.close()
    finally:
        os.close(master)
        os.close(slave)


def test_port_sets_tiocexcl(monkeypatch):
    calls = []
    real = cl.fcntl.ioctl
    monkeypatch.setattr(cl.fcntl, "ioctl", lambda fd, req, *a: calls.append(req) or real(fd, req, *a))
    master, path, slave = _pty()
    try:
        cl.Port(path).close()
        assert calls == [termios.TIOCEXCL, termios.TIOCNXCL]    # 独占标志挂在 tty 上: 关之前撤掉, 重连才开得开
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() == 0,
                    reason="macOS 的 pty 不认 TIOCEXCL; root 绕得过")
def test_port_is_exclusive_second_open_fails():
    """开着时别的进程 (调试用的 picocom / 别的上位机) 再开同一个节点要立刻失败, 不能悄悄抢字节."""
    master, path, slave = _pty()
    try:
        port = cl.Port(path)
        with pytest.raises(OSError):
            cl.Port(path)
        port.close()
    finally:
        os.close(master)
        os.close(slave)


def test_port_write_finishes_big_frame_through_small_pty_buffer():
    """非阻塞写在 PTY 上只写进一部分: 必须循环写完, 不能截断一帧."""
    master, path, slave = _pty()
    got = bytearray()

    def drain():
        while len(got) < 64 * 1024:
            got.extend(os.read(master, 1024))
    th = threading.Thread(target=drain, daemon=True)
    try:
        port = cl.Port(path)
        th.start()
        data = os.urandom(64 * 1024)
        port.write(data, timeout=5.0)
        th.join(5.0)
        assert bytes(got) == data
        port.close()
    finally:
        os.close(master)
        os.close(slave)


def test_port_write_times_out_when_nobody_reads():
    master, path, slave = _pty()
    try:
        port = cl.Port(path)
        with pytest.raises(OSError):
            port.write(b"\0" * (4 * 1024 * 1024), timeout=0.2)
        port.close()
    finally:
        os.close(master)
        os.close(slave)


# ---- 会话

def _link(**kw):
    dev = FakeDevice(**kw)
    return cl.Link(dev, clock=dev.clock), dev


def test_hello_sends_time_and_zone_and_parses_ack():
    link, dev = _link()
    info = link.hello(0x12345678, tz_min=540)
    assert dev.frames[0] == (p.HELLO, dev.frames[0][1], bytes.fromhex("78563412 1c02"))
    assert info["width"] == 800 and info["max_payload"] == 32768


def test_request_matches_type_and_seq_and_routes_keys():
    link, dev = _link()
    link.hello(0, 0)
    dev.press(p.KEY_PLUS)                             # KEY 夹在应答前面
    dev.out += p.encode(p.ACK, 0xEE, struct.pack("<BBH9I", p.PING, 0, 0, *([0] * 9)))   # 对不上的 seq
    a = link.request(p.PING)
    assert a["for_type"] == p.PING
    assert [k["key"] for k in link.take_keys()] == [p.KEY_PLUS]
    assert link.take_keys() == []


def test_request_times_out_with_oserror():
    link, dev = _link()
    dev.silent.add(p.PING)
    with pytest.raises(OSError):
        link.request(p.PING, timeout=0.5)


def test_seq_increments_and_wraps():
    link, dev = _link()
    link.seq = 254
    for _ in range(3):
        link.request(p.PING)
    assert [s for _t, s, _p in dev.frames] == [255, 0, 1]


def test_poll_collects_keys_without_blocking():
    link, dev = _link()
    dev.press(p.KEY_MINUS)
    link.poll()
    assert [k["key"] for k in link.take_keys()] == [p.KEY_MINUS]


def test_send_records_last_tx_time():
    link, dev = _link()
    dev.clock.t = 50.0
    link.send(p.RECT, b"")
    assert link.last_tx == 50.0
