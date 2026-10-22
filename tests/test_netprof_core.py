"""netprof_core 测试: 网口配置文件的命名/解析/规则/命令/DHCP 探测报文, 不碰真机."""

import struct

import pytest

import netprof_core as np_

CONS = "\n".join([
    "campusnet:6c4d-1:802-11-wireless:wlp4s0:yes",
    "tailscale0:0659-2:tun:tailscale0:yes",
    "enp3s0:1603-3:802-3-ethernet::no",
    "Lan1-pxe:a737-4:802-3-ethernet:eno1:yes",
    "cart-enp3s0-static-华为交换机:bbbb-5:802-3-ethernet::no",
    "cart-eno1-dhcp-临时:cccc-6:802-3-ethernet:eno1:no",
    "随便谁建的:dddd-7:802-3-ethernet::no",
])


# ------------------------------------------------------------ 命名 / 列表

def test_con_name_roundtrip():
    name = np_.con_name("enp3s0", "static", "H3C 交换机")
    assert name == "cart-enp3s0-static-H3C 交换机"
    assert np_.parse_name(name) == {"iface": "enp3s0", "kind": "static", "label": "H3C 交换机"}


def test_parse_name_builtins_and_unknown():
    assert np_.parse_name("enp3s0") == {"iface": "enp3s0", "kind": "dhcp", "label": "默认"}
    assert np_.parse_name("Lan1-pxe") == {"iface": "eno1", "kind": "serve", "label": "装机"}
    assert np_.parse_name("随便谁建的") is None
    assert np_.parse_name("cart-wlp4s0-dhcp-x") is None        # 无线不归这里管
    assert np_.parse_name("cart-eno1-bogus-x") is None


def test_parse_cons_lists_only_cart_profiles_grouped():
    profs = np_.parse_cons(CONS)
    assert [(p["iface"], p["label"], p["active"]) for p in profs] == [
        ("eno1", "装机", True), ("eno1", "临时", False),
        ("enp3s0", "默认", False), ("enp3s0", "华为交换机", False)]
    assert profs[0]["uuid"] == "a737-4" and profs[0]["builtin"] is True
    assert profs[1]["builtin"] is False and profs[1]["kind"] == "dhcp"


# ------------------------------------------------------------ 规则

def test_check_serve_only_on_lan1():
    assert np_.check("eno1", "serve") is None
    assert "LAN1" in np_.check("enp3s0", "serve")          # 管理口永不发地址
    assert np_.check("enp3s0", "static") is None
    assert np_.check("wlp4s0", "dhcp") is not None
    assert np_.check("eno1", "nope") is not None


def test_check_label():
    assert np_.check_label("华为 交换机") is None
    assert np_.check_label("") is not None
    assert np_.check_label("a:b") is not None              # nmcli -t 的分隔符, 别找麻烦
    assert np_.check_label("x" * 40) is not None


# ------------------------------------------------------------ 静态地址

def test_pick_self_avoids_peer():
    assert np_.pick_self("192.168.1.253/24") == "192.168.1.250/24"
    assert np_.pick_self("192.168.1.250/24") == "192.168.1.249/24"
    assert np_.pick_self("10.0.0.1/30") == "10.0.0.2/30"   # 小网段只剩一个能用
    assert np_.pick_self("192.168.0.233") == "192.168.0.250/24"   # 不写前缀按 /24


def test_pick_self_rejects_bad_input():
    for bad in ("192.168.1.0/24", "192.168.1.255/24", "10.0.0.1/31", "abc", "300.1.1.1/24"):
        with pytest.raises(ValueError):
            np_.pick_self(bad)


def test_templates_are_valid_peers():
    for label, peer in np_.STATIC_TEMPLATES:
        assert np_.check_label(label) is None
        np_.pick_self(peer)


# ------------------------------------------------------------ nmcli 命令

def _opt(cmd, key):
    return cmd[cmd.index(key) + 1]


def test_cmd_create_dhcp():
    cmd = np_.cmd_create("eno1", "dhcp", "临时")
    assert cmd[:5] == ["nmcli", "con", "add", "type", "ethernet"]
    assert _opt(cmd, "ifname") == "eno1" and _opt(cmd, "con-name") == "cart-eno1-dhcp-临时"
    assert _opt(cmd, "ipv4.method") == "auto"
    assert _opt(cmd, "connection.autoconnect") == "no"     # 配置文件只手动切, 开机不乱跳


def test_cmd_create_static_never_steals_default_route():
    cmd = np_.cmd_create("enp3s0", "static", "华为交换机", addr="192.168.1.250/24")
    assert _opt(cmd, "ipv4.method") == "manual"
    assert _opt(cmd, "ipv4.addresses") == "192.168.1.250/24"
    assert _opt(cmd, "ipv4.never-default") == "yes"
    assert "ipv4.gateway" not in cmd


def test_cmd_create_refuses_bad_combos():
    with pytest.raises(ValueError):
        np_.cmd_create("enp3s0", "serve", "x", addr="10.0.0.1/24")
    with pytest.raises(ValueError):
        np_.cmd_create("eno1", "static", "x")              # 静态没给地址


def test_plan_switch_to_non_serve_on_lan1_stops_pxe():
    cur = {"uuid": "a737-4", "iface": "eno1", "kind": "serve"}
    new = {"uuid": "cccc-6", "iface": "eno1", "kind": "dhcp"}
    plan = np_.plan_switch(cur, new)
    assert plan["stop_pxe"] is True and plan["probe"] is False
    assert plan["up"] == ["nmcli", "con", "up", "uuid", "cccc-6"]
    assert plan["rollback"] == ["nmcli", "con", "up", "uuid", "a737-4"]


def test_plan_switch_to_serve_needs_probe():
    plan = np_.plan_switch(None, {"uuid": "a737-4", "iface": "eno1", "kind": "serve"})
    assert plan["probe"] is True and plan["stop_pxe"] is False
    assert plan["rollback"] is None


def test_plan_switch_lan2_warns_remote():
    plan = np_.plan_switch(None, {"uuid": "bbbb-5", "iface": "enp3s0", "kind": "static"})
    assert plan["warn_remote"] is True and plan["stop_pxe"] is False


# ------------------------------------------------------------ DHCP 探测报文

MAC = bytes.fromhex("241c041768a2")


def test_build_discover():
    pkt = np_.build_discover(MAC, 0x12345678)
    op, htype, hlen, _, xid, _, flags = struct.unpack("!BBBBIHH", pkt[:12])
    assert (op, htype, hlen, xid, flags) == (1, 1, 6, 0x12345678, 0x8000)   # 广播回应
    assert pkt[28:34] == MAC
    assert pkt[236:240] == b"\x63\x82\x53\x63"
    assert pkt[240:243] == b"\x35\x01\x01"                # DHCPDISCOVER
    assert pkt.rstrip(b"\x00").endswith(b"\xff")


def _offer(xid, msg_type=2, server=b"\xc0\xa8\x09\x01", yiaddr=b"\xc0\xa8\x09\x63"):
    head = struct.pack("!BBBBIHH", 2, 1, 6, 0, xid, 0, 0x8000)
    body = head + b"\x00" * 4 + yiaddr + b"\x00" * 8 + MAC + b"\x00" * 10 + b"\x00" * 192
    opts = b"\x63\x82\x53\x63" + bytes([53, 1, msg_type]) + b"\x36\x04" + server + b"\xff"
    return body + opts


def test_parse_offer():
    assert np_.parse_offer(_offer(7), 7) == {"server": "192.168.99.1", "yiaddr": "192.168.99.99"}


def test_parse_offer_ignores_wrong_xid_type_and_garbage():
    assert np_.parse_offer(_offer(7), 8) is None
    assert np_.parse_offer(_offer(7, msg_type=5), 7) is None    # ACK 不算
    assert np_.parse_offer(b"\x02" * 50, 7) is None
    assert np_.parse_offer(np_.build_discover(MAC, 7), 7) is None   # 自己发的请求
