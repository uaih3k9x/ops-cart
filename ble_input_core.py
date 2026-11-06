"""ble_input_core: 手机经蓝牙 BLE 给推车"隔空打字"的纯逻辑 (#52). 不碰文件、不碰 D-Bus.

协议见 docs/BLE_INPUT.md, 两侧共用的测试向量在 docs/ble_input_vectors.json.

规矩:
- 蓝牙通道只是"打字": 收到的是文字数据, 不是命令. 这里只重组、校验、去重, 交给投递目标 (Sink), 不执行任何东西.
- 只认已配对、已在车机上 trust、没被 block 的设备 (device_trusted); 不认的设备写进来直接回 unpaired, 不留半包.
- 一条消息最多 MAX_LEN 字节 UTF-8; 半包 PARTIAL_TTL_S 没新分片就丢; 每台设备同时最多 MAX_PARTIAL 个半包.
- 已投递过的 (来源, 序号) 在 DEDUP_TTL_S 内再来只回 ack dup, 不重复投递 (断线重连后手机会重发).
- 正文只能是可见文字加换行 / 制表: 控制字符、ESC 序列、双向文字覆盖符一律拒 (ctrl).
- 审计 (audit) 只写来源设备、序号、长度、结果, 不写正文.
"""

import collections
import json
import unicodedata
import zlib

import cartlog

SERVICE_UUID = "c7fa50cf-d4f6-4e23-a71d-7b725c250660"
RX_UUID = "d0b9f7e0-8914-4d78-9501-69062f225d38"        # 手机写 (write / write without response)
TX_UUID = "a1e1ae6b-abf5-4e3d-9e69-19bafa051e86"        # 推车 notify: 回执 / 错误 / 回复摘要

T_START, T_CONT = 0x01, 0x02
START_HDR = 9               # 类型 1 + 序号 2 + 总长 2 + CRC32 4
CONT_HDR = 5                # 类型 1 + 序号 2 + 偏移 2
ATT_OVERHEAD = 3            # 一次 ATT 写 / 通知能带 MTU-3 字节
MAX_LEN = 4096
PARTIAL_TTL_S = 10
MAX_PARTIAL = 2
DEDUP_KEEP = 64
DEDUP_TTL_S = 600
REPLY_MAX = 512             # 回复摘要最多多少字节正文

ALLOWED_CTRL = "\n\t"
BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


class Bad(Exception):
    """帧或正文不合格. code: frame / len / overlap / crc / utf8 / ctrl / empty."""

    def __init__(self, code, msg=""):
        super().__init__(msg or code)
        self.code = code


class Event:
    """重组的结果. kind: text (投递 text) / dup (已投递过) / err (code 说明)."""

    __slots__ = ("kind", "src", "msg_id", "text", "size", "code")

    def __init__(self, kind, src, msg_id, text=None, size=None, code=None):
        self.kind, self.src, self.msg_id = kind, src, msg_id
        self.text, self.size, self.code = text, size, code

    def __repr__(self):
        return f"Event({self.kind}, {self.src}, {self.msg_id}, size={self.size}, code={self.code})"


# ------------------------------------------------------------ 帧

def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def encode(msg_id, payload, mtu):
    """一条消息 -> 帧列表 (每帧不超过 mtu-3 字节). 首帧带总长和 CRC32, 后续帧带偏移."""
    if not 0 <= msg_id <= 0xFFFF:
        raise ValueError("序号是 16 位")
    if not 0 < len(payload) <= MAX_LEN:
        raise ValueError(f"消息长度要在 1..{MAX_LEN} 字节")
    room = mtu - ATT_OVERHEAD
    if room - START_HDR < 1:
        raise ValueError(f"MTU {mtu} 太小")
    mid = msg_id.to_bytes(2, "big")
    n = room - START_HDR
    frames = [bytes([T_START]) + mid + len(payload).to_bytes(2, "big") + crc32(payload).to_bytes(4, "big")
              + payload[:n]]
    off = n
    while off < len(payload):
        chunk = payload[off:off + room - CONT_HDR]
        frames.append(bytes([T_CONT]) + mid + off.to_bytes(2, "big") + chunk)
        off += len(chunk)
    return frames


def decode_frame(frame):
    """('start', 序号, 总长, crc, 正文片) 或 ('cont', 序号, 偏移, 正文片). 不合格抛 Bad('frame')."""
    frame = bytes(frame)
    if len(frame) > START_HDR and frame[0] == T_START:
        return ("start", int.from_bytes(frame[1:3], "big"), int.from_bytes(frame[3:5], "big"),
                int.from_bytes(frame[5:9], "big"), frame[9:])
    if len(frame) > CONT_HDR and frame[0] == T_CONT:
        return ("cont", int.from_bytes(frame[1:3], "big"), int.from_bytes(frame[3:5], "big"), None, frame[5:])
    raise Bad("frame", "帧太短或类型不认识")


def peek_id(frame):
    """尽量从帧里读出序号 (回错误时带上); 读不出是 None."""
    return int.from_bytes(frame[1:3], "big") if len(frame) >= 3 and frame[0] in (T_START, T_CONT) else None


def check_text(data):
    """字节 -> 文字. 只收可见文字加换行 / 制表."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Bad("utf8", "不是 UTF-8") from None
    if not text:
        raise Bad("empty")
    for ch in text:
        if ch in ALLOWED_CTRL:
            continue
        if unicodedata.category(ch) == "Cc" or ord(ch) in BIDI:
            raise Bad("ctrl", f"含控制字符 U+{ord(ch):04X}")
    return text


# ------------------------------------------------------------ 设备

def device_trusted(props):
    """BlueZ org.bluez.Device1 属性 -> 收不收它写的东西. 要 Paired + Trusted、没 Blocked;
    有 Bonded 属性 (新 BlueZ) 时还要 Bonded (存了长期密钥, 不是一次性配对)."""
    if not props:
        return False
    if props.get("Blocked") is True or props.get("Paired") is not True or props.get("Trusted") is not True:
        return False
    return props.get("Bonded", True) is True


def may_broadcast(devices):
    """notify 发给所有订阅者, 分不了设备: 只在连着的设备全是受信设备时才发回复正文."""
    on = [d for d in devices if d.get("Connected") is True]
    return bool(on) and all(device_trusted(d) for d in on)


# ------------------------------------------------------------ 重组

class _Partial:
    __slots__ = ("total", "crc", "pieces", "have", "last")

    def __init__(self, total, crc, now):
        self.total, self.crc, self.pieces, self.have, self.last = total, crc, {}, 0, now

    def add(self, off, chunk):
        """放一片; 完全一样的重复片忽略, 越界抛 len, 和已有片交叠抛 overlap."""
        end = off + len(chunk)
        if end > self.total:
            raise Bad("len", "分片越过总长")
        if self.pieces.get(off) == chunk:
            return
        for o, c in self.pieces.items():
            if off < o + len(c) and o < end:
                raise Bad("overlap", "分片交叠且内容不同")
        self.pieces[off] = chunk
        self.have += len(chunk)

    def data(self):
        return b"".join(self.pieces[o] for o in sorted(self.pieces))


class Receiver:
    """按来源设备重组. feed / expire 返回 Event 列表, 调用方据此投递、回执、审计."""

    def __init__(self):
        self.partial = {}                       # (src, id) -> _Partial, 按到达顺序
        self.done = collections.OrderedDict()   # (src, id) -> 投递时间

    def expire(self, now):
        out = []
        for key in [k for k, p in self.partial.items() if now - p.last > PARTIAL_TTL_S]:
            del self.partial[key]
            out.append(Event("err", key[0], key[1], code="timeout"))
        for key in [k for k, t in self.done.items() if now - t > DEDUP_TTL_S]:
            del self.done[key]
        return out

    def feed(self, src, frame, now, trusted):
        out = self.expire(now)
        frame = bytes(frame)
        if not trusted:
            return out + [Event("err", src, peek_id(frame), code="unpaired")]
        try:
            kind, mid, a, crc, chunk = decode_frame(frame)
        except Bad as e:
            return out + [Event("err", src, peek_id(frame), code=e.code)]
        key = (src, mid)
        if kind == "start":
            if key in self.done:
                return out + [Event("dup", src, mid)]
            self.partial.pop(key, None)         # 同一序号重新开头 = 手机重发, 按新的来
            if not 0 < a <= MAX_LEN or len(chunk) > a:
                return out + [Event("err", src, mid, code="len")]
            mine = [k for k in self.partial if k[0] == src]
            while len(mine) >= MAX_PARTIAL:
                old = mine.pop(0)
                del self.partial[old]
                out.append(Event("err", src, old[1], code="busy"))
            p = self.partial[key] = _Partial(a, crc, now)
            off = 0
        else:
            p = self.partial.get(key)
            if p is None:
                if key in self.done:
                    return out                  # 已投递消息的迟到分片, 安静丢掉
                return out + [Event("err", src, mid, code="nostart")]
            off = a
        try:
            p.add(off, chunk)
        except Bad as e:
            del self.partial[key]
            return out + [Event("err", src, mid, code=e.code)]
        p.last = now
        if p.have < p.total:
            return out
        del self.partial[key]
        data = p.data()
        if crc32(data) != p.crc:
            return out + [Event("err", src, mid, code="crc")]
        try:
            text = check_text(data)
        except Bad as e:
            return out + [Event("err", src, mid, code=e.code)]
        self.done[key] = now
        while len(self.done) > DEDUP_KEEP:
            self.done.popitem(last=False)
        return out + [Event("text", src, mid, text=text, size=len(data))]

    def forget(self, src, msg_id):
        """投递失败 (例如队列满) 时调用: 手机重发同一条要能再收."""
        self.done.pop((src, msg_id), None)


class Sender:
    """推车 -> 手机的 notify 帧: 和手机写过来的同一种分包, 序号自己递增."""

    def __init__(self, next_id=0):
        self.next_id = next_id & 0xFFFF

    def frames(self, payload, mtu):
        mid, self.next_id = self.next_id, (self.next_id + 1) & 0xFFFF
        return encode(mid, payload, mtu)


# ------------------------------------------------------------ 回执 / 回复 / 审计

def _j(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def ack(msg_id, dup=False):
    return _j({"t": "ack", "id": msg_id, "dup": True} if dup else {"t": "ack", "id": msg_id})


def err(msg_id, code):
    return _j({"t": "err", "id": msg_id, "code": code})


def reply(text):
    """大肥鱼回复的摘要: 超过 REPLY_MAX 字节在字符边界截断, 加省略号."""
    raw = text.encode("utf-8")
    if len(raw) > REPLY_MAX:
        cut = REPLY_MAX - len("…".encode())
        text = raw[:cut].decode("utf-8", "ignore") + "…"
    return _j({"t": "reply", "text": text})


def status_for(ev):
    if ev.kind == "text":
        return ack(ev.msg_id)
    if ev.kind == "dup":
        return ack(ev.msg_id, dup=True)
    return err(ev.msg_id, ev.code)


def audit(ev, result=None):
    """审计行正文 (交给 cartlog.event): 只有来源设备、序号、长度、结果. 不写正文."""
    return cartlog.fields(dev=ev.src, id=ev.msg_id, len=ev.size, kind=ev.kind, code=ev.code, result=result)
