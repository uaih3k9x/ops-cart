"""kiosk 的大肥鱼部分 (#53): 菜单入口、确认 / 拒绝复用现有确认框、急停复用全部急停; 假 self, 不起窗口."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import kiosk  # noqa: E402
import kiosk_ui_core as kui  # noqa: E402
import serial_write_core as sw  # noqa: E402

T0 = 1_700_000_000.0
GATE = {"verdict": "confirm", "layer": 1, "reason": "不在只读白名单里", "danger": [], "vendor": "huawei"}


def test_menu_has_bigfish_pane():
    items = [it for it in kui.MENU["main"] if it.get("arg") == "bigfish"]
    assert items and items[0]["do"] == "toggle_pane" and "bigfish" in kui.PANES


def _view(slot):
    b = sw.Book.from_text(None, T0)
    if slot:
        tok = b.acquire(slot, "a", T0)["lease"]
        b.submit(slot, tok, "a", "line", "undo shutdown", GATE, T0, 30, context="<SW1>")
    return b.view(T0)


class _Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


def _fake(sw_view, kvm_view=None):
    calls = {"confirm": [], "kvm": [], "serial": []}
    fake = SimpleNamespace(_sw_view=sw_view, _kvm_view=kvm_view, banner=_Lab())
    fake._open_confirm = lambda n: calls["confirm"].append(n)
    fake._open_kvm = lambda: calls["kvm"].append(True)
    fake._serial_action = lambda op, arg: calls["serial"].append((op, arg))
    for name in ("_bigfish_confirm", "_bigfish_estop", "_bigfish_pending", "_agent_states"):
        setattr(fake, name, getattr(kiosk.Kiosk, name).__get__(fake))
    return calls, fake


def test_confirm_opens_existing_dialog_for_pending_slot():
    calls, fake = _fake(_view(3))
    assert fake._bigfish_pending() == 1
    fake._bigfish_confirm()
    assert calls["confirm"] == [3] and calls["kvm"] == []


def test_confirm_opens_kvm_dialog_when_only_kvm_pending():
    kv = {"stopped": False, "pending": [{"id": 1}], "writer": "a", "sending": False}
    calls, fake = _fake(_view(None), kv)
    fake._bigfish_confirm()
    assert calls["kvm"] == [True] and calls["confirm"] == []


def test_confirm_with_nothing_pending_only_says_so():
    calls, fake = _fake(None)
    assert fake._bigfish_pending() == 0
    fake._bigfish_confirm()
    assert calls["confirm"] == [] and calls["kvm"] == [] and fake.banner.cfg["text"]


def test_estop_is_the_existing_all_estop():
    calls, fake = _fake(None)
    fake._bigfish_estop()
    assert calls["serial"] == [("estop", None)]


# ------------------------------------------------------------ 硬急停兜底 (systemctl 用替身)

import json  # noqa: E402

import bigfish_core as bf  # noqa: E402


def _hard_rig(monkeypatch, uid=1001):
    st = {"t": 1000.0, "all": False, "state": "running", "online": True}
    calls = {"bg": [], "ran": [], "log": []}
    fake = SimpleNamespace(_bf_guard=bf.HardStopGuard(), _bf_note="", banner=_Lab())
    fake.ui = SimpleNamespace(run_bg=lambda work, done, key=None: calls["bg"].append((work, done, key)) or True)
    fake.after = lambda ms, fn: None
    for name in ("_bigfish_tick", "_bigfish_hard"):
        setattr(fake, name, getattr(kiosk.Kiosk, name).__get__(fake))
    monkeypatch.setattr(kiosk.time, "time", lambda: st["t"])
    monkeypatch.setattr(kiosk.bfc, "flag_all", lambda path=None: st["all"])
    monkeypatch.setattr(kiosk.bfc, "load", lambda now: bf.parse(
        json.dumps({"v": 1, "updated": now, "state": st["state"]}) if st["online"] else None, now))
    monkeypatch.setattr(kiosk.bfc, "bigfish_uid", lambda user=bf.USER: uid)
    monkeypatch.setattr(kiosk.kc, "run", lambda cmd, timeout=5: calls["ran"].append(cmd) or (0, ""))
    monkeypatch.setattr(kiosk.bfc.cartlog, "event", lambda kind, m: calls["log"].append((kind, m)))

    def tick(dt=1.0):
        st["t"] += dt
        fake._bigfish_tick()
        for work, done, _key in calls["bg"]:
            done(work())
        calls["bg"].clear()
    return st, calls, fake, tick


def test_tick_confirmed_within_10s_no_stop(monkeypatch):
    st, calls, fake, tick = _hard_rig(monkeypatch)
    st["all"] = True
    tick()
    assert "等" in fake._bf_note
    st["state"] = "estopped"
    tick(3)
    for _ in range(20):
        tick()
    assert calls["ran"] == [] and fake._bf_note == "大肥鱼已急停"


def test_tick_not_confirmed_stops_once_and_logs(monkeypatch):
    st, calls, fake, tick = _hard_rig(monkeypatch)
    st["all"] = True
    tick()
    for _ in range(int(bf.HARD_AFTER_S) + 15):
        tick()
    assert calls["ran"] == [["systemctl", "stop", "user@1001.service"]]
    assert calls["log"] and calls["log"][0][0] == "bigfish"
    assert fake._bf_note == bf.HARD_NOTE


def test_tick_offline_stops(monkeypatch):
    st, calls, fake, tick = _hard_rig(monkeypatch)
    st["all"], st["online"] = True, False
    tick()
    tick(bf.HARD_AFTER_S)
    assert calls["ran"] == [["systemctl", "stop", "user@1001.service"]]


def test_tick_no_bigfish_user_does_nothing_but_says_once(monkeypatch):
    st, calls, fake, tick = _hard_rig(monkeypatch, uid=None)
    st["all"] = True
    tick()
    tick(bf.HARD_AFTER_S)
    assert calls["ran"] == [] and calls["log"] == [] and "找不到 bigfish 用户" in fake._bf_note
    tick()
    assert calls["ran"] == [] and "找不到 bigfish 用户" in fake._bf_note


def test_tick_resume_within_10s_clears(monkeypatch):
    st, calls, fake, tick = _hard_rig(monkeypatch)
    st["all"] = True
    tick()
    st["all"] = False
    tick(bf.HARD_AFTER_S)
    assert calls["ran"] == [] and fake._bf_note == ""
