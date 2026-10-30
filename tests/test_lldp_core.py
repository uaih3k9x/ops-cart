"""lldp_core 测试: 帧都是测试里现拼的字节, 设备名/地址全是示例 (SW-A / 192.0.2.x / 02:.. 本地 MAC)."""

import random
import socket
import struct

import pytest

import lldp_core as lc

SRC = b"\x02\x00\x00\x00\x00\x01"
SW_MAC = b"\x02\x00\x00\x00\x00\xaa"


# ---- 拼帧小工具

def tlv(t, v):
    return struct.pack("!H", (t << 9) | len(v)) + v


def chassis(sub, val):
    return tlv(1, bytes([sub]) + val)


def port(sub, val):
    return tlv(2, bytes([sub]) + val)


def ttl(n):
    return tlv(3, struct.pack("!H", n))


def mgmt(ip):
    if ":" in ip:
        addr = bytes([2]) + socket.inet_pton(socket.AF_INET6, ip)
    else:
        addr = bytes([1]) + socket.inet_aton(ip)
    return tlv(8, bytes([len(addr)]) + addr + bytes([2]) + struct.pack("!I", 5) + b"\x00")


def pvid(v):
    return tlv(127, b"\x00\x80\xc2\x01" + struct.pack("!H", v))


def end():
    return tlv(0, b"")


def eth(payload, dst=lc.LLDP_MAC, etype=lc.ETH_LLDP, vlan=None):
    tag = struct.pack("!HH", 0x8100, vlan) if vlan is not None else b""
    return dst + SRC + tag + struct.pack("!H", etype) + payload


def huawei():
    """华为风格: 机箱 MAC, 端口按接口名, 带端口描述/系统名/描述/管理地址/PVID, 外加 802.3 的 TLV."""
    return eth(chassis(4, SW_MAC) + port(5, b"GigabitEthernet0/0/1") + ttl(120)
               + tlv(4, b"GigabitEthernet0/0/1") + tlv(5, b"SW-A")
               + tlv(6, b"Versatile Routing Platform") + tlv(7, b"\x00\x14\x00\x04")
               + mgmt("192.0.2.1") + tlv(127, b"\x00\x12\x0f\x01\x03\x6c\x00\x00\x1e")
               + pvid(10) + end())


def cisco():
    """思科风格: 端口 ID 是短名 Gi1/0/1, 描述是长名; 系统名带域名."""
    return eth(chassis(4, SW_MAC) + port(5, b"Gi1/0/1") + ttl(120)
               + tlv(4, b"GigabitEthernet1/0/1") + tlv(5, b"SW-B.example.com")
               + tlv(7, b"\x00\x14\x00\x04") + mgmt("192.0.2.2") + pvid(20) + end())


def h3c():
    """H3C 风格: 端口 ID 用 MAC 子类型, 名字在端口描述里; 没有 VLAN TLV."""
    return eth(chassis(4, SW_MAC) + port(3, b"\x02\x00\x00\x00\x00\xbb") + ttl(121)
               + tlv(4, b"Ten-GigabitEthernet1/0/49") + tlv(5, b"SW-C")
               + mgmt("2001:db8::1") + end())


# ---- 解析

def test_huawei_style():
    nb = lc.parse_frame(huawei())
    assert nb["proto"] == "lldp" and nb["src"] == "02:00:00:00:00:01"
    assert nb["chassis_id"] == "02:00:00:00:00:aa" and nb["chassis_subtype"] == 4
    assert nb["port_id"] == "GigabitEthernet0/0/1" and nb["port_subtype"] == 5
    assert nb["ttl"] == 120 and nb["sys_name"] == "SW-A"
    assert nb["port_desc"] == "GigabitEthernet0/0/1"
    assert nb["mgmt"] == ["192.0.2.1"] and nb["vlan"] == 10


def test_cisco_style_short_port_id():
    nb = lc.parse_frame(cisco())
    assert nb["port_id"] == "Gi1/0/1" and nb["port_desc"] == "GigabitEthernet1/0/1"
    assert nb["sys_name"] == "SW-B.example.com" and nb["vlan"] == 20
    assert lc.port_name(nb) == "Gi1/0/1" and lc.device_name(nb) == "SW-B.example.com"


def test_h3c_style_mac_port_id_falls_back_to_description():
    nb = lc.parse_frame(h3c())
    assert nb["port_id"] == "02:00:00:00:00:bb" and nb["port_subtype"] == 3
    assert nb["vlan"] is None and nb["mgmt"] == ["2001:db8::1"]
    assert lc.port_name(nb) == "Ten-GigabitEthernet1/0/49"     # MAC 当口名没法看, 用描述


def test_only_mandatory_tlvs():
    nb = lc.parse_frame(eth(chassis(7, b"box-1") + port(7, b"12") + ttl(90)))   # 没 End 也收
    assert nb["chassis_id"] == "box-1" and nb["port_id"] == "12"
    assert nb["sys_name"] is None and nb["port_desc"] is None and nb["mgmt"] == []
    assert lc.device_name(nb) == "box-1" and lc.port_name(nb) == "12"


def test_network_address_chassis_and_802_1q_tagged_frame():
    payload = chassis(5, b"\x01" + socket.inet_aton("192.0.2.9")) + port(5, b"ge-0/0/1") + ttl(30)
    nb = lc.parse_frame(eth(payload, vlan=10))
    assert nb["chassis_id"] == "192.0.2.9"


def test_strips_trailing_nul_and_spaces():
    nb = lc.parse_frame(eth(chassis(4, SW_MAC) + port(5, b"Gi0/1\x00") + ttl(120)
                            + tlv(5, b"SW-A \x00") + end()))
    assert nb["port_id"] == "Gi0/1" and nb["sys_name"] == "SW-A"


def test_wrong_destination_or_ethertype_is_not_lldp():
    assert lc.parse_frame(eth(chassis(4, SW_MAC) + port(5, b"x") + ttl(1),
                              dst=b"\xff" * 6)) is None
    assert lc.parse_frame(eth(chassis(4, SW_MAC) + port(5, b"x") + ttl(1), etype=0x0800)) is None


@pytest.mark.parametrize("payload", [
    b"",                                                    # 空
    chassis(4, SW_MAC),                                     # 缺 Port ID / TTL
    port(5, b"Gi0/1") + chassis(4, SW_MAC) + ttl(120),      # 顺序错: 第一个必须是 Chassis ID
    chassis(4, SW_MAC) + port(5, b"Gi0/1"),                 # 缺 TTL
    chassis(4, SW_MAC) + port(5, b"Gi0/1") + tlv(3, b"\x00"),   # TTL 长度不对
    tlv(1, b"") + port(5, b"Gi0/1") + ttl(120),             # Chassis ID 空 (连子类型都没有)
    chassis(4, SW_MAC) + port(5, b"Gi0/1") + ttl(120)[:3],  # TLV 截断
    chassis(4, SW_MAC) + b"\x04\xff" + b"x" * 4,            # 长度越界
])
def test_malformed_returns_none(payload):
    assert lc.parse_frame(eth(payload)) is None
    with pytest.raises(ValueError):
        lc.parse_lldpdu(payload)


def test_malformed_optional_tlvs_are_ignored():
    bad_mgmt = tlv(8, b"\x09\x01\xc0")                      # 地址长度说 9, 实际没那么多
    bad_org = tlv(127, b"\x00\x80")                         # OUI 都不完整
    bad_pvid = tlv(127, b"\x00\x80\xc2\x01\x00")            # PVID 只有 1 字节
    nb = lc.parse_frame(eth(chassis(4, SW_MAC) + port(5, b"Gi0/1") + ttl(120)
                            + bad_mgmt + bad_org + bad_pvid + tlv(5, b"SW-A") + end()))
    assert nb["sys_name"] == "SW-A" and nb["mgmt"] == [] and nb["vlan"] is None


def test_never_raises_on_truncation_or_garbage():
    good = huawei()
    for i in range(len(good)):
        lc.parse_frame(good[:i])                            # 截在任何位置都不炸
    rnd = random.Random(14)
    for _ in range(500):
        junk = bytearray(good)
        for _ in range(rnd.randint(1, 8)):
            junk[rnd.randrange(14, len(junk))] = rnd.randrange(256)
        nb = lc.parse_frame(bytes(junk))
        assert nb is None or isinstance(nb, dict)
        lc.parse_frame(bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 80))))


# ---- CDP (思科老设备默认只开 CDP)

def cdp_tlv(t, v):
    return struct.pack("!HH", t, len(v) + 4) + v


def cdp_frame(*tlvs):
    body = b"\x02\xb4\x00\x00" + b"".join(tlvs)             # 版本 2, TTL 180, 校验和不查
    llc = b"\xaa\xaa\x03\x00\x00\x0c\x20\x00" + body
    return lc.CDP_MAC + SRC + struct.pack("!H", len(llc)) + llc


def cdp_addr(ip):
    one = b"\x01\x01\xcc" + struct.pack("!H", 4) + socket.inet_aton(ip)
    return cdp_tlv(2, struct.pack("!I", 1) + one)


def test_cdp_frame():
    nb = lc.parse_frame(cdp_frame(cdp_tlv(1, b"SW-D"), cdp_addr("192.0.2.4"),
                                  cdp_tlv(3, b"FastEthernet0/3"), cdp_tlv(6, b"cisco WS-C2960"),
                                  cdp_tlv(10, b"\x00\x1e")))
    assert nb["proto"] == "cdp" and nb["sys_name"] == "SW-D" and nb["chassis_id"] == "SW-D"
    assert nb["port_id"] == "FastEthernet0/3" and nb["ttl"] == 180
    assert nb["mgmt"] == ["192.0.2.4"] and nb["vlan"] == 30


def test_cdp_without_device_or_port_is_none():
    assert lc.parse_frame(cdp_frame(cdp_tlv(3, b"Fa0/3"))) is None
    assert lc.parse_frame(cdp_frame(cdp_tlv(1, b"SW-D"), b"\x00\x03\x00\x40ab")) is None   # 越界


def test_cdp_never_raises_on_truncation():
    good = cdp_frame(cdp_tlv(1, b"SW-D"), cdp_addr("192.0.2.4"), cdp_tlv(3, b"Fa0/3"))
    for i in range(len(good)):
        lc.parse_frame(good[:i])


# ---- 邻居表: 按网口记最近一次邻居, 超过 TTL 变旧, 再久就忘掉 (注入时钟)

IFACES = [("LAN1", "eth-a"), ("LAN2", "eth-b")]


def _nb(name="SW-A", port_id="Gi0/1", hold=120):
    return lc.parse_frame(eth(chassis(4, SW_MAC) + port(5, port_id.encode()) + ttl(hold)
                              + tlv(5, name.encode()) + end()))


def test_entry_state_by_ttl():
    e = {"neighbor": _nb(hold=120), "at": 1000.0}
    assert lc.entry_state(None, 1000.0) == "none"
    assert lc.entry_state(e, 1000.0) == "fresh"
    assert lc.entry_state(e, 1120.0) == "fresh"
    assert lc.entry_state(e, 1121.0) == "stale"
    assert lc.entry_state(e, 1120.0 + lc.FORGET_S + 1) == "none"


def test_table_fresh_then_stale_then_forgotten():
    t = lc.Neighbors(IFACES)
    t.seen("eth-a", _nb(), 1000.0)
    s = t.snapshot(1010.0)
    assert s["LAN1"]["state"] == "fresh" and s["LAN1"]["age"] == 10
    assert s["LAN1"]["neighbor"]["sys_name"] == "SW-A" and s["LAN1"]["iface"] == "eth-a"
    assert s["LAN2"] == {"iface": "eth-b", "state": "none", "age": None, "neighbor": None, "error": ""}
    assert t.snapshot(1200.0)["LAN1"]["state"] == "stale"          # 拔线了: 旧邻居变灰
    gone = t.snapshot(1121.0 + lc.FORGET_S)["LAN1"]
    assert gone["state"] == "none" and gone["neighbor"] is None      # 不残留
    assert t.snapshot(1000.0)["LAN1"]["state"] == "none"             # 忘掉了就是忘掉了


def test_table_new_port_replaces_old_neighbor():
    t = lc.Neighbors(IFACES)
    t.seen("eth-a", _nb(port_id="Gi0/1"), 1000.0)
    t.seen("eth-a", _nb(port_id="Gi0/7"), 1030.0)                    # 换了口
    assert t.snapshot(1031.0)["LAN1"]["neighbor"]["port_id"] == "Gi0/7"


def test_table_ttl_zero_means_neighbor_left():
    t = lc.Neighbors(IFACES)
    t.seen("eth-a", _nb(), 1000.0)
    t.seen("eth-a", _nb(hold=0), 1001.0)                             # 交换机关 LLDP 时发的告别帧
    assert t.snapshot(1002.0)["LAN1"]["state"] == "none"


def test_table_unknown_iface_ignored_and_error_means_off():
    t = lc.Neighbors(IFACES)
    t.seen("wlan0", _nb(), 1000.0)
    t.error("eth-b", "没有权限 (要 root)")
    s = t.snapshot(1000.0)
    assert set(s) == {"LAN1", "LAN2"}
    assert s["LAN2"]["state"] == "off" and "root" in s["LAN2"]["error"]
    t.error("eth-b", "")
    assert t.snapshot(1000.0)["LAN2"]["state"] == "none"


def test_snapshot_is_json():
    import json
    t = lc.Neighbors(IFACES)
    t.seen("eth-a", _nb(), 1000.0)
    assert json.loads(json.dumps(t.snapshot(1000.0)))["LAN1"]["neighbor"]["port_id"] == "Gi0/1"


# ---- 监听: 假套接字, 只收不发

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSock:
    """只有收的方法; 监听器要是调了 send/sendto 会直接 AttributeError."""

    def __init__(self, frames=(), fail=None):
        self.frames, self.fail, self.closed = list(frames), fail, False

    def fileno(self):
        return 99

    def recvfrom(self, n):
        if self.fail:
            raise self.fail
        if not self.frames:
            raise BlockingIOError()
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def _all_ready(r, w, x, timeout):
    return list(r), [], []


def _addr(iface, pkttype=1):
    return (iface, lc.ETH_LLDP, pkttype, 1, SRC)


def test_watcher_reads_frames_into_table():
    sock = FakeSock([(huawei(), _addr("eth-a")), (b"junk", _addr("eth-a"))])
    w = lc.Watcher(IFACES, opener=lambda i: [sock] if i == "eth-a" else [], select_fn=_all_ready,
                   clock=Clock())
    w.poll(0)
    s = w.snapshot()
    assert s["LAN1"]["state"] == "fresh" and s["LAN1"]["neighbor"]["sys_name"] == "SW-A"
    assert s["LAN2"]["state"] == "none"


def test_watcher_ignores_outgoing_frames():
    sock = FakeSock([(huawei(), _addr("eth-a", pkttype=lc.PACKET_OUTGOING))])
    w = lc.Watcher(IFACES, opener=lambda i: [sock], select_fn=_all_ready, clock=Clock())
    w.poll(0)
    assert w.snapshot()["LAN1"]["state"] == "none"


def test_watcher_without_root_degrades_and_retries_slowly():
    clk, calls = Clock(), []

    def opener(iface):
        calls.append(iface)
        raise PermissionError(1, "Operation not permitted")
    w = lc.Watcher(IFACES, opener=opener, select_fn=_all_ready, clock=clk)
    w.poll(0)
    w.poll(0)
    s = w.snapshot()
    assert s["LAN1"]["state"] == "off" and "root" in s["LAN1"]["error"]
    assert len(calls) == 2                                  # 两个口各试一次, 不每轮都撞
    clk.t += lc.RETRY_S
    w.poll(0)
    assert len(calls) == 4


def test_watcher_missing_iface_and_broken_socket_do_not_raise():
    import errno
    clk = Clock()
    bad = FakeSock(fail=OSError(errno.ENETDOWN, "Network is down"))

    def opener(iface):
        if iface == "eth-b":
            raise OSError(errno.ENODEV, "No such device")
        return [bad]
    w = lc.Watcher(IFACES, opener=opener, select_fn=_all_ready, clock=clk)
    w.poll(0)
    s = w.snapshot()
    assert s["LAN2"]["state"] == "off" and "不存在" in s["LAN2"]["error"]
    assert bad.closed and s["LAN1"]["state"] == "off"       # 拔了 USB 网卡: 关掉, 稍后重开
    clk.t += lc.RETRY_S
    w.poll(0)                                               # 重开, 照样不抛


def test_watcher_select_failure_does_not_raise():
    def boom(r, w, x, t):
        raise OSError("bad fd")
    sock = FakeSock()
    w = lc.Watcher(IFACES, opener=lambda i: [sock], select_fn=boom, clock=Clock())
    w.poll(0)
    assert sock.closed


def test_watcher_thread_starts_and_stops():
    import time
    w = lc.Watcher(IFACES, opener=lambda i: [FakeSock([(huawei(), _addr(i))])],
                   select_fn=_all_ready)
    w.start()
    try:
        for _ in range(200):
            if w.snapshot()["LAN1"]["state"] == "fresh":
                break
            time.sleep(0.01)
        assert w.snapshot()["LAN1"]["state"] == "fresh"
    finally:
        w.close()


def test_real_opener_off_linux_is_unavailable(monkeypatch):
    monkeypatch.delattr(lc.socket, "AF_PACKET", raising=False)
    with pytest.raises(OSError):
        lc.open_socket("eth-a")
    w = lc.Watcher(IFACES)
    w.poll(0)
    assert w.snapshot()["LAN1"]["state"] == "off"


def test_module_never_sends():
    import os
    src = open(os.path.join(os.path.dirname(lc.__file__), "lldp_core.py"), encoding="utf-8").read()
    assert ".send(" not in src and ".sendto(" not in src and ".sendmsg(" not in src
