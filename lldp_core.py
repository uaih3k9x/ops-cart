#!/usr/bin/env python3
"""lldp_core: 插上网线就知道对面是哪台交换机的哪个口 (issue #14). 只收不发.

  解析   以太网帧字节 → 邻居 dict (LLDP 0x88cc; 思科老设备只开 CDP, 也认)
  邻居表 按网口记"最近一次邻居 + 收到时间", 超过 TTL 变旧, 再久一点就忘掉
  监听   Linux AF_PACKET, 只收 01:80:c2:00:00:0e / 01:00:0c:cc:cc:cc, 没 root / 不是 Linux 降级

解析和邻居表是纯函数 (测试用现拼的帧 + 注入时钟); 监听线程由 cart-dash (dash_service) 起.
畸形帧一律返回 None, 不往上抛.
"""

import errno
import ipaddress
import select
import socket
import struct
import threading
import time

LLDP_MAC = b"\x01\x80\xc2\x00\x00\x0e"    # nearest bridge
CDP_MAC = b"\x01\x00\x0c\xcc\xcc\xcc"
ETH_LLDP = 0x88cc
ETH_8021Q = 0x8100
CDP_SNAP = b"\xaa\xaa\x03\x00\x00\x0c\x20\x00"   # LLC + 思科 OUI + PID 0x2000

# Chassis ID / Port ID 子类型 (802.1AB)
CH_MAC, CH_NET = 4, 5
PT_MAC, PT_NET, PT_IFNAME = 3, 4, 5
OUI_8021 = b"\x00\x80\xc2"


def mac_str(b):
    return ":".join("%02x" % x for x in b)


def _text(b):
    return b.decode("utf-8", "replace").strip("\x00 \t\r\n")


def _net_addr(b):
    """IANA 地址族 1 字节 + 地址: 1=IPv4 2=IPv6; 认不出就给十六进制."""
    if len(b) == 5 and b[0] == 1:
        return str(ipaddress.IPv4Address(b[1:]))
    if len(b) == 17 and b[0] == 2:
        return str(ipaddress.IPv6Address(b[1:]))
    return b.hex()


def _id_value(sub, val, mac_sub, net_sub):
    if sub == mac_sub and len(val) == 6:
        return mac_str(val)
    if sub == net_sub:
        return _net_addr(val)
    return _text(val)


def _mgmt(v):
    """管理地址 TLV: 地址串长(含子类型) + 子类型 + 地址 + 接口子类型/编号/OID. 坏了返回 None."""
    if not v:
        return None
    n = v[0]
    if n < 2 or len(v) < 1 + n:
        return None
    a = v[1:1 + n]
    return _net_addr(a) if a[0] in (1, 2) else None


def _tlvs(data):
    """LLDPDU → [(类型, 值)]; 越界/截断抛 ValueError; 碰到 End 就停."""
    out, i = [], 0
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError("TLV 头截断")
        hdr, = struct.unpack_from("!H", data, i)
        t, n = hdr >> 9, hdr & 0x1ff
        i += 2
        if i + n > len(data):
            raise ValueError("TLV 长度越界")
        if t == 0:
            break
        out.append((t, data[i:i + n]))
        i += n
    return out


def _blank(proto, src):
    return {"proto": proto, "src": src, "chassis_id": "", "chassis_subtype": 0, "port_id": "",
            "port_subtype": 0, "ttl": 0, "port_desc": None, "sys_name": None, "sys_desc": None,
            "mgmt": [], "vlan": None}


def parse_lldpdu(data, src=""):
    """LLDPDU (以太网头之后) → 邻居 dict. 前三个必须依次是 Chassis ID / Port ID / TTL, 否则 ValueError.
    可选 TLV 坏了只跳过那一个."""
    tl = _tlvs(data)
    if len(tl) < 3 or [t for t, _ in tl[:3]] != [1, 2, 3]:
        raise ValueError("缺必需 TLV")
    (_, ch), (_, pt), (_, tt) = tl[:3]
    if len(ch) < 2 or len(pt) < 2 or len(tt) != 2:
        raise ValueError("必需 TLV 长度不对")
    nb = _blank("lldp", src)
    nb.update(chassis_subtype=ch[0], chassis_id=_id_value(ch[0], ch[1:], CH_MAC, CH_NET),
              port_subtype=pt[0], port_id=_id_value(pt[0], pt[1:], PT_MAC, PT_NET),
              ttl=struct.unpack("!H", tt)[0])
    for t, v in tl[3:]:
        if t == 4:
            nb["port_desc"] = _text(v)
        elif t == 5:
            nb["sys_name"] = _text(v)
        elif t == 6:
            nb["sys_desc"] = _text(v)
        elif t == 8:
            a = _mgmt(v)
            if a:
                nb["mgmt"].append(a)
        elif t == 127 and len(v) == 6 and v[:4] == OUI_8021 + b"\x01":
            nb["vlan"] = struct.unpack("!H", v[4:])[0] or None    # 0 = 没配 PVID
    return nb


def _cdp_addrs(v):
    """CDP 地址 TLV: 个数(4) + [协议类型 协议长 协议 地址长(2) 地址]; 只取 IPv4."""
    out = []
    if len(v) < 4:
        return out
    n, = struct.unpack_from("!I", v)
    i = 4
    for _ in range(min(n, 16)):
        if i + 2 > len(v):
            break
        plen = v[i + 1]
        j = i + 2 + plen
        if j + 2 > len(v):
            break
        proto = v[i + 2:j]
        alen, = struct.unpack_from("!H", v, j)
        a = v[j + 2:j + 2 + alen]
        if len(a) != alen:
            break
        if proto == b"\xcc" and alen == 4:
            out.append(str(ipaddress.IPv4Address(a)))
        i = j + 2 + alen
    return out


def parse_cdp(data, src=""):
    """CDP 报文 (SNAP 头之后): 版本/TTL/校验和 + TLV(类型2 长度2 含头). 要有设备名和端口."""
    if len(data) < 4:
        raise ValueError("CDP 头截断")
    nb = _blank("cdp", src)
    nb["ttl"] = data[1]
    i = 4
    while i < len(data):
        if i + 4 > len(data):
            raise ValueError("CDP TLV 头截断")
        t, n = struct.unpack_from("!HH", data, i)
        if n < 4 or i + n > len(data):
            raise ValueError("CDP TLV 长度越界")
        v = data[i + 4:i + n]
        if t == 1:
            nb["sys_name"] = nb["chassis_id"] = _text(v)
        elif t == 2:
            nb["mgmt"] = _cdp_addrs(v)
        elif t == 3:
            nb["port_id"] = _text(v)
            nb["port_subtype"] = PT_IFNAME
        elif t == 5:
            nb["sys_desc"] = _text(v)
        elif t == 10 and len(v) == 2:
            nb["vlan"] = struct.unpack("!H", v)[0] or None
        i += n
    if not nb["chassis_id"] or not nb["port_id"]:
        raise ValueError("CDP 缺设备名或端口")
    return nb


def parse_frame(frame):
    """以太网帧 → 邻居 dict; 不是 LLDP/CDP 或者畸形都返回 None. 本函数不抛."""
    try:
        if len(frame) < 14:
            return None
        dst, src = frame[:6], mac_str(frame[6:12])
        etype, = struct.unpack_from("!H", frame, 12)
        off = 14
        if etype == ETH_8021Q:
            if len(frame) < 18:
                return None
            etype, = struct.unpack_from("!H", frame, 16)
            off = 18
        if dst == LLDP_MAC and etype == ETH_LLDP:
            return parse_lldpdu(frame[off:], src)
        if dst == CDP_MAC and etype <= 1500 and frame[off:off + 8] == CDP_SNAP:
            return parse_cdp(frame[off + 8:off + etype], src)
    except (ValueError, struct.error, IndexError):
        return None
    return None


def device_name(nb):
    """对端设备: 系统名优先, 没有就用 Chassis ID."""
    return nb.get("sys_name") or nb.get("chassis_id") or ""


def port_name(nb):
    """对端口: Port ID 是 MAC / 网络地址时人看不懂, 有描述就用描述."""
    if nb.get("port_subtype") in (PT_MAC, PT_NET) and nb.get("port_desc"):
        return nb["port_desc"]
    return nb.get("port_id") or nb.get("port_desc") or ""


# ======== 邻居表 ========

FORGET_S = 600          # 过了 TTL 还灰着显示这么久, 再久就当没见过 (换了地方别残留)


def entry_state(entry, now, forget=FORGET_S):
    """{"neighbor", "at"} → fresh (TTL 内) / stale (过期, 灰显) / none (没有或早忘了)."""
    if not entry:
        return "none"
    age = now - entry["at"]
    hold = entry["neighbor"].get("ttl") or 0
    if age <= hold:
        return "fresh"
    return "stale" if age <= hold + forget else "none"


class Neighbors:
    """按网口记最近一次邻居. ifaces: [(显示名, 网口名)], 快照按显示名给 (LAN1 / LAN2)."""

    def __init__(self, ifaces, forget=FORGET_S):
        self.ifaces, self.forget = list(ifaces), forget
        self._e, self._err = {}, {}

    def seen(self, iface, nb, now):
        if iface not in (i for _, i in self.ifaces):
            return
        if not nb.get("ttl"):              # TTL 0 = 对端说"我走了"
            self._e.pop(iface, None)
        else:
            self._e[iface] = {"neighbor": nb, "at": now}

    def error(self, iface, msg):
        """监听这个口出了什么问题 (没权限 / 网口不存在); 空串 = 好了."""
        self._err[iface] = msg

    def snapshot(self, now):
        out = {}
        for name, iface in self.ifaces:
            e = self._e.get(iface)
            st = entry_state(e, now, self.forget)
            if st == "none":
                self._e.pop(iface, None)
                e = None
            err = self._err.get(iface, "")
            if st == "none" and err:
                st = "off"
            out[name] = {"iface": iface, "state": st, "age": int(now - e["at"]) if e else None,
                         "neighbor": e["neighbor"] if e else None, "error": err}
        return out


# ======== 监听 (Linux AF_PACKET, 只收) ========

ETH_P_802_2 = 0x0004            # 带 LLC 的 802.3 帧 (CDP 走这个)
SOL_PACKET, PACKET_ADD_MEMBERSHIP, PACKET_MR_MULTICAST = 263, 1, 0
PACKET_OUTGOING = 4
RETRY_S = 10                    # 打不开的口隔多久再试


def _join(s, ifindex, mac):
    """让网卡收这个组播 (有的网卡默认把 LLDP 组播滤掉). 只是收的设置, 不发包; 失败不管."""
    try:
        s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP,
                     struct.pack("iHH8s", ifindex, PACKET_MR_MULTICAST, 6, mac))
    except OSError:
        pass


def open_socket(iface):
    """给一个网口开两个只收的原始套接字: LLDP (0x88cc) 和 LLC (CDP). 失败抛 OSError."""
    if not hasattr(socket, "AF_PACKET"):
        raise OSError("不是 Linux, 没有 AF_PACKET")
    ifindex = socket.if_nametoindex(iface)          # 网口不存在在这里就抛
    socks = []
    try:
        for proto, mac in ((ETH_LLDP, LLDP_MAC), (ETH_P_802_2, CDP_MAC)):
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(proto))
            socks.append(s)
            s.bind((iface, proto))
            _join(s, ifindex, mac)
            s.setblocking(False)
    except Exception:
        for s in socks:
            s.close()
        raise
    return socks


def _reason(e):
    if isinstance(e, PermissionError):
        return "没有权限 (要 root)"
    if isinstance(e, OSError) and e.errno in (errno.ENODEV, errno.ENXIO):
        return "网口不存在"
    if isinstance(e, OSError) and e.errno == errno.ENETDOWN:
        return "网口掉了"
    if isinstance(e, OSError) and e.errno is None and e.args:
        return str(e.args[0])
    return "%s: %s" % (type(e).__name__, e)


class Watcher:
    """监听若干网口, 收到 LLDP/CDP 就记进邻居表. 所有错误都吞掉变成"不可用", 不往上抛.

    poll() 跑一步 (测试直接调); start() 起守护线程反复 poll, close() 停.
    opener / select_fn / clock 可注入, 测试里不碰真网卡.
    """

    def __init__(self, ifaces, opener=None, select_fn=None, clock=time.time, retry=RETRY_S):
        self.ifaces = list(ifaces)
        self.table = Neighbors(self.ifaces)
        self.opener = opener or open_socket
        self.select = select_fn or select.select
        self.clock, self.retry = clock, retry
        self._socks, self._next = {}, {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _drop(self, iface, e, now):
        for s in self._socks.pop(iface, []):
            try:
                s.close()
            except OSError:
                pass
        self._next[iface] = now + self.retry
        with self._lock:
            self.table.error(iface, _reason(e))

    def _open_due(self, now):
        for _, iface in self.ifaces:
            if iface in self._socks or now < self._next.get(iface, 0):
                continue
            try:
                self._socks[iface] = list(self.opener(iface))
                with self._lock:
                    self.table.error(iface, "")
            except Exception as e:                  # 没 root / 网口不存在 / 不是 Linux
                self._drop(iface, e, now)

    def _drain(self, iface, s):
        for _ in range(64):                         # 一轮最多收这么多, 别被刷屏卡住
            try:
                data, addr = s.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            if len(addr) > 2 and addr[2] == PACKET_OUTGOING:
                continue
            nb = parse_frame(data)
            if nb:
                with self._lock:
                    self.table.seen(iface, nb, self.clock())

    def poll(self, timeout=1.0):
        """一步: 到点的口重开, 等一会儿, 把能读的都读掉. 返回开着的口数. 不抛."""
        now = self.clock()
        self._open_due(now)
        owner = {s: iface for iface, ss in self._socks.items() for s in ss}
        if not owner:
            return 0
        try:
            ready = self.select(list(owner), [], [], timeout)[0]
        except (OSError, ValueError) as e:
            for iface in set(owner.values()):
                self._drop(iface, e, now)
            return 0
        for s in ready:
            iface = owner[s]
            if iface not in self._socks:
                continue
            try:
                self._drain(iface, s)
            except OSError as e:                    # 网卡被拔 (ENETDOWN / ENXIO)
                self._drop(iface, e, now)
        return len(self._socks)

    def snapshot(self):
        with self._lock:
            return self.table.snapshot(self.clock())

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self.poll(1.0):
                    self._stop.wait(1.0)
            except Exception:                       # 兜底: 监听线程不能死
                self._stop.wait(1.0)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="lldp", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        for iface in list(self._socks):
            for s in self._socks.pop(iface):
                try:
                    s.close()
                except OSError:
                    pass
