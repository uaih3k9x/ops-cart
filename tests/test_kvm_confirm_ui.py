"""键鼠灯 / 确认框 / 网页拒绝 / 副屏 (#42 #43): 纯逻辑 + 假 self."""

import json
from types import SimpleNamespace

import pytest

import confirm_core as cc
import dash_core
import mcp_kvm as mk
import mcp_serial as ms

VIEW = {"writer": "agent-a", "expires_in": 40, "stopped": False, "sending": False, "history": [],
        "pending": [{"id": 3, "what": "format c: + 回车", "who": "agent-a", "t": 0, "reason": "这一行带高危词 format, 要人确认",
                     "danger": ["format"], "line": "format c:"}]}


def v(**kw):
    return dict(VIEW, **kw)


def test_summary_only_counts_no_names_or_commands():
    s = cc.kvm_summary(v())
    assert s == {"stopped": False, "pending": 1, "writer": True, "sending": False}
    assert "format" not in json.dumps(s) and "agent-a" not in json.dumps(s)


def test_summary_unreadable_means_stopped():
    assert cc.kvm_summary(None)["stopped"] is True
    assert cc.kvm_summary({"bad": 1})["stopped"] is True


@pytest.mark.parametrize("state,text,color,blink", [
    ({"stopped": True, "pending": 0, "writer": False, "sending": False}, "键鼠\n急停", "ESTOP", False),
    ({"stopped": False, "pending": 2, "writer": True, "sending": False}, "键鼠\n待确认 2", "PENDING", True),
    ({"stopped": False, "pending": 0, "writer": True, "sending": True}, "键鼠\nAgent 输入", "AGENT", True),
    ({"stopped": False, "pending": 0, "writer": True, "sending": False}, "键鼠\nAgent", "AGENT", False),
])
def test_lamp_priority(state, text, color, blink):
    look = cc.kvm_lamp(state, True)
    assert look["text"] == text and look["bar"] == getattr(cc, color + "_COLOR") and look["blink"] is blink


def test_lamp_idle_is_none():
    assert cc.kvm_lamp({"stopped": False, "pending": 0, "writer": False, "sending": False}, True) is None
    assert cc.kvm_lamp(None, True) is None


def test_card():
    c = cc.kvm_card(v())
    assert c["title"] == "键鼠" and c["stopped"] is False and "agent-a" in c["status"][0]
    it = c["items"][0]
    assert it["id"] == 3 and "format c:" in it["head"] and "format" in it["danger"]


def test_card_stopped_or_unreadable():
    assert cc.kvm_card(v(stopped=True, pending=[], writer=None))["stopped"] is True
    assert cc.kvm_card(None)["stopped"] is True


def test_web_can_reject_keyboard_but_not_confirm():
    assert "kvm_reject" in cc.WEB_OPS and "kvm_confirm" not in cc.WEB_OPS
    req = cc.make_request("kvm_reject", 3, "web:192.0.2.4/x", 100.0)
    assert cc.parse_request(json.dumps(req), 100.0)["op"] == "kvm_reject"
    with pytest.raises(ValueError):
        cc.make_request("kvm_reject", "x", "web:192.0.2.4/x", 100.0)


def test_process_requests_runs_kvm_reject(tmp_path, monkeypatch):
    got = []

    class S:
        def reject(self, iid, by, why=None):
            got.append((iid, by, why))
    monkeypatch.setattr(mk, "LeaseStore", S)
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("kvm_reject", 3, "web:192.0.2.4/x", 100.0), str(d))
    st = ms.Store(str(tmp_path / "serial-write.json"), str(tmp_path / "log"), clock=lambda: 100.0, event=None,
                  on_estop_all=None)
    out = ms.process_requests(st, str(d))
    assert got == [(3, "web:192.0.2.4/x", "网页上拒绝")] and "已拒绝" in out[0][1] and list(d.iterdir()) == []


def test_process_requests_kvm_reject_gone_is_message(tmp_path, monkeypatch):
    class S:
        def reject(self, iid, by, why=None):
            raise ValueError("不在等确认")
    monkeypatch.setattr(mk, "LeaseStore", S)
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("kvm_reject", 3, "web:192.0.2.4/x", 100.0), str(d))
    st = ms.Store(str(tmp_path / "s.json"), str(tmp_path / "log"), clock=lambda: 100.0, event=None, on_estop_all=None)
    assert "不行" in ms.process_requests(st, str(d))[0][1]


# ---- 副屏

def test_dash_kvm_sample_never_raises(monkeypatch):
    monkeypatch.setattr(mk, "peek", lambda: 1 / 0)
    assert dash_core.kvm_sample()["stopped"] is True


def test_dash_lower_shows_look():
    state = {"stopped": False, "pending": 1, "writer": True, "sending": False}
    low = dash_core._lower([], set(), state)
    tile = low[0]
    assert tile["name"] == "键鼠 待确认 1" and tile["look"]["bar"] == cc.PENDING_COLOR
    assert "look" not in low[1]
    assert "look" not in dash_core._lower([], set(), None)[0]


# ---- 车机

pytest.importorskip("tkinter")
import kiosk  # noqa: E402


def _fake(view):
    calls = []
    return calls, SimpleNamespace(
        _kvm_view=view, _kvm_win=None, _kvm_store="store", _open_pane=lambda *a: calls.append(("pane",) + a),
        _open_kvm=lambda: calls.append(("dialog",)), _kvm_say=lambda t, ok=True: calls.append(("say", t, ok)),
        _serial_action=lambda op, arg: calls.append(("serial", op, arg)),
        ui=SimpleNamespace(run_bg=lambda work, done, key=None: (done(work()), True)[1]))


def test_click_opens_dialog_only_when_agent_involved():
    calls, f = _fake(v())
    kiosk.Kiosk._kvm_click(f, "kvm-pane")
    assert calls == [("dialog",)]
    calls, f = _fake(v(writer=None, pending=[]))
    kiosk.Kiosk._kvm_click(f, "kvm-pane")
    assert calls == [("pane", "kvm-pane")]
    calls, f = _fake(None)
    kiosk.Kiosk._kvm_click(f, "kvm-pane")
    assert calls == [("pane", "kvm-pane")]


def test_action_confirm_goes_to_kvm_store_in_background(monkeypatch):
    seen = []
    monkeypatch.setattr(kiosk.mcp_kvm, "human_action",
                        lambda store, op, arg, by: seen.append((store, op, arg, by)) or "已发")
    calls, f = _fake(v())
    kiosk.Kiosk._kvm_action(f, "confirm", 3)
    assert seen == [("store", "confirm", 3, cc.BY_KIOSK)] and ("say", "已发", True) in calls


def test_action_estop_uses_serial_all_stop():
    calls, f = _fake(v())
    kiosk.Kiosk._kvm_action(f, "estop", None)
    assert calls == [("serial", "estop", None)]
