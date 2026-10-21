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
  GET_INFO=0x01, SEND_DATA=0x02 (键盘8字节报告), ACK: CMD|0xC0
"""

import argparse
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

# HID modifier 位
MOD = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08}

# HID usage ID 表 (US 布局); shift=需按 shift
KEYS = {
    "enter": (0x28, 0), "esc": (0x29, 0), "backspace": (0x2A, 0), "tab": (0x2B, 0),
    "space": (0x2C, 0), "delete": (0x4C, 0), "insert": (0x49, 0),
    "up": (0x52, 0), "down": (0x51, 0), "left": (0x50, 0), "right": (0x4F, 0),
    "home": (0x4A, 0), "end": (0x4D, 0), "pgup": (0x4B, 0), "pgdn": (0x4E, 0),
    "capslock": (0x39, 0), "win": (0x65, 0), "menu": (0x65, 0),
    "f1": (0x3A, 0), "f2": (0x3B, 0), "f3": (0x3C, 0), "f4": (0x3D, 0),
    "f5": (0x3E, 0), "f6": (0x3F, 0), "f7": (0x40, 0), "f8": (0x41, 0),
    "f9": (0x42, 0), "f10": (0x43, 0), "f11": (0x44, 0), "f12": (0x45, 0),
}
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


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(cmd: int, data: bytes = b"", addr: int = 0) -> bytes:
    payload = bytes([addr, cmd, len(data)]) + data
    return HEADER + payload + bytes([checksum(HEADER + payload)])


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
    def __init__(self, port, baud=115200, log=print):
        self.log = log
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        speed = {9600: termios.B9600, 57600: termios.B57600,
                 115200: termios.B115200, 921600: termios.B921600}.get(baud)
        if speed is None:
            raise ValueError(f"不支持的波特率 {baud}")
        a = termios.tcgetattr(self.fd)
        a[0] = a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = a[5] = speed
        termios.tcsetattr(self.fd, termios.TCSANOW, a)

    def close(self):
        os.close(self.fd)

    def _send_raw(self, frame):
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        os.write(self.fd, frame)
        r, _, _ = select.select([self.fd], [], [], 0.4)
        if r:
            try:
                return os.read(self.fd, 64)
            except OSError:
                return b""
        return b""

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
        """逐字符打字 (US 键盘布局)."""
        for ch in text:
            if ch == "\n":
                self.send_keys(["enter"], press_ms)
                continue
            if ch not in KEYS:
                raise ValueError(f"无法输入的字符: {ch!r}")
            self.send_keys([ch], press_ms)


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
