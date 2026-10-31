"""netprof_core 测试: 认旧连接名、静态地址预填/校验、读地址、DHCP 探测报文, 不碰真机."""

import struct

import pytest

import netprof_core as np_


# ------------------------------------------------------------ 旧连接名 (网口模式面板列成"旧配置", 只认不删)

def test_parse_name_old_panel_profiles():
    assert np_.parse_name("cart-enp3s0-static-H3C 交换机") == \
        {"iface": "enp3s0", "kind": "static", "label": "H3C 交换机"}
    assert np_.parse_name("cart-enp3s0-static") is None         # 网口模式自己建的 (三段) 不算旧配置


def test_parse_name_builtins_and_unknown():
    assert np_.parse_name("enp3s0") == {"iface": "enp3s0", "kind": "dhcp", "label": "默认"}
    assert np_.parse_name("Lan1-pxe") == {"iface": "eno1", "kind": "serve", "label": "装机"}
    assert np_.parse_name("随便谁建的") is None
    assert np_.parse_name("cart-wlp4s0-dhcp-x") is None        # 无线不归这里管
    assert np_.parse_name("cart-eno1-bogus-x") is None


# ------------------------------------------------------------ 静态地址

def test_pick_self_avoids_peer():
    assert np_.pick_self("192.168.1.253/24") == "192.168.1.250/24"
    assert np_.pick_self("192.168.1.250/24") == "192.168.1.249/24"
    assert np_.pick_self("10.0.0.1/30") == "10.0.0.2/30"   # 小网段只剩一个能用


@pytest.mark.parametrize("peer, want", [
    ("172.16.5.1/16", "172.16.0.250/16"),        # 非 /24: 网段和边界按人写的前缀算 (#13)
    ("192.0.2.9/23", "192.0.2.250/23"),
    ("192.0.2.130/25", "192.0.2.254/25"),      # 上半段: 网络号 .128 + 126
    ("192.0.2.1/25", "192.0.2.126/25"),          # /25 下半段, 不能越过广播
    ("192.0.2.5/30", "192.0.2.6/30"),
])
def test_pick_self_follows_given_prefix(peer, want):
    assert np_.pick_self(peer) == want


def test_pick_self_needs_prefix_never_guesses_24():
    """#13: 对端不带前缀直接报错, 不再默认 /24."""
    with pytest.raises(ValueError, match="前缀"):
        np_.pick_self("192.168.0.233")
    with pytest.raises(ValueError, match="前缀"):
        np_.pick_self("")


def test_pick_self_rejects_bad_input():
    for bad in ("192.168.1.0/24", "192.168.1.255/24", "10.0.0.1/31", "abc", "300.1.1.1/24"):
        with pytest.raises(ValueError):
            np_.pick_self(bad)


def test_templates_are_valid_peers():
    for _label, peer in np_.STATIC_TEMPLATES:
        np_.pick_self(peer)


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


# ------------------------------------------------- 读地址

def test_parse_ip_br():
    text = "eno1             UP             192.0.2.5/24 \nenp3s0           DOWN           \n"
    assert np_.parse_ip_br(text) == {"eno1": "192.0.2.5/24"}
    assert np_.parse_ip_br("") == {}
    assert np_.CMD_IP_BR == ["ip", "-4", "-br", "addr", "show"]
