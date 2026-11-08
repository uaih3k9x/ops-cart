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
