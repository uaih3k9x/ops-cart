"""cart_snap 测试: 只测命令拼装与选路, 不碰采集卡和 mpv."""

import json

import cart_snap as cs


def test_mpv_request_takes_raw_video_frame():
    req = json.loads(cs.mpv_request("/tmp/a.png", 7))
    # "video" = 只要视频帧本身, 不带 OSD/字幕; 锁屏盖在窗口上也不影响
    assert req["command"]["flags"] == "video" and req["command"]["filename"] == "/tmp/a.png"


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
            with open(req["command"]["filename"], "wb") as f:
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


# ---- 残留的旧 mpv 不能冒充当前预览; 默认不写死 video0 (#39)

def _proc(root, pid, argv, start):
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    fields = ["S"] + ["0"] * 18 + [str(start)]      # ")" 后面第 20 个是 starttime
    (d / "stat").write_text(f"{pid} (mpv) " + " ".join(fields))


def test_running_mpv_is_the_newest_not_the_first(tmp_path):
    _proc(tmp_path, 10, ["mpv", "av://v4l2:/dev/video0"], 100)
    _proc(tmp_path, 20, ["mpv", "av://v4l2:/dev/video3"], 300)
    _proc(tmp_path, 30, ["mpv", "av://v4l2:/dev/video0"], 200)
    (tmp_path / "self").mkdir()
    assert cs.running_mpv_device(str(tmp_path)) == "/dev/video3"


def test_running_mpv_ignores_other_programs(tmp_path):
    _proc(tmp_path, 10, ["bash", "av://v4l2:/dev/video0"], 100)
    assert cs.running_mpv_device(str(tmp_path)) is None


def test_route_default_comes_from_caller():
    assert cs.route(None, None, False, default="/dev/video3") == ("ffmpeg", "/dev/video3")
    assert cs.route("/dev/video2", None, False, default="/dev/video3") == ("ffmpeg", "/dev/video2")


def test_route_named_device_being_previewed_uses_mpv():
    assert cs.route("/dev/video3", "/dev/video3", True) == ("mpv", "/dev/video3")


def test_default_device_prefers_capture_with_signal(monkeypatch):
    import kiosk_core as kc
    monkeypatch.setattr(kc, "video_nodes", lambda: [{"dev": "/dev/video0", "name": "USB Camera"},
                                                    {"dev": "/dev/video3", "name": "SC0710:RAW 01"}])
    monkeypatch.setattr(kc, "video_signal", lambda d: True)
    assert cs.default_device() == "/dev/video3"


def test_default_device_falls_back_when_probe_fails(monkeypatch):
    import kiosk_core as kc
    def boom():
        raise OSError("no v4l2")
    monkeypatch.setattr(kc, "video_nodes", boom)
    assert cs.default_device() == cs.DEFAULT_DEV


# ------------------------------------------------------------ #49 审查: 不闪 OSD, ffmpeg 进程间互斥 + 冷却

def test_mpv_request_no_osd():
    req = json.loads(cs.mpv_request("/run/x.png", 7))
    assert req == {"command": {"name": "screenshot-to-file", "filename": "/run/x.png", "flags": "video",
                               "_flags": ["no-osd"]}, "request_id": 7}


def test_ffmpeg_slot_mutex_and_cooldown(tmp_path):
    import fcntl
    import os
    import pytest
    p = str(tmp_path / "snap.lock")
    t = [1000.0]
    with cs.ffmpeg_slot(p, cooldown=3, clock=lambda: t[0]):         # 第一次: 文件刚建也不算冷却中
        pass
    t[0] += 1
    with pytest.raises(cs.SnapBusy, match="刚抓过"):
        with cs.ffmpeg_slot(p, cooldown=3, clock=lambda: t[0]):
            pass
    t[0] += 3
    fd = os.open(p, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                                  # 别的进程正在抓
    try:
        with pytest.raises(cs.SnapBusy, match="正在"):
            with cs.ffmpeg_slot(p, cooldown=3, clock=lambda: t[0]):
                pass
    finally:
        os.close(fd)
    with cs.ffmpeg_slot(p, cooldown=3, clock=lambda: t[0]):
        pass


def test_grab_reports_busy_instead_of_running_ffmpeg(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(cs, "running_mpv_device", lambda: None)
    monkeypatch.setattr(cs, "via_ffmpeg", lambda dev, out: ran.append(dev) or (True, ""))
    monkeypatch.setattr(cs, "SNAP_LOCK", str(tmp_path / "snap.lock"))
    out = str(tmp_path / "a.png")
    assert cs.grab("/dev/video0", out)[0] == out
    path, why, _ = cs.grab("/dev/video0", out)
    assert path is None and "刚抓过" in why and len(ran) == 1
