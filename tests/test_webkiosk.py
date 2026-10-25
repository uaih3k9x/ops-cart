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


def call(app, method, path, ip=TAIL, body=None, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
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
    status, _, _ = app.handle("POST", "/api/login", TAIL, {}, b"{nope")
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
        self.frames, self.mice = [], []

    def send_report(self, report):
        self.frames.append(report)

    def send_mouse(self, buttons, dx, dy, wheel):
        self.mice.append((buttons, dx, dy, wheel))


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
    status, _, _ = call(app, "POST", "/api/type", TAIL, {"src": "video0", "text": "aB"}, ck)
    assert status == 200
    assert fake.frames == [bytes([0, 0, 0x04, 0, 0, 0, 0, 0]), bytes(8),
                           bytes([2, 0, 0x05, 0, 0, 0, 0, 0]), bytes(8)]


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
    assert call(app, "POST", "/api/serial/send", TAIL, {"data": "aGk="}, ck)[0] == 409


def test_serial_send_goes_to_session_socket(tmp_path):
    app = make_app(tmp_path)
    ck = login(app)
    sent = []

    class Sock:
        def sendall(self, b):
            sent.append(b)

    app.serial_socks[ck.split("=", 1)[1]] = Sock()
    status, _, _ = call(app, "POST", "/api/serial/send", TAIL, {"data": "aGkN"}, ck)
    assert status == 200 and sent == [b"hi\r"]


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
    app.gate_for("trusted")                               # 任何一次鉴权都会发现 PIN 变了
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
    assert app.gate_for("lan").max_lock_s > app.gate_for("lan").lock_s


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
    status, _, _ = app.handle("POST", "/api/hid", TAIL, {"Cookie": ck}, raw)
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
    app.serial_socks[ck.split("=", 1)[1]] = object()
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

    app.serial_socks[ck.split("=", 1)[1]] = SlowSock()
    ts = [threading.Thread(target=call, args=(app, "POST", "/api/serial/send", TAIL, {"data": "YQ=="}, ck))
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
    _, r, _ = _req(port, "POST", "/api/login", body=b"x" * (webkiosk.BODY_MAX + 1))
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
