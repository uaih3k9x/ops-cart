"""web_core 的离线测试: 来源过滤 / 局域网开关 / 会话 / 状态 JSON."""

import json

import pytest

import web_core as wc


# ------------------------------------------------------------ 来源过滤

# tailnet 段 (CGNAT) 只能写这个段里的地址; 都是编的, 不对应真实设备
@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "100.100.0.1", "100.127.255.1",  # public-safe-ignore
                                "fd7a:115c:a1e0::1", "::ffff:100.100.0.2"])             # public-safe-ignore
def test_tailnet_and_loopback_always_allowed(ip):
    assert wc.client_kind(ip) == "trusted"
    assert wc.client_allowed(ip, lan_open=False, pin_set=False)


@pytest.mark.parametrize("ip", ["192.0.2.138", "10.0.0.5", "::ffff:192.0.2.1"])
def test_lan_follows_switch(ip):
    assert wc.client_kind(ip) == "lan"
    assert not wc.client_allowed(ip, lan_open=False, pin_set=True)
    assert wc.client_allowed(ip, lan_open=True, pin_set=True)


def test_lan_refused_without_pin_even_if_open():
    assert not wc.client_allowed("192.0.2.138", lan_open=True, pin_set=False)


def test_garbage_ip_refused():
    assert not wc.client_allowed("not-an-ip", lan_open=True, pin_set=True)


# ------------------------------------------------------------ 配置

def test_conf_defaults_when_missing(tmp_path):
    cfg = wc.web_conf_read(tmp_path / "nope.json")
    assert cfg == {"lan_open": False, "port": 8088}


def test_conf_roundtrip_keeps_other_keys(tmp_path):
    p = tmp_path / "web.conf"
    p.write_text(json.dumps({"port": 9000, "extra": 1}))
    wc.web_conf_set(p, lan_open=True)
    cfg = json.loads(p.read_text())
    assert cfg == {"port": 9000, "extra": 1, "lan_open": True}
    assert wc.web_conf_read(p)["lan_open"] is True


def test_conf_bad_json_falls_back(tmp_path):
    p = tmp_path / "web.conf"
    p.write_text("{oops")
    assert wc.web_conf_read(p)["lan_open"] is False


# ------------------------------------------------------------ 会话

def test_session_issue_check_expire():
    s = wc.Sessions(ttl_s=100)
    tok = s.issue(now=0)
    assert len(tok) >= 32
    assert s.check(tok, now=50)
    assert s.check(tok, now=140)          # 用过就续期
    assert not s.check(tok, now=300)      # 闲置超时
    assert not s.check("forged", now=0)
    assert not s.check(None, now=0)


def test_session_revoke():
    s = wc.Sessions()
    tok = s.issue()
    s.revoke(tok)
    assert not s.check(tok)


def test_cookie_token_parse():
    assert wc.cookie_token("a=1; cart_sid=abc; b=2") == "abc"
    assert wc.cookie_token("") is None
    assert wc.cookie_token(None) is None


# ------------------------------------------------------------ 状态 JSON

def test_status_payload_shape():
    src = {
        "services": lambda: {"pxe-server": (True, False)},
        "nics": lambda: [{"role": "LAN2", "name": "enp3s0", "ip": "192.0.2.228/24",
                          "method": "dhcp", "link": True}],
        "misc": lambda: {"tailscale_ip": "192.0.2.10", "ch9329": True, "uptime": "up 3 days"},
        "temps": lambda: [("CPU", 61.0)],
        "fans": lambda: [("FAN1", 1200)],
        "version": lambda: "1.00.00 (17 bfc0651)",
    }
    p = wc.status_payload(src, now=0)
    assert p["services"] == [{"name": "pxe-server", "enabled": True, "active": False}]
    assert p["nics"][0]["ip"] == "192.0.2.228/24"
    assert p["temps"] == [{"name": "CPU", "c": 61.0, "level": "ok"}]
    assert p["fans"] == [{"name": "FAN1", "rpm": 1200}]
    assert p["version"].startswith("1.00.00")
    json.dumps(p, ensure_ascii=False)     # 能序列化


def test_status_payload_survives_broken_source():
    src = {k: (lambda: 1 / 0) for k in wc.STATUS_KEYS}
    p = wc.status_payload(src, now=0)
    assert set(wc.STATUS_KEYS) <= set(p["errors"])
    json.dumps(p)


# ------------------------------------------------------------ 键盘: 浏览器 code -> HID 报告

def test_keyboard_press_hold_release():
    k = wc.KeyboardState()
    assert k.apply("KeyA", True) == bytes([0, 0, 0x04, 0, 0, 0, 0, 0])
    assert k.apply("ShiftLeft", True) == bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0])
    assert k.apply("KeyA", False) == bytes([0x02, 0, 0, 0, 0, 0, 0, 0])
    assert k.apply("ShiftLeft", False) == bytes(8)


def test_keyboard_right_modifiers_and_specials():
    k = wc.KeyboardState()
    assert k.apply("ControlRight", True)[0] == 0x10
    assert k.apply("AltRight", True)[0] == 0x50
    assert k.apply("Delete", True)[2] == 0x4C
    k2 = wc.KeyboardState()
    assert k2.apply("F12", True)[2] == 0x45
    assert k2.apply("Numpad0", True)[3] == 0x62
    assert k2.apply("ArrowUp", True)[4] == 0x52


def test_keyboard_unknown_or_repeat_is_none():
    k = wc.KeyboardState()
    assert k.apply("LaunchMail", True) is None
    k.apply("KeyB", True)
    assert k.apply("KeyB", True) is None          # 浏览器自动重复不重发
    assert k.apply("KeyC", False) is None         # 没按过的键抬起


def test_keyboard_max_six_keys():
    k = wc.KeyboardState()
    for c in "ABCDEF":
        assert k.apply(f"Key{c}", True) is not None
    assert k.apply("KeyG", True) is None          # 第 7 个丢掉
    assert k.release_all() == bytes(8)
    assert k.apply("KeyG", True)[2] == 0x0A


# ------------------------------------------------------------ 鼠标

def test_mouse_chunks_split_big_moves():
    assert wc.mouse_chunks(10, -5, 0) == [(10, -5, 0)]
    chunks = wc.mouse_chunks(300, 0, 0)
    assert sum(c[0] for c in chunks) == 300 and all(abs(c[0]) <= 127 for c in chunks)
    assert wc.mouse_chunks(0, 0, -3) == [(0, 0, -1)]   # 滚轮夹到 ±1
    assert wc.mouse_chunks(0, 0, 0) == [(0, 0, 0)]


def test_mouse_buttons_mask():
    assert wc.mouse_buttons(1) == 1          # 浏览器 buttons: 左1 右2 中4, 跟 HID 一样
    assert wc.mouse_buttons(7) == 7
    assert wc.mouse_buttons(255) == 7


# ------------------------------------------------------------ 视频

def test_video_device_validation(tmp_path):
    (tmp_path / "video2").write_text("")
    assert wc.video_device("video2", root=tmp_path) == f"{tmp_path}/video2"
    assert wc.video_device("video9", root=tmp_path) is None
    assert wc.video_device("../etc/passwd", root=tmp_path) is None
    assert wc.video_device("sda", root=tmp_path) is None


def test_video_cmd():
    cmd = wc.video_cmd("/dev/video2", fps=15, quality=6)
    assert cmd[0] == "ffmpeg" and "/dev/video2" in cmd
    assert cmd[cmd.index("-f", cmd.index("/dev/video2")) + 1] == "mpjpeg"
    assert cmd[-1] == "pipe:1"


# ------------------------------------------------------------ 串口

def test_serial_ports_lists_running_brokers(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "ttyUSB0.json").write_text('{"port": 40001}')
    (run / "bad.json").write_text("{oops")
    (run / "notes.txt").write_text("x")
    assert wc.serial_ports(tmp_path) == [{"name": "ttyUSB0", "port": 40001}]
    assert wc.serial_ports(tmp_path / "missing") == []


# ------------------------------------------------------------ 1.06.00: 来源原因 / 装机口排除

LAN1_NET = ["192.0.2.0/24"]


def test_source_reason_explains_refusal():
    assert wc.source_reason("127.0.0.1", lan_open=False, pin_set=False) is None
    assert wc.source_reason("198.51.100.7", lan_open=False, pin_set=True) == "未开放局域网访问"
    assert "未设 PIN" in wc.source_reason("198.51.100.7", lan_open=True, pin_set=False)
    assert wc.source_reason("198.51.100.7", lan_open=True, pin_set=True) is None
    assert wc.source_reason("not-an-ip", lan_open=True, pin_set=True) is not None


def test_install_port_net_refused_even_when_lan_open():
    r = wc.source_reason("192.0.2.50", lan_open=True, pin_set=True, deny_nets=LAN1_NET)
    assert r is not None and "装机口" in r
    assert wc.source_reason("::ffff:192.0.2.50", True, True, deny_nets=LAN1_NET) is not None
    assert wc.source_reason("198.51.100.7", True, True, deny_nets=LAN1_NET) is None


def test_install_port_local_address_refused():
    """被测机在 LAN1 上手配了别的网段: 看它连进来的是推车哪个地址."""
    r = wc.source_reason("203.0.113.9", True, True, deny_nets=LAN1_NET, local_ip="192.0.2.1")
    assert r is not None and "装机口" in r


# ------------------------------------------------------------ PIN 状态: 读不到 != 没设

def test_pin_state(tmp_path):
    assert wc.pin_state(tmp_path / "none") == ("unset", None)
    p = tmp_path / "kiosk.conf"
    p.write_text('{"pin": "pbkdf2$1$aa$bb"}')
    assert wc.pin_state(p) == ("set", "pbkdf2$1$aa$bb")
    p.write_text('{"other": 1}')
    assert wc.pin_state(p) == ("unset", None)
    p.write_text("{oops")
    assert wc.pin_state(p)[0] == "unreadable"
    assert wc.pin_state(tmp_path)[0] == "unreadable"          # 是目录 / 没权限之类


# ------------------------------------------------------------ 会话绑定 PIN 与来源

def test_session_dies_when_pin_changes():
    s = wc.Sessions()
    tok = s.issue("trusted", pin_tag="A", now=0)
    assert s.check(tok, now=1, pin_tag="A")
    assert not s.check(tok, now=2, pin_tag="B")
    assert not s.check(tok, now=3, pin_tag="A")            # 作废了就别复活


def test_session_absolute_ttl():
    s = wc.Sessions(ttl_s=100, max_age_s=250)
    tok = s.issue("trusted", now=0)
    for t in (90, 180):
        assert s.check(tok, now=t)
    assert not s.check(tok, now=260)                       # 一直在用也有上限


def test_session_revoke_kind_returns_tokens():
    s = wc.Sessions()
    a, b = s.issue("lan", now=0), s.issue("trusted", now=0)
    assert s.revoke_kind("lan") == [a]
    assert not s.check(a, now=1) and s.check(b, now=1)


def test_session_issue_prunes_expired():
    s = wc.Sessions(ttl_s=10)
    for i in range(5):
        s.issue("lan", now=0)
    s.issue("lan", now=100)
    assert len(s) == 1


# ------------------------------------------------------------ 键鼠请求校验

def test_parse_hid_events_normalizes():
    ev = wc.parse_hid_events({"events": [
        {"t": "k", "code": "KeyA", "down": 1},
        {"t": "m", "dx": 3.7, "dy": -2, "b": 9, "w": -5},
        {"t": "release"}]})
    assert ev == [{"t": "k", "code": "KeyA", "down": True},
                  {"t": "m", "dx": 3, "dy": -2, "b": 1, "w": -1},
                  {"t": "release"}]


def test_parse_hid_events_clamps_huge_moves():
    ev = wc.parse_hid_events({"events": [{"t": "m", "dx": 100000000, "dy": -1e9}]})
    assert ev[0]["dx"] == wc.MOUSE_MAX and ev[0]["dy"] == -wc.MOUSE_MAX
    assert len(wc.mouse_chunks(10 ** 8, 0, 0)) <= wc.MOUSE_MAX // 127 + 1


@pytest.mark.parametrize("body", [
    [1, 2], {"events": "xx"}, {"events": [1]}, {"events": [{"t": "m", "dx": "abc"}]},
    {"events": [{"t": "m", "dx": float("inf")}]}, {"events": [{"t": "m", "dx": True}]},
    {"events": [{"t": "k", "code": 5, "down": True}]}, {"events": [{"t": "zz"}]},
    {"events": [{"t": "release"}] * (wc.HID_MAX_EVENTS + 1)},
])
def test_parse_hid_events_rejects_garbage(body):
    with pytest.raises(ValueError):
        wc.parse_hid_events(body)


# ------------------------------------------------------------ 流: 定时复查鉴权

def test_relay_stops_when_auth_lost():
    chunks = iter([b"a", None, b"b", b"c", b"d"])
    out, oks, clock = [], iter([True, False]), iter(range(0, 100, 3))
    wc.relay(lambda: next(chunks), out.append, lambda: next(oks), clock=lambda: next(clock), every=2)
    assert out == [b"a"]                  # 第二次复查不过: 读超时那一下就收摊, 后面的不再写


def test_relay_ends_on_eof():
    chunks = iter([b"a", b""])
    out = []
    wc.relay(lambda: next(chunks), out.append, lambda: True, clock=lambda: 0, every=2)
    assert out == [b"a"]


# ============================================================ C 线: 请求来源头、会话锁

def test_request_reason_host_and_origin():
    ok = {"Host": "192.0.2.1:8088"}
    assert wc.request_reason("GET", ok) is None
    assert wc.request_reason("GET", {"Host": "x.example"}) is not None
    assert wc.request_reason("GET", {"Host": "x.example"}, hosts=["X.example"]) is None
    assert wc.request_reason("POST", dict(ok, Origin="http://192.0.2.1:8088")) is None
    assert wc.request_reason("POST", dict(ok, Origin="https://192.0.2.1:8088")) is None
    assert wc.request_reason("POST", dict(ok, Origin="http://192.0.2.1")) is not None
    assert wc.request_reason("POST", ok) is not None
    assert wc.request_reason("POST", dict(ok, **{"Sec-Fetch-Site": "same-origin"})) is None
    assert wc.request_reason("POST", dict(ok, **{"Sec-Fetch-Site": "same-site"})) is not None


def test_host_name_parsing():
    assert wc.host_name("[2001:db8::1]:80") == "2001:db8::1"
    assert wc.host_name("Example.ORG:8088") == "example.org"
    assert wc.host_name("192.0.2.1") == "192.0.2.1"


def test_sessions_kind_and_prefix_are_safe():
    s = wc.Sessions()
    tok = s.issue("lan", now=0)
    assert s.kind(tok) == "lan" and s.kind("nope") is None
    tag = wc.session_tag(tok)
    assert len(tag) == 8 and tag not in tok and wc.session_tag(None) == "-"


def test_abs_coords_full_box_and_letterbox():
    assert wc.abs_coords(0, 0, 100, 50) == (0, 0, True)
    assert wc.abs_coords(100, 50, 100, 50) == (4095, 4095, True)
    assert wc.abs_coords(50, 25, 100, 50) == (2048, 2048, True)
    # 方形画面放进 2:1 的框: 左右各 25 像素黑边
    assert wc.abs_coords(25, 0, 100, 50, 640, 640) == (0, 0, True)
    assert wc.abs_coords(75, 50, 100, 50, 640, 640) == (4095, 4095, True)
    assert wc.abs_coords(10, 25, 100, 50, 640, 640) == (0, 2048, False)    # 黑边上: 夹到边, 标出来
    assert wc.abs_coords(5, 5, 0, 0) == (0, 0, False)


def test_parse_hid_events_abs():
    out = wc.parse_hid_events({"events": [{"t": "a", "x": 50, "y": 25, "vw": 100, "vh": 50, "b": 9, "w": 3}]})
    assert out == [{"t": "a", "x": 2048, "y": 2048, "inside": True, "b": 1, "w": 1}]
    with pytest.raises(ValueError):
        wc.parse_hid_events({"events": [{"t": "a", "x": "1", "y": 0, "vw": 1, "vh": 1}]})
