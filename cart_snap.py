#!/usr/bin/env python3
"""推车截图, 给远程的 Claude 用: 推车锁屏了也看得见被测机器的画面.

    sudo python3 /opt/pxe/cart_snap.py [-d /dev/video0] [-o 输出.png]

- kiosk 开着预览: 让 mpv 经 IPC 存当前视频帧 (取的是解码后的帧, 不是屏幕, 锁屏盖住也照拿),
  等到 mpv 对这条命令的回包 (request_id 对上) 才算数;
- 没开预览: 采集卡空着, ffmpeg 直接抓一帧;
- 要 root (采集卡是 root:video, mpv 的 socket 属 root). 不给 -o 时输出到 /run/ops-cart 下
  新建的私有文件 (mkstemp, 0600), 不写 /tmp 里的固定文件名 (root 写固定名会被符号链接坑).
成功时打印图片路径, 退出码 0; 抓图失败 1; 用法/设备状态问题 2.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

MPV_SOCK = "/run/ops-cart/mpv.sock"     # kiosk 起 mpv 时带 --input-ipc-server
RUN_DIR = "/run/ops-cart"
DEFAULT_DEV = "/dev/video0"
FFMPEG_TIMEOUT = 20


def mpv_request(path, rid=1):
    return json.dumps({"command": ["screenshot-to-file", path, "video"], "request_id": rid}) + "\n"


def default_out(run_dir=RUN_DIR):
    """新建一个只有自己能读写的空文件当输出; /run/ops-cart 不在就退回系统临时目录."""
    d = run_dir if os.path.isdir(run_dir) and os.access(run_dir, os.W_OK) else None
    fd, path = tempfile.mkstemp(prefix="cart-snap-", suffix=".png", dir=d)
    os.close(fd)
    return path


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


def _connect_mpv():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(MPV_SOCK)
    return s


def via_mpv(out, timeout=5.0, connect=_connect_mpv):
    """发截图命令, 等 request_id 对得上的回包; mpv 回 success 时文件已经写完.
    中间夹着的事件行 (property-change 之类) 和别人的回包都跳过."""
    rid = int(time.time() * 1000) & 0x7FFFFFFF
    deadline = time.monotonic() + timeout
    try:
        with connect() as s:
            s.settimeout(timeout)
            s.sendall(mpv_request(out, rid).encode())
            buf = b""
            while time.monotonic() < deadline:
                s.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = s.recv(4096)
                if not chunk:
                    return False
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("request_id") == rid:
                        return msg.get("error") == "success" and os.path.getsize(out) > 0
    except (OSError, socket.timeout):
        return False
    return False


def via_ffmpeg(dev, out):
    """-> (成功?, 失败原因)."""
    try:
        r = subprocess.run(ffmpeg_cmd(dev, out), capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg 超时 ({FFMPEG_TIMEOUT} 秒): 采集卡没出帧, 看看信号线"
    except OSError as e:
        return False, f"ffmpeg 起不来: {e}"
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or b"").decode(errors="replace")
    if "busy" in err.lower():
        return False, f"{dev} 被别的进程占用 (网页口的画面或别的 ffmpeg), 先关掉再抓"
    return False, err.strip().splitlines()[-1] if err.strip() else f"ffmpeg 退出码 {r.returncode}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-d", "--dev", help="采集设备, 默认: 正在预览的那块, 没预览就 /dev/video0")
    ap.add_argument("-o", "--out", help="输出文件, 默认在 /run/ops-cart 下新建私有文件")
    a = ap.parse_args(argv)
    how, dev = route(a.dev, running_mpv_device(), os.path.exists(MPV_SOCK))
    if how == "busy":
        print(f"{dev} 被预览占着, 但这个 mpv 没开 IPC (kiosk 还是旧版?)", file=sys.stderr)
        return 2
    out = a.out or default_out()
    if how == "mpv":
        ok, why = via_mpv(out), "mpv 没回成功 (预览刚关? 再试一次)"
    else:
        ok, why = via_ffmpeg(dev, out)
    if not ok:
        print(f"截图失败 ({how} {dev}): {why}", file=sys.stderr)
        if not a.out:                           # 自己建的空文件别留着
            try:
                os.unlink(out)
            except OSError:
                pass
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
