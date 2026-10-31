"""网口的共用小件: 认旧连接名、静态地址校验/预填、读地址、DHCP 探测报文. 不碰 Tk, 方便测试.

网口怎么切归 netmode_core (#12); 这里剩下的给它和 pxeserver 用:
- 旧连接名: 老的两个内置连接 (enp3s0、Lan1-pxe) 和旧「网口配置文件」面板建的 cart-<网口>-<种类>-<标签>,
  网口模式面板把它们列成"旧配置", 只认不删. 种类 serve = 发地址 / PXE, 只在 LAN1.
- static_form / pick_self: 静态地址人手填 地址/前缀, 不猜 /24 (#8 #13).
- dhcp_probe: 发地址前探测链路上有没有别人的 DHCP (pxeserver 守卫也用).
无线 (wlp4s0) 不归这里管, 永远留作后路.
"""

import ipaddress
import os
import select
import socket
import struct
import time

IFACES = {"eno1": "LAN1", "enp3s0": "LAN2"}
KINDS = {"dhcp": "DHCP 客户端", "static": "静态地址", "serve": "发地址 / PXE"}
SERVE_IFACES = ("eno1",)
PREFIX = "cart"
BUILTIN = {"enp3s0": ("enp3s0", "dhcp", "默认"), "Lan1-pxe": ("eno1", "serve", "装机")}
PXE_UNITS = ("pxe-server", "pxe-http-repo", "pxe-http-boot")

# 常见设备出厂管理地址; 只是起点, 面板里可以手填
STATIC_TEMPLATES = (
    ("华为交换机", "192.168.1.253/24"),
    ("H3C 交换机", "192.168.0.233/24"),
    ("华硕路由", "192.168.50.1/24"),
    ("小米路由", "192.168.31.1/24"),
    ("TP-Link", "192.168.1.1/24"),
    ("通用 192.168.0.1", "192.168.0.1/24"),
)


# ------------------------------------------------------------ 旧连接名

def parse_name(name):
    """连接名 -> {iface, kind, label}; 不是内置连接或旧面板的配置文件返回 None."""
    if name in BUILTIN:
        iface, kind, label = BUILTIN[name]
        return {"iface": iface, "kind": kind, "label": label}
    parts = name.split("-", 3)
    if len(parts) != 4 or parts[0] != PREFIX or parts[1] not in IFACES or parts[2] not in KINDS:
        return None
    return {"iface": parts[1], "kind": parts[2], "label": parts[3]}


CMD_CONS = ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "con", "show"]


# ------------------------------------------------------------ 静态地址

def pick_self(peer):
    """对端 地址/前缀 -> 推车自己用的地址 (只做预填): 同网段, 从 .250 往下避开对端.
    #13: 必须写明前缀, 不再默认 /24 —— 猜错了网段和边界全错."""
    peer = (peer or "").strip()
    if "/" not in peer:
        raise ValueError(f"对端也要写前缀, 比如 {peer or '192.0.2.1'}/24, 不替你猜掩码")
    try:
        iface = ipaddress.ip_interface(peer)
    except ValueError as ex:
        raise ValueError(f"地址不对: {peer}") from ex
    net = iface.network
    if iface.version != 4 or net.prefixlen > 30:
        raise ValueError(f"网段太小: {peer}")
    if iface.ip in (net.network_address, net.broadcast_address):
        raise ValueError(f"{iface.ip} 是网络号或广播地址, 不是设备")
    cand = net.network_address + min(250, net.num_addresses - 2)
    while cand > net.network_address:
        if cand != iface.ip:
            return f"{cand}/{net.prefixlen}"
        cand -= 1
    raise ValueError(f"{peer} 网段里没有空地址")


def _v4(text, what):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError as ex:
        raise ValueError(f"{what}地址不对: {text}") from ex
    if ip.version != 4:
        raise ValueError(f"{what}只支持 IPv4: {text}")
    return ip


def static_form(addr, gateway="", dns="", avoid=""):
    """人手填的静态地址 (#8): 校验并规整成 {addr, gateway, dns}, 不对就 ValueError(人话).
    addr 必须写明 地址/前缀 (不替人猜 /24); 网关、DNS 可空; avoid = 对端设备地址, 不许撞."""
    addr = (addr or "").strip()
    if not addr:
        raise ValueError("地址不能为空, 写成 地址/前缀, 比如 192.0.2.5/24")
    if "/" not in addr:
        raise ValueError(f"{addr} 没写前缀, 要写成 地址/前缀, 比如 {addr}/24")
    host, prefix = addr.split("/", 1)
    ip = _v4(host.strip(), "")
    prefix = prefix.strip()
    if not prefix.isdigit() or not 1 <= int(prefix) <= 32:
        raise ValueError(f"前缀要是 1–32 的数字: /{prefix}")
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_reserved:
        raise ValueError(f"{ip} 是保留/回环/组播地址, 不能用")
    net = ipaddress.ip_interface(f"{ip}/{prefix}").network
    if net.prefixlen <= 30 and ip == net.network_address:
        raise ValueError(f"{ip} 是 {net} 的网络号, 不能当地址")
    if net.prefixlen <= 30 and ip == net.broadcast_address:
        raise ValueError(f"{ip} 是 {net} 的广播地址, 不能当地址")
    if avoid and ip == _v4(avoid.split("/")[0].strip(), "对端"):
        raise ValueError(f"{ip} 就是对端设备的地址, 会撞, 换一个")

    gateway = (gateway or "").strip()
    if gateway:
        gw = _v4(gateway, "网关")
        if gw not in net:
            raise ValueError(f"网关 {gw} 不在 {net} 网段里")
        if gw == ip:
            raise ValueError(f"网关 {gw} 不能是推车自己")
        if net.prefixlen <= 30 and gw in (net.network_address, net.broadcast_address):
            raise ValueError(f"网关 {gw} 是网络号或广播地址")
        gateway = str(gw)
    servers = [d for d in (dns or "").replace(",", " ").split() if d]
    try:
        dns = ",".join(str(_v4(d, "DNS ")) for d in servers)
    except ValueError as ex:
        raise ValueError(f"DNS 不对: {ex}") from ex
    return {"addr": f"{ip}/{prefix}", "gateway": gateway, "dns": dns}


# ------------------------------------------------------------ 读地址

CMD_IP_BR = ["ip", "-4", "-br", "addr", "show"]


def parse_ip_br(text):
    """CMD_IP_BR 输出 -> {网口: 第一个地址}; 没地址的口不出现."""
    out = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) >= 3:
            out[f[0]] = f[2]
    return out


# ------------------------------------------------------------ DHCP 探测

COOKIE = b"\x63\x82\x53\x63"


def build_discover(mac, xid):
    head = struct.pack("!BBBBIHH", 1, 1, 6, 0, xid, 0, 0x8000)
    body = head + b"\x00" * 16 + mac + b"\x00" * (10 + 192)
    opts = COOKIE + bytes([53, 1, 1, 55, 3, 1, 3, 54, 255])
    return body + opts + b"\x00" * (300 - len(body) - len(opts))


def _options(data):
    out, i = {}, 240
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        n = data[i + 1]
        out[code] = data[i + 2:i + 2 + n]
        i += 2 + n
    return out


def parse_offer(data, xid):
    """是对 xid 的 DHCPOFFER 就返回 {server, yiaddr}, 否则 None."""
    if len(data) < 241 or data[0] != 2 or data[236:240] != COOKIE:
        return None
    if struct.unpack("!I", data[4:8])[0] != xid:
        return None
    opts = _options(data)
    if opts.get(53) != b"\x02":
        return None
    server = opts.get(54) if len(opts.get(54, b"")) == 4 else data[20:24]
    return {"server": socket.inet_ntoa(server), "yiaddr": socket.inet_ntoa(data[16:20])}


def iface_mac(iface):
    with open(f"/sys/class/net/{iface}/address") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def carrier(iface, root="/sys/class/net"):
    """网口有没有插线: True / False / None (读不到, 比如网口不存在)."""
    try:
        with open(os.path.join(root, iface, "carrier")) as f:
            return f.read().strip() == "1"
    except (OSError, ValueError):
        return None


class ProbeResult(list):
    """dhcp_probe 的结果: 还是 [{server, yiaddr}] 列表 (老调用方式不变), 另带
    carrier (探测时有没有插线) 和 unknown (没插线/读不到 = 探测不算数, 不能当成"没有别人的 DHCP")."""

    def __init__(self, offers=(), carrier=True):
        super().__init__(offers)
        self.carrier = carrier

    @property
    def unknown(self):
        return self.carrier is not True


def dhcp_probe(iface, timeout=4.0, ignore=(), carrier=carrier):
    """在 iface 上广播一个 DISCOVER, 收集 timeout 秒内的 OFFER (ignore 里的服务器地址不算, 比如自己).
    要 root (绑 68 端口 + SO_BINDTODEVICE). 返回 ProbeResult; 没插线时不发包, 直接返回 unknown."""
    link = carrier(iface)
    if link is not True:
        return ProbeResult(carrier=link)
    xid = struct.unpack("!I", os.urandom(4))[0]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, 25, iface.encode() + b"\0")    # SO_BINDTODEVICE
        s.bind(("0.0.0.0", 68))
        s.sendto(build_discover(iface_mac(iface), xid), ("255.255.255.255", 67))
        found, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([s], [], [], max(0.0, end - time.monotonic()))
            if not r:
                break
            o = parse_offer(s.recv(1500), xid)
            if o and o["server"] not in ignore and o not in found:
                found.append(o)
        return ProbeResult(found)
    finally:
        s.close()
