#!/usr/bin/env python3
"""lldp_core: 插上网线就知道对面是哪台交换机的哪个口 (issue #14). 只收不发.

  解析   以太网帧字节 → 邻居 dict (LLDP 0x88cc; 思科老设备只开 CDP, 也认)
  邻居表 按网口记"最近一次邻居 + 收到时间", 超过 TTL 变旧, 再久一点就忘掉
  监听   Linux AF_PACKET, 只收 01:80:c2:00:00:0e / 01:00:0c:cc:cc:cc, 没 root / 不是 Linux 降级

解析和邻居表是纯函数 (测试用现拼的帧 + 注入时钟); 监听线程由 cart-dash (dash_service) 起.
畸形帧一律返回 None, 不往上抛.
"""

import ipaddress
import struct

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
