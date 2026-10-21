import struct

import ruijie_portal as rp


def test_ec_matches_js_encode_uri_component():
    assert rp.ec("a b&c=d/é!*'()~") == "a%20b%26c%3Dd%2F%C3%A9!*'()~"


def test_encrypt_password_is_textbook_rsa_of_pw_mac():
    """页面 JS: 反转 "密码>mac" 后按小端两字节一位拼大数 == 原串按大端解释; 不填充, 直接 m^e mod n.
    输出按模长补齐的十六进制 (1024 位 = 256 字符)."""
    n = (1 << 1023) + 1155                     # 随便一个 1024 位奇数, 只测编码方向和格式
    out = rp.encrypt_password("secret", "10001", format(n, "x"))
    assert len(out) == 256
    assert int(out, 16) == pow(int.from_bytes(b"secret>111111111", "big"), 0x10001, n)


def test_encrypt_password_custom_mac():
    n = (1 << 1023) + 1155
    out = rp.encrypt_password("pw", "10001", format(n, "x"), mac="abc")
    assert int(out, 16) == pow(int.from_bytes(b"pw>abc", "big"), 0x10001, n)


def test_split_eportal():
    loc = "http://10.10.232.49/eportal/index.jsp?wlanssid=SCNUNET&wlanuserip=10.253.97.104&mac=x"
    assert rp.split_eportal(loc) == ("http://10.10.232.49/eportal/",
                                     "wlanssid=SCNUNET&wlanuserip=10.253.97.104&mac=x")
    assert rp.split_eportal("http://auth-a1.wifi.com/?x=1") is None


def test_mac_from_query_like_js():
    """JS 用 [?&]mac= 取; 取不到用 111111111. wlanusermac 不算."""
    assert rp.mac_from_query("wlanusermac=84FD&a=1") == "111111111"
    assert rp.mac_from_query("a=1&mac=abc&b=2") == "abc"


def test_login_body_double_encodes_like_js():
    body = rp.login_body("3178", "p w", "a=1&b=2")
    assert body == ("userId=3178&password=p%2520w&service=&queryString=a%253D1%2526b%253D2"
                    "&operatorPwd=&operatorUserId=&validcode=&passwordEncrypt=true")


def test_dns_query_roundtrip():
    q = rp.dns_query(0x1234, "auth.wifi.com")
    assert q[:2] == b"\x12\x34" and b"\x04auth\x04wifi\x03com\x00" in q
    # 伪造一个应答: 头 + 原问题 + 一条压缩指针 A 记录
    ans = (struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + q[12:]
           + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([2, 2, 2, 1]))
    assert rp.dns_parse_a(ans) == ["2.2.2.1"]


def test_classify_probe():
    assert rp.classify(200, "", "Microsoft Connect Test") == "online"
    assert rp.classify(302, "http://auth-a1.wifi.com/?x=1", "") == "portal"
    assert rp.classify(302, "http://10.10.232.49/eportal/index.jsp?a=1", "") == "portal"
    assert rp.classify(0, "", "") == "down"


def test_parse_login_result():
    assert rp.parse_result('{"result":"success","message":"","userIndex":"x"}') == (True, "")
    assert rp.parse_result('{"result":"fail","message":"密码错误"}') == (False, "密码错误")
    assert rp.parse_result("<html>") == (False, "<html>")
