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


# ------------------------------------------------- 静态地址人填 + 校验 (#8)

@pytest.mark.parametrize("text, want", [
    ("192.0.2.5/24", "192.0.2.5/24"),
    (" 198.51.100.6/30 ", "198.51.100.6/30"),
    ("192.0.2.1/32", "192.0.2.1/32"),          # /32 单主机, 没有网络号/广播之说
    ("192.0.2.0/31", "192.0.2.0/31"),          # /31 点对点, 两个地址都能用
    ("192.0.2.5/1", "192.0.2.5/1"),
])
def test_static_form_accepts(text, want):
    assert np_.static_form(text)["addr"] == want


@pytest.mark.parametrize("text, why", [
    ("", "不能为空"),
    ("192.0.2.5", "前缀"),                     # 必须人写明前缀, 不替人猜 /24
    ("192.0.2.5/0", "1–32"),
    ("192.0.2.5/33", "1–32"),
    ("192.0.2.5/abc", "1–32"),
    ("192.0.2.5/255.255.255.0", "1–32"),       # 只认数字前缀, 免得两种写法混着来
    ("192.0.2.256/24", "地址不对"),
    ("2001:db8::1/64", "IPv4"),
    ("192.0.2.0/24", "网络号"),
    ("192.0.2.255/24", "广播"),
    ("127.0.0.1/8", "不能用"),
    ("0.0.0.0/8", "不能用"),
    ("224.0.0.5/24", "不能用"),               # public-safe-ignore  组播, 不是真实主机
    ("240.0.0.1/24", "不能用"),
])
def test_static_form_rejects(text, why):
    with pytest.raises(ValueError, match=why):
        np_.static_form(text)


def test_static_form_must_not_take_peer_address():
    """人手填最容易撞的就是对端设备自己的地址."""
    with pytest.raises(ValueError, match="对端"):
        np_.static_form("192.0.2.1/24", avoid="192.0.2.1/24")
    assert np_.static_form("192.0.2.250/24", avoid="192.0.2.1")["addr"] == "192.0.2.250/24"


def test_static_form_gateway_and_dns_optional_but_checked():
    f = np_.static_form("192.0.2.5/24", gateway=" 192.0.2.254 ", dns="198.51.100.53, 198.51.100.54")
    assert f == {"addr": "192.0.2.5/24", "gateway": "192.0.2.254", "dns": "198.51.100.53,198.51.100.54"}
    assert np_.static_form("192.0.2.5/24", gateway="", dns="") == {
        "addr": "192.0.2.5/24", "gateway": "", "dns": ""}
    with pytest.raises(ValueError, match="网关.*网段"):
        np_.static_form("192.0.2.5/24", gateway="198.51.100.1")
    with pytest.raises(ValueError, match="网关.*自己"):
        np_.static_form("192.0.2.5/24", gateway="192.0.2.5")
    with pytest.raises(ValueError, match="网关"):
        np_.static_form("192.0.2.5/24", gateway="192.0.2.255")
    with pytest.raises(ValueError, match="DNS"):
        np_.static_form("192.0.2.5/24", dns="198.51.100.x")


# ------------------------------------------------- 确认框写明回滚方式 (#8)

CUR = {"name": "cart-enp3s0-static-华为交换机", "uuid": "bbbb-5", "iface": "enp3s0",
       "kind": "static", "label": "华为交换机", "active": True, "builtin": False}
NEW = {"name": "enp3s0", "uuid": "1603-3", "iface": "enp3s0", "kind": "dhcp",
       "label": "默认", "active": False, "builtin": True}


def test_plan_switch_rollback_text_names_old_profile_and_command():
    plan = np_.plan_switch(CUR, NEW)
    t = plan["rollback_text"]
    assert "华为交换机" in t and "回退上一个" in t
    assert "nmcli con up id 'cart-enp3s0-static-华为交换机'" in t     # 车边终端能直接敲的
    assert "自动" in t                                                  # 切换失败会自动回去


def test_plan_switch_rollback_text_without_previous():
    t = np_.plan_switch(None, NEW)["rollback_text"]
    assert "没有生效配置" in t and "nmcli dev disconnect enp3s0" in t


def test_rollback_text_for_create_uses_current_profile():
    assert np_.rollback_text(CUR, "enp3s0") == np_.plan_switch(CUR, NEW)["rollback_text"]


IPV4_MANUAL = "manual\n192.0.2.5/24\n192.0.2.254\n198.51.100.53\n"
IPV4_AUTO = "auto\n\n\n\n"


def test_parse_ipv4_settings():
    assert np_.parse_ipv4(IPV4_MANUAL) == {"method": "manual", "addr": "192.0.2.5/24",
                                           "gateway": "192.0.2.254", "dns": "198.51.100.53"}
    assert np_.parse_ipv4(IPV4_AUTO) == {"method": "auto", "addr": "", "gateway": "", "dns": ""}
    assert np_.parse_ipv4("auto") == np_.parse_ipv4(IPV4_AUTO)   # kiosk_core.run 会 strip 掉末尾空行
    assert np_.parse_ipv4("") is None                     # 读不到 = 不知道原来是什么
    assert np_.cmd_ipv4("enp3s0") == ["nmcli", "-g", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                                      "con", "show", "enp3s0"]


def test_lan2_rollback_restores_previous_static():
    rb = np_.lan2_rollback("enp3s0", np_.parse_ipv4(IPV4_MANUAL))
    assert rb["cmds"] == [
        ["nmcli", "con", "mod", "enp3s0", "ipv4.method", "manual", "ipv4.addresses", "192.0.2.5/24",
         "ipv4.gateway", "192.0.2.254", "ipv4.dns", "198.51.100.53"],
        ["nmcli", "con", "up", "enp3s0"]]
    assert "静态 192.0.2.5/24" in rb["text"]
    assert "LAN2 管理口配置" in rb["text"]                                # 人话: 从哪儿改回去
    assert "nmcli con mod enp3s0 ipv4.method manual ipv4.addresses 192.0.2.5/24" in rb["text"]


def test_lan2_rollback_restores_previous_dhcp_with_empty_fields_quoted():
    rb = np_.lan2_rollback("enp3s0", np_.parse_ipv4(IPV4_AUTO))
    assert rb["cmds"][0][4:6] == ["ipv4.method", "auto"]
    assert "DHCP" in rb["text"]
    assert "ipv4.addresses '' ipv4.gateway '' ipv4.dns ''" in rb["text"]  # 空值要带引号才能照抄


def test_lan2_rollback_unknown_before():
    rb = np_.lan2_rollback("enp3s0", None)
    assert rb["cmds"] is None
    assert "读不到" in rb["text"] and "nmcli con show enp3s0" in rb["text"]


def test_lan2_confirm_text_shows_change_and_rollback():
    before = np_.parse_ipv4(IPV4_AUTO)
    after = {"method": "manual", "addr": "198.51.100.7/24", "gateway": "", "dns": ""}
    t = np_.lan2_confirm_text("enp3s0", before, after)
    assert "原来: DHCP" in t and "改为: 静态 198.51.100.7/24" in t
    assert "出问题怎么回滚" in t and "nmcli con up enp3s0" in t


# ------------------------------------------------- LAN1/LAN2 切换统一确认框 (#8)

L1_SERVE = {"name": "Lan1-pxe", "uuid": "a737-4", "iface": "eno1", "kind": "serve",
            "label": "装机", "active": True, "builtin": True}
L1_STATIC = {"name": "cart-eno1-static-临时", "uuid": "cccc-6", "iface": "eno1", "kind": "static",
             "label": "临时", "active": False, "builtin": False}
L1_DHCP = {"name": "cart-eno1-dhcp-临时", "uuid": "dddd-7", "iface": "eno1", "kind": "dhcp",
           "label": "临时", "active": False, "builtin": False}


def test_parse_ip_br():
    text = "eno1             UP             192.0.2.5/24 \nenp3s0           DOWN           \n"
    assert np_.parse_ip_br(text) == {"eno1": "192.0.2.5/24"}
    assert np_.parse_ip_br("") == {}
    assert np_.CMD_IP_BR == ["ip", "-4", "-br", "addr", "show"]


def test_switch_confirm_lan1_shows_before_after_and_rollback():
    plan = np_.plan_switch(L1_SERVE, L1_STATIC)
    t = np_.switch_confirm_text(L1_SERVE, L1_STATIC, plan, ip="192.0.2.5/24")
    assert "LAN1 (eno1)" in t
    assert "原来: 装机 · 发地址 / PXE · 192.0.2.5/24" in t
    assert "改为: 临时 · 静态地址" in t
    assert plan["rollback_text"] in t and "nmcli con up id Lan1-pxe" in t
    assert "先停 PXE" in t                         # LAN1 切离发地址会停服务, 事先说
    assert "管理口" not in t and "探测" not in t
    assert t.rstrip().endswith("要继续吗?")


def test_switch_confirm_lan1_without_previous_or_ip():
    t = np_.switch_confirm_text(None, L1_DHCP, np_.plan_switch(None, L1_DHCP))
    assert "原来: 没有生效配置 · 当前地址 -" in t
    assert "nmcli dev disconnect eno1" in t


def test_switch_confirm_serve_adds_probe_and_proxy_note():
    plan = np_.plan_switch(L1_DHCP, L1_SERVE)
    t = np_.switch_confirm_text(L1_DHCP, L1_SERVE, plan, ip="198.51.100.9/24")
    assert "探测" in t and "DHCP" in t
    assert "代理" in t and "不发地址" in t
    assert "先停 PXE" not in t


def test_switch_confirm_lan2_keeps_remote_warning():
    plan = np_.plan_switch(CUR, NEW)
    t = np_.switch_confirm_text(CUR, NEW, plan, ip="192.0.2.5/24")
    assert "LAN2 (enp3s0)" in t and "远程连接会断" in t
    assert plan["rollback_text"] in t
