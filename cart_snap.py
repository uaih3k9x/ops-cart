#!/usr/bin/env python3
"""推车截图, 给远程的 Claude 用: 推车锁屏了也看得见被测机器的画面.

    sudo python3 /opt/pxe/cart_snap.py [-d /dev/video0] [-o 输出.png]

- kiosk 开着预览: 让 mpv 经 IPC 存当前视频帧 (取的是解码后的帧, 不是屏幕, 锁屏盖住也照拿),
  等到 mpv 对这条命令的回包 (request_id 对上) 才算数;
- 没开预览: 采集卡空着, ffmpeg 直接抓一帧. 进程间互斥 + 冷却 (#49): 同时只一个 ffmpeg, 上次抓完
  FFMPEG_COOLDOWN_S 秒内不再开卡; 动手档等锁 / 等冷却 (最多 SNAP_WAIT_S), 只读档不等直接拒,
  而且只读档不管走哪条路 (mpv / ffmpeg, 截图 / OCR) 都按 READONLY_COOLDOWN_S 限速, 免得只读 Agent 把卡和 CPU 占住;
- 要 root (采集卡是 root:video, mpv 的 socket 属 root). 不给 -o 时输出到 /run/ops-cart 下
  新建的私有文件 (mkstemp, 0600), 不写 /tmp 里的固定文件名 (root 写固定名会被符号链接坑).
成功时打印图片路径, 退出码 0; 抓图失败 1; 用法/设备状态问题 2.
"""

import argparse
import contextlib
import fcntl
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
SNAP_LOCK = "/run/ops-cart/snap.lock"   # ffmpeg 抓帧的进程间锁, 内容是上次抓完的时刻
FFMPEG_COOLDOWN_S = 3                   # 动手档: ffmpeg 两次抓帧之间
READONLY_COOLDOWN_S = 10                # 只读档: 任何截图 / OCR 之间 (比动手档长, 让着人盯的会话)
SNAP_WAIT_S = 25                        # 动手档等锁 / 等冷却最多多久 (> ffmpeg 超时)


class SnapBusy(Exception):
    """采集卡正被别的 ffmpeg 抓, 或刚抓过还在冷却."""


def mpv_request(path, rid=1, legacy=False):
    """命名参数形式才能带前缀: no-osd = 截图时预览画面上不闪提示字. legacy = 旧的数组形式 (老 mpv 不认命名参数时退回,
    代价是预览上可能闪一下提示字)."""
    if legacy:
        return json.dumps({"command": ["screenshot-to-file", path, "video"], "request_id": rid}) + "\n"
    return json.dumps({"command": {"name": "screenshot-to-file", "filename": path, "flags": "video",
                                   "_flags": ["no-osd"]}, "request_id": rid}) + "\n"


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


def route(want, mpv_dev, sock, default=DEFAULT_DEV):
    """-> (怎么抓, 哪块卡). want=None 表示"现在预览的那块, 没预览就 default"."""
    dev = want or mpv_dev or default
    if mpv_dev and dev == mpv_dev:
        return ("mpv" if sock else "busy"), dev
    return "ffmpeg", dev


def running_mpv_device(proc_root="/proc"):
    """正在预览的卡 = 最晚起的那个 mpv; 残留的旧 mpv (#39) 不算."""
    best = None
    for pid in filter(str.isdigit, os.listdir(proc_root)):
        try:
            with open(f"{proc_root}/{pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").split("\0")
            if not argv or os.path.basename(argv[0]) != "mpv":
                continue
            dev = mpv_device(argv)
            with open(f"{proc_root}/{pid}/stat") as f:
                start = int(f.read().rsplit(")", 1)[1].split()[19])      # starttime, 进程名里可能带括号
        except (OSError, ValueError, IndexError):
            continue
        if dev and (best is None or start > best[0]):
            best = (start, dev)
    return best[1] if best else None


def default_device():
    """没预览也没指定: 有信号的采集卡 (同车机默认画面源), 节点号会变, 不写死 video0 (#39). 探测不了才退回默认."""
    try:
        import kiosk_core as kc
        return kc.default_video(kc.video_nodes(), kc.video_signal) or DEFAULT_DEV
    except Exception:                   # noqa: BLE001  截图不能因为探测挂掉
        return DEFAULT_DEV


def _connect_mpv():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(MPV_SOCK)
    return s


def via_mpv(out, timeout=5.0, connect=_connect_mpv):
    """先用命名参数 + no-osd; mpv 明确回了错误 (老版本不认) 再用旧的数组形式试一次. 超时 / 连不上不重试."""
    err = _mpv_shot(out, timeout, connect, legacy=False)
    if err not in (None, "success"):
        err = _mpv_shot(out, timeout, connect, legacy=True)
    return err == "success" and os.path.getsize(out) > 0


def _mpv_shot(out, timeout, connect, legacy):
    """发截图命令, 等 request_id 对得上的回包 -> mpv 的 error 字段 ("success" 时文件已经写完); 没回包 None.
    中间夹着的事件行 (property-change 之类) 和别人的回包都跳过."""
    rid = int(time.time() * 1000) & 0x7FFFFFFF
    deadline = time.monotonic() + timeout
    try:
        with connect() as s:
            s.settimeout(timeout)
            s.sendall(mpv_request(out, rid, legacy).encode())
            buf = b""
            while time.monotonic() < deadline:
                s.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = s.recv(4096)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("request_id") == rid:
                        return str(msg.get("error"))
    except (OSError, socket.timeout):
        return None
    return None


@contextlib.contextmanager
def ffmpeg_slot(path=None, cooldown=FFMPEG_COOLDOWN_S, clock=time.time, wait_s=0, sleep=time.sleep):
    """拿到才开卡. wait_s=0 (只读档): 别人在抓 / 冷却中直接抛 SnapBusy; wait_s>0 (动手档): 等锁、等冷却,
    超过 wait_s 才抛. 锁文件开不了 (不在推车上, 没有 /run/ops-cart) 就不限."""
    try:
        fd = os.open(path or SNAP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SnapBusy("别的进程正在抓这块卡, 过几秒再试") from None
                sleep(0.1)
        try:
            last = float(os.pread(fd, 64, 0).decode() or 0)
        except (OSError, ValueError):
            last = 0.0
        now = clock()
        left = cooldown - (now - last) if last <= now else 0      # 时钟往回拨: 不算冷却
        if left > 0 and left <= deadline - time.monotonic():
            sleep(left)                                         # 动手档: 等冷却过去
        elif left > 0:
            raise SnapBusy(f"采集卡刚抓过, {left:.0f} 秒后再试 (screen_ocr 也走这里)")
        try:
            yield
        finally:
            stamp = repr(clock()).encode()
            os.ftruncate(fd, 0)
            os.pwrite(fd, stamp, 0)
    finally:
        os.close(fd)


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


BUSY_TEXT = "被预览占着, 但这个 mpv 没开 IPC (kiosk 还是旧版?)"


def _slot_for(how, readonly):
    if readonly:                                # 只读档: 哪条路都限速, 不等
        return ffmpeg_slot(cooldown=READONLY_COOLDOWN_S)
    if how == "ffmpeg":                         # 动手档: 只有 ffmpeg 占卡, 等一会儿
        return ffmpeg_slot(cooldown=FFMPEG_COOLDOWN_S, wait_s=SNAP_WAIT_S)
    return contextlib.nullcontext()


def grab(want=None, out=None, readonly=False):
    """抓一帧 -> (图片路径 或 None, 失败原因, 哪块卡). 不往 stdout 打字 (cart_mcp 的 stdio 是协议通道).
    out=None 时在 /run/ops-cart 下新建私有文件, 失败时删掉. readonly: 只读档的限速 (见模块说明);
    只读档先拿锁、过冷却, 再探设备 (#60: 冷却期内不看 /proc、不起探测子进程)."""
    if not readonly:
        return _grab(want, out, lambda how: _slot_for(how, False))
    try:
        with _slot_for(None, True):
            return _grab(want, out, lambda how: contextlib.nullcontext())
    except SnapBusy as e:
        dev = want or DEFAULT_DEV
        return None, f"截图失败 ({dev}): {e}", dev


def _grab(want, out, slot):
    mpv_dev = running_mpv_device()
    how, dev = route(want, mpv_dev, os.path.exists(MPV_SOCK), default_device() if not (want or mpv_dev) else DEFAULT_DEV)
    if how == "busy":
        return None, f"{dev} {BUSY_TEXT}", dev
    path = None
    try:
        with slot(how):                         # 拿到才建临时文件: 被拒时什么都不写
            path = out or default_out(RUN_DIR)
            if how == "mpv":
                ok, why = via_mpv(path), "mpv 没回成功 (预览刚关? 再试一次)"
            else:
                ok, why = via_ffmpeg(dev, path)
    except SnapBusy as e:
        ok, why = False, str(e)
    if ok:
        return path, "", dev
    if not out and path:                        # 自己建的空文件别留着
        try:
            os.unlink(path)
        except OSError:
            pass
    return None, f"截图失败 ({how} {dev}): {why}", dev


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-d", "--dev", help="采集设备, 默认: 正在预览的那块, 没预览就 /dev/video0")
    ap.add_argument("-o", "--out", help="输出文件, 默认在 /run/ops-cart 下新建私有文件")
    a = ap.parse_args(argv)
    path, why, _dev = grab(a.dev, a.out)
    if path is None:
        print(why, file=sys.stderr)
        return 2 if BUSY_TEXT in why else 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
