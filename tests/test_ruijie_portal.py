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
    loc = "http://10.98.232.49/eportal/index.jsp?wlanssid=CAMPUSNET&wlanuserip=10.99.97.104&mac=x"
    assert rp.split_eportal(loc) == ("http://10.98.232.49/eportal/",
                                     "wlanssid=CAMPUSNET&wlanuserip=10.99.97.104&mac=x")
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
    assert rp.classify(302, "http://10.98.232.49/eportal/index.jsp?a=1", "") == "portal"
    assert rp.classify(0, "", "") == "down"


def test_parse_login_result():
    assert rp.parse_result('{"result":"success","message":"","userIndex":"x"}') == (True, "")
    assert rp.parse_result('{"result":"fail","message":"密码错误"}') == (False, "密码错误")
    assert rp.parse_result("<html>") == (False, "<html>")


# ------------------------------------------------------------ 只在 CAMPUSNET 上跑 / 结果可见 (2026-10-22)

def test_parse_iw_ssid():
    text = "Connected to 02:00:00:86:ae:38 (on wlp4s0)\n\tSSID: CAMPUSNET\n\tfreq: 5300.0\n"
    assert rp.parse_iw_ssid(text) == "CAMPUSNET"
    assert rp.parse_iw_ssid("Not connected.\n") is None


def test_on_target_case_insensitive():
    assert rp.on_target("CAMPUSNET", {"ssid": "CAMPUSNET"})
    assert rp.on_target("campusnet", {})                       # 老配置没写 ssid, 默认 CAMPUSNET
    assert not rp.on_target("ExampleCoLtd_5G", {})          # 别的网的锐捷页不能交 CAMPUSNET 的密码
    assert not rp.on_target(None, {})


def test_record_marks_ok_and_fail():
    ok = rp.record("online", "", now=100)
    assert ok == {"time": 100, "state": "online", "ok": True, "message": ""}
    assert rp.record("skip", "连的是 X", now=1)["ok"] is True
    assert rp.record("login ok", "", now=1)["ok"] is True
    for bad in ("login fail", "error", "down", "portal?"):
        assert rp.record(bad, "x", now=1)["ok"] is False


def test_changed_only_on_state_or_message():
    a = rp.record("online", "", now=1)
    assert rp.changed(None, a)
    assert not rp.changed(a, rp.record("online", "", now=999))   # 时间变了不算, 不刷日志
    assert rp.changed(a, rp.record("login fail", "密码错误", now=2))
    f = rp.record("login fail", "密码错误", now=2)
    assert rp.changed(f, rp.record("login fail", "账号欠费", now=3))


def test_conf_update_keeps_other_keys_and_validates():
    old = {"iface": "wlp4s0", "user": "a", "password": "b"}
    new = rp.conf_update(old, " 2025xxxx ", "p w")
    assert new == {"iface": "wlp4s0", "ssid": "CAMPUSNET", "user": "2025xxxx", "password": "p w"}
    assert old["user"] == "a"                                   # 不改原对象
    assert rp.conf_update(None, "u", "p")["iface"] == "wlp4s0"   # 没有配置也能新建
    for u, p in (("", "p"), ("u", "")):
        try:
            rp.conf_update(old, u, p)
            assert False
        except ValueError:
            pass


def test_attempt_wraps_exceptions(monkeypatch):
    def boom(conf):
        raise ValueError("pageInfo 不是 JSON")
    monkeypatch.setattr(rp, "run_once", boom)
    r = rp.attempt({"iface": "wlp4s0"}, "CAMPUSNET", now=5)
    assert r["state"] == "error" and "pageInfo" in r["message"] and r["ok"] is False


def test_attempt_skips_other_networks(monkeypatch):
    monkeypatch.setattr(rp, "run_once", lambda conf: (_ for _ in ()).throw(AssertionError("不该跑")))
    r = rp.attempt({"iface": "wlp4s0"}, "ExampleCoLtd_5G", now=5)
    assert r["state"] == "skip" and "ExampleCoLtd_5G" in r["message"] and r["ok"]


def test_attempt_splits_login_fail_message(monkeypatch):
    monkeypatch.setattr(rp, "run_once", lambda conf: "login fail: 密码错误")
    r = rp.attempt({"iface": "wlp4s0"}, "CAMPUSNET", now=5)
    assert (r["state"], r["message"], r["ok"]) == ("login fail", "密码错误", False)


def test_conf_save_is_root_only_and_roundtrips(tmp_path):
    import os, stat
    p = tmp_path / "ops-cart" / "campusnet.json"
    rp.conf_save({"iface": "wlp4s0", "ssid": "CAMPUSNET", "user": "u", "password": "密码"}, path=str(p))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600            # 有密码, 只给 root 读
    assert rp._read_json(str(p))["password"] == "密码"
    p.chmod(0o644)
    rp.conf_save({"user": "x", "password": "y"}, path=str(p))    # 覆盖时也收紧权限
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
