#!/usr/bin/env python3
"""推车截图, 给远程的 Claude 用: 推车锁屏了也看得见被测机器的画面.

    sudo python3 /opt/pxe/cart_snap.py [-d /dev/video0] [-o /tmp/snap.png]

- kiosk 开着预览: 让 mpv 经 IPC 存当前视频帧 (取的是解码后的帧, 不是屏幕, 锁屏盖住也照拿);
- 没开预览: 采集卡空着, ffmpeg 直接抓一帧;
- 要 root (采集卡是 root:video, mpv 的 socket 属 root).
成功时打印图片路径, 退出码 0.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

MPV_SOCK = "/run/ops-cart/mpv.sock"     # kiosk 起 mpv 时带 --input-ipc-server
DEFAULT_DEV = "/dev/video0"


def mpv_request(path):
    return json.dumps({"command": ["screenshot-to-file", path, "video"]}) + "\n"


def ffmpeg_cmd(dev, out):
    return ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-i", dev, "-frames:v", "1", "-y", out]


def mpv_device(argv):
    """mpv 命令行里 av://v4l2:<dev> 的设备; 不是采集预览返回 None."""
    for a in argv:
        if a.startswith("av://v4l2:"):
            return a[len("av://v4l2:"):]
    return None


def route(want, mpv_dev, sock):
    """-> (怎么抓, 哪块卡). want=None 表示"现在预览的那块, 没预览就卡 1"."""
    dev = want or mpv_dev or DEFAULT_DEV
    if mpv_dev and dev == mpv_dev:
        return ("mpv" if sock else "busy"), dev
    return "ffmpeg", dev


def running_mpv_device():
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == "mpv":
            dev = mpv_device(argv)
            if dev:
                return dev
    return None


def via_mpv(out, timeout=5.0):
    if os.path.exists(out):
        os.remove(out)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(MPV_SOCK)
        s.sendall(mpv_request(out).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:          # mpv 回 {"error":"success"} 后文件才写完
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return True
            time.sleep(0.1)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-d", "--dev", help="采集设备, 默认: 正在预览的那块, 没预览就 /dev/video0")
    ap.add_argument("-o", "--out", default="/tmp/cart-snap.png")
    a = ap.parse_args(argv)
    how, dev = route(a.dev, running_mpv_device(), os.path.exists(MPV_SOCK))
    if how == "mpv":
        ok = via_mpv(a.out)
    elif how == "ffmpeg":
        ok = subprocess.run(ffmpeg_cmd(dev, a.out), timeout=20).returncode == 0
    else:
        print(f"{dev} 被预览占着, 但这个 mpv 没开 IPC (kiosk 还是旧版?)", file=sys.stderr)
        return 2
    if not ok:
        print(f"截图失败 ({how} {dev})", file=sys.stderr)
        return 1
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
