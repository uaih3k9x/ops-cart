"""webkiosk 路由层离线测试: App.handle 是纯方法, 不起 socket."""

import json

import kiosk_core as kc
import web_core as wc
import webkiosk

TAIL = "100.100.0.1"           # tailnet 段里编的地址  public-safe-ignore
LAN = "198.51.100.7"
PXE_NET_HOST = "192.0.2.50"          # 装机口网段 (测试里的 pxe.env 给的是 192.0.2.1/24)
SERIAL = "0000000001"


def make_app(tmp_path, pin="2468", lan_open=False, bound=True):
    conf = tmp_path / "web.conf"
    wc.web_conf_set(conf, lan_open=lan_open)
    pin_path = tmp_path / "kiosk.conf"
    if pin:
        kc.pin_set(pin, pin_path)
    env = tmp_path / "pxe.env"
    env.write_text("PXE_IP=192.0.2.1\nPXE_PREFIX=24\n")
    src = {k: (lambda k=k: {"services": {}, "nics": [], "misc": {}, "temps": [],
                            "fans": [], "version": "Ver test"}[k]) for k in wc.STATUS_KEYS}
    app = webkiosk.App(conf_path=conf, pin_path=pin_path, status_src=src,
                       selftest=lambda: [{"label": "LAN2", "state": "OK", "detail": "x"}],
                       static_dir=tmp_path, pxe_env=env)
    # 画面源 / 键鼠绑定: 一块采集卡 video0 绑在一块在线的 CH9329 上; video1 是没绑的摄像头
    app.video_nodes = lambda: [{"dev": "/dev/video0", "name": "SC0710 PCI", "key": "0000:02:00.0",
                                "friendly": "SC0710 卡1"},
                               {"dev": "/dev/video2", "name": "USB Cam", "key": "1234:5678",
                                "friendly": "USB Cam"}]
    app.kvm_load = lambda: {"names": {}, "bind": {"0000:02:00.0": SERIAL} if bound else {}}
    app.kvm_ports = lambda: {SERIAL: "/dev/serial/by-id/test-" + SERIAL}
    return app


HOST = "192.0.2.10:8088"               # 浏览器地址栏里的推车地址 (IP 字面量, 白名单默认放行)
ORIGIN = "http://" + HOST


def hdrs_for(method, cookie=None, **extra):
    h = {"Host": HOST}
    if method == "POST":
        h["Origin"] = ORIGIN
    if cookie:
        h["Cookie"] = cookie
    h.update(extra)
    return h


def call(app, method, path, ip=TAIL, body=None, cookie=None):
    headers = hdrs_for(method, cookie)
    raw = json.dumps(body).encode() if body is not None else b""
    status, hdrs, out = app.handle(method, path, ip, headers, raw)
    try:
        data = json.loads(out)
    except ValueError:
        data = out
    return status, hdrs, data


def login(app, ip=TAIL, pin="2468"):
    status, hdrs, data = call(app, "POST", "/api/login", ip, {"pin": pin})
    assert status == 200 and data["result"] == "ok", data
    return hdrs["Set-Cookie"].split(";")[0]


# ------------------------------------------------------------ 来源 / 局域网开关

def test_lan_blocked_by_default(tmp_path):
    app = make_app(tmp_path)
    assert call(app, "GET", "/api/whoami", LAN)[0] == 403


def test_lan_allowed_when_switch_on(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    status, _, data = call(app, "GET", "/api/whoami", LAN)
    assert status == 200 and data["kind"] == "lan" and data["authed"] is False


def test_lan_blocked_without_pin_even_if_on(tmp_path):
    app = make_app(tmp_path, pin=None, lan_open=True)
    assert call(app, "GET", "/api/whoami", LAN)[0] == 403


def test_switch_change_takes_effect_live(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    status, _, data = call(app, "POST", "/api/lan", TAIL, {"open": True}, ck)
    assert status == 200 and data["lan_open"] is True
    assert call(app, "GET", "/api/whoami", LAN)[0] == 200
    call(app, "POST", "/api/lan", TAIL, {"open": False}, ck)
    assert call(app, "GET", "/api/whoami", LAN)[0] == 403


def test_lan_client_cannot_flip_switch(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    ck = login(app, LAN)
    assert call(app, "POST", "/api/lan", LAN, {"open": False}, ck)[0] == 403


# ------------------------------------------------------------ 登录

def test_api_needs_login(tmp_path):
    app = make_app(tmp_path)
    assert call(app, "GET", "/api/status")[0] == 401


def test_wrong_pin_then_lockout(tmp_path):
    app = make_app(tmp_path)
    results = [call(app, "POST", "/api/login", TAIL, {"pin": "0000"})[2]["result"] for _ in range(5)]
    assert results[:4] == ["bad"] * 4 and results[4] == "locked"
    assert call(app, "POST", "/api/login", TAIL, {"pin": "2468"})[2]["result"] == "locked"


def test_login_cookie_is_httponly_and_works(tmp_path):
    app = make_app(tmp_path)
    status, hdrs, _ = call(app, "POST", "/api/login", TAIL, {"pin": "2468"})
    assert "HttpOnly" in hdrs["Set-Cookie"] and "SameSite=Strict" in hdrs["Set-Cookie"]
    ck = hdrs["Set-Cookie"].split(";")[0]
    assert call(app, "GET", "/api/status", cookie=ck)[0] == 200


def test_no_pin_set_tailnet_logs_in_freely(tmp_path):
    app = make_app(tmp_path, pin=None)
    assert call(app, "POST", "/api/login", TAIL, {"pin": ""})[2]["result"] == "ok"


def test_logout_revokes(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    call(app, "POST", "/api/logout", cookie=ck)
    assert call(app, "GET", "/api/status", cookie=ck)[0] == 401


def test_bad_json_body_is_400(tmp_path):
    app = make_app(tmp_path)
    status, _, _ = app.handle("POST", "/api/login", TAIL, hdrs_for("POST"), b"{nope")
    assert status == 400


# ------------------------------------------------------------ 数据接口

def test_status_and_selftest(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    status, _, data = call(app, "GET", "/api/status", cookie=ck)
    assert status == 200 and data["version"] == "Ver test"
    status, _, data = call(app, "GET", "/api/selftest", cookie=ck)
    assert data["rows"][0]["state"] == "OK"


def test_index_served_and_unknown_404(tmp_path):
    (tmp_path / "index.html").write_text("<title>cart</title>")
    app = make_app(tmp_path)
    status, hdrs, body = call(app, "GET", "/")
    assert status == 200 and "text/html" in hdrs["Content-Type"] and b"cart" in body
    assert call(app, "GET", "/nope")[0] == 404


# ------------------------------------------------------------ 键鼠透传

class FakeCh:
    def __init__(self):
        self.frames, self.mice, self.abs = [], [], []

    def send_report(self, report):
        self.frames.append(report)

    def send_mouse(self, buttons, dx, dy, wheel):
        self.mice.append((buttons, dx, dy, wheel))

    def send_mouse_abs(self, buttons, x, y, wheel):
        self.abs.append((buttons, x, y, wheel))


def test_hid_events_reach_ch9329(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    ev = [{"t": "k", "code": "ControlLeft", "down": True},
          {"t": "k", "code": "KeyC", "down": True},
          {"t": "k", "code": "KeyC", "down": True},          # 自动重复, 不重发
          {"t": "m", "dx": 200, "dy": 0, "b": 1, "w": 0}]
    status, _, data = call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": ev}, ck)
    assert status == 200 and data["ok"]
    assert fake.frames == [bytes([1, 0, 0, 0, 0, 0, 0, 0]), bytes([1, 0, 0x06, 0, 0, 0, 0, 0])]
    assert fake.mice == [(1, 127, 0, 0), (1, 73, 0, 0)]
    call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": [{"t": "release"}]}, ck)
    assert fake.frames[-1] == bytes(8) and fake.mice[-1] == (0, 0, 0, 0)


def test_hid_without_ch9329_is_503(tmp_path):
    app = make_app(tmp_path)
    app.ch_open = lambda path: None
    ck = login(app)
    status, _, data = call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": [{"t": "release"}]}, ck)
    assert status == 503


def test_hid_send_failure_drops_handle(tmp_path):
    app = make_app(tmp_path)

    class Broken(FakeCh):
        def send_report(self, report):
            raise OSError("unplugged")

    opened = []
    app.ch_open = lambda path: opened.append(1) or Broken()
    ck = login(app)
    ev = {"src": "video0", "events": [{"t": "k", "code": "KeyA", "down": True}]}
    assert call(app, "POST", "/api/hid", TAIL, ev, ck)[0] == 503
    call(app, "POST", "/api/hid", TAIL, ev, ck)
    assert len(opened) == 2                                 # 下次重开


def test_hid_needs_login(tmp_path):
    app = make_app(tmp_path)
    assert call(app, "POST", "/api/hid", TAIL, {"events": []})[0] == 401


def test_type_text(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    status, _, data = call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": "aB"}, ck)
    assert status == 200 and data["sent"] == 2 and data["skipped"] == 0
    assert fake.frames == [bytes([0, 0, 0x04, 0, 0, 0, 0, 0]), bytes(8),
                           bytes([2, 0, 0x05, 0, 0, 0, 0, 0]), bytes(8),
                           bytes(8)]                            # 结束时真发一次全部松开


# ------------------------------------------------------------ 视频 / 串口 列表

def test_video_list_uses_capture_nodes_and_binding(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    assert call(app, "GET", "/api/videos", cookie=ck)[2] == {"devices": [
        {"dev": "video0", "name": "SC0710 卡1", "kvm": "ok", "kvm_label": "键鼠 …0001"},
        {"dev": "video2", "name": "USB Cam", "kvm": "unbound", "kvm_label": "不绑定"}]}


def test_serial_list(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "ttyUSB0.json").write_text('{"port": 1}')
    app.serial_dir = lambda: str(tmp_path)
    ck = login(app)
    assert call(app, "GET", "/api/serial/ports", cookie=ck)[2] == {"ports": ["ttyUSB0"]}


def test_serial_send_without_stream_is_409(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    assert call(app, "POST", "/api/serial/send", TAIL, {"cid": "c1", "data": "aGk="}, ck)[0] == 409


def test_serial_send_goes_to_session_socket(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    sent = []

    class Sock:
        def sendall(self, b):
            sent.append(b)

    app.serial_socks[(ck.split("=", 1)[1], "c1")] = Sock()
    status, _, _ = call(app, "POST", "/api/serial/send", TAIL, {"cid": "c1", "data": "aGkN"}, ck)
    assert status == 200 and sent == [b"hi\r"]
    assert call(app, "POST", "/api/serial/send", TAIL, {"cid": "c2", "data": "aGkN"}, ck)[0] == 409


# ============================================================ 1.06.00 复审修复

import http.client
import threading
import time

import pytest


# ------------------------------------------------------------ 会话跟着 PIN / 局域网开关走

def test_pin_change_kills_old_sessions(tmp_path):
    app = make_app(tmp_path, pin=None)
    ck = login(app, pin="whatever")                       # 没设 PIN: tailnet 随便进
    kc.pin_set("2468", app.pin_path)
    _, _, who = call(app, "GET", "/api/whoami", cookie=ck)
    assert who["authed"] is False
    assert call(app, "GET", "/api/status", cookie=ck)[0] == 401


def test_lan_switch_off_revokes_lan_sessions_for_good(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    lan_ck, tail_ck = login(app, LAN), login(app)
    call(app, "POST", "/api/lan", TAIL, {"open": False}, tail_ck)
    call(app, "POST", "/api/lan", TAIL, {"open": True}, tail_ck)
    assert call(app, "GET", "/api/status", LAN, cookie=lan_ck)[0] == 401
    assert call(app, "GET", "/api/status", TAIL, cookie=tail_ck)[0] == 200


def test_streams_closed_on_logout_lan_off_and_pin_change(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    tail_ck, lan_ck = login(app), login(app, LAN)
    tail_tok, lan_tok = tail_ck.split("=", 1)[1], lan_ck.split("=", 1)[1]
    closed = []
    app.stream_register(lan_tok, lambda: closed.append("lan"))
    call(app, "POST", "/api/lan", TAIL, {"open": False}, tail_ck)
    assert closed == ["lan"]
    app.stream_register(tail_tok, lambda: closed.append("tail"))
    call(app, "POST", "/api/logout", TAIL, {}, tail_ck)
    assert closed == ["lan", "tail"]
    ck2 = login(app)
    app.stream_register(ck2.split("=", 1)[1], lambda: closed.append("pin"))
    kc.pin_set("1357", app.pin_path)
    app.gate_for(TAIL)                                    # 任何一次鉴权都会发现 PIN 变了
    assert closed[-1] == "pin"


def test_stream_ok_rechecks_source_and_session(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    ck = login(app, LAN)
    tok = ck.split("=", 1)[1]
    assert app.stream_ok(LAN, tok)
    wc.web_conf_set(app.conf_path, lan_open=False)        # 别处 (车机) 关了开关
    assert not app.stream_ok(LAN, tok)


# ------------------------------------------------------------ 输错锁定按来源分开

def test_lan_lockout_does_not_lock_out_tailnet(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    for _ in range(5):
        r = call(app, "POST", "/api/login", LAN, {"pin": "0000"})[2]["result"]
    assert r == "locked"
    assert call(app, "POST", "/api/login", TAIL, {"pin": "2468"})[2]["result"] == "ok"


def test_lan_gate_backs_off(tmp_path):
    app = make_app(tmp_path)
    assert app.gate_for(LAN).max_lock_s > app.gate_for(LAN).lock_s
    assert app.gate_for(TAIL).max_lock_s == app.gate_for(TAIL).lock_s


def test_unreadable_pin_refuses_everyone(tmp_path):
    app = make_app(tmp_path, pin=None)
    app.pin_path = tmp_path                               # 目录: 读不了
    status, _, data = call(app, "POST", "/api/login", TAIL, {"pin": ""})
    assert data["result"] == "unavailable"
    _, _, who = call(app, "GET", "/api/whoami")
    assert who["authed"] is False


# ------------------------------------------------------------ 来源: 装机口 / 原因

def test_install_port_source_refused_even_when_lan_open(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    status, _, body = call(app, "GET", "/api/whoami", PXE_NET_HOST)
    assert status == 403 and "装机口" in body.decode()


def test_lan_open_without_pin_says_why(tmp_path):
    app = make_app(tmp_path, pin=None, lan_open=True)
    status, _, body = call(app, "GET", "/api/whoami", LAN)
    assert status == 403 and "未设 PIN" in body.decode()


# ------------------------------------------------------------ 畸形请求: 400, 不碰 CH9329

@pytest.mark.parametrize("body", [
    [1, 2], {"src": "video0", "events": "xx"}, {"src": "video0", "events": [{"t": "m", "dx": "abc"}]},
    {"src": "video0", "events": [{"t": "m", "dx": 1e400}]}, {"src": "video0", "events": [7]},
])
def test_malformed_hid_is_400_without_opening_ch(tmp_path, body):
    app = make_app(tmp_path)
    opened = []
    app.ch_open = lambda path: opened.append(path)
    ck = login(app)
    raw = json.dumps(body).replace("1e+400", "1e400").encode()
    status, _, _ = app.handle("POST", "/api/hid", TAIL, hdrs_for("POST", ck), raw)
    assert status == 400 and opened == []


def test_huge_mouse_move_sends_bounded_frames(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": [{"t": "m", "dx": 100000000}]}, ck)
    assert 0 < len(fake.mice) <= wc.MOUSE_MAX // 127 + 1


@pytest.mark.parametrize("path,body", [("/api/login", [1]), ("/api/type", {"src": "video0", "text": 5}),
                                       ("/api/serial/send", {"data": 123}), ("/api/lan", "x")])
def test_non_object_or_wrong_type_bodies_are_400(tmp_path, path, body):
    app = make_app(tmp_path)
    ck = login(app)
    app.serial_socks[(ck.split("=", 1)[1], "c1")] = object()
    if isinstance(body, dict) and path == "/api/serial/send":
        body = dict(body, cid="c1")
    assert call(app, "POST", path, TAIL, body, ck)[0] == 400


def test_type_text_has_length_cap(tmp_path):
    app = make_app(tmp_path)
    app.ch_open = lambda path: FakeCh()
    ck = login(app)
    big = "a" * (webkiosk.TYPE_MAX + 1)
    assert call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": big}, ck)[0] == 413


# ------------------------------------------------------------ 键鼠按绑定走

def test_hid_unbound_source_is_409_and_never_opens(tmp_path):
    app = make_app(tmp_path)
    opened = []
    app.ch_open = lambda path: opened.append(path)
    ck = login(app)
    for src in ("video2", "video9", None):
        status, _, data = call(app, "POST", "/api/hid", TAIL, {"src": src, "events": [{"t": "release"}]}, ck)
        assert status == 409, src
    assert opened == []


def test_hid_offline_binding_is_409(tmp_path):
    app = make_app(tmp_path)
    app.kvm_ports = lambda: {}
    opened = []
    app.ch_open = lambda path: opened.append(path)
    ck = login(app)
    status, _, data = call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": []}, ck)
    assert status == 409 and "不在线" in data["error"] and opened == []


def test_hid_opens_the_bound_port(tmp_path):
    app = make_app(tmp_path)
    opened = []
    app.ch_open = lambda path: opened.append(path) or FakeCh()
    ck = login(app)
    call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": [{"t": "release"}]}, ck)
    assert opened == ["/dev/serial/by-id/test-" + SERIAL]


def test_open_video_only_capture_nodes(tmp_path):
    app = make_app(tmp_path)
    assert app.video_dev("video1") is None                # UVC 元数据节点 / 不存在
    assert app.video_dev("../video0") is None
    assert app.video_dev("video0") == "/dev/video0"


def test_second_viewer_replaces_first_ffmpeg(tmp_path):
    app = make_app(tmp_path)
    procs = []

    class P:
        def __init__(self, *a, **k):
            self.killed = False
            procs.append(self)

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    app.popen = P
    app.open_video("video0")
    app.open_video("video0")
    assert [p.killed for p in procs] == [True, False]


# ------------------------------------------------------------ 串口: 同一会话的发送不交错

def test_serial_send_is_serialized_per_session(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    inside, overlap = [0], []

    class SlowSock:
        def sendall(self, b):
            inside[0] += 1
            overlap.append(inside[0])
            time.sleep(0.01)
            inside[0] -= 1

    app.serial_socks[(ck.split("=", 1)[1], "c1")] = SlowSock()
    ts = [threading.Thread(target=call, args=(app, "POST", "/api/serial/send", TAIL,
                                               {"cid": "c1", "data": "YQ=="}, ck))
          for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert max(overlap) == 1


def test_web_page_queues_serial_input():
    """前端串口输入走一条串行队列 (上一个 POST 回来之前攒着), 不再每个键各发各的."""
    import pathlib
    html = (pathlib.Path(webkiosk.STATIC_DIR) / "index.html").read_text()
    assert "serialPump" in html and "api(\"/api/serial/send\", { data: b64(d) })" not in html


def test_web_page_has_no_emoji():
    import pathlib
    import re
    html = (pathlib.Path(webkiosk.STATIC_DIR) / "index.html").read_text()
    assert not re.search("[\U0001F000-\U0001FAFF☀-➿]", html)
    assert 'rel="icon"' in html


# ------------------------------------------------------------ 真 socket: HTTP/1.1、超限、坏头 (只在 127.0.0.1)

@pytest.fixture
def served(tmp_path):
    (tmp_path / "index.html").write_text("<title>cart</title>")
    app = make_app(tmp_path)

    class H(webkiosk.Handler):
        pass
    H.app = app
    srv = webkiosk.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return c, r, r.read()


def test_http11_keepalive(served):
    _app, port = served
    c, r, _ = _req(port, "GET", "/api/whoami")
    assert r.version == 11 and r.status == 200
    c.request("GET", "/api/whoami")                       # 同一条连接再来一次
    assert c.getresponse().status == 200


def test_body_too_large_is_413(served):
    _app, port = served
    _, r, _ = _req(port, "POST", "/api/login", body=b"x" * (webkiosk.BODY_MAX + 1),
                   headers={"Origin": f"http://127.0.0.1:{port}"})
    assert r.status == 413


@pytest.mark.parametrize("path,hdrs", [("/api/login", {"Content-Length": "abc"}),
                                       ("/api/video?dev=video0&fps=abc", {})])
def test_bad_header_or_query_is_400(served, path, hdrs):
    app, port = served
    method = "POST" if "login" in path else "GET"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path)
    for k, v in hdrs.items():
        c.putheader(k, v)
    if method == "POST":
        c.putheader("Origin", f"http://127.0.0.1:{port}")
    if method == "GET":
        c.putheader("Cookie", login(app))
    c.endheaders()
    assert c.getresponse().status == 400


def test_handler_exception_is_500_not_dropped(served):
    app, port = served
    app.handle = lambda *a, **k: 1 / 0
    _, r, _ = _req(port, "GET", "/api/whoami")
    assert r.status == 500


def test_refused_source_body_never_read(served):
    app, port = served
    app.source_reason = lambda ip, local_ip=None: "未开放局域网访问"
    seen = []
    app.handle = lambda *a, **k: seen.append(a) or (200, {}, b"")
    _, r, _ = _req(port, "POST", "/api/login", body=b'{"pin":"1"}')
    assert r.status == 403 and seen == []


# ============================================================ C 线: Host / Origin、按 IP 计数

@pytest.mark.parametrize("host", ["evil.example", "evil.example:8088", "", None])
def test_host_not_in_whitelist_is_403(tmp_path, host):
    app = make_app(tmp_path)
    h = {} if host is None else {"Host": host}
    status, _, body = app.handle("GET", "/api/whoami", TAIL, h, b"")
    assert status == 403


@pytest.mark.parametrize("host", ["192.0.2.10:8088", "127.0.0.1", "[2001:db8::1]:8088", "localhost:8088",
                                  "cart.example:8088", "CART.example"])
def test_host_ip_literal_or_configured_is_ok(tmp_path, host):
    app = make_app(tmp_path)
    wc.web_conf_set(app.conf_path, hosts=["cart.example"])
    assert app.handle("GET", "/api/whoami", TAIL, {"Host": host}, b"")[0] == 200


@pytest.mark.parametrize("extra", [{"Origin": "http://evil.example"}, {"Origin": "null"},
                                   {"Origin": "http://192.0.2.10:9999"},
                                   {"Origin": None, "Sec-Fetch-Site": "cross-site"},
                                   {"Origin": None}])
def test_post_must_be_same_origin(tmp_path, extra):
    app = make_app(tmp_path)
    h = hdrs_for("POST", **{k: v for k, v in extra.items() if v is not None})
    if extra.get("Origin", "") is None:
        h.pop("Origin")
    status, _, _ = app.handle("POST", "/api/login", TAIL, h, json.dumps({"pin": "2468"}).encode())
    assert status == 403


def test_post_same_origin_via_fetch_metadata(tmp_path):
    app = make_app(tmp_path)
    h = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    status, _, data = app.handle("POST", "/api/login", TAIL, h, json.dumps({"pin": "2468"}).encode())
    assert status == 200 and json.loads(data)["result"] == "ok"


def test_cross_origin_post_body_never_read(served):
    app, port = served
    seen = []
    app.handle = lambda *a, **k: seen.append(a) or (200, {}, b"")
    _, r, _ = _req(port, "POST", "/api/login", body=b'{"pin":"1"}', headers={"Origin": "http://evil.example"})
    assert r.status == 403 and seen == []


def test_lockout_is_per_source_ip(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    for _ in range(5):
        r = call(app, "POST", "/api/login", LAN, {"pin": "0000"})[2]["result"]
    assert r == "locked"
    assert call(app, "POST", "/api/login", "198.51.100.8", {"pin": "2468"})[2]["result"] == "ok"
    assert app.gate_for(LAN) is not app.gate_for("198.51.100.8")


# ------------------------------------------------------------ 打字: 限长 / 分块 / 跳过统计

def test_type_reports_skipped_and_still_types_the_rest(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    status, _, data = call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": "a中b\r\n"}, ck)
    assert status == 200
    assert (data["sent"], data["skipped"], data["unknown"]) == (3, 1, ["中"])
    assert fake.frames[-3] == bytes([0, 0, 0x28, 0, 0, 0, 0, 0])      # \r\n 只算一次回车
    assert fake.frames[-1] == bytes(8)


def test_type_is_sent_in_chunks_releasing_lock_between(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    held = []
    real = app._with_ch

    def spy(path, fn):
        held.append(app._ch_lock.locked())
        return real(path, fn)
    app._with_ch = spy
    n = webkiosk.TYPE_CHUNK * 3 + 1
    status, _, data = call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": "a" * n}, ck)
    assert status == 200 and data["sent"] == n
    assert len(held) == 4 + 1 and not any(held)            # 4 块 + 结束松开, 进来时锁都是空的


def test_type_unbound_is_409(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    assert call(app, "POST", "/api/type", TAIL, {"src": "video2", "text": "a"}, ck)[0] == 409


# ------------------------------------------------------------ 绝对坐标鼠标

def test_abs_mouse_goes_to_ch9329(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda path: fake
    ck = login(app)
    ev = [{"t": "a", "x": 50, "y": 25, "vw": 100, "vh": 50, "b": 1, "w": 0},
          {"t": "a", "x": 0, "y": 49, "vw": 100, "vh": 50, "nw": 100, "nh": 100, "b": 1, "w": 0},   # 黑边上按下: 不发
          {"t": "a", "x": 0, "y": 50, "vw": 100, "vh": 50, "nw": 100, "nh": 100, "b": 0, "w": 0}]   # 黑边上松开: 照发
    status, _, _ = call(app, "POST", "/api/hid", TAIL, {"src": "video0", "events": ev}, ck)
    assert status == 200
    assert fake.abs == [(1, 2048, 2048, 0), (0, 0, 4095, 0)]


# ------------------------------------------------------------ 审计日志

def test_audit_log_events_without_secrets(tmp_path):
    app = make_app(tmp_path, lan_open=True)
    logged = []
    app.audit = lambda kind, msg: logged.append((kind, msg))
    fake = FakeCh()
    app.ch_open = lambda path: fake
    call(app, "POST", "/api/login", LAN, {"pin": "1111"})
    ck = login(app)
    call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": "secret-text"}, ck)
    call(app, "POST", "/api/lan", TAIL, {"open": False}, ck)
    call(app, "POST", "/api/logout", TAIL, {}, ck)
    text = "\n".join(m for _, m in logged)
    assert all(k == "web" for k, _ in logged)
    rows = [dict(f.split("=", 1) for f in m.split()) for _, m in logged]
    for want in ({"act": "login", "result": "bad"}, {"act": "login", "result": "ok"},
                 {"act": "type", "n": "11"}, {"act": "lan", "open": "false"}, {"act": "logout"}):
        assert any(want.items() <= r.items() for r in rows), want
    assert f"src={LAN}" in text and f"sid={wc.session_tag(ck.split('=', 1)[1])}" in text
    for secret in ("1111", "2468", "secret-text", ck.split("=", 1)[1]):
        assert secret not in text


# ------------------------------------------------------------ 串口: 按连接 id、不自动回车、回放历史

def _broker(tmp_path, name="ttyUSB0", log=b""):
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    import socket as _s
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    (tmp_path / "run" / f"{name}.json").write_text(json.dumps({"port": srv.getsockname()[1]}))
    (tmp_path / "logs" / f"{name}.log").write_bytes(log)
    return srv


def test_open_serial_per_connection_id_and_no_auto_enter(tmp_path):
    app = make_app(tmp_path)
    app.serial_dir = lambda: str(tmp_path)
    srv = _broker(tmp_path)
    try:
        a = app.open_serial("tok", "ttyUSB0", "c1")
        b = app.open_serial("tok", "ttyUSB0", "c2")             # 第二个标签页不挤掉第一个
        peer_a, _ = srv.accept()
        peer_b, _ = srv.accept()
        assert set(app.serial_socks) == {("tok", "c1"), ("tok", "c2")}
        peer_a.settimeout(0.3)
        assert peer_a.recv(64) == b"HELLO human\n"            # 只打招呼, 不替人按回车
        import socket as _s
        with pytest.raises(_s.timeout):
            peer_a.recv(64)
        app.serial_close("tok", "c1", a)
        assert set(app.serial_socks) == {("tok", "c2")}
        for x in (b, peer_a, peer_b):
            x.close()
    finally:
        srv.close()


def test_serial_history_tail(tmp_path):
    srv = _broker(tmp_path, log=b"x" * 100 + b"\r\nprompt> ")
    srv.close()
    assert wc.serial_history(str(tmp_path), "ttyUSB0", max_bytes=10) == b"\r\nprompt> "
    assert wc.serial_history(str(tmp_path), "nope") == b""
    assert wc.serial_history(str(tmp_path), "../run/ttyUSB0") == b""


@pytest.mark.parametrize("cid", [None, "", "a b", "x" * 40, 7])
def test_serial_send_needs_valid_cid(tmp_path, cid):
    app = make_app(tmp_path)
    ck = login(app)
    body = {"data": "YQ=="} if cid is None else {"cid": cid, "data": "YQ=="}
    assert call(app, "POST", "/api/serial/send", TAIL, body, ck)[0] == 400


def test_serial_stream_replays_history_and_sends_no_enter(served, tmp_path):
    app, port = served
    srv = _broker(tmp_path, log=b"old\r\nSwitch> ")
    app.serial_dir = lambda: str(tmp_path)
    logged = []
    app.audit = lambda kind, msg: logged.append(msg)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/serial/stream?name=ttyUSB0&cid=t1", headers={"Cookie": login(app)})
        r = c.getresponse()
        assert r.status == 200 and "event-stream" in r.getheader("Content-Type")
        first = r.fp.readline()
        import base64
        assert base64.b64decode(first.split(b"data: ", 1)[1].strip()) == b"old\r\nSwitch> "
        peer, _ = srv.accept()
        peer.settimeout(0.5)
        assert peer.recv(64) == b"HELLO human\n"
        c.close()
        peer.close()
        for _ in range(50):                               # 断开后 relay 下一轮读到 EOF 才收尾
            if any("serial-close" in m for m in logged):
                break
            time.sleep(0.1)
        assert any("act=serial-open" in m for m in logged)
        assert any("act=serial-close" in m and "tx=0" in m for m in logged)
        assert app.serial_socks == {}
    finally:
        srv.close()


def test_serial_stream_needs_cid(served):
    app, port = served
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/serial/stream?name=ttyUSB0", headers={"Cookie": login(app)})
    assert c.getresponse().status == 400
