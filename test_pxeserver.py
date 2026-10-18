"""pxeserver 协议层测试: 红绿循环的绿侧. 全部本机回环高位端口, 不碰真接口."""

import socket
import struct
import threading

import pytest

from pxeserver import (
    DhcpServer, TftpServer, arch_bootfile, build_dhcp_reply, encode_opt,
    parse_dhcp, tftp_safe_name,
)

TFTP_PORTS = iter(range(11069, 11100))


def make_discover(mac=b"\xaa\xbb\xcc\xdd\xee\x01", arch=7, with_arch=True):
    msg = bytearray(240)
    msg[0] = 1  # BOOTREQUEST
    msg[1] = 1  # ethernet
    msg[2] = 6  # hlen
    msg[0:4] = b"\x99\x88\x77\x66"  # xid
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


def dhcp_for_test():
    return DhcpServer("192.168.200.1", "192.168.200.10", "192.168.200.12",
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
    assert socket.inet_ntoa(reply[20:24]) == "192.168.200.1"  # siaddr = TFTP


def test_offer_has_option_60_pxeclient():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    _, _, opts = parse_dhcp(reply)
    assert opts[60] == b"PXEClient"


def test_request_ack_has_option_60():
    d = dhcp_for_test()
    d.handle(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"), None)
    req = bytes(bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))[:240] +
                encode_opt(53, b"\x03") + encode_opt(50, ("192.168.200.10",)) + encode_opt(54, ("192.168.200.1",)) + b"\xff")
    reply = d.handle(req, None)
    _, _, opts = parse_dhcp(reply)
    assert opts[60] == b"PXEClient"


def test_offer_subnet_mask_is_packed_ip():
    d = dhcp_for_test()
    reply = d.handle(make_discover(), None)
    _, _, opts = parse_dhcp(reply)
    assert opts[1] == b"\xff\xff\xff\x00"  # 255.255.255.0, 必须是 4 字节
    assert opts[3] == b"\xc0\xa8\x4d\x01"


def test_proxy_request_gets_ack_with_zero_yiaddr():
    d = dhcp_for_test()
    req = bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))
    req[12:16] = socket.inet_aton("192.168.200.10")  # ciaddr
    req = bytes(bytearray(req)[:240] + encode_opt(53, b"\x03") + b"\xff")
    reply = d.handle_proxy(req, ("192.168.200.10", 4011))
    _, _, opts = parse_dhcp(reply)
    assert opts[53] == b"\x05"  # ACK
    assert socket.inet_ntoa(reply[16:20]) == "0.0.0.0"  # yiaddr 不分 IP
    assert opts[60] == b"PXEClient"
    assert opts[67] == b"shimx64.efi"
    assert socket.inet_ntoa(reply[20:24]) == "192.168.200.1"  # siaddr TFTP


def test_proxy_discover_gets_offer():
    d = dhcp_for_test()
    reply = d.handle_proxy(make_discover(), ("192.168.200.10", 4011))
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
    assert ip.startswith("192.168.200.")
    assert 10 <= int(ip.rsplit(".", 1)[1]) <= 12


def test_same_mac_gets_same_ip():
    d = dhcp_for_test()
    r1 = d.handle(make_discover(mac=b"\x01\x02\x03\x04\x05\x06"), None)
    r2 = d.handle(make_discover(mac=b"\x01\x02\x03\x04\x05\x06"), None)
    assert r1[16:20] == r2[16:20]  # yiaddr


def test_pool_prefix_mismatch_rejected():
    with pytest.raises(ValueError):
        DhcpServer("192.168.200.1", "192.168.200.10", "192.168.78.20",
                   "shim.efi", "pxelinux.0", FakeLog())


def test_request_acks_offered_ip():
    d = dhcp_for_test()
    d.handle(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"), None)
    req = bytearray(make_discover(mac=b"\x0a\x0b\x0c\x0d\x0e\x0f"))
    opts = encode_opt(53, b"\x03") + encode_opt(50, ("192.168.200.10",)) + encode_opt(54, "192.168.200.1")
    req = bytes(bytearray(req[:240]) + opts + b"\xff")
    reply = d.handle(req, None)
    _, _, ropts = parse_dhcp(reply)
    assert ropts[53] == b"\x05"  # ACK
    assert socket.inet_ntoa(reply[16:20]) == "192.168.200.10"


def test_request_outside_pool_naks():
    d = dhcp_for_test()
    req = bytes(bytearray(make_discover())[:240] +
                encode_opt(53, b"\x03") + encode_opt(50, ("10.99.99.99",)) + b"\xff")
    reply = d.handle(req, None)
    assert parse_dhcp(reply)[2][53] == b"\x06"  # NAK


def test_build_reply_preserves_xid_and_chaddr():
    msg = make_discover(mac=b"\xde\xad\xbe\xef\x00\x01")
    reply = build_dhcp_reply(msg, 2, "192.168.200.10", "192.168.200.1", [encode_opt(53, b"\x02")])
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
    port = next(TFTP_PORTS)
    srv = TftpServer("127.0.0.1", str(tmp_path), FakeLog(), port=port, **kw)
    ev = threading.Event()
    threading.Thread(target=srv.serve, args=(ev,), daemon=True).start()
    assert ev.wait(3)
    return srv, port


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
