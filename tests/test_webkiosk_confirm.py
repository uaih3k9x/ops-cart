"""网页确认 UI (#23): 看挂起 / 急停 (只读 peek), 急停和拒绝投到 serial-req 目录; 确认和解除网页上不给."""

import json
import os

import confirm_core as cc
import mcp_serial as ms
import serial_write_core as sw
from test_webkiosk import LAN, TAIL, call, login, make_app

T0 = 1_700_000_000.0
DANGER = {"verdict": "confirm", "layer": 1, "reason": "带高危词", "danger": ["save"], "vendor": "huawei"}


def _app(tmp_path, held=True):
    app = make_app(tmp_path)
    b = sw.Book.from_text(None, T0)
    if held:
        tok = b.acquire(1, "stdio:ops@x", T0)["lease"]
        b.submit(1, tok, "stdio:ops@x", "line", "save", DANGER, T0, 30, name="SW1")
    state = tmp_path / "serial-write.json"
    state.write_text(b.dumps())
    app.serial_peek = lambda: ms.peek(str(state), clock=lambda: T0 + 3)
    app.req_dir = str(tmp_path / "req")
    os.makedirs(app.req_dir)
    app.clock_wall = lambda: T0 + 3
    return app


def _reqs(app):
    return [json.load(open(os.path.join(app.req_dir, n))) for n in sorted(os.listdir(app.req_dir))]


def test_view_needs_login(tmp_path):
    app = _app(tmp_path)
    assert call(app, "GET", "/api/serial-write")[0] == 401
    assert call(app, "POST", "/api/serial-write/estop", body={})[0] == 401
    assert _reqs(app) == []


def test_view_shows_pending_cards_and_no_tokens(tmp_path):
    app = _app(tmp_path)
    c = login(app)
    status, _, data = call(app, "GET", "/api/serial-write", cookie=c)
    assert status == 200 and data["ops"] == ["estop", "reject"]
    card = data["cards"][0]
    assert card["items"][0]["head"] == "Agent 想执行：save" and card["items"][0]["danger"] == "高危词：save"
    assert data["heads"][0]["text"] == "1\n待确认 1" and data["summary"]["level"] == "warn"
    assert "token" not in json.dumps(data) and "lease" not in json.dumps(data)
    assert "车机" in data["note"]


def test_view_unreadable_state_shows_all_stopped(tmp_path):
    app = _app(tmp_path)
    (tmp_path / "bad.json").write_text("{")
    app.serial_peek = lambda: ms.peek(str(tmp_path / "bad.json"), clock=lambda: T0)
    data = call(app, "GET", "/api/serial-write", cookie=login(app))[2]
    assert all(c["stopped"] for c in data["cards"]) and data["summary"]["text"] == "Agent 全部急停"


def test_estop_one_and_all_queue_requests(tmp_path):
    app = _app(tmp_path)
    c = login(app)
    assert call(app, "POST", "/api/serial-write/estop", body={"slot": 2}, cookie=c)[0] == 202
    assert call(app, "POST", "/api/serial-write/estop", body={}, cookie=c)[0] == 202
    reqs = _reqs(app)
    assert [(r["op"], r["slot"]) for r in reqs] == [("estop", 2), ("estop", None)]
    assert all(r["by"].startswith("web:" + TAIL) and r["t"] == T0 + 3 for r in reqs)
    assert "serial-estop" in (tmp_path / "audit.log").read_text()


def test_reject_queues_and_bad_input_refused(tmp_path):
    app = _app(tmp_path)
    c = login(app)
    assert call(app, "POST", "/api/serial-write/reject", body={"id": 1}, cookie=c)[0] == 202
    for body in ({"id": "1"}, {"id": True}, {}):
        assert call(app, "POST", "/api/serial-write/reject", body=body, cookie=c)[0] == 400
    assert call(app, "POST", "/api/serial-write/estop", body={"slot": 9}, cookie=c)[0] == 400
    assert [r["op"] for r in _reqs(app)] == ["reject"]


def test_confirm_and_resume_not_offered_on_web(tmp_path):
    app = _app(tmp_path)
    c = login(app)
    for op in ("confirm", "resume"):
        status, _, data = call(app, "POST", f"/api/serial-write/{op}", body={"id": 1, "slot": 1}, cookie=c)
        assert status == 403 and "车机" in data["error"]
    assert _reqs(app) == []


def test_estop_channel_missing_says_so(tmp_path):
    app = _app(tmp_path)
    app.req_dir = str(tmp_path / "gone")
    status, _, data = call(app, "POST", "/api/serial-write/estop", body={}, cookie=login(app))
    assert status == 503 and "车机" in data["error"]


def test_queued_request_runs_through_root_side(tmp_path):
    """网页投的请求交给 process_requests (root 那头) 真能停下来."""
    app = _app(tmp_path)
    c = login(app)
    call(app, "POST", "/api/serial-write/estop", body={"slot": 1}, cookie=c)
    store = ms.Store(str(tmp_path / "serial-write.json"), str(tmp_path / "log"), clock=lambda: T0 + 4,
                     event=None)
    res = ms.process_requests(store, app.req_dir, sleep=lambda s: None)
    assert "已急停" in res[0][1] and os.listdir(app.req_dir) == []
    data = call(app, "GET", "/api/serial-write", cookie=c)[2]
    assert data["cards"][0]["stopped"] and data["cards"][0]["items"] == []
    assert cc.slot_states(ms.peek(str(tmp_path / "serial-write.json"), clock=lambda: T0 + 4))[1]["estop"]


def test_lan_without_switch_still_blocked(tmp_path):
    app = _app(tmp_path)
    assert call(app, "POST", "/api/serial-write/estop", ip=LAN, body={})[0] == 403


def test_web_page_has_estop_and_no_confirm_button():
    import pathlib
    import webkiosk
    html = (pathlib.Path(webkiosk.STATIC_DIR) / "index.html").read_text()
    assert 'id="estop-all"' in html and "/api/serial-write/${op}" in html and 'data-p="agent"' in html
    assert "data-confirm" not in html and "/api/serial-write/confirm" not in html
