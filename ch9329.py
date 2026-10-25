#!/usr/bin/env python3
"""ch9329-tool: 通过串口驱动 CH9329, 让它对目标机器当 USB 键盘.

用法 (在接了 CH9329 的 Linux 主机上):
  python3 ch9329.py --port /dev/ttyACM0 info
  python3 ch9329.py --port /dev/ttyACM0 type "hello world"
  python3 ch9329.py --port /dev/ttyACM0 key enter
  python3 ch9329.py --port /dev/ttyACM0 combo ctrl alt del
  python3 ch9329.py --port /dev/ttyACM0 hold 2000 a   # 按住 a 键 2000ms

协议 (WCH CH9329): 帧 = 57 AB ADDR CMD LEN DATA... SUM
  SUM = 整帧(含 57 AB)求和 & 0xFF  (本机实测如此)
  GET_INFO=0x01, SEND_DATA=0x02 (键盘8字节报告); 应答 CMD|0x80 = 正常, CMD|0xC0 = 出错

串口独占用 flock (和 pyserial exclusive=True 同一套), 车机和网页口不会同时抢一块;
拔线 / 打不开统一抛 OSError 的子类, 调用方只捕获 OSError 就够.
"""

import argparse
import fcntl
import os
import select
import sys
import termios
import time

HEADER = b"\x57\xab"
CMD_GET_INFO = 0x01
CMD_SEND_DATA = 0x02      # 普通键盘数据, 8 字节
CMD_SEND_MS_ABS = 0x04    # 绝对鼠标, 7 字节: 0x02, 按键, xL, xH, yL, yH, 滚轮 (0~4095)
CMD_SEND_MS_REL = 0x05    # 相对鼠标, 5 字节: 0x01, 按键, dx, dy, 滚轮

ACK_TIMEOUT = 0.05        # 等应答的上限; 115200 下应答 1ms 内就回, 等不到也不该卡住键鼠

# HID modifier 位 (Win / Super 键是修饰位 gui, 不是普通键)
MOD = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08, "win": 0x08, "super": 0x08}

# HID usage ID 表 (US 布局); shift=需按 shift
KEYS = {
    "enter": (0x28, 0), "esc": (0x29, 0), "backspace": (0x2A, 0), "tab": (0x2B, 0),
    "space": (0x2C, 0), "delete": (0x4C, 0), "insert": (0x49, 0),
    "up": (0x52, 0), "down": (0x51, 0), "left": (0x50, 0), "right": (0x4F, 0),
    "home": (0x4A, 0), "end": (0x4D, 0), "pgup": (0x4B, 0), "pgdn": (0x4E, 0),
    "capslock": (0x39, 0), "menu": (0x65, 0),
    "printscreen": (0x46, 0), "scrolllock": (0x47, 0), "pause": (0x48, 0), "numlock": (0x53, 0),
}
for _i in range(12):
    KEYS[f"f{_i + 1}"] = (0x3A + _i, 0)         # F1-F12
    KEYS[f"f{_i + 13}"] = (0x68 + _i, 0)        # F13-F24
KEYS["del"] = KEYS["delete"]
for _i, _c in enumerate("1234567890"):
    KEYS[_c] = (0x1E + _i, 0)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYS[_c] = (0x04 + _i, 0)
    KEYS[_c.upper()] = (0x04 + _i, 1)  # 大写 = shift+键
SYMBOLS = {
    "`": (0x35, 0), "~": (0x35, 1), "-": (0x2D, 0), "_": (0x2D, 1),
    "=": (0x2E, 0), "+": (0x2E, 1), "[": (0x2F, 0), "{": (0x2F, 1),
    "]": (0x30, 0), "}": (0x30, 1), "\\": (0x31, 0), "|": (0x31, 1),
    ";": (0x33, 0), ":": (0x33, 1), "'": (0x34, 0), '"': (0x34, 1),
    ",": (0x36, 0), "<": (0x36, 1), ".": (0x37, 0), ">": (0x37, 1),
    "/": (0x38, 0), "?": (0x38, 1), "!": (0x1E, 1), "@": (0x1F, 1),
    "#": (0x20, 1), "$": (0x21, 1), "%": (0x22, 1), "^": (0x23, 1),
    "&": (0x24, 1), "*": (0x25, 1), "(": (0x26, 1), ")": (0x27, 1),
}
KEYS.update(SYMBOLS)
KEYS[" "] = (0x2C, 0)   # 空格
KEYS["\t"] = KEYS["tab"]


class Ch9329Gone(OSError):
    """串口没了 (拔线 / 设备节点消失 / termios 出错). 句柄作废, 重开才行."""


class Ch9329Busy(OSError):
    """别的进程 (车机 / 网页口 / conbroker) 正占着这块键鼠."""


def baud_const(baud):
    """波特率 -> termios 常量; 按需取, 平台没有的 (macOS 没 B921600) 返回 None."""
    return getattr(termios, f"B{int(baud)}", None)


MOUSE_MAX = 4096            # 一次相对位移上限 (像素): 再大也只是甩到屏幕边


def mouse_chunks(dx, dy, wheel, limit=MOUSE_MAX):
    """相对位移拆成多帧 int8 (先夹到 ±limit); 滚轮夹到 ±1, 只跟第一帧走. 车机和网页口共用."""
    dx, dy = (max(-limit, min(limit, int(v))) for v in (dx, dy))
    wheel = max(-1, min(1, int(wheel)))
    out = []
    while True:
        sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
        out.append((sx, sy, wheel))
        dx, dy, wheel = dx - sx, dy - sy, 0
        if not (dx or dy):
            return out


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(cmd: int, data: bytes = b"", addr: int = 0) -> bytes:
    payload = bytes([addr, cmd, len(data)]) + data
    return HEADER + payload + bytes([checksum(HEADER + payload)])


def parse_ack(buf):
    """在收到的字节里找第一份完整应答 -> {cmd, ok, data}; 没有 / 校验不对返回 None."""
    i = buf.find(HEADER)
    while i >= 0:
        if len(buf) - i < 6:
            return None
        n = buf[i + 4]
        frame = buf[i:i + 6 + n]
        if len(frame) == 6 + n and checksum(frame[:-1]) == frame[-1] and buf[i + 3] & 0x80:
            return {"cmd": buf[i + 3] & 0x3F, "ok": not buf[i + 3] & 0x40, "data": frame[5:-1]}
        i = buf.find(HEADER, i + 1)
    return None


def untypable(text):
    """type_text 打不出的字符 (去重, 按出现顺序)."""
    out = []
    for c in text:
        if c != "\n" and c not in KEYS and c not in out:
            out.append(c)
    return out


def key_report(key_names, hold_shift_special=None):
    """把 ['ctrl','alt','del'] 或 ['a'] 转成 8 字节键盘报告.
    返回 (report, 未识别键列表)."""
    modifier = 0
    codes = []
    unknown = []
    for k in key_names:
        if k in MOD:
            modifier |= MOD[k]
        elif k in KEYS:
            code, shift = KEYS[k]
            modifier |= 0x02 if shift else 0
            if code not in codes:
                codes.append(code)
        else:
            unknown.append(k)
    if len(codes) > 6:
        raise ValueError("一次最多 6 个普通键")
    report = bytes([modifier, 0]) + bytes(codes) + b"\x00" * (6 - len(codes))
    return report, unknown


class Ch9329:
    """port = 设备路径, 或 fd = 已打开的描述符 (测试用 pty). 关闭时连同独占锁一起放掉."""

    def __init__(self, port=None, baud=115200, log=print, fd=None, ack_timeout=ACK_TIMEOUT):
        self.log, self.ack_timeout = log, ack_timeout
        speed = baud_const(baud)
        if speed is None:
            raise ValueError(f"不支持的波特率 {baud}")
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK) if fd is None else fd
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Ch9329Busy(f"键鼠被占用: {port or self.fd}") from None
            try:
                a = termios.tcgetattr(self.fd)
                a[0] = a[1] = 0
                a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                a[3] = 0
                a[4] = a[5] = speed
                termios.tcsetattr(self.fd, termios.TCSANOW, a)
            except termios.error as e:
                raise Ch9329Gone(f"串口设置失败: {e}") from None
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        try:
            os.close(self.fd)           # 关描述符 = 放掉 flock
        except OSError:
            pass

    def _send_raw(self, frame):
        """写一帧, 最多等 ack_timeout 收应答 (原始字节, 没收到为 b'').
        只清输入: 上一帧迟到的应答别算到这一帧头上; 输出缓冲里没发完的不能丢."""
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
            os.write(self.fd, frame)
            r, _, _ = select.select([self.fd], [], [], self.ack_timeout)
            if not r:
                return b""
            try:
                return os.read(self.fd, 64)
            except BlockingIOError:
                return b""
        except (termios.error, OSError, ValueError) as e:     # ValueError: select 遇到已关闭的 fd
            raise Ch9329Gone(f"串口出错: {e}") from None

    def info(self):
        return self._send_raw(build_frame(CMD_GET_INFO)).hex(" ")

    def send_keys(self, key_names, press_ms=30):
        """按下组合键 -> 等待 -> 全部抬起."""
        report, unknown = key_report(key_names)
        if unknown:
            raise ValueError(f"不认识的键: {unknown}")
        down = self._send_raw(build_frame(CMD_SEND_DATA, report))
        time.sleep(press_ms / 1000.0)
        up = self._send_raw(build_frame(CMD_SEND_DATA, b"\x00" * 8))
        return down, up

    def send_report(self, report):
        """直接发一份 8 字节键盘报告 (按住/松开同步用)."""
        return self._send_raw(build_frame(CMD_SEND_DATA, report))

    def send_mouse(self, buttons=0, dx=0, dy=0, wheel=0):
        """发一帧鼠标位移 (相对位移, int8 两补码). buttons: 左1 右2 中4."""
        def i8(v):
            v = max(-127, min(127, int(v)))
            return v & 0xFF
        report = bytes([0x01, buttons & 0x07, i8(dx), i8(dy), i8(wheel)])
        return self._send_raw(build_frame(CMD_SEND_MS_REL, report))

    def send_mouse_abs(self, buttons=0, x=0, y=0, wheel=0):
        """绝对鼠标: x/y 0~4095 对应目标屏幕左上到右下. BIOS 里多半不认, 那里用相对鼠标."""
        x, y = (max(0, min(4095, int(v))) for v in (x, y))
        w = max(-127, min(127, int(wheel))) & 0xFF
        report = bytes([0x02, buttons & 0x07, x & 0xFF, x >> 8, y & 0xFF, y >> 8, w])
        return self._send_raw(build_frame(CMD_SEND_MS_ABS, report))

    def type_text(self, text, press_ms=30):
        """逐字符打字 (US 键盘布局). 先整串校验: 有打不出的字就一个都不发."""
        bad = untypable(text)
        if bad:
            raise ValueError(f"无法输入的字符: {''.join(bad)!r}")
        for ch in text:
            self.send_keys(["enter"] if ch == "\n" else [ch], press_ms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("type"); p.add_argument("text")
    p = sub.add_parser("key"); p.add_argument("key")
    p = sub.add_parser("combo"); p.add_argument("keys", nargs="+")
    p = sub.add_parser("hold"); p.add_argument("ms", type=int); p.add_argument("key")

    args = ap.parse_args()
    dev = Ch9329(args.port, args.baud)
    try:
        if args.cmd == "info":
            print("CH9329 应答:", dev.info() or "(无)")
        elif args.cmd == "type":
            dev.type_text(args.text)
            print(f"已输入 {len(args.text)} 字符")
        elif args.cmd == "key":
            dev.send_keys([args.key])
            print(f"已按 {args.key}")
        elif args.cmd == "combo":
            dev.send_keys(args.keys)
            print(f"已按组合 {'+'.join(args.keys)}")
        elif args.cmd == "hold":
            dev.send_keys([args.key], press_ms=args.ms)
            print(f"已按住 {args.key} {args.ms}ms")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
