"""网口配置文件的纯逻辑: 命名/解析 nmcli 输出/规则/拼命令/DHCP 探测报文. 不碰 Tk, 方便测试.

一个配置文件 = 一个 NM 以太网连接, 名字 cart-<网口>-<种类>-<标签>; 老的两个连接当内置配置.
切换 = nmcli con up, 同一网口同时只有一个生效, 切之前记下原来那个用于一键回退.

种类:
  dhcp    DHCP 客户端
  static  静态地址 (不抢默认路由, 用来摸设备出厂网段)
  serve   发地址 / PXE —— 只许在 LAN1, 启用前先探测链路上有没有别人的 DHCP
无线 (wlp4s0) 不归这里管, 永远留作后路.
"""

import ipaddress
import os
import select
import socket
import struct
import time

from wifi_core import split_terse

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


# ------------------------------------------------------------ 命名 / 列表

def con_name(iface, kind, label):
    return f"{PREFIX}-{iface}-{kind}-{label}"


def parse_name(name):
    """连接名 -> {iface, kind, label}; 不是推车配置文件返回 None."""
    if name in BUILTIN:
        iface, kind, label = BUILTIN[name]
        return {"iface": iface, "kind": kind, "label": label}
    parts = name.split("-", 3)
    if len(parts) != 4 or parts[0] != PREFIX or parts[1] not in IFACES or parts[2] not in KINDS:
        return None
    return {"iface": parts[1], "kind": parts[2], "label": parts[3]}


CMD_CONS = ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "con", "show"]


def parse_cons(text):
    """CMD_CONS 输出 -> 推车配置文件列表, 按 LAN1/LAN2 分组, 组内内置在前、其余按标签."""
    out = []
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 5 or f[2] != "802-3-ethernet":
            continue
        p = parse_name(f[0])
        if p:
            p.update(name=f[0], uuid=f[1], active=f[4] == "yes", builtin=f[0] in BUILTIN)
            out.append(p)
    order = list(IFACES)
    return sorted(out, key=lambda p: (order.index(p["iface"]), not p["builtin"], p["label"]))


# ------------------------------------------------------------ 规则

def check(iface, kind):
    """不允许的组合返回原因, 允许返回 None."""
    if iface not in IFACES:
        return f"{iface} 不归配置文件管 (只管 {'/'.join(IFACES)})"
    if kind not in KINDS:
        return f"不认识的种类 {kind}"
    if kind == "serve" and iface not in SERVE_IFACES:
        return "发地址 / PXE 只能放在 LAN1 (eno1), 管理口永不对外发地址"
    return None


def check_label(label):
    if not label.strip():
        return "名字不能为空"
    if ":" in label or "\\" in label:
        return "名字里别带 : 或 \\"
    if len(label) > 24:
        return "名字太长 (最多 24 个字)"
    return None


def pick_self(peer):
    """对端地址 (可带前缀, 默认 /24) -> 推车自己用的地址: 同网段, 从 .250 往下避开对端."""
    try:
        iface = ipaddress.ip_interface(peer if "/" in peer else peer + "/24")
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


# ------------------------------------------------------------ nmcli 命令

def cmd_create(iface, kind, label, addr=None):
    why = check(iface, kind) or check_label(label)
    if why:
        raise ValueError(why)
    cmd = ["nmcli", "con", "add", "type", "ethernet", "ifname", iface, "con-name", con_name(iface, kind, label),
           "connection.autoconnect", "no", "ipv6.method", "disabled"]
    if kind == "dhcp":
        return cmd + ["ipv4.method", "auto"]
    if not addr:
        raise ValueError("静态 / 发地址配置要给地址")
    return cmd + ["ipv4.method", "manual", "ipv4.addresses", addr, "ipv4.never-default", "yes"]


def cmd_up(uuid):
    return ["nmcli", "con", "up", "uuid", uuid]


def cmd_delete(uuid):
    return ["nmcli", "con", "delete", "uuid", uuid]


def plan_switch(cur, new):
    """cur = 该网口当前生效的配置 (可为 None), new = 要切的. 返回要做的事, 由面板按顺序执行."""
    return {
        "up": cmd_up(new["uuid"]),
        "rollback": cmd_up(cur["uuid"]) if cur else None,
        "probe": new["kind"] == "serve",
        "stop_pxe": new["iface"] in SERVE_IFACES and new["kind"] != "serve",
        "warn_remote": new["iface"] == "enp3s0",
    }


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
