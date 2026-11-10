"""打印机写入和状态 (#104): 写有超时, 打印机不在有提示, 忙有提示. 全用假设备, 不碰 /dev/usb/lp0."""

import contextlib
import fcntl
import os
import socket
from types import SimpleNamespace

import pytest

import label_core as lc
import label_device as ld
import mcp_core as mc


# ---------------------------------------------------------------- 写

def test_write_job_writes_everything(tmp_path):
    p = tmp_path / "lp"
    p.write_bytes(b"")
    ld.write_job(b"abc" * 1000, dev=str(p))
    assert p.read_bytes() == b"abc" * 1000


def test_write_job_missing_printer_is_clear(tmp_path):
    with pytest.raises(mc.ToolError, match="没接"):
        ld.write_job(b"x", dev=str(tmp_path / "gone"))
    assert not (tmp_path / "gone").exists()     # 不许造出普通文件


def test_write_job_times_out_when_printer_stalls(tmp_path):
    fifo = str(tmp_path / "lp")
    os.mkfifo(fifo)
    rd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # 有读端但从不读, 管道写满后一直堵
    try:
        with pytest.raises(mc.ToolError, match="超时"):
            ld.write_job(b"x" * (1 << 20), dev=fifo, timeout=0.2)
    finally:
        os.close(rd)


def test_write_job_oserror_is_clear(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setattr(ld.ml, "_open", lambda dev, flags: a.detach())
    b.close()
    with pytest.raises(mc.ToolError, match="失败"):
        ld.write_job(b"x" * 100000, dev="/dev/fake", timeout=1)


class FakeUsblp:
    """按 usblp 非阻塞写记账: write 提交 URB 马上返回, 在途时不可写 (wcomplete=0), close 取消在途 URB."""

    def __init__(self, stall=False):
        self.got, self.inflight, self.stall = bytearray(), False, stall
        self.cancelled, self.closed, self.log = 0, 0, []

    def select(self, r, w, x, t=None):
        self.log.append("select")
        if self.inflight and self.stall:
            return [], [], []
        self.inflight = False
        return [], w, []

    def write(self, fd, data):
        if self.inflight:
            raise BlockingIOError
        self.inflight = True
        self.got += bytes(data)
        self.log.append("write")
        return len(data)

    def close(self, fd):
        self.cancelled += self.inflight
        self.closed += 1
        self.log.append("close")


def _use_usblp(monkeypatch, dev):
    monkeypatch.setattr(ld.ml, "_open", lambda path, flags: 99)
    monkeypatch.setattr(ld, "select", SimpleNamespace(select=dev.select))
    monkeypatch.setattr(ld, "os", SimpleNamespace(write=dev.write, close=dev.close, O_WRONLY=1, O_RDWR=2, O_NONBLOCK=4))


def test_write_job_waits_for_last_urb_before_close(monkeypatch):
    dev = FakeUsblp()
    _use_usblp(monkeypatch, dev)
    data = b"x" * (2 * ld._CHUNK + 1)
    ld.write_job(data, dev="/dev/fake", timeout=1)
    assert bytes(dev.got) == data and dev.cancelled == 0 and dev.log[-3:] == ["write", "select", "close"]


def test_write_job_last_urb_stalled_is_timeout_not_success(monkeypatch):
    dev = FakeUsblp(stall=True)
    _use_usblp(monkeypatch, dev)
    with pytest.raises(mc.ToolError, match="超时"):
        ld.write_job(b"x", dev="/dev/fake", timeout=0.01)
    assert dev.closed == 1


# ---------------------------------------------------------------- 状态

def test_read_status_returns_byte(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setattr(ld.ml, "_open", lambda dev, flags: a.detach())
    b.sendall(b"\x04")
    try:
        assert ld.read_status(dev="/dev/fake", timeout=1) == 0x04
        assert b.recv(16) == b"\x1b!?"
    finally:
        b.close()


def test_read_status_no_reply_is_clear(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setattr(ld.ml, "_open", lambda dev, flags: a.detach())
    try:
        with pytest.raises(mc.ToolError, match="没回状态"):
            ld.read_status(dev="/dev/fake", timeout=0.05)
    finally:
        b.close()


def test_query_status_does_not_wait_for_lock():
    waits = []

    @contextlib.contextmanager
    def lock(wait_s=None):
        waits.append(wait_s)
        yield
    assert ld.query_status(read=lambda: 0x04, lock=lock)["flags"] == ["缺纸"]
    assert waits == [0]


def test_query_status_lock_held_is_printer_busy(tmp_path):
    path = tmp_path / "lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(ld.ml.PrinterBusy, match="忙"):
            ld.query_status(read=lambda: 0, lock=lambda wait_s: ld.ml.printer_lock(path=str(path), wait_s=wait_s))
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 发送

def test_send_checks_status_then_writes_under_lock():
    log = []

    @contextlib.contextmanager
    def lock():
        log.append("lock")
        yield
        log.append("unlock")
    ld.send(b"job", write=lambda d: log.append(d), read=lambda: log.append("status") or 0, lock=lock)
    assert log == ["lock", "status", b"job", "unlock"]


@pytest.mark.parametrize("byte, word", [(0x04, "缺纸"), (0x08, "缺碳带"), (0x01, "打印头")])
def test_send_refuses_when_printer_not_ready(byte, word):
    wrote = []
    with pytest.raises(lc.LabelError, match=word):
        ld.send(b"job", write=wrote.append, read=lambda: byte, lock=contextlib.nullcontext)
    assert wrote == []


def test_send_allows_busy_printing_status():
    wrote = []
    ld.send(b"job", write=wrote.append, read=lambda: 0x20, lock=contextlib.nullcontext)
    assert wrote == [b"job"]


def test_send_propagates_device_failure():
    def bad(_d):
        raise mc.ToolError("写 /dev/usb/lp0 失败: EIO")
    with pytest.raises(mc.ToolError, match="失败"):
        ld.send(b"job", write=bad, read=lambda: 0, lock=contextlib.nullcontext)


def _plan():
    def render(lines, fold):
        ink = lc.Ink(8, 2, b"\x01" * 16)
        return lc.build_plan(lc.Fit(20, lines, [ink], 40), fold)
    return render


def test_print_label_renders_and_sends_copies():
    sent, notes = [], []
    plan = ld.print_label(["SW-A"], 10.0, copies=2, render=_plan(), send_fn=sent.append, note=notes.append)
    assert plan.lines == ["SW-A"] and sent == [lc.tspl_job(plan, 2)] and notes


def test_print_label_refuses_blocking_layout():
    def render(lines, fold):
        ink = lc.Ink(8, 2, b"\x01" * 16)
        return lc.Plan([lc.Placed(0, 20, ink)], fold, lc.dots(fold), 20, lines)      # 左边出了旗子
    sent = []
    with pytest.raises(lc.LabelError, match="旗子"):
        ld.print_label(["SW-A"], 10.0, render=render, send_fn=sent.append, note=lambda m: None)
    assert sent == []


def test_print_label_allows_fold_warning_and_returns_it():      # 兜底路径: 真实版面不压折痕, 这里手搓一个
    def render(lines, fold):
        ink = lc.Ink(8, 2, b"\x01" * 16)
        return lc.Plan([lc.Placed(480, lc.dots(fold) - 1, ink)], fold, lc.dots(fold), 20, lines)
    sent = []
    plan = ld.print_label(["SW-A"], 10.0, render=render, send_fn=sent.append, note=lambda m: None)
    assert sent and any("折痕" in m for m in lc.problems(plan))


def test_note_failure_does_not_block_print():
    sent = []

    def bad(_m):
        raise RuntimeError("log down")
    ld.print_label(["SW-A"], 10.0, render=_plan(), send_fn=sent.append, note=bad)
    assert len(sent) == 1


def test_feed_and_ruler_go_through_send():
    sent = []
    ld.feed(send_fn=sent.append)
    ld.ruler(send_fn=sent.append)
    assert sent == [lc.feed_job(), lc.ruler_job()]
