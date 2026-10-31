"""网页只读 PXE 日志 (#24 后续): /api/pxe-log 按 MAC 分组; 不显示在等的申请、不给确认 / 拒绝, 码和摘要绝不出."""

import json
import os

import mcp_pxe as mp
import pxe_req_core as pr
from test_webkiosk import LAN, call, login, make_app

T0 = 1_700_000_000.0
MAC = "02:00:00:00:00:0a"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app(tmp_path):
    app = make_app(tmp_path)
    app.pxe_log_paths = (str(tmp_path / "pxe-req.log"), str(tmp_path / "events.log"))
    return app


def test_needs_login_and_source_filter(tmp_path):
    app = _app(tmp_path)
    assert call(app, "GET", "/api/pxe-log")[0] == 401
    assert call(app, "GET", "/api/pxe-log", ip=LAN)[0] == 403


def test_missing_logs_give_empty(tmp_path):
    app = _app(tmp_path)
    status, _, data = call(app, "GET", "/api/pxe-log", cookie=login(app))
    assert status == 200 and data == {"groups": [], "unlinked": []}


def test_no_write_route(tmp_path):
    app = _app(tmp_path)
    c = login(app)
    for path in ("/api/pxe-log", "/api/pxe-log/reject", "/api/pxe-log/confirm"):
        assert call(app, "POST", path, body={"id": 1}, cookie=c)[0] == 404


def test_code_and_digest_never_leak(tmp_path):
    """mcp_pxe 真写一条申请 (状态文件里有码和摘要), 再照抄错一次码 (换码), 网页日志里都看不到."""
    app = _app(tmp_path)
    state = str(tmp_path / "pxe-req.json")
    store = mp.Store(state, app.pxe_log_paths[0], clock=lambda: T0, event=None, stopped=lambda: False)
    store.run(lambda b, now, st: b.submit("stdio:ops", "rocky", MAC, False, pr.simple_plan("rocky", MAC), now, st))
    first = mp.peek(state, clock=lambda: T0, stopped=lambda: False)["pending"][0]
    try:
        store.run(lambda b, now, st: b.take(1, "000000" if first["code"] != "000000" else "111111",
                                            first["digest"], now, st))
    except pr.Refused:
        pass
    second = mp.peek(state, clock=lambda: T0, stopped=lambda: False)["pending"][0]
    raw = open(app.pxe_log_paths[0]).read()
    with open(app.pxe_log_paths[0], "a") as f:     # 就算日志里混进了带码的行, 也不出
        f.write(json.dumps({"t": "2000-01-01T00:00:00", "ev": "request", "id": 1, "macs": [MAC],
                            "code": second["code"], "digest": second["digest"]}) + "\n")
    out = call(app, "GET", "/api/pxe-log", cookie=login(app))[2]
    text = json.dumps(out)
    assert out["groups"] and out["groups"][0]["mac"] == MAC
    assert [e["ev"] for e in out["groups"][0]["events"]][:2] == ["bad_code", "request"]
    for secret in (first["code"], second["code"], first["digest"]):
        assert secret not in text and secret not in raw
    assert '"code"' not in text and '"digest"' not in text and "pending" not in text


def test_page_is_read_only():
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    assert 'api("/api/pxe-log")' in html
    start = html.index("<section id=\"p-pxe\"")
    section = html[start:html.index("</section>", start)]
    assert "button" not in section
