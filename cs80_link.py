#!/usr/bin/env python3
"""cs80_link: Monita-CS80 的字节流传输 + 会话. 只用标准库 (不加 pyserial), Python 3.9 可用.

Port: 打开一个串口节点 (Linux 上的 ttyUSB / ttyACM, 或 fwsim 的 PTY), 设成 raw, select 带超时读写.
Link: 帧序号、按 type+seq 等应答、等应答时把 KEY 分出来、超时抛 OSError (上层当"没插"处理).
"""

import fcntl
import os
import select
import termios
import time

import cs80_proto as p

WRITE_TIMEOUT_S = 1.0
ACK_TIMEOUT_S = 0.5


def raw_attrs(attrs):
    """tcgetattr 列表 -> 完整 cfmakeraw 的标志. 3.9 没有 tty.cfmakeraw; tty.setraw 在 3.9 上不清 INLCR / IGNCR,
    会把 0x0A / 0x0D 改掉. 不设 raw 时 ttyUSB 默认回显, 设备回包会被原样回给设备."""
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    t = termios
    iflag &= ~(t.IGNBRK | t.BRKINT | t.PARMRK | t.ISTRIP | t.INLCR | t.IGNCR | t.ICRNL | t.IXON)
    oflag &= ~t.OPOST
    lflag &= ~(t.ECHO | t.ECHONL | t.ICANON | t.ISIG | t.IEXTEN)
    cflag &= ~(t.CSIZE | t.PARENB)
    cflag |= t.CS8 | t.CLOCAL | t.CREAD
    cc = list(cc)
    cc[t.VMIN], cc[t.VTIME] = 0, 0
    return [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]


class Port:
    """一个已打开的串口节点. O_NOCTTY: root 跑也不变成控制终端; O_NONBLOCK + CLOCAL: 没载波不卡 open.
    不设波特率 (USB 虚拟串口), 不动 DTR. TIOCEXCL: 开着时别的非 root 进程再开同一个节点直接 EBUSY,
    不会两边悄悄抢字节 (root / ModemManager 绕得过, 不能代替 udev 规则)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            termios.tcsetattr(self.fd, termios.TCSANOW, raw_attrs(termios.tcgetattr(self.fd)))
            self.flush()
        except Exception:
            os.close(self.fd)
            raise

    def flush(self):
        """丢掉上一次会话残留的字节和迟到的 ACK."""
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, data, timeout=WRITE_TIMEOUT_S):
        """整帧写完才返回 (设备半截帧 0.5 s 没后续就丢); 超时抛 OSError."""
        view = memoryview(data)
        end = time.monotonic() + timeout
        while view:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("写 %s 超时" % self.path)
            _r, w, _x = select.select([], [self.fd], [], left)
            if not w:
                continue
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                continue
            view = view[n:]

    def read(self, timeout):
        """最多等 timeout 秒, 返回读到的字节 (可能为空)."""
        r, _w, _x = select.select([self.fd], [], [], max(0.0, timeout))
        if not r:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        if not data:
            raise OSError("%s 已断开" % self.path)
        return data

    def close(self):
        if self.fd is not None:
            try:                            # TIOCEXCL 挂在 tty 上不跟 fd 走: 不撤的话 tty 还活着 (PTY) 时自己重连也 EBUSY
                fcntl.ioctl(self.fd, termios.TIOCNXCL)
            except OSError:
                pass                        # 拔掉了: 节点已经没了
            os.close(self.fd)
            self.fd = None


class Link:
    """会话: 帧序号、等应答、KEY 分流. port 只要有 write(bytes) / read(timeout) / close()."""

    def __init__(self, port, clock=time.monotonic):
        self.port, self.clock = port, clock
        self.parser = p.Parser()
        self.seq = 0
        self.keys = []
        self.last_tx = None

    def _next_seq(self):
        self.seq = (self.seq + 1) & 0xFF
        return self.seq

    def send(self, ptype, payload=b""):
        """只写不等 (RECT 没有应答). 返回用的 seq."""
        seq = self._next_seq()
        self.port.write(p.encode(ptype, seq, payload))
        self.last_tx = self.clock()
        return seq

    def request(self, ptype, payload=b"", timeout=ACK_TIMEOUT_S):
        """发一条有应答的消息, 等对应的 HELLO_ACK / ACK. 返回解出来的 dict; 超时抛 OSError."""
        seq = self.send(ptype, payload)
        want = p.HELLO_ACK if ptype == p.HELLO else p.ACK
        end = self.clock() + timeout
        while True:
            for t, s, pl in self._frames(max(0.0, end - self.clock())):
                if t == want and s == seq:
                    if want == p.HELLO_ACK:
                        return p.parse_hello_ack(pl)
                    a = p.parse_ack(pl)
                    if a["for_type"] == ptype:
                        return a
            if self.clock() >= end:
                raise TimeoutError("等 0x%02x 的应答超时" % ptype)

    def _frames(self, timeout):
        out = []
        for t, s, pl in self.parser.feed(self.port.read(timeout)):
            if t == p.KEY:
                self.keys.append(p.parse_key(pl))
            else:
                out.append((t, s, pl))
        return out

    def poll(self):
        """不等: 把已经到了的字节读掉 (KEY 收进队列, 迟到的应答丢掉)."""
        self._frames(0.0)

    def take_keys(self):
        keys, self.keys = self.keys, []
        return keys

    def hello(self, unix_time, tz_min):
        self.parser = p.Parser()
        return self.request(p.HELLO, p.hello_payload(unix_time, tz_min), timeout=1.0)

    def close(self):
        self.port.close()
