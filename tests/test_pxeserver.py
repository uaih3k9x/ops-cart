"""pxeserver 协议层测试: 红绿循环的绿侧. 全部本机回环高位端口, 不碰真接口."""

import errno
import io
import socket
import struct
import threading

import pytest

from pxeserver import (
    DhcpServer, TftpServer, arch_bootfile, build_dhcp_reply, encode_opt,
    parse_dhcp, tftp_safe_name,
)


def make_discover(mac=b"\xaa\xbb\xcc\xdd\xee\x01", arch=7, with_arch=True):
    msg = bytearray(240)
    msg[0] = 1  # BOOTREQUEST
    msg[1] = 1  # ethernet
    msg[2] = 6  # hlen
    msg[4:8] = b"\x99\x88\x77\x66"  # xid
    msg[28:34] = mac
    msg[236:240] = b"\x63\x82\x53\x63"
    opts = encode_opt(53, b"\x01")
    if with_arch:
        opts += encode_opt(93, struct.pack("!H", arch))
    return bytes(msg) + opts + b"\xff"


class FakeLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def exception(self, *a, **k): pass
    def error(self, *a, **k): pass


def dhcp_for_test():
    return DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.12",
                      "shimx64.efi", "pxelinux.0", FakeLog(), port=1167)


# ---- DHCP 报文层

def test_parse_dhcp_roundtrip():
    xid, mac, opts = parse_dhcp(make_discover())
    assert xid == b"\x99\x88\x77\x66"
    assert mac == b"\xaa\xbb\xcc\xdd\xee\x01"
    assert opts[53] == b"\x01"
    assert struct.unpack("!H", opts[93])[0] == 7


def test_parse_rejects_garbage():
    assert parse_dhcp(b"\x01" * 100) is None
    assert parse_dhcp(b"\x00" * 240) is None  # 无 magic cookie


def test_arch_bootfile_dispatch():
    assert arch_bootfile({93: struct.pack("!H", 7)}, "shim.efi", "pxelinux.0") == "shim.efi"
    assert arch_bootfile({93: struct.pack("!H", 9)}, "shim.efi", "pxelinux.0") == "shim.efi"
    assert arch_bootfile({93: struct.pack("!H", 0)}, "shim.efi", "pxelinux.0") == "pxelinux.0"
    assert arch_bootfile({}, "shim.efi", "pxelinux.0") == "shim.efi"


def test_offer_has_bootfile_and_next_server():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), ("127.0.0.1", 68))
    assert reply is not None
    _, _, opts = parse_dhcp(reply)
    assert opts[53] == b"\x02"  # OFFER
    assert opts[67] == b"shimx64.efi"
    assert socket.inet_ntoa(reply[20:24]) == "192.0.2.1"  # siaddr = TFTP


def make_ipxe_discover():
    """iPXE 自己发的 DHCP: option 77 (user class) = "iPXE"."""
    return make_discover()[:-1] + encode_opt(77, b"iPXE") + b"\xff"


def test_ipxe_client_gets_script_url():
    # GRUB 链式加载 iPXE 后, iPXE 再要一次地址: 给脚本 URL, 不然又拿 shim 绕回 GRUB
    d = dhcp_for_test()
    reply = d.handle(make_ipxe_discover(), ("127.0.0.1", 68))
    _, _, opts = parse_dhcp(reply)
    assert opts[67] == b"http://192.0.2.1/boot.ipxe"
    assert reply[108:236].split(b"\x00")[0] == b"http://192.0.2.1/boot.ipxe"


def test_ipxe_client_via_proxy_gets_script_url():
    d = dhcp_for_test()
    reply = d.handle_proxy(make_ipxe_discover(), ("192.0.2.10", 4011))
    _, _, opts = parse_dhcp(reply)
    assert opts[67] == b"http://192.0.2.1/boot.ipxe"


def test_firmware_pxe_still_gets_shim():
    d = dhcp_for_test()
    _, _, opts = parse_dhcp(d.handle(make_discover(), ("127.0.0.1", 68)))
    assert opts[67] == b"shimx64.efi"


def test_offer_has_option_60_pxeclient():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    _, _, opts = parse_dhcp(reply)
    assert opts[60] == b"PXEClient"


def test_request_ack_has_option_60():
    d = dhcp_for_test()
    d.handle(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"), None)
    req = bytes(bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))[:240] +
                encode_opt(53, b"\x03") + encode_opt(50, ("192.0.2.10",)) + encode_opt(54, ("192.0.2.1",)) + b"\xff")
    reply = d.handle(req, None)
    _, _, opts = parse_dhcp(reply)
    assert opts[60] == b"PXEClient"


def test_offer_subnet_mask_is_packed_ip():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    _, _, opts = parse_dhcp(reply)
    assert opts[1] == b"\xff\xff\xff\x00"  # 255.255.255.0, 必须是 4 字节
    assert opts[3] == socket.inet_aton("192.0.2.1")


def test_proxy_request_gets_ack_with_zero_yiaddr():
    d = dhcp_for_test()
    req = bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))
    req[12:16] = socket.inet_aton("192.0.2.10")  # ciaddr
    req = bytes(bytearray(req)[:240] + encode_opt(53, b"\x03") + b"\xff")
    reply = d.handle_proxy(req, ("192.0.2.10", 4011))
    _, _, opts = parse_dhcp(reply)
    assert opts[53] == b"\x05"  # ACK
    assert socket.inet_ntoa(reply[16:20]) == "0.0.0.0"  # yiaddr 不分 IP
    assert opts[60] == b"PXEClient"
    assert opts[67] == b"shimx64.efi"
    assert socket.inet_ntoa(reply[20:24]) == "192.0.2.1"  # siaddr TFTP


def test_proxy_discover_gets_offer():
    d = dhcp_for_test()
    reply = d.handle_proxy(make_discover(), ("192.0.2.10", 4011))
    _, _, opts = parse_dhcp(reply)
    assert opts[53] == b"\x02"  # OFFER
    assert socket.inet_ntoa(reply[16:20]) == "0.0.0.0"


def test_reply_bootfile_in_header_file_field():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    field = reply[108:236].split(b"\x00")[0]  # BOOTP file 字段
    assert field == b"shimx64.efi"


def test_offer_yiaddr_from_pool():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    ip = socket.inet_ntoa(reply[16:20])  # yiaddr
    assert ip.startswith("192.0.2.")
    assert 10 <= int(ip.rsplit(".", 1)[1]) <= 12


def test_same_mac_gets_same_ip():
    d = dhcp_for_test()
    r1 = d.handle(make_discover(mac=b"\x01\x02\x03\x04\x05\x06"), None)
    r2 = d.handle(make_discover(mac=b"\x01\x02\x03\x04\x05\x06"), None)
    assert r1[16:20] == r2[16:20]  # yiaddr


def test_pool_prefix_mismatch_rejected():
    with pytest.raises(ValueError):
        DhcpServer("192.0.2.1", "192.0.2.10", "198.51.100.20",
                   "shim.efi", "pxelinux.0", FakeLog())


def test_request_acks_offered_ip():
    d = dhcp_for_test()
    d.handle(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"), None)
    req = bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))
    opts = encode_opt(53, b"\x03") + encode_opt(50, ("192.0.2.10",)) + encode_opt(54, ("192.0.2.1",))
    req = bytes(bytearray(req[:240]) + opts + b"\xff")
    reply = d.handle(req, None)
    _, _, ropts = parse_dhcp(reply)
    assert ropts[53] == b"\x05"  # ACK
    assert socket.inet_ntoa(reply[16:20]) == "192.0.2.10"


def test_request_outside_pool_naks_when_we_are_selected():
    d = dhcp_for_test()
    req = bytes(bytearray(make_discover())[:240] + encode_opt(53, b"\x03") +
                encode_opt(50, ("198.51.100.99",)) + encode_opt(54, ("192.0.2.1",)) + b"\xff")
    reply = d.handle(req, None)
    assert parse_dhcp(reply)[2][53] == b"\x06"  # NAK: 客户端选的是我们, 地址又不对


def test_build_reply_preserves_xid_and_chaddr():
    msg = make_discover(mac=b"\xde\xad\xbe\xef\x00\x01")
    reply = build_dhcp_reply(msg, 2, "192.0.2.10", "192.0.2.1", [encode_opt(53, b"\x02")])
    assert reply[4:8] == msg[4:8]      # xid
    assert reply[28:34] == b"\xde\xad\xbe\xef\x00\x01"


# ---- TFTP 路径安全

def test_tftp_safe_name_rejects_traversal(tmp_path):
    with pytest.raises(PermissionError):
        tftp_safe_name(str(tmp_path), "../../etc/passwd")
    with pytest.raises(PermissionError):
        tftp_safe_name(str(tmp_path), "..\\..\\windows\\system32\\config\\sam")


def test_tftp_safe_name_accepts_plain(tmp_path):
    (tmp_path / "shimx64.efi").write_bytes(b"efi")
    assert tftp_safe_name(str(tmp_path), "shimx64.efi").endswith("shimx64.efi")


# ---- TFTP 传输 (真实回环 UDP)

def start_tftp(tmp_path, **kw):
    """临时端口: 别和并行跑的其他测试抢固定端口."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    srv = TftpServer("127.0.0.1", str(tmp_path), FakeLog(), port=sock.getsockname()[1], **kw)
    ev = threading.Event()
    threading.Thread(target=srv.serve, args=(ev, sock), daemon=True).start()
    assert ev.wait(3)
    return srv, srv.port


def test_tftp_transfer_with_options(tmp_path):
    payload = bytes(range(256)) * 40  # 10240 bytes
    (tmp_path / "grubx64.efi").write_bytes(payload)
    _, port = start_tftp(tmp_path, blksize=512)
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(b"\x00\x01grubx64.efi\x00octet\x00blksize\x00512\x00tsize\x000\x00",
             ("127.0.0.1", port))
    oack, srv_addr = c.recvfrom(2048)
    assert oack[0:2] == b"\x00\x06"
    assert b"blksize\x00512" in oack
    assert b"tsize\x0010240" in oack
    c.sendto(b"\x00\x04" + struct.pack("!H", 0), srv_addr)  # ACK0 回源地址
    got = b""
    block = 0
    while True:
        pkt, _ = c.recvfrom(2048)
        assert pkt[0:2] == b"\x00\x03"
        block = struct.unpack("!H", pkt[2:4])[0]
        got += pkt[4:]
        c.sendto(b"\x00\x04" + struct.pack("!H", block), srv_addr)
        if len(pkt) < 4 + 512:
            break
    assert got == payload
    assert block in (20, 21)  # 整除时最后收到的是 0 字节 EOF 块 21


def test_tftp_missing_file_gets_error(tmp_path):
    _, port = start_tftp(tmp_path)
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(b"\x00\x01nope.efi\x00octet\x00", ("127.0.0.1", port))
    pkt, _ = c.recvfrom(2048)
    assert pkt[0:2] == b"\x00\x05"  # ERROR


def test_tftp_transfer_no_options(tmp_path):
    payload = b"x" * 300
    (tmp_path / "initrd.img").write_bytes(payload)
    _, port = start_tftp(tmp_path)
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(b"\x00\x01initrd.img\x00octet\x00", ("127.0.0.1", port))
    got = b""
    while True:
        pkt, srv_addr = c.recvfrom(2048)
        blk = struct.unpack("!H", pkt[2:4])[0]
        got += pkt[4:]
        c.sendto(b"\x00\x04" + struct.pack("!H", blk), srv_addr)
        if len(pkt) < 4 + 512:
            break
    assert got == payload


# ---- 套接字绑定 (2026-10-21 实机暴露: Linux 上绑具体 IP 收不到 255.255.255.255 的 DISCOVER;
#      开机时网线没插, 绑 192.0.2.1 报 EADDRNOTAVAIL, 线程静默死掉)

class FakeSock:
    def __init__(self, fail_times=0, errno_=errno.EADDRNOTAVAIL):
        self.opts, self.binds, self.fail_times, self.errno_ = [], [], fail_times, errno_

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def bind(self, addr):
        self.binds.append(addr)
        if self.fail_times:
            self.fail_times -= 1
            raise OSError(self.errno_, "Cannot assign requested address")


SO_BINDTODEVICE = 25


def test_dhcp_linux_binds_any_locked_to_iface():
    from pxeserver import open_dhcp_socket
    s = FakeSock()
    open_dhcp_socket(s, "192.0.2.1", 67, iface="eno1", platform="linux")
    assert s.binds == [("", 67)]                                     # 收得到广播
    assert (socket.SOL_SOCKET, SO_BINDTODEVICE, b"eno1") in s.opts   # 只在装机口, 绝不漏到 LAN2
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in s.opts


def test_dhcp_windows_keeps_ip_bind():
    from pxeserver import open_dhcp_socket
    s = FakeSock()
    open_dhcp_socket(s, "192.0.2.1", 67, iface="eno1", platform="win32")
    assert s.binds == [("192.0.2.1", 67)]
    assert not [o for o in s.opts if o[1] == SO_BINDTODEVICE]


def test_dhcp_linux_without_iface_never_binds_any():
    """没给网卡名时宁可收不到广播, 也不能绑 0.0.0.0 变成全网口的野 DHCP."""
    from pxeserver import open_dhcp_socket
    s = FakeSock()
    open_dhcp_socket(s, "192.0.2.1", 67, iface=None, platform="linux")
    assert s.binds == [("192.0.2.1", 67)]


def test_bind_retry_waits_for_address():
    from pxeserver import bind_retry
    s, sleeps = FakeSock(fail_times=2), []
    bind_retry(s, ("192.0.2.1", 69), sleep=sleeps.append, log=FakeLog())
    assert s.binds == [("192.0.2.1", 69)] * 3 and len(sleeps) == 2


def test_bind_retry_reraises_other_errors():
    from pxeserver import bind_retry
    s = FakeSock(fail_times=1, errno_=errno.EADDRINUSE)     # 端口被占, 等也没用
    with pytest.raises(OSError):
        bind_retry(s, ("192.0.2.1", 69), sleep=lambda _: None, log=FakeLog())


# ---- 地址池 / 子网 / 别人的 DHCP

def _request(mac, ip, server_id=None):
    opts = encode_opt(53, b"\x03") + encode_opt(50, (ip,))
    if server_id:
        opts += encode_opt(54, (server_id,))
    return bytes(bytearray(make_discover(mac=mac))[:240] + opts + b"\xff")


def test_pool_exhausted_sends_no_offer():
    d = dhcp_for_test()                         # 池子 3 个地址
    macs = [bytes([2, 0, 0, 0, 0, i]) for i in range(4)]
    ips = [d.handle(make_discover(mac=m), None) for m in macs]
    assert all(ips[:3]) and ips[3] is None      # 用完了不再重复发第一个地址
    assert len({r[16:20] for r in ips[:3]}) == 3


def test_request_for_other_server_is_ignored():
    d = dhcp_for_test()
    assert d.handle(_request(b"\x02\x00\x00\x00\x00\x01", "192.0.2.10", "198.51.100.1"), None) is None


def test_mask_and_broadcast_follow_prefix():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.20", "shim.efi", "pxelinux.0",
                   FakeLog(), prefix=25)
    assert d.broadcast == "192.0.2.127"
    _, _, opts = parse_dhcp(d.handle(make_discover(), None))
    assert socket.inet_ntoa(opts[1]) == "255.255.255.128"


def test_default_prefix_is_24():
    d = dhcp_for_test()
    assert d.broadcast == "192.0.2.255"


def test_pool_must_sit_inside_subnet():
    with pytest.raises(ValueError):
        DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.200", "shim.efi", "pxelinux.0",
                   FakeLog(), prefix=25)


def test_pool_must_not_contain_server_ip():
    with pytest.raises(ValueError):
        DhcpServer("192.0.2.1", "192.0.2.1", "192.0.2.5", "shim.efi", "pxelinux.0", FakeLog())


# ---- A1: 租约 / 静默 / RELEASE / DECLINE

M1, M2 = b"\x02\x00\x00\x00\x00\x01", b"\x02\x00\x00\x00\x00\x02"


def _msg(mac, mtype, ip=None, server_id=None, ciaddr=None):
    head = bytearray(make_discover(mac=mac))[:240]
    if ciaddr:
        head[12:16] = socket.inet_aton(ciaddr)
    opts = encode_opt(53, mtype)
    if ip:
        opts += encode_opt(50, (ip,))
    if server_id:
        opts += encode_opt(54, (server_id,))
    return bytes(head + opts + b"\xff")


def _yiaddr(reply):
    return socket.inet_ntoa(reply[16:20])


def test_request_without_server_id_for_unknown_lease_is_silent():
    """INIT-REBOOT: 客户端拿着别处的旧地址来问, 不是我们的租约就不吭声 (不发 NAK 抢戏)."""
    d = dhcp_for_test()
    assert d.handle(_msg(M1, b"\x03", "198.51.100.99"), None, now=0) is None
    assert d.handle(_msg(M1, b"\x03", "192.0.2.10"), None, now=0) is None     # 池里但没租给它


def test_request_without_server_id_renews_own_lease():
    d = dhcp_for_test()
    d.handle(make_discover(mac=M1), None, now=0)
    assert d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=1)[242] == 5
    r = d.handle(_msg(M1, b"\x03", ciaddr="192.0.2.10"), None, now=1000)     # RENEWING: 只填 ciaddr
    assert parse_dhcp(r)[2][53] == b"\x05" and _yiaddr(r) == "192.0.2.10"


def test_request_for_ip_leased_to_someone_else_naks():
    d = dhcp_for_test()
    d.handle(make_discover(mac=M1), None, now=0)
    d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    d.handle(make_discover(mac=M2), None, now=0)
    r = d.handle(_msg(M2, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    assert parse_dhcp(r)[2][53] == b"\x06"


def test_twelve_macs_eleven_addresses_last_gets_no_offer():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.20", "shim.efi", "pxelinux.0", FakeLog())
    replies = [d.handle(make_discover(mac=bytes([2, 0, 0, 0, 1, i])), None, now=0) for i in range(12)]
    assert all(replies[:11]) and replies[11] is None
    assert len({_yiaddr(r) for r in replies[:11]}) == 11


def test_expired_lease_is_reclaimed():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.10", "shim.efi", "pxelinux.0", FakeLog())
    d.handle(make_discover(mac=M1), None, now=0)
    d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    assert d.handle(make_discover(mac=M2), None, now=3599) is None           # 还在租期内
    r = d.handle(make_discover(mac=M2), None, now=3601)                       # 3600 秒到期回收
    assert _yiaddr(r) == "192.0.2.10"


def test_unrequested_offer_is_held_briefly_only():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.10", "shim.efi", "pxelinux.0", FakeLog())
    d.handle(make_discover(mac=M1), None, now=0)                              # 只 DISCOVER 不 REQUEST
    assert d.handle(make_discover(mac=M2), None, now=10) is None
    assert d.handle(make_discover(mac=M2), None, now=d.OFFER_HOLD_S + 1) is not None


def test_ack_lease_time_is_3600():
    d = dhcp_for_test()
    d.handle(make_discover(mac=M1), None, now=0)
    r = d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    assert struct.unpack("!I", parse_dhcp(r)[2][51])[0] == 3600


def test_release_frees_address():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.10", "shim.efi", "pxelinux.0", FakeLog())
    d.handle(make_discover(mac=M1), None, now=0)
    d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    assert d.handle(_msg(M1, b"\x07", server_id="192.0.2.1", ciaddr="192.0.2.10"), None, now=5) is None
    assert _yiaddr(d.handle(make_discover(mac=M2), None, now=6)) == "192.0.2.10"


def test_release_of_someone_elses_address_ignored():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.10", "shim.efi", "pxelinux.0", FakeLog())
    d.handle(make_discover(mac=M1), None, now=0)
    d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    d.handle(_msg(M2, b"\x07", server_id="192.0.2.1", ciaddr="192.0.2.10"), None, now=5)
    assert d.handle(make_discover(mac=M2), None, now=6) is None


def test_decline_quarantines_address():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.11", "shim.efi", "pxelinux.0", FakeLog())
    d.handle(make_discover(mac=M1), None, now=0)
    d.handle(_msg(M1, b"\x03", "192.0.2.10", "192.0.2.1"), None, now=0)
    assert d.handle(_msg(M1, b"\x04", "192.0.2.10", "192.0.2.1"), None, now=1) is None   # ARP 撞了
    assert _yiaddr(d.handle(make_discover(mac=M1), None, now=2)) == "192.0.2.11"     # 换一个
    assert d.handle(make_discover(mac=M2), None, now=3) is None                       # .10 隔离中
    assert _yiaddr(d.handle(make_discover(mac=M2), None, now=d.DECLINE_HOLD_S + 2)) == "192.0.2.10"


# ---- A6: TFTP 块号回绕 / 流式读 / blksize 校验

class AckingSock:
    """假 TFTP 会话套接字: 发 DATA 就排一个对应 ACK, 发 OACK 就排 ACK0; 全程记账."""

    def __init__(self, client=("192.0.2.10", 2000), refuse_oack=False):
        self.client, self.refuse_oack = client, refuse_oack
        self.sent, self.inbox, self.closed = [], [], False

    def settimeout(self, t):
        pass

    def sendto(self, pkt, addr):
        assert addr == self.client
        self.sent.append(pkt)
        if pkt[:2] == b"\x00\x03":
            self.inbox.append(b"\x00\x04" + pkt[2:4])
        elif pkt[:2] == b"\x00\x06":
            self.inbox.append(b"\x00\x05\x00\x08nope\x00" if self.refuse_oack else b"\x00\x04\x00\x00")

    def recvfrom(self, n):
        if not self.inbox:
            raise socket.timeout()
        return self.inbox.pop(0), self.client

    def close(self):
        self.closed = True


class ReadSpy(io.BytesIO):
    def __init__(self, data, limit):
        super().__init__(data)
        self.limit, self.biggest = limit, 0

    def read(self, n=-1):
        assert 0 <= n <= self.limit, n          # 只按块读, 不整文件读进内存
        self.biggest = max(self.biggest, n)
        return super().read(n)


def _tftp(tmp_path, sock):
    return TftpServer("192.0.2.1", str(tmp_path), FakeLog(), sock_factory=lambda: sock)


def test_tftp_block_number_wraps_past_65535(tmp_path):
    blk = 8
    payload = bytes(i % 251 for i in range(65537 * blk + 3))       # 65538 块: 超过 16 位
    (tmp_path / "big.img").write_bytes(payload)
    sock = AckingSock()
    _tftp(tmp_path, sock)._rrq(b"\x00\x01big.img\x00octet\x00blksize\x008\x00", sock.client)
    data = [p for p in sock.sent if p[:2] == b"\x00\x03"]
    assert b"".join(p[4:] for p in data) == payload
    wire = [struct.unpack("!H", p[2:4])[0] for p in data]
    assert len(wire) == 65538 and wire[65534:65538] == [65535, 0, 1, 2]
    assert sock.closed


def test_tftp_transfer_reads_block_by_block():
    from pxeserver import tftp_send_file
    spy = ReadSpy(b"z" * 5000, 512)
    sock = AckingSock()
    assert tftp_send_file(sock, sock.client, spy, 5000, 512, retries=2)
    assert spy.biggest == 512


def test_tftp_empty_file_sends_one_empty_block(tmp_path):
    (tmp_path / "empty").write_bytes(b"")
    sock = AckingSock()
    _tftp(tmp_path, sock)._rrq(b"\x00\x01empty\x00octet\x00", sock.client)
    assert [p for p in sock.sent if p[:2] == b"\x00\x03"] == [b"\x00\x03\x00\x01"]


@pytest.mark.parametrize("bad", [b"7", b"65465", b"abc", b""])
def test_tftp_bad_blksize_gets_error(tmp_path, bad):
    (tmp_path / "f").write_bytes(b"x" * 100)
    sock = AckingSock()
    _tftp(tmp_path, sock)._rrq(b"\x00\x01f\x00octet\x00blksize\x00" + bad + b"\x00", sock.client)
    assert len(sock.sent) == 1 and sock.sent[0][:2] == b"\x00\x05"


@pytest.mark.parametrize("msg", [b"\x00\x01", b"\x00\x01noterminator", b"\x00\x01f\x00octet\x00tsize\x00"])
def test_tftp_malformed_rrq_gets_error(tmp_path, msg):
    (tmp_path / "f").write_bytes(b"x")
    sock = AckingSock()
    _tftp(tmp_path, sock)._rrq(msg, sock.client)
    assert len(sock.sent) == 1 and sock.sent[0][:2] == b"\x00\x05"


def test_tftp_blksize_limits_accepted(tmp_path):
    from pxeserver import parse_rrq
    assert parse_rrq(b"\x00\x01f\x00octet\x00blksize\x008\x00")[2]["blksize"] == 8
    assert parse_rrq(b"\x00\x01f\x00octet\x00BLKSIZE\x0065464\x00")[2]["blksize"] == 65464


def test_tftp_client_refusing_oack_aborts(tmp_path):
    (tmp_path / "f").write_bytes(b"x" * 100)
    sock = AckingSock(refuse_oack=True)
    _tftp(tmp_path, sock)._rrq(b"\x00\x01f\x00octet\x00tsize\x000\x00", sock.client)
    assert not [p for p in sock.sent if p[:2] == b"\x00\x03"]       # 客户端回 ERROR 就别再发数据


# ---- A7: serve 循环不静默死, 主线程盯着工作线程

class StopLoop(BaseException):
    pass


class ScriptedSock:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    def recvfrom(self, n):
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def sendto(self, pkt, addr):
        self.sent.append((pkt, addr))


def test_serve_loop_survives_recv_and_handler_errors():
    from pxeserver import serve_loop
    seen = []

    def handler(sock, msg, addr):
        if msg == b"boom":
            raise ValueError("bad packet")
        seen.append(msg)

    sock = ScriptedSock([OSError("ENETDOWN"), (b"boom", ("192.0.2.9", 68)),
                         (b"ok", ("192.0.2.9", 68)), StopLoop()])
    with pytest.raises(StopLoop):
        serve_loop(sock, handler, FakeLog(), "测试", sleep=lambda s: None)
    assert seen == [b"ok"]


class FakeThread:
    def __init__(self, name, alive=True):
        self.name, self.alive = name, alive

    def is_alive(self):
        return self.alive


def test_supervise_exits_1_when_worker_dies():
    from pxeserver import EXIT_THREAD_DIED, supervise
    ts = [FakeThread("dhcp"), FakeThread("tftp")]
    ticks = []

    def sleep(s):
        ticks.append(s)
        if len(ticks) == 3:
            ts[1].alive = False
    assert supervise(ts, FakeLog(), sleep=sleep) == EXIT_THREAD_DIED == 1
    assert len(ticks) == 3


# ---- A2: 别在别人的网段上发 DHCP

import json  # noqa: E402

from pxeserver import PxeGuard, supervise  # noqa: E402


class Box:
    """可变的假环境: carrier / 网口地址 / 探测结果, 测试里随时改."""

    def __init__(self, link=True, addr="192.0.2.1", found=()):
        self.link, self.addr, self.found, self.probes = link, addr, list(found), []

    def probe(self, iface, timeout=4.0, ignore=()):
        self.probes.append((iface, ignore))
        return [o for o in self.found if o["server"] not in ignore]


def guard_for(tmp_path, box, events=None):
    return PxeGuard("192.0.2.1", "eno1", FakeLog(), prefix=24, probe=box.probe, carrier=lambda: box.link,
                    addr=lambda: box.addr, state_path=str(tmp_path / "pxe-guard.json"),
                    event=(events.append if events is not None else lambda m: None))


def guard_state(tmp_path):
    return json.loads((tmp_path / "pxe-guard.json").read_text())


def test_guard_probes_at_start_and_serves_when_alone(tmp_path):
    box = Box()
    g = guard_for(tmp_path, box)
    assert not g.may_reply()                      # 没探测过之前不回包
    g.tick()
    assert box.probes == [("eno1", ("192.0.2.1",))]
    assert g.may_reply() and guard_state(tmp_path)["state"] == "serving"


def test_guard_goes_proxy_on_foreign_offer_at_start(tmp_path):
    """#11: 探到别人的 DHCP 不停服, 转代理模式: 不发地址, 只回引导信息."""
    box, events = Box(found=[{"server": "198.51.100.1", "yiaddr": "198.51.100.50"}]), []
    g = guard_for(tmp_path, box, events)
    g.tick()
    assert g.state == "proxy" and not g.may_reply() and g.may_proxy()
    st = guard_state(tmp_path)
    assert st["state"] == "proxy" and st["server"] == "198.51.100.1" and st["leases"] is False
    assert st["offered"] == "198.51.100.50" and st["prefix"] == 24
    assert events and "198.51.100.1" in events[0] and "不发地址" in events[0]


def test_guard_serving_state_file_says_leases(tmp_path):
    g = guard_for(tmp_path, Box())
    g.tick()
    assert g.may_proxy() and guard_state(tmp_path)["leases"] is True


def test_guard_ignores_own_offer_in_probe(tmp_path):
    box = Box(found=[{"server": "192.0.2.1", "yiaddr": "192.0.2.10"}])
    g = guard_for(tmp_path, box)
    g.tick()
    assert g.state == "serving" and g.may_reply()


def test_guard_reprobes_on_carrier_up(tmp_path):
    box = Box(link=False)
    g = guard_for(tmp_path, box)
    g.tick()
    assert box.probes == [] and not g.may_reply() and guard_state(tmp_path)["state"] == "no-link"
    box.link = True
    g.tick()
    assert len(box.probes) == 1 and g.may_reply()
    g.tick()
    assert len(box.probes) == 1                   # 一直插着不重复探测
    box.link = False
    g.tick()
    box.link, box.found = True, [{"server": "203.0.113.1", "yiaddr": "203.0.113.9"}]
    g.tick()                                      # 拔了插到别处: 0 -> 1 再探测一次
    assert len(box.probes) == 2 and g.state == "proxy"
    g.tick()
    assert len(box.probes) == 2 and g.state == "proxy"      # 插着不动: 不重探, 也不自己变回去
    box.link = False
    g.tick()
    assert not g.may_proxy() and guard_state(tmp_path)["state"] == "no-link"
    box.link, box.found = True, []
    g.tick()                                      # 又插回直连: 重探没人, 恢复发地址
    assert len(box.probes) == 3 and g.state == "serving" and g.may_reply()
    assert guard_state(tmp_path)["server"] is None


def test_guard_passive_hit_during_probe_wins_over_empty_probe(tmp_path):
    """探测那 4 秒里被动监听先看到别人: 探测自己没收到也不能当没有."""
    box = Box(link=False)
    g = guard_for(tmp_path, box)
    g.tick()

    def probe(iface, timeout=4.0, ignore=()):
        g.foreign("198.51.100.1", "被动监听")     # 模拟另一个线程
        return []
    g.probe, box.link = probe, True
    g.tick()
    assert g.state == "proxy" and not g.may_reply()


def test_guard_no_ip_even_with_foreign_dhcp(tmp_path):
    box = Box(addr=None, found=[{"server": "198.51.100.1", "yiaddr": "198.51.100.50"}])
    g = guard_for(tmp_path, box)
    g.tick()
    assert not g.may_proxy() and guard_state(tmp_path)["state"] == "no-ip"
    box.addr = "192.0.2.1"
    g.tick()
    assert g.state == "proxy"


def test_guard_silent_when_iface_lacks_our_ip(tmp_path):
    box = Box(addr="198.51.100.7")
    g = guard_for(tmp_path, box)
    g.tick()
    assert not g.may_reply() and guard_state(tmp_path)["state"] == "no-ip"
    box.addr = "192.0.2.1"
    g.tick()
    assert g.may_reply()


def test_guard_unknown_probe_result_does_not_trip(tmp_path):
    box = Box()
    box.probe = lambda iface, timeout=4.0, ignore=(): None          # 探测说不准 (如没插线)
    g = guard_for(tmp_path, box)
    g.tick()
    assert g.state == "probing" and not g.may_proxy()


def test_guard_probe_result_unknown_holds_and_retries(tmp_path):
    """netprof_core.dhcp_probe 读不到 carrier 时给空的 ProbeResult(unknown): 不能当"没有别人"放行."""
    from netprof_core import ProbeResult
    box, results = Box(), [ProbeResult(carrier=None), ProbeResult()]

    def probe(iface, timeout=4.0, ignore=()):
        box.probes.append((iface, ignore))
        return results.pop(0)
    box.probe = probe
    g = guard_for(tmp_path, box)
    g.tick()
    assert not g.may_proxy() and guard_state(tmp_path)["state"] == "probing"
    g.tick()                                      # 下一轮重探, 这次算数
    assert len(box.probes) == 2 and g.may_reply()


def test_guard_probe_error_falls_back_to_proxy(tmp_path):
    """探测坏了 = 不知道有没有别人: 偏向不发地址."""
    box = Box()

    def boom(*a, **k):
        raise PermissionError("need root")
    box.probe = boom
    g = guard_for(tmp_path, box)
    g.tick()
    assert g.state == "proxy" and not g.may_reply() and g.may_proxy()
    assert guard_state(tmp_path)["server"] is None


def test_guard_without_probe_function_falls_back_to_proxy(tmp_path):
    g = PxeGuard("192.0.2.1", "eno1", FakeLog(), probe=None, carrier=lambda: True,
                 addr=lambda: "192.0.2.1", state_path=str(tmp_path / "pxe-guard.json"),
                 event=lambda m: None)
    g.tick()
    assert g.state == "proxy" and not g.may_reply()


def test_guard_state_write_failure_is_swallowed(tmp_path):
    box = Box()
    g = PxeGuard("192.0.2.1", "eno1", FakeLog(), probe=box.probe, carrier=lambda: True,
                 addr=lambda: "192.0.2.1", state_path=str(tmp_path / "file" / "x.json"),
                 event=lambda m: None)
    (tmp_path / "file").write_text("not a dir")
    g.tick()
    assert g.may_reply()


def _reply_from(server, kind=b"\x02", mac=M1):
    head = bytearray(make_discover(mac=mac))[:240]
    head[0] = 2                                   # BOOTREPLY
    return bytes(head + encode_opt(53, kind) + encode_opt(54, (server,)) + b"\xff")


PXE_VENDOR = b"PXEClient:Arch:00007:UNDI:003016"


def make_pxe_discover(mac=M2, extra=b"", arch=7, with_arch=True):
    """固件 PXE 的 DISCOVER: 带 option 60 PXEClient 和 option 93."""
    return make_discover(mac=mac, arch=arch, with_arch=with_arch)[:-1] + encode_opt(60, PXE_VENDOR) + \
        extra + b"\xff"


def proxy_dhcp(tmp_path, found=(("198.51.100.1", "192.0.2.150"),)):
    g = guard_for(tmp_path, Box(found=[{"server": s, "yiaddr": y} for s, y in found]))
    g.tick()
    d = dhcp_for_test()
    d.guard = g
    return d, g


def test_passive_foreign_offer_switches_to_proxy(tmp_path):
    box = Box()
    g = guard_for(tmp_path, box)
    g.tick()
    d = dhcp_for_test()
    d.guard = g
    d.watch_reply(_reply_from("192.0.2.1"))       # 自己的回包: 不算
    assert g.may_reply()
    d.watch_reply(_reply_from("198.51.100.1", b"\x05"))
    assert g.state == "proxy" and guard_state(tmp_path)["server"] == "198.51.100.1"
    assert d.handle(make_discover(mac=M2), None) is None      # 不是 PXE 客户端: 一个字都不回
    assert d.leases == {}
    assert d.handle_proxy(make_discover(mac=M2), ("192.0.2.10", 4011)) is not None


def test_request_selecting_foreign_server_switches_to_proxy(tmp_path):
    g = guard_for(tmp_path, Box())
    g.tick()
    d = dhcp_for_test()
    d.guard = g
    assert d.handle(_msg(M1, b"\x03", "198.51.100.50", "198.51.100.1"), None) is None
    assert g.state == "proxy"


def test_proxy_mode_67_offer_has_no_address(tmp_path):
    """PXE 2.1 / dnsmasq proxy: 67 口只对 PXEClient 的 DISCOVER 回 yiaddr=0 的 OFFER,
    带 option 60 + 54, 不带掩码/网关/租期; 客户端地址照用正式 DHCP 的."""
    d, _ = proxy_dhcp(tmp_path)
    reply = d.handle(make_pxe_discover(), None)
    _, _, opts = parse_dhcp(reply)
    assert reply[0] == 2 and opts[53] == b"\x02"
    assert _yiaddr(reply) == "0.0.0.0"
    assert socket.inet_ntoa(reply[20:24]) == "192.0.2.1"          # siaddr = TFTP
    assert opts[60] == b"PXEClient" and opts[54] == socket.inet_aton("192.0.2.1")
    assert not ({1, 3, 6, 28, 50, 51, 58, 59} & set(opts))         # 一点地址信息都不给
    assert d.leases == {}


def test_proxy_mode_67_offer_echoes_client_uuid(tmp_path):
    d, _ = proxy_dhcp(tmp_path)
    uuid = encode_opt(97, b"\x00" + bytes(range(16)))
    _, _, opts = parse_dhcp(d.handle(make_pxe_discover(extra=uuid), None))
    assert opts[97] == b"\x00" + bytes(range(16))


def test_proxy_mode_67_ignores_everything_but_pxe_discover(tmp_path):
    d, _ = proxy_dhcp(tmp_path)
    pxe = lambda mtype, **kw: _msg(M2, mtype, **kw)[:-1] + encode_opt(60, PXE_VENDOR) + b"\xff"
    for msg in (pxe(b"\x03", ip="192.0.2.10", server_id="192.0.2.1"),    # 选了我们也不 ACK/NAK
                pxe(b"\x03", ip="192.0.2.10"),                           # INIT-REBOOT
                pxe(b"\x04", ip="192.0.2.10", server_id="192.0.2.1"),    # DECLINE
                pxe(b"\x07", ciaddr="192.0.2.10", server_id="192.0.2.1"),  # RELEASE
                pxe(b"\x08", ciaddr="192.0.2.10"),                       # INFORM
                make_discover(mac=M2),                                   # 没有 option 60
                make_pxe_discover(with_arch=False),                      # 没有 option 93 (dnsmasq 也不回)
                make_pxe_discover(arch=0),                               # BIOS: 仓库里没有 BIOS 引导
                make_pxe_discover(arch=11)):                             # ARM64: shimx64 给了也起不来
        assert d.handle(msg, None) is None
    assert d.leases == {}


def test_proxy_mode_answers_efi_x64_arch_9(tmp_path):
    d, _ = proxy_dhcp(tmp_path)
    assert _yiaddr(d.handle(make_pxe_discover(arch=9), None)) == "0.0.0.0"


def test_proxy_mode_ipxe_gets_script(tmp_path):
    d, _ = proxy_dhcp(tmp_path)
    reply = d.handle(make_pxe_discover(extra=encode_opt(77, b"iPXE")), None)
    assert parse_dhcp(reply)[2][67] == b"http://192.0.2.1/boot.ipxe"
    assert _yiaddr(reply) == "0.0.0.0"


def test_proxy_mode_4011_still_answers(tmp_path):
    d, _ = proxy_dhcp(tmp_path)
    reply = d.handle_proxy(_msg(M2, b"\x03", ciaddr="192.0.2.150"), ("192.0.2.150", 4011))
    assert parse_dhcp(reply)[2][53] == b"\x05" and _yiaddr(reply) == "0.0.0.0"


def test_4011_drops_clients_outside_our_subnet(tmp_path):
    """单播回包到别的网段会走默认路由从管理口出去: 不回."""
    d, _ = proxy_dhcp(tmp_path)
    assert d.handle_proxy(_msg(M2, b"\x03", ciaddr="198.51.100.50"), ("198.51.100.50", 4011)) is None
    d.guard = None
    assert d.handle_proxy(make_discover(), ("198.51.100.50", 4011)) is None


def test_serving_mode_unchanged_for_pxe_discover(tmp_path):
    d, _ = proxy_dhcp(tmp_path, found=())
    reply = d.handle(make_pxe_discover(), None)
    assert _yiaddr(reply) == "192.0.2.10" and parse_dhcp(reply)[2][51]


def test_dhcp_does_not_answer_before_guard_ready(tmp_path):
    d = dhcp_for_test()
    d.guard = guard_for(tmp_path, Box())
    assert d.handle(make_discover(mac=M1), None) is None     # 还没 tick: 没探测过
    assert d.handle(make_pxe_discover(mac=M1), None) is None
    assert d.handle_proxy(make_discover(mac=M1), ("192.0.2.10", 4011)) is None
    assert d.leases == {}                                    # 也不占地址


def test_supervise_keeps_running_when_guard_goes_proxy(tmp_path):
    box = Box()
    g = guard_for(tmp_path, box)
    ticks = []

    def sleep(s):
        ticks.append(s)
        box.link = False if box.link else True
        box.found = [{"server": "198.51.100.1", "yiaddr": "198.51.100.2"}]
        if len(ticks) == 3:                       # 第 2 轮 tick 已重插线、探到别人
            raise StopLoop
    with pytest.raises(StopLoop):
        supervise([FakeThread("dhcp")], FakeLog(), guard=g, sleep=sleep)
    assert g.state == "proxy"


def test_pxe_unit_no_longer_blocks_restart_on_foreign_dhcp():
    import pathlib
    u = (pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd" /
         "pxe-server.service").read_text()
    line = next(l for l in u.splitlines() if l.startswith("RestartPreventExitStatus="))
    assert line.split("=", 1)[1].split() == ["2", "4"]      # 3 (别人的 DHCP 就退出) 已不用
    assert "--iface eno1" in u


# ---- A4: 服务端兜底, 发装机菜单前看 pxe-mode.json

from pxeserver import grub_menu, pxe_mode  # noqa: E402

ARMED_MAC = "02:00:00:00:00:01"
ARMED_CFG = "grub.cfg-01-02-00-00-00-00-01"


def _tree(tmp_path, armed_until=None, mac=ARMED_MAC):
    root = tmp_path / "tftp"
    (root / "EFI" / "BOOT").mkdir(parents=True)
    install = grub_menu("192.0.2.1", install=True)
    (root / ARMED_CFG).write_text(install)
    (root / "grub.cfg-01-02-00-00-00-00-02").write_text(install)       # 没解锁的那台
    (root / "EFI" / "BOOT" / "grub.cfg").write_text(install)           # 老部署留下的自动装机菜单
    (root / "grub.cfg").write_text(grub_menu("192.0.2.1"))
    mode = tmp_path / "pxe-mode.json"
    if armed_until is not None:
        mode.write_text(json.dumps({"mac": mac, "since": 0, "until": armed_until}))
    return root, mode


def _fetch(root, mode, name, now=100):
    sock = AckingSock()
    srv = TftpServer("192.0.2.1", str(root), FakeLog(), sock_factory=lambda: sock,
                     pxe_mode_path=str(mode), now=lambda: now, event=lambda m: None)
    srv._rrq(b"\x00\x01" + name.encode() + b"\x00octet\x00", sock.client)
    return b"".join(p[4:] for p in sock.sent if p[:2] == b"\x00\x03").decode()


def test_tftp_serves_install_menu_to_armed_mac(tmp_path):
    root, mode = _tree(tmp_path, armed_until=1000)
    assert "inst.ks=" in _fetch(root, mode, "/" + ARMED_CFG)


@pytest.mark.parametrize("name", ["/grub.cfg-01-02-00-00-00-00-02", "EFI/BOOT/grub.cfg"])
def test_tftp_rescue_for_other_mac_or_shared_menu(tmp_path, name):
    root, mode = _tree(tmp_path, armed_until=1000)
    got = _fetch(root, mode, name)
    assert "inst.ks" not in got and "inst.rescue" in got


def test_tftp_rescue_when_expired(tmp_path):
    root, mode = _tree(tmp_path, armed_until=50)
    assert "inst.ks" not in _fetch(root, mode, "/" + ARMED_CFG, now=51)


def test_tftp_rescue_when_not_armed(tmp_path):
    root, mode = _tree(tmp_path)                                         # 没有 pxe-mode.json
    assert "inst.ks" not in _fetch(root, mode, "/" + ARMED_CFG)


def test_tftp_plain_files_untouched_by_gate(tmp_path):
    root, mode = _tree(tmp_path)
    (root / "vmlinuz").write_bytes(b"kernel inst.ks= not a menu")
    assert _fetch(root, mode, "vmlinuz") == "kernel inst.ks= not a menu"


def test_tftp_consumed_log_line_only_for_real_install_menu(tmp_path):
    """车机靠 'TFTP RRQ /<菜单> (' 这行判断菜单被取走, 回救援时不能写成同样的行."""
    root, mode = _tree(tmp_path, armed_until=1000)
    lines = []

    class L(FakeLog):
        def info(self, fmt, *a, **k):
            lines.append(fmt % a)
    for name, armed in (("/" + ARMED_CFG, True), ("/grub.cfg-01-02-00-00-00-00-02", False)):
        sock = AckingSock()
        TftpServer("192.0.2.1", str(root), L(), sock_factory=lambda: sock, pxe_mode_path=str(mode),
                   now=lambda: 100, event=lambda m: None)._rrq(
            b"\x00\x01" + name.encode() + b"\x00octet\x00", sock.client)
        hit = [l for l in lines if l.startswith(f"TFTP RRQ {name} (")]
        assert bool(hit) == armed, lines


def test_tftp_install_menu_fetch_is_logged_to_cartlog(tmp_path):
    root, mode = _tree(tmp_path, armed_until=1000)
    events, sock = [], AckingSock()
    TftpServer("192.0.2.1", str(root), FakeLog(), sock_factory=lambda: sock, pxe_mode_path=str(mode),
               now=lambda: 100, event=events.append)._rrq(
        b"\x00\x01/" + ARMED_CFG.encode() + b"\x00octet\x00", sock.client)
    assert events and ARMED_MAC in events[0]


def test_pxe_mode_reader():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = f"{d}/m.json"
        assert pxe_mode(p, now=0) == {"mode": "rescue"}
        with open(p, "w") as f:
            json.dump({"mac": "02-00-00-00-00-01", "since": 1, "until": 10}, f)
        assert pxe_mode(p, now=5)["mac"] == ARMED_MAC and pxe_mode(p, now=5)["mode"] == "install"
        assert pxe_mode(p, now=11)["mode"] == "expired"


def test_grub_menu_rescue_and_install():
    r, i = grub_menu("198.51.100.1"), grub_menu("198.51.100.1", install=True)
    assert "inst.ks" not in r and "inst.rescue" in r and "198.51.100.1" in r
    assert "inst.ks=http://198.51.100.1:8000/ks.cfg" in i and "inst.ks.sendmac" in i


def test_parse_args_defaults_point_at_cart_state_files():
    from pxeserver import GUARD_STATE, PXE_MODE, parse_args
    a = parse_args(["serve", "--ip", "192.0.2.1", "--pool", "192.0.2.10,192.0.2.20", "--tftp-root", "/x"])
    assert (a.prefix, a.pxe_mode, a.guard_state, a.iface) == (24, PXE_MODE, GUARD_STATE, None)


def test_tmpfiles_log_dir_readable_by_wheel_and_run_dir_exists():
    import pathlib
    t = (pathlib.Path(__file__).resolve().parent.parent / "deploy" / "tmpfiles.d" / "ops-cart.conf").read_text()
    rows = {l.split()[1]: l.split() for l in t.splitlines() if l.strip() and not l.startswith("#")}
    for d in ("/var/log/ops-cart", "/var/log/ops-cart/reports"):
        assert rows[d][2:5] == ["0750", "root", "wheel"], rows[d]
    assert rows["/run/ops-cart"][0] == "d"                  # pxe-guard.json / pin-gate.json 放这


def test_guard_state_sticks_at_proxy(tmp_path):
    g = guard_for(tmp_path, Box())
    g.tick()
    g.foreign("198.51.100.1", "被动监听")
    g._set("serving", "")                          # 模拟另一线程晚到的 tick
    assert g.state == "proxy" and not g.may_reply()
    g.tick()
    assert g.state == "proxy"


# ---- #11: Windows PE (GRUB -> iPXE -> wimboot), 只进解锁后的 MAC 菜单

import pxeserver as _px  # noqa: E402


def _winpe_entry(cfg):
    return [b for b in cfg.split("menuentry ")[1:] if "WinPE" in b]


def test_grub_menu_rocky_and_rescue_have_no_winpe():
    for cfg in (grub_menu("192.0.2.1"), grub_menu("192.0.2.1", install=True)):
        assert not _winpe_entry(cfg) and _px.WINPE_CHAIN not in cfg


def test_grub_menu_winpe_default_winpe_then_rescue_no_ks():
    """解锁用途 winpe: 第 0 项 (默认) WinPE, 后面救援, 不带任何自动装机项, timeout 15."""
    cfg = _px.grub_menu_winpe("192.0.2.1")
    entries = cfg.split("menuentry ")[1:]
    assert "set default=0" in cfg and "set timeout=15" in cfg
    assert entries[0].startswith("'WinPE (GRUB -> iPXE -> wimboot)'")
    assert "chainloader (http,192.0.2.1)/win/ipxe.efi" in entries[0]
    assert "inst.rescue" in entries[1] and len(entries) == 3            # 救援 HTTP + TFTP
    assert "inst.ks" not in cfg and "inst.repo" not in cfg


def test_ipxe_script_template():
    s = _px.ipxe_script()
    assert s.startswith("#!ipxe\n") and s.rstrip().endswith("boot")
    assert "kernel win/wimboot" in s
    for name in ("bootmgfw.efi", "bcd", "boot.sdi", "boot.wim"):
        assert f"initrd -n {name} win/{name}" in s
    assert set(_px.WINPE_FILES) >= {"ipxe.efi", "wimboot", "bootmgfw.efi", "bcd", "boot.sdi", "boot.wim"}


def test_dhcp_ipxe_url_uses_script_constant():
    d = DhcpServer("192.0.2.1", "192.0.2.10", "192.0.2.20", "shimx64.efi", "pxelinux.0", FakeLog())
    assert d.ipxe_script == f"http://192.0.2.1/{_px.IPXE_SCRIPT}" and _px.IPXE_SCRIPT == "boot.ipxe"


def test_winpe_missing_lists_absent_or_empty_files(tmp_path):
    assert _px.winpe_missing(str(tmp_path)) == [f"win/{n}" for n in _px.WINPE_FILES]
    (tmp_path / "win").mkdir()
    for n in _px.WINPE_FILES:
        (tmp_path / "win" / n).write_bytes(b"x")
    assert _px.winpe_missing(str(tmp_path)) == []
    (tmp_path / "win" / "boot.wim").write_bytes(b"")                    # 空文件 (拷到一半) 也算缺
    assert _px.winpe_missing(str(tmp_path)) == ["win/boot.wim"]


@pytest.mark.parametrize("text,want", [
    ("linux /vmlinuz inst.ks=http://192.0.2.1:8000/ks.cfg", True),
    ("chainloader (http,192.0.2.1)/win/ipxe.efi", True),
    ("chainloader /win/ipxe.efi", True),
    ("linux /vmlinuz inst.rescue", False),
])
def test_unlock_only_menu(text, want):
    assert _px.unlock_only_menu(text) is want


def test_tftp_winpe_menu_only_for_armed_mac(tmp_path):
    """带 WinPE 项、不带 ks 的 MAC 菜单 (10/30 手写的那种) 也只给解锁的那台."""
    root, mode = _tree(tmp_path, armed_until=1000)
    winpe = "set default=0\nmenuentry 'WinPE' {\n    chainloader (http,192.0.2.1)/win/ipxe.efi\n}\n"
    (root / ARMED_CFG).write_text(winpe)
    (root / "grub.cfg-01-02-00-00-00-00-02").write_text(winpe)
    assert "ipxe.efi" in _fetch(root, mode, "/" + ARMED_CFG)
    got = _fetch(root, mode, "/grub.cfg-01-02-00-00-00-00-02")
    assert "ipxe.efi" not in got and "inst.rescue" in got
    assert "ipxe.efi" not in _fetch(root, mode, "/" + ARMED_CFG, now=1001)   # 过期


# ---- #18: Clonezilla 克隆菜单, 同 WinPE: 只进解锁后的 MAC 菜单

CLONE_PARAMS = ["boot=live", "noprompt", 'ocs_live_run="ocs-sr -batch restoredisk lab-img SERIALNO=SN-EXAMPLE-1"',
                'ocs_repository="smb://192.0.2.1/clone-ro/"']


def test_grub_menu_clone_restore_defaults_to_rescue():
    """恢复会写盘, 默认落到救援 (第 1 项), 要人在屏幕前手选恢复项; 抓镜像只读, 仍默认克隆."""
    restore = _px.grub_menu_clone("192.0.2.1", "clone-restore", CLONE_PARAMS)
    assert "set default=1" in restore and "set default=0" not in restore
    assert "inst.rescue" in restore.split("menuentry ")[1:][1]
    save = _px.grub_menu_clone("192.0.2.1", "clone-save", CLONE_PARAMS)
    assert "set default=0" in save and "set timeout=15" in save


def test_grub_menu_clone_entries_clone_then_rescue_no_ks():
    cfg = _px.grub_menu_clone("192.0.2.1", "clone-restore", CLONE_PARAMS)
    entries = cfg.split("menuentry ")[1:]
    assert "set timeout=15" in cfg
    assert entries[0].startswith(f"'{_px.CLONE_TITLES['clone-restore']}'")
    assert "linux (http,192.0.2.1)/clonezilla/vmlinuz boot=live noprompt " in entries[0]
    assert 'ocs_live_run=\\"ocs-sr -batch restoredisk lab-img SERIALNO=SN-EXAMPLE-1\\"' in entries[0]
    assert "fetch=http://192.0.2.1/clonezilla/filesystem.squashfs" in entries[0]
    assert "initrd (http,192.0.2.1)/clonezilla/initrd.img" in entries[0]
    assert "inst.rescue" in entries[1] and len(entries) == 3
    assert "inst.ks" not in cfg and _px.WINPE_CHAIN not in cfg


def test_grub_menu_clone_titles_say_what_happens():
    assert "save" in _px.grub_menu_clone("192.0.2.1", "clone-save", CLONE_PARAMS).split("menuentry ")[1].lower()
    assert "overwrite" in _px.CLONE_TITLES["clone-restore"].lower()


@pytest.mark.parametrize("bad", ["x$y", "a;b", "a\\b", "a'b", "a`b", "a{b", "a}b", "a#b", "a\nb", 'a"b',
                                 "a|b", "a&b", "a<b", ""])
def test_grub_menu_clone_refuses_unsafe_tokens(bad):
    with pytest.raises(ValueError):
        _px.grub_menu_clone("192.0.2.1", "clone-restore", CLONE_PARAMS + [bad])


def test_grub_menu_clone_refuses_unknown_purpose():
    with pytest.raises(ValueError):
        _px.grub_menu_clone("192.0.2.1", "rocky", CLONE_PARAMS)


def test_clone_missing_lists_absent_or_empty_files(tmp_path):
    assert _px.clone_missing(str(tmp_path)) == [f"clonezilla/{n}" for n in _px.CLONE_FILES]
    assert set(_px.CLONE_FILES) == {"vmlinuz", "initrd.img", "filesystem.squashfs"}
    (tmp_path / "clonezilla").mkdir()
    for n in _px.CLONE_FILES:
        (tmp_path / "clonezilla" / n).write_bytes(b"x")
    assert _px.clone_missing(str(tmp_path)) == []
    (tmp_path / "clonezilla" / "filesystem.squashfs").write_bytes(b"")
    assert _px.clone_missing(str(tmp_path)) == ["clonezilla/filesystem.squashfs"]


@pytest.mark.parametrize("text,want", [
    ("linux (http,192.0.2.1)/clonezilla/vmlinuz boot=live", True),
    ("linux /clonezilla/vmlinuz", True),
    ('linux /vmlinuz ocs_live_run=\\"ocs-sr\\"', True),              # 换了目录名的手写克隆菜单也拦
])
def test_unlock_only_menu_clone(text, want):
    assert _px.unlock_only_menu(text) is want


def test_rescue_and_rocky_menus_have_no_clone():
    for cfg in (grub_menu("192.0.2.1"), grub_menu("192.0.2.1", install=True), _px.grub_menu_winpe("192.0.2.1")):
        assert "clonezilla" not in cfg and "ocs_live_run" not in cfg


def test_tftp_clone_menu_only_for_armed_mac(tmp_path):
    root, mode = _tree(tmp_path, armed_until=1000)
    clone = _px.grub_menu_clone("192.0.2.1", "clone-restore", CLONE_PARAMS)
    (root / ARMED_CFG).write_text(clone)
    (root / "grub.cfg-01-02-00-00-00-00-02").write_text(clone)
    assert "ocs_live_run" in _fetch(root, mode, "/" + ARMED_CFG)
    got = _fetch(root, mode, "/grub.cfg-01-02-00-00-00-00-02")
    assert "ocs_live_run" not in got and "inst.rescue" in got
    assert "ocs_live_run" not in _fetch(root, mode, "/" + ARMED_CFG, now=1001)   # 过期


def test_pxe_mode_batch_macs(tmp_path):
    """恢复一批: pxe-mode.json 带 macs, 名单里的每台都放行, 名单外的不放."""
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"mac": "02:00:00:00:00:01", "macs": ["02-00-00-00-00-01", "02:00:00:00:00:02"],
                             "since": 1, "until": 10}))
    st = pxe_mode(str(p), now=5)
    assert st["macs"] == ["02:00:00:00:00:01", "02:00:00:00:00:02"]
    assert _px.install_allowed("02:00:00:00:00:02", st) and _px.install_allowed("02:00:00:00:00:01", st)
    assert not _px.install_allowed("02:00:00:00:00:03", st)
    assert not _px.install_allowed("02:00:00:00:00:02", pxe_mode(str(p), now=11))   # 过期


def test_pxe_mode_single_mac_unchanged(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"mac": "02:00:00:00:00:01", "since": 1, "until": 10}))
    assert pxe_mode(str(p), now=5) == {"mode": "install", "mac": "02:00:00:00:00:01", "since": 1, "until": 10}


@pytest.mark.parametrize("macs", ["02:00:00:00:00:02", ["nope"], [None], [], {"a": 1}])
def test_pxe_mode_bad_macs_is_rescue(tmp_path, macs):
    """名单坏了 -> 整个当没解锁 (宁可谁都不给)."""
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"mac": "02:00:00:00:00:01", "macs": macs, "since": 1, "until": 10}))
    assert pxe_mode(str(p), now=5) == {"mode": "rescue"}


def test_tftp_clone_batch_members_each_get_their_menu(tmp_path):
    root, mode = _tree(tmp_path)
    mode.write_text(json.dumps({"mac": ARMED_MAC, "macs": [ARMED_MAC, "02:00:00:00:00:02"],
                                "since": 0, "until": 1000}))
    clone = _px.grub_menu_clone("192.0.2.1", "clone-restore", CLONE_PARAMS)
    for n in (ARMED_CFG, "grub.cfg-01-02-00-00-00-00-02", "grub.cfg-01-02-00-00-00-00-03"):
        (root / n).write_text(clone)
    assert "ocs_live_run" in _fetch(root, mode, "/" + ARMED_CFG)
    assert "ocs_live_run" in _fetch(root, mode, "/grub.cfg-01-02-00-00-00-00-02")
    assert "ocs_live_run" not in _fetch(root, mode, "/grub.cfg-01-02-00-00-00-00-03")
