"""ble_input_core: 手机经蓝牙 BLE 给推车"隔空打字"的纯逻辑 (#52). 不碰文件、不碰 D-Bus.

协议见 docs/BLE_INPUT.md, 两侧共用的测试向量在 docs/ble_input_vectors.json.

规矩:
- 蓝牙通道只是"打字": 收到的是文字数据, 不是命令. 这里只重组、校验、去重, 交给投递目标 (Sink), 不执行任何东西.
- 只认已配对、已在车机上 trust、没被 block 的设备 (device_trusted); 不认的设备写进来直接回 unpaired, 不留半包.
- 一条消息最多 MAX_LEN 字节 UTF-8; 半包 PARTIAL_TTL_S 没新分片就丢; 每台设备同时最多 MAX_PARTIAL 个半包.
- 已投递过的 (来源, 序号, 总长, CRC) 在 DEDUP_TTL_S 内再来只回 ack dup, 不重复投递 (断线重连后手机会重发);
  同序号不同内容当新消息. 去重表按来源设备分开计数.
- 回执 / 错误带被确认消息的 (序号, CRC), 每台手机只认自己的.
- 正文只能是可见文字加换行 / 制表: 控制字符、格式字符 (Cf, 只放行 ZWJ)、标签字符、行 / 段分隔符、
  非字符 (U+FDD0..FDEF、每个平面末尾两个)、私用区、看不见的填充字一律拒 (ctrl). 检查在重组之后做.
  未分配 (Cn) 不整类拒: 推车的 Python 认的 Unicode 版本可能比手机老, 新 emoji 在它眼里就是 Cn.
- 文字只进输入框: 换行不得触发提交; 送往 HID 或串口必须再走现有危险操作确认 (见 docs/BLE_INPUT.md).
- 审计 (audit) 只写来源设备、序号、长度、结果, 不写正文.
"""

import collections
import json
import unicodedata
import zlib

import cartlog

SERVICE_UUID = "c7fa50cf-d4f6-4e23-a71d-7b725c250660"
RX_UUID = "d0b9f7e0-8914-4d78-9501-69062f225d38"        # 手机写 (secure-write, 带回应的写)
TX_UUID = "a1e1ae6b-abf5-4e3d-9e69-19bafa051e86"        # 推车 notify: 回执 / 错误 / 回复摘要

T_START, T_CONT = 0x01, 0x02
START_HDR = 9               # 类型 1 + 序号 2 + 总长 2 + CRC32 4
CONT_HDR = 5                # 类型 1 + 序号 2 + 偏移 2
ATT_OVERHEAD = 3            # 一次 ATT 写 / 通知能带 MTU-3 字节
ATT_MAX_ATTR = 512          # ATT 属性值最多 512 字节: 单帧 = min(MTU-3, 512)
MAX_LEN = 4096
PARTIAL_TTL_S = 10
MAX_PARTIAL = 2
DEDUP_KEEP = 64
DEDUP_TTL_S = 600
REPLY_MAX = 512             # 回复摘要最多多少字节正文

ALLOWED_CTRL = "\n\t"
ZWJ = "\u200d"               # emoji 组合要用, Cf 里只放行它
BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}       # 不含 Cn, 见模块说明
TAGS = range(0xE0000, 0xE0080)                     # 标签字符 (有的是 Cf, 有的未分配), 整段拒
INVISIBLE = {0x115F, 0x1160, 0x3164, 0xFFA0}        # 韩文填充字: 字母类但看不见


class Bad(Exception):
    """帧或正文不合格. code: frame / len / overlap / crc / utf8 / ctrl / empty."""

    def __init__(self, code, msg=""):
        super().__init__(msg or code)
        self.code = code


class Event:
    """重组的结果. kind: text (投递 text) / dup (已投递过) / err (code 说明)."""

    __slots__ = ("kind", "src", "msg_id", "text", "size", "code", "crc")

    def __init__(self, kind, src, msg_id, text=None, size=None, code=None, crc=None):
        self.kind, self.src, self.msg_id = kind, src, msg_id
        self.text, self.size, self.code, self.crc = text, size, code, crc

    def __repr__(self):
        return f"Event({self.kind}, {self.src}, {self.msg_id}, size={self.size}, code={self.code})"


# ------------------------------------------------------------ 帧

def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def encode(msg_id, payload, mtu):
    """一条消息 -> 帧列表 (每帧不超过 min(mtu-3, 512) 字节). 首帧带总长和 CRC32, 后续帧带偏移."""
    if not 0 <= msg_id <= 0xFFFF:
        raise ValueError("序号是 16 位")
    if not 0 < len(payload) <= MAX_LEN:
        raise ValueError(f"消息长度要在 1..{MAX_LEN} 字节")
    room = min(mtu - ATT_OVERHEAD, ATT_MAX_ATTR)
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


def peek_crc(frame):
    """START 帧里声明的 CRC (回错误时带上, 手机据此认领); 读不出是 None."""
    return int.from_bytes(frame[5:9], "big") if len(frame) >= START_HDR and frame[0] == T_START else None


def _bad_char(ch):
    if ch in ALLOWED_CTRL or ch == ZWJ:
        return False
    cp = ord(ch)
    return (unicodedata.category(ch) in BAD_CATEGORIES or cp in TAGS or cp in INVISIBLE
            or 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE)


def clean_text(text):
    """推车自己发出去的正文 (大肥鱼回复) 用: 同一套规矩, 不合格的字符直接去掉, 换行 / 制表留着."""
    return "".join(ch for ch in text if not _bad_char(ch))


def check_text(data):
    """字节 -> 文字. 只收可见文字加换行 / 制表 (ZWJ 也放行)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Bad("utf8", "不是 UTF-8") from None
    if not text:
        raise Bad("empty")
    for ch in text:
        if _bad_char(ch):
            raise Bad("ctrl", f"含控制 / 格式 / 看不见的字符 U+{ord(ch):04X}")
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


def may_notify(devices, recent, now, fresh):
    """回执 / 回复能不能发 (通知是广播): 连着的每台设备都受信, 而且本次连接内最近 fresh 秒里 secure-write 成功过
    (recent: 大写地址 -> 时间). 冒充已 trust 地址的未加密连接可能靠 BlueZ 恢复的 CCC 收到通知, 但它写不了 secure-write."""
    on = [d for d in devices if d.get("Connected") is True]
    if not on:
        return False
    for d in on:
        t = recent.get(str(d.get("Address") or "").upper())
        if not device_trusted(d) or t is None or not 0 <= now - t <= fresh:
            return False
    return True


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
        self.done = {}                          # src -> OrderedDict((id, 总长, crc) -> 投递时间)

    def expire(self, now):
        out = []
        for key in [k for k, p in self.partial.items() if now - p.last > PARTIAL_TTL_S]:
            p = self.partial.pop(key)
            out.append(Event("err", key[0], key[1], code="timeout", crc=p.crc))
        for src, seen in list(self.done.items()):
            for key in [k for k, t in seen.items() if now - t > DEDUP_TTL_S]:
                del seen[key]
            if not seen:
                del self.done[src]
        return out

    def _delivered(self, src, mid):
        return any(k[0] == mid for k in self.done.get(src, ()))

    def feed(self, src, frame, now, trusted):
        out = self.expire(now)
        frame = bytes(frame)
        if not trusted:
            return out + [Event("err", src, peek_id(frame), code="unpaired", crc=peek_crc(frame))]
        try:
            kind, mid, a, crc, chunk = decode_frame(frame)
        except Bad as e:
            return out + [Event("err", src, peek_id(frame), code=e.code)]
        key = (src, mid)
        if kind == "start":
            if (mid, a, crc) in self.done.get(src, ()):
                return out + [Event("dup", src, mid, crc=crc)]
            self.partial.pop(key, None)         # 同一序号重新开头 = 手机重发, 按新的来
            if not 0 < a <= MAX_LEN or len(chunk) > a:
                return out + [Event("err", src, mid, code="len", crc=crc)]
            mine = [k for k in self.partial if k[0] == src]
            while len(mine) >= MAX_PARTIAL:
                old = mine.pop(0)
                out.append(Event("err", src, old[1], code="busy", crc=self.partial.pop(old).crc))
            p = self.partial[key] = _Partial(a, crc, now)
            off = 0
        else:
            p = self.partial.get(key)
            if p is None:
                if self._delivered(src, mid):
                    return out                  # 已投递消息的迟到分片, 安静丢掉
                return out + [Event("err", src, mid, code="nostart")]
            off = a
        try:
            p.add(off, chunk)
        except Bad as e:
            del self.partial[key]
            return out + [Event("err", src, mid, code=e.code, crc=p.crc)]
        p.last = now
        if p.have < p.total:
            return out
        del self.partial[key]
        data = p.data()
        if crc32(data) != p.crc:
            return out + [Event("err", src, mid, code="crc", crc=p.crc)]
        try:
            text = check_text(data)
        except Bad as e:
            return out + [Event("err", src, mid, code=e.code, crc=p.crc)]
        seen = self.done.setdefault(src, collections.OrderedDict())
        seen[(mid, p.total, p.crc)] = now
        while len(seen) > DEDUP_KEEP:
            seen.popitem(last=False)
        return out + [Event("text", src, mid, text=text, size=len(data), crc=p.crc)]

    def export(self):
        """去重表 -> 可写 JSON 的行 [来源, 序号, 总长, crc, 时间]. 时间是调用方给的 (monotonic)."""
        return [[src, k[0], k[1], k[2], t] for src, seen in self.done.items() for k, t in seen.items()]

    def restore(self, rows, now):
        """export 的反面; 坏行、过期的、比 now 还新的 (时钟不对) 都丢掉."""
        for r in rows if isinstance(rows, list) else ():
            try:
                src, mid, total, crc, t = r
                key = (int(mid), int(total), int(crc))
                t = float(t)
            except (TypeError, ValueError):
                continue
            if not isinstance(src, str) or not 0 <= now - t <= DEDUP_TTL_S:
                continue
            seen = self.done.setdefault(src, collections.OrderedDict())
            seen[key] = t
            while len(seen) > DEDUP_KEEP:
                seen.popitem(last=False)

    def forget(self, ev):
        """投递失败 (例如队列满) 时调用: 手机重发同一条要能再收."""
        self.done.get(ev.src, {}).pop((ev.msg_id, ev.size, ev.crc), None)


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


def ack(msg_id, crc, dup=False):
    """回执带被确认消息的 (序号, CRC): 通知是广播, 手机只认和自己发件箱对得上的."""
    return _j({"t": "ack", "id": msg_id, "crc": crc, "dup": True} if dup else {"t": "ack", "id": msg_id, "crc": crc})


def err(msg_id, code, crc=None):
    """crc 读不出来 (例如没有 START) 时是 null; 手机对 null 的只按序号试着重发, 不删发件箱."""
    return _j({"t": "err", "id": msg_id, "crc": crc, "code": code})


def reply(text):
    """大肥鱼回复的摘要: 超过 REPLY_MAX 字节在字符边界截断, 加省略号."""
    text = clean_text(text)
    raw = text.encode("utf-8")
    if len(raw) > REPLY_MAX:
        cut = REPLY_MAX - len("…".encode())
        text = raw[:cut].decode("utf-8", "ignore") + "…"
    return _j({"t": "reply", "text": text})


def status_for(ev):
    if ev.kind == "text":
        return ack(ev.msg_id, ev.crc)
    if ev.kind == "dup":
        return ack(ev.msg_id, ev.crc, dup=True)
    return err(ev.msg_id, ev.code, ev.crc)


def audit(ev, result=None):
    """审计行正文 (交给 cartlog.event): 只有来源设备、序号、长度、结果. 不写正文."""
    return cartlog.fields(dev=ev.src, id=ev.msg_id, len=ev.size, kind=ev.kind, code=ev.code, result=result)
