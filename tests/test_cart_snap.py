"""cart_snap 测试: 只测命令拼装与选路, 不碰采集卡和 mpv."""

import json

import cart_snap as cs


def test_mpv_request_takes_raw_video_frame():
    req = json.loads(cs.mpv_request("/tmp/a.png", 7))
    # "video" = 只要视频帧本身, 不带 OSD/字幕; 锁屏盖在窗口上也不影响
    assert req == {"command": ["screenshot-to-file", "/tmp/a.png", "video"], "request_id": 7}


def test_ffmpeg_grabs_one_frame():
    cmd = cs.ffmpeg_cmd("/dev/video1", "/tmp/b.png")
    assert cmd[:2] == ["ffmpeg", "-loglevel"]
    assert ["-f", "v4l2", "-i", "/dev/video1"] == cmd[3:7]
    assert cmd[-3:] == ["1", "-y", "/tmp/b.png"]


def test_mpv_device_from_cmdline():
    argv = ["mpv", "--wid=1", "--input-ipc-server=/run/ops-cart/mpv.sock", "av://v4l2:/dev/video0"]
    assert cs.mpv_device(argv) == "/dev/video0"
    assert cs.mpv_device(["mpv", "foo.mkv"]) is None


def test_route_prefers_mpv_when_it_holds_the_device():
    assert cs.route(want=None, mpv_dev="/dev/video0", sock=True) == ("mpv", "/dev/video0")
    assert cs.route(want="/dev/video0", mpv_dev="/dev/video0", sock=True) == ("mpv", "/dev/video0")
    # 要的是另一块卡: mpv 没占它, 直接抓
    assert cs.route(want="/dev/video1", mpv_dev="/dev/video0", sock=True) == ("ffmpeg", "/dev/video1")
    # 没开预览: 直接抓, 默认卡 1
    assert cs.route(want=None, mpv_dev=None, sock=False) == ("ffmpeg", "/dev/video0")
    # 老版本 kiosk 起的 mpv 没有 IPC: 占着设备又没接口, 只能报错说明
    assert cs.route(want=None, mpv_dev="/dev/video0", sock=False) == ("busy", "/dev/video0")


def test_mpv_cmd_has_ipc_socket():
    import kiosk_core as kc
    assert f"--input-ipc-server={cs.MPV_SOCK}" in kc.mpv_cmd(1, "/dev/video0")


# ============================================================ C 线: 等 mpv 回包 / 超时 / 安全路径

import socket
import subprocess
import threading


def _fake_mpv(reply_error="success", write=True, extra_lines=()):
    a, b = socket.socketpair()

    def serve():
        req = json.loads(b.makefile().readline())
        for ln in extra_lines:
            b.sendall(ln.encode() + b"\n")
        if write:
            with open(req["command"][1], "wb") as f:
                f.write(b"PNG")
        b.sendall(json.dumps({"request_id": req["request_id"] + 1, "error": "success"}).encode() + b"\n")
        b.sendall(json.dumps({"request_id": req["request_id"], "error": reply_error}).encode() + b"\n")
    threading.Thread(target=serve, daemon=True).start()
    return a


def test_via_mpv_waits_for_matching_reply(tmp_path):
    out = tmp_path / "s.png"
    conn = _fake_mpv(extra_lines=['{"event":"property-change"}'])
    assert cs.via_mpv(str(out), timeout=2, connect=lambda: conn) is True
    assert out.read_bytes() == b"PNG"


def test_via_mpv_reports_mpv_error(tmp_path):
    conn = _fake_mpv(reply_error="error running command", write=False)
    assert cs.via_mpv(str(tmp_path / "s.png"), timeout=2, connect=lambda: conn) is False


def test_via_mpv_times_out_without_reply(tmp_path):
    a, b = socket.socketpair()
    try:
        assert cs.via_mpv(str(tmp_path / "s.png"), timeout=0.2, connect=lambda: a) is False
    finally:
        b.close()


def test_default_output_is_private_temp_file(tmp_path):
    path = cs.default_out(str(tmp_path))
    import os
    import stat
    assert path.startswith(str(tmp_path)) and path.endswith(".png")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    other = cs.default_out(str(tmp_path))
    assert other != path


def test_ffmpeg_timeout_and_busy_are_explained(monkeypatch, capsys):
    monkeypatch.setattr(cs, "running_mpv_device", lambda: None)

    def slow(cmd, **kw):
        assert kw["timeout"] > 0
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(cs.subprocess, "run", slow)
    assert cs.main(["-o", "/nonexistent/x.png"]) == 1
    assert "超时" in capsys.readouterr().err

    class R:
        returncode = 1
        stderr = b"[video4linux2] ioctl(VIDIOC_STREAMON): Device or resource busy"
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **kw: R())
    assert cs.main(["-o", "/nonexistent/x.png"]) == 1
    assert "占用" in capsys.readouterr().err


# ============================================================ grab(): 给 cart_mcp 用, 不往 stdout 打字

def test_grab_ok_returns_path_and_prints_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cs, "running_mpv_device", lambda: None)
    monkeypatch.setattr(cs, "RUN_DIR", str(tmp_path))

    def ok(dev, out):
        open(out, "wb").write(b"PNG")
        return True, ""
    monkeypatch.setattr(cs, "via_ffmpeg", ok)
    path, why, dev = cs.grab("/dev/video1")
    assert path.startswith(str(tmp_path)) and open(path, "rb").read() == b"PNG"
    assert why == "" and dev == "/dev/video1"
    assert capsys.readouterr().out == ""            # stdio 模式下 stdout 是协议通道


def test_grab_failure_removes_own_temp_file(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(cs, "running_mpv_device", lambda: None)
    monkeypatch.setattr(cs, "RUN_DIR", str(tmp_path))
    monkeypatch.setattr(cs, "via_ffmpeg", lambda dev, out: (False, "没信号"))
    path, why, dev = cs.grab()
    assert path is None and "没信号" in why and dev == cs.DEFAULT_DEV
    assert os.listdir(tmp_path) == []


def test_grab_busy_without_ipc(monkeypatch):
    monkeypatch.setattr(cs, "running_mpv_device", lambda: "/dev/video0")
    monkeypatch.setattr(cs.os.path, "exists", lambda p: False)
    path, why, dev = cs.grab()
    assert path is None and "IPC" in why
