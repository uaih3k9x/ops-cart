"""测试用的假 CS80: 按协议文档的行为应答主机帧. 代替串口 (write / read), 时间走假时钟.

只模拟 cart-dash 用到的部分: HELLO / SEG / RECT / PRESENT / BACKLIGHT / LED / SLEEP / PING, 主动发 KEY.
画面只记"最新提交"的一块 RGB565 缓冲, 够对账.
"""

import struct

import cs80_proto as p


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeDevice:
    def __init__(self, clock=None, caps=0xB3, max_payload=32768, width=800, height=1280, pixfmt=1,
                 seg_bytes=72, proto_ver=0):
        self.clock = clock or Clock()
        self.hello = dict(proto_ver=proto_ver, fw_ver=10, width=width, height=height, pixfmt=pixfmt,
                          seg_bytes=seg_bytes, caps=caps, max_payload=max_payload)
        self.parser = p.Parser(max_len=65535)
        self.out = bytearray()
        self.frames = []                    # 收到的合法主机帧 (type, seq, payload)
        self.fb = bytearray(width * height * 2)
        self.status = {}                    # type -> 强制回的 status
        self.silent = set()                 # 这些 type 不应答 (模拟坏帧 / 掉线)
        self.reject_rects = 0               # 接下来几条 RECT 当 bad_param
        self.closed = False
        self.unplugged = False
        self.backlight = 1                  # 跟真固件一样: HELLO 不动背光
        self.reset()
        self.writes = 0

    def reset(self):
        self.cnt = dict(rx_bytes=0, frames=0, crc_err=0, len_err=0, rects=0, presents=0, bad_param=0)

    # ---- 主机那一侧的接口 (同 cs80_link.Port)
    def write(self, data, timeout=1.0):
        if self.unplugged:
            raise OSError("unplugged")
        self.writes += 1
        self.cnt["rx_bytes"] += len(data)
        for t, seq, pl in self.parser.feed(bytes(data)):
            self.frames.append((t, seq, pl))
            self.cnt["frames"] += 1
            self._on(t, seq, pl)

    def read(self, timeout):
        if self.unplugged:
            raise OSError("unplugged")
        if not self.out:
            self.clock.advance(timeout)
            return b""
        data, self.out = bytes(self.out), bytearray()
        return data

    def flush(self):
        self.out = bytearray()

    def close(self):
        self.closed = True

    # ---- 设备行为
    def press(self, key, ev=p.EV_UP, n=0):
        self.out += p.encode(p.KEY, 0, struct.pack("<BBHI", key, ev, n, 0))

    def _ack(self, t, seq, status=0, extra=0):
        c = self.cnt
        pl = struct.pack("<BBH9I", t, self.status.get(t, status), 0, c["rx_bytes"], c["frames"], c["crc_err"],
                         c["len_err"], c["rects"], c["presents"], c["bad_param"], 0, extra)
        self.out += p.encode(p.ACK, seq, pl)

    def _on(self, t, seq, pl):
        if t in self.silent:
            return
        if t == p.HELLO:
            self.reset()
            h = self.hello
            self.out += p.encode(p.HELLO_ACK, seq, struct.pack(
                "<HHHHBBHHH", h["proto_ver"], h["fw_ver"], h["width"], h["height"], h["pixfmt"],
                h["seg_bytes"], h["caps"], h["max_payload"], 0))
        elif t == p.RECT:
            x, y, w, h = struct.unpack_from("<4H", pl) if len(pl) >= 8 else (0, 0, 1, 1)
            if self.reject_rects or len(pl) != 8 + w * h * 2 or x + w > 800 or y + h > 1280:
                self.reject_rects = max(0, self.reject_rects - 1)
                self.cnt["bad_param"] += 1
                return
            for row in range(h):
                off = ((y + row) * 800 + x) * 2
                self.fb[off:off + w * 2] = pl[8 + row * w * 2:8 + (row + 1) * w * 2]
            self.cnt["rects"] += 1
        elif t == p.PRESENT:
            self.cnt["presents"] += 1
            self._ack(t, seq)
        elif t == p.BACKLIGHT:
            self.backlight = pl[0] if pl else 1
            self._ack(t, seq)
        elif t == p.LED and not self.hello["caps"] & p.CAP_LED:
            self._ack(t, seq, status=3)
        else:
            self._ack(t, seq)

    # ---- 断言用
    def types(self):
        return [t for t, _s, _p in self.frames]

    def last(self, t):
        return next((pl for tt, _s, pl in reversed(self.frames) if tt == t), None)
