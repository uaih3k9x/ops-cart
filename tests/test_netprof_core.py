"""netprof_core 测试: 网口配置文件的命名/解析/规则/命令/DHCP 探测报文, 不碰真机."""

import struct

import pytest

import netprof_core as np_

CONS = "\n".join([
    "campus-net:6c4d-1:802-11-wireless:wlp4s0:yes",
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


def _offer(xid, msg_type=2, server=b"\xc0\x00\x02\x01", yiaddr=b"\xc0\x00\x02\x63"):
    head = struct.pack("!BBBBIHH", 2, 1, 6, 0, xid, 0, 0x8000)
    body = head + b"\x00" * 4 + yiaddr + b"\x00" * 8 + MAC + b"\x00" * 10 + b"\x00" * 192
    opts = b"\x63\x82\x53\x63" + bytes([53, 1, msg_type]) + b"\x36\x04" + server + b"\xff"
    return body + opts


def test_parse_offer():
    assert np_.parse_offer(_offer(7), 7) == {"server": "192.0.2.1", "yiaddr": "192.0.2.99"}


def test_parse_offer_ignores_wrong_xid_type_and_garbage():
    assert np_.parse_offer(_offer(7), 8) is None
    assert np_.parse_offer(_offer(7, msg_type=5), 7) is None    # ACK 不算
    assert np_.parse_offer(b"\x02" * 50, 7) is None
    assert np_.parse_offer(np_.build_discover(MAC, 7), 7) is None   # 自己发的请求


# ------------------------------------------------------------ 探测前先看有没有插线 (2026-10-25)
# 没插线时探测不到任何 OFFER, 以前会被当成"对面没有 DHCP"放行; 现在返回"未知"

def test_carrier_reads_sysfs(tmp_path):
    (tmp_path / "eno1").mkdir()
    (tmp_path / "eno1" / "carrier").write_text("1\n")
    (tmp_path / "enp3s0").mkdir()
    (tmp_path / "enp3s0" / "carrier").write_text("0\n")
    assert np_.carrier("eno1", root=str(tmp_path)) is True
    assert np_.carrier("enp3s0", root=str(tmp_path)) is False
    assert np_.carrier("nope", root=str(tmp_path)) is None              # 读不到 = 不知道


@pytest.mark.parametrize("link", [False, None])
def test_dhcp_probe_without_carrier_is_unknown_and_sends_nothing(monkeypatch, link):
    monkeypatch.setattr(np_.socket, "socket", lambda *a: pytest.fail("没插线不该开 socket"))
    res = np_.dhcp_probe("eno1", carrier=lambda iface: link)
    assert res == [] and not res and res.unknown and res.carrier is link
    assert isinstance(res, list)                                         # 形状兼容: 还是 list


class _FakeSock:
    def __init__(self, replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        pass

    def sendto(self, data, addr):
        self.sent.append(data)

    def recv(self, n):
        return self.replies.pop(0)

    def close(self):
        self.closed = True


def test_dhcp_probe_with_carrier_collects_offers(monkeypatch):
    fake = {}

    def make(*a):
        xid = 0x11223344
        fake["s"] = _FakeSock([_offer(xid)])
        return fake["s"]
    monkeypatch.setattr(np_.socket, "socket", make)
    monkeypatch.setattr(np_.os, "urandom", lambda n: b"\x11\x22\x33\x44")
    monkeypatch.setattr(np_, "iface_mac", lambda iface: MAC)
    calls = iter([True, False])
    monkeypatch.setattr(np_.select, "select", lambda r, w, x, t: (r if next(calls) else [], [], []))
    res = np_.dhcp_probe("eno1", timeout=1, carrier=lambda iface: True)
    assert res == [{"server": "192.0.2.1", "yiaddr": "192.0.2.99"}]
    assert not res.unknown and res.carrier is True and fake["s"].closed


# ------------------------------------------------------- LAN2 单一口径 (#8)

NICS = [{"name": "eno1", "con": "Lan1-pxe", "role": "PXE 装机口", "ip": "-",
         "method": "auto", "link": False},
        {"name": "enp3s0", "con": "enp3s0", "role": "管理口", "ip": "192.0.2.5/24",
         "method": "auto", "link": True}]

# 只有内置 enp3s0 在管这个口
CONS_BUILTIN_ONLY = "\n".join([
    "enp3s0:1603-3:802-3-ethernet::yes",
    "Lan1-pxe:a737-4:802-3-ethernet:eno1:yes",
])


def test_lan2_view_builtin_only_has_no_conflict():
    v = np_.lan2_view("enp3s0", CONS_BUILTIN_ONLY, NICS)
    assert v["ip"] == "192.0.2.5/24" and v["method"] == "auto"
    assert v["builtin"] == "enp3s0"
    assert v["named"] == []
    assert v["active"] == "enp3s0"
    assert v["entries"] == ["LAN2 管理口配置"]
    assert v["conflict"] is False
    assert v["warning"] == ""


def test_lan2_view_flags_two_entries_on_same_iface():
    """命名配置一出现, 两个入口就能同时改这个口了 —— kiosk 改内置, 网口面板改命名."""
    v = np_.lan2_view("enp3s0", CONS, NICS)
    assert v["named"] == ["cart-enp3s0-static-华为交换机"]
    assert v["entries"] == ["LAN2 管理口配置", "网口配置文件"]
    assert v["conflict"] is True
    assert "两个入口" in v["warning"]


def test_lan2_view_missing_nic_is_unknown():
    v = np_.lan2_view("enp3s0", CONS, [])
    assert v["ip"] == "-" and v["method"] == "-"
    assert v["conflict"] is True          # 读不到实时状态不影响"有两个入口"这个事实


def test_cmd_create_static_requires_confirmation():
    """#8: 静态地址只是建议值, 没人确认前不许写 NetworkManager."""
    with pytest.raises(ValueError):
        np_.cmd_create("enp3s0", "static", "x", addr="192.168.1.250/24", confirmed=False)
    cmd = np_.cmd_create("enp3s0", "static", "x", addr="192.168.1.250/24", confirmed=True)
    assert "ipv4.addresses" in cmd


def test_cmd_create_dhcp_ignores_confirmation():
    assert np_.cmd_create("enp3s0", "dhcp", "x", confirmed=False)[-2:] == ["ipv4.method", "auto"]
