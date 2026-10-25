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
