"""webkiosk 路由层离线测试: App.handle 是纯方法, 不起 socket."""

import json

import kiosk_core as kc
import web_core as wc
import webkiosk

TAIL = "100.64.0.9"
LAN = "192.168.9.138"


def make_app(tmp_path, pin="2468", lan_open=False):
    conf = tmp_path / "web.conf"
    wc.web_conf_set(conf, lan_open=lan_open)
    pin_path = tmp_path / "kiosk.conf"
    if pin:
        kc.pin_set(pin, pin_path)
    src = {k: (lambda k=k: {"services": {}, "nics": [], "misc": {}, "temps": [],
                            "fans": [], "version": "Ver test"}[k]) for k in wc.STATUS_KEYS}
    return webkiosk.App(conf_path=conf, pin_path=pin_path, status_src=src,
                        selftest=lambda: [{"label": "LAN2", "state": "OK", "detail": "x"}],
                        static_dir=tmp_path)


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
    app.ch_open = lambda: fake
    ck = login(app)
    ev = [{"t": "k", "code": "ControlLeft", "down": True},
          {"t": "k", "code": "KeyC", "down": True},
          {"t": "k", "code": "KeyC", "down": True},          # 自动重复, 不重发
          {"t": "m", "dx": 200, "dy": 0, "b": 1, "w": 0}]
    status, _, data = call(app, "POST", "/api/hid", TAIL, {"events": ev}, ck)
    assert status == 200 and data["ok"]
    assert fake.frames == [bytes([1, 0, 0, 0, 0, 0, 0, 0]), bytes([1, 0, 0x06, 0, 0, 0, 0, 0])]
    assert fake.mice == [(1, 127, 0, 0), (1, 73, 0, 0)]
    call(app, "POST", "/api/hid", TAIL, {"events": [{"t": "release"}]}, ck)
    assert fake.frames[-1] == bytes(8) and fake.mice[-1] == (0, 0, 0, 0)


def test_hid_without_ch9329_is_503(tmp_path):
    app = make_app(tmp_path)
    app.ch_open = lambda: None
    ck = login(app)
    status, _, data = call(app, "POST", "/api/hid", TAIL, {"events": [{"t": "release"}]}, ck)
    assert status == 503


def test_hid_send_failure_drops_handle(tmp_path):
    app = make_app(tmp_path)

    class Broken(FakeCh):
        def send_report(self, report):
            raise OSError("unplugged")

    opened = []
    app.ch_open = lambda: opened.append(1) or Broken()
    ck = login(app)
    ev = {"events": [{"t": "k", "code": "KeyA", "down": True}]}
    assert call(app, "POST", "/api/hid", TAIL, ev, ck)[0] == 503
    call(app, "POST", "/api/hid", TAIL, ev, ck)
    assert len(opened) == 2                                 # 下次重开


def test_hid_needs_login(tmp_path):
    app = make_app(tmp_path)
    assert call(app, "POST", "/api/hid", TAIL, {"events": []})[0] == 401


def test_type_text(tmp_path):
    app = make_app(tmp_path)
    fake = FakeCh()
    app.ch_open = lambda: fake
    ck = login(app)
    status, _, _ = call(app, "POST", "/api/type", TAIL, {"text": "aB"}, ck)
    assert status == 200
    assert fake.frames == [bytes([0, 0, 0x04, 0, 0, 0, 0, 0]), bytes(8),
                           bytes([2, 0, 0x05, 0, 0, 0, 0, 0]), bytes(8)]


# ------------------------------------------------------------ 视频 / 串口 列表

def test_video_list(tmp_path):
    app = make_app(tmp_path)
    app.video_root = tmp_path
    (tmp_path / "video2").write_text("")
    ck = login(app)
    assert call(app, "GET", "/api/videos", cookie=ck)[2] == {"devices": ["video2"]}


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
