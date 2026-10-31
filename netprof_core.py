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
import shlex
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


def static_hint(addr, peer=""):
    """面板静态地址下方的小灰字 (#13): 主输入是本机 地址/前缀, 对端可选 (填了只防撞址)."""
    addr = (addr or "").strip()
    if not addr:
        return ("写成 地址/前缀, 比如 192.0.2.5/24, 掩码按你填的来; "
                "对端地址可选: 填了只防撞址, 也可点「按对端预填」(对端也要带前缀)")
    try:
        form = static_form(addr, avoid=(peer or "").strip())
    except ValueError as ex:
        return str(ex)
    net = ipaddress.ip_interface(form["addr"]).network
    return f"推车用 {form['addr']} (网段 {net}, 不抢默认路由), 建的时候还会确认"


# ------------------------------------------------------------ nmcli 命令

def cmd_create(iface, kind, label, addr=None, confirmed=True):
    """拼 `nmcli con add`. confirmed=False 时静态地址一律拒绝 —— 地址可能只是探测来的建议值,
    必须由人确认后才允许写进 NetworkManager (issue #8)."""
    why = check(iface, kind) or check_label(label)
    if why:
        raise ValueError(why)
    if kind != "dhcp" and not confirmed:
        raise ValueError("静态地址没经确认, 不能直接写")
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


def rollback_text(cur, iface):
    """确认框里的退路 (#8): 人话 + 车边终端能照抄的命令. cur = 该网口当前生效的配置 (可为 None)."""
    if cur:
        name = cur.get("name")
        cmd = f"nmcli con up id {shlex.quote(name)}" if name else shlex.join(cmd_up(cur["uuid"]))
        return (f"出问题怎么回滚: 切换失败会自动回到「{cur.get('label') or name or cur['uuid']}」; "
                f"切过去后不对, 在本面板点「回退上一个」,\n或车边终端执行: {cmd}")
    return (f"出问题怎么回滚: {IFACES.get(iface, iface)} 原来没有生效配置, 没有可回到的; "
            f"不对就车边终端执行: nmcli dev disconnect {iface}")


def plan_switch(cur, new):
    """cur = 该网口当前生效的配置 (可为 None), new = 要切的. 返回要做的事, 由面板按顺序执行."""
    return {
        "up": cmd_up(new["uuid"]),
        "rollback": cmd_up(cur["uuid"]) if cur else None,
        "rollback_text": rollback_text(cur, new["iface"]),
        "probe": new["kind"] == "serve",
        "stop_pxe": new["iface"] in SERVE_IFACES and new["kind"] != "serve",
        "warn_remote": new["iface"] == "enp3s0",
    }


CMD_IP_BR = ["ip", "-4", "-br", "addr", "show"]


def parse_ip_br(text):
    """CMD_IP_BR 输出 -> {网口: 第一个地址}; 没地址的口不出现."""
    out = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) >= 3:
            out[f[0]] = f[2]
    return out


def switch_confirm_text(cur, new, plan, ip=""):
    """切网口配置前的确认框 (#8): LAN1/LAN2 都弹, 写明原来/改为/要注意的/怎么回滚.
    ip = 该网口当前地址 (读不到给空). 这是额外一层, 不替代切换失败自动回滚和发地址前的探测."""
    iface, ip = new["iface"], ip or "-"
    before = f"{cur['label']} · {KINDS[cur['kind']]} · {ip}" if cur else f"没有生效配置 · 当前地址 {ip}"
    lines = [f"{IFACES.get(iface, iface)} ({iface}) 切换网口配置", "",
             f"原来: {before}", f"改为: {new['label']} · {KINDS[new['kind']]}", ""]
    if plan["warn_remote"]:
        lines.append("LAN2 是管理口, 切换后经它的远程连接会断 (无线和 tailscale 还在).")
    if plan["stop_pxe"]:
        lines.append("会先停 PXE 装机服务, 再切换.")
    if plan["probe"]:
        lines.append("发地址前会先探测链路上有没有别的 DHCP 服务器, 有 (或没插线探测不了) 就不启用.")
        lines.append("如果接在交换机/已有网络上, 推车可能进代理模式, 只补 PXE 引导、不发地址.")
    if lines[-1]:
        lines.append("")
    return "\n".join(lines + [plan["rollback_text"], "", "要继续吗?"])


# ------------------------------------------------------------ LAN2 单一口径 (issue #8)

LAN2 = "enp3s0"


def lan2_view(iface=LAN2, cons_text="", nics=()):
    """LAN2 管理口的唯一口径: 两个入口都读它, 不再各自判断.

    kiosk 的「LAN2 管理口配置」改的是内置连接 (名字就是网口名),
    「网口配置文件」面板改的是 cart-<网口>-<种类>-<标签>. 两个入口都会动这个口,
    所以只要命名配置出现, 就标冲突 (entries 列出参与方), 由界面明确提示.
    """
    nic = next((n for n in nics if n.get("name") == iface), None) or {}
    builtin = iface if parse_name(iface) else None
    profs = parse_cons(cons_text)
    named = [p["name"] for p in profs if p["iface"] == iface and not p["builtin"]]
    active = next((p["name"] for p in profs if p["iface"] == iface and p["active"]), None)
    entries = (["LAN2 管理口配置"] if builtin else []) + (["网口配置文件"] if named else [])
    conflict = builtin is not None and bool(named)
    warning = ""
    if conflict:
        warning = ("两个入口都能改这个口：「LAN2 管理口配置」改内置连接，"
                   "「网口配置文件」改命名配置；切换后以实际状态为准")
    return {
        "iface": iface,
        "ip": nic.get("ip") or "-",
        "method": nic.get("method") or "-",
        "link": bool(nic.get("link")),
        "con": nic.get("con") or "",
        "builtin": builtin,
        "named": named,
        "active": active,
        "entries": entries,
        "conflict": conflict,
        "warning": warning,
    }


IPV4_FIELDS = ("method", "addr", "gateway", "dns")


def cmd_ipv4(con):
    return ["nmcli", "-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "con", "show", con]


def parse_ipv4(text):
    """cmd_ipv4 输出 (一行一个字段, 末尾空行可能被裁掉) -> {method, addr, gateway, dns}; 读不到返回 None."""
    lines = (text or "").splitlines()
    if not lines or not lines[0].strip():
        return None
    lines += [""] * len(IPV4_FIELDS)
    return {k: lines[i].strip() for i, k in enumerate(IPV4_FIELDS)}


def describe_ipv4(s):
    if not s:
        return "读不到"
    if s["method"] == "auto":
        return "DHCP(自动获取)"
    if s["method"] != "manual":
        return s["method"]
    return (f"静态 {s['addr'] or '-'}" + (f" 网关 {s['gateway']}" if s.get("gateway") else "")
            + (f" DNS {s['dns']}" if s.get("dns") else ""))


def lan2_rollback(con, before):
    """kiosk「LAN2 管理口配置」是原地改内置连接, 回滚 = 把改之前的四个 ipv4 字段写回去再 up.
    before = parse_ipv4 的结果; 读不到 (None) 就给不出命令, 只给人话."""
    if not before:
        return {"cmds": None,
                "text": (f"出问题怎么回滚: 读不到改之前的设置, 只能手动 —— 车边终端 nmcli con show {con} 看现状, "
                         f"想改回 DHCP 就再开「LAN2 管理口配置」选 DHCP")}
    cmds = [["nmcli", "con", "mod", con, "ipv4.method", before["method"],
             "ipv4.addresses", before["addr"], "ipv4.gateway", before["gateway"], "ipv4.dns", before["dns"]],
            ["nmcli", "con", "up", con]]
    text = (f"出问题怎么回滚 (回到原来的 {describe_ipv4(before)}):\n"
            f"  人在车边: 再开「LAN2 管理口配置」改回去\n"
            f"  或车边终端执行:\n" + "\n".join("    " + shlex.join(c) for c in cmds))
    return {"cmds": cmds, "text": text}


def lan2_confirm_text(con, before, after):
    """下发前最后一个确认框: 原来什么、改成什么、怎么回滚."""
    return (f"原来: {describe_ipv4(before)}\n改为: {describe_ipv4(after)}\n\n"
            f"{lan2_rollback(con, before)['text']}\n\n确认下发?")


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
