"""cart_snap 测试: 只测命令拼装与选路, 不碰采集卡和 mpv."""

import json

import cart_snap as cs


def test_mpv_request_takes_raw_video_frame():
    req = json.loads(cs.mpv_request("/tmp/a.png"))
    # "video" = 只要视频帧本身, 不带 OSD/字幕; 锁屏盖在窗口上也不影响
    assert req == {"command": ["screenshot-to-file", "/tmp/a.png", "video"]}


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
