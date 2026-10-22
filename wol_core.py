"""网络唤醒 (WOL) 的纯逻辑: MAC 解析、魔术包、常用目标清单 (/opt/pxe/wol.json). 发包要 root.

魔术包 = 6 个 0xff + 目标 MAC 重复 16 次, UDP 广播到 9 号端口, 从指定网口发 (SO_BINDTODEVICE),
目标得和那个网口在同一个二层里.
"""

import json
import re
import socket

import atomicfile

WOL_CONF = "/opt/pxe/wol.json"
IFACES = {"enp3s0": "LAN2", "eno1": "LAN1", "wlp4s0": "无线"}


def parse_mac(s):
    """各种写法 -> 'aa:bb:cc:dd:ee:ff'; 广播/全零/组播地址不收."""
    hexs = re.sub(r"[\s:.\-]", "", s or "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", hexs):
        raise ValueError(f"MAC 不对: {s!r}")
    if hexs in ("f" * 12, "0" * 12) or int(hexs[1], 16) & 1:
        raise ValueError(f"{s} 是广播/组播/全零地址, 不是一台机器")
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))


def magic_packet(mac):
    return b"\xff" * 6 + bytes.fromhex(parse_mac(mac).replace(":", "")) * 16


def load(path=WOL_CONF):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save(targets, path=WOL_CONF):
    atomicfile.write_json(path, targets, indent=1)


def add(targets, name, mac, iface):
    """返回新清单 (同 MAC 视为更新), 按名字排序."""
    name = name.strip()
    if not name:
        raise ValueError("名字不能为空")
    if iface not in IFACES:
        raise ValueError(f"网口只能是 {'/'.join(IFACES)}")
    mac = parse_mac(mac)
    out = [t for t in targets if t["mac"] != mac] + [{"name": name, "mac": mac, "iface": iface}]
    return sorted(out, key=lambda t: t["name"])


def remove(targets, mac):
    mac = parse_mac(mac)
    return [t for t in targets if t["mac"] != mac]


def send(mac, iface, port=9, repeat=3):
    """从 iface 广播魔术包 (发几遍, UDP 会丢)."""
    pkt = magic_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, 25, iface.encode() + b"\0")    # SO_BINDTODEVICE
        for _ in range(repeat):
            s.sendto(pkt, ("255.255.255.255", port))
    finally:
        s.close()
