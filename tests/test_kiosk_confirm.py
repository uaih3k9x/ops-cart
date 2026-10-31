"""kiosk 的 Agent 写串口部分 (#23): 不起 Kiosk 窗口, 用假 self 调方法; 后台、确认框、状态都打桩."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import confirm_core as cfc  # noqa: E402
import kiosk  # noqa: E402
import serial_write_core as sw  # noqa: E402

T0 = 1_700_000_000.0
DANGER = {"verdict": "confirm", "layer": 1, "reason": "带高危词", "danger": ["save"], "vendor": "huawei"}


def _view(pending_slot=1, writer_slot=None, stop=None):
    b = sw.Book.from_text(None, T0)
    if pending_slot:
        tok = b.acquire(pending_slot, "a", T0)["lease"]
        b.submit(pending_slot, tok, "a", "line", "save", DANGER, T0, 30)
    if writer_slot:
        b.acquire(writer_slot, "b", T0)
    if stop:
        b.estop(stop, "h", "human", T0)
    return b.view(T0)


class _Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


def _fake(view):
    calls = {"bg": [], "say": [], "pane": [], "confirm": []}
    fake = SimpleNamespace(_sw_view=view, _sw_store=object(), _confirm_win=None,
                           banner=_Lab(), _lamp_slots=None, _active=set(), _bell_active=set(),
                           _check_rows=[])
    fake.ui = SimpleNamespace(run_bg=lambda work, done, key=None: calls["bg"].append((work, done, key)) or True)
    fake._serial_say = lambda text, ok=True: calls["say"].append((text, ok))
    fake._open_pane = lambda name, sel=None: calls["pane"].append((name, sel))
    fake._open_confirm = lambda n: calls["confirm"].append(n)
    for name in ("_serial_action", "_head_click", "_lamp_paint"):
        setattr(fake, name, getattr(kiosk.Kiosk, name).__get__(fake))
    return calls, fake


def test_head_click_opens_confirm_only_when_agent_involved():
    calls, fake = _fake(_view(pending_slot=1, writer_slot=2, stop=3))
    for n in (1, 2, 3, 4):
        fake._head_click(n)
    assert calls["confirm"] == [1, 2, 3] and calls["pane"] == [("serial", "/dev/con-4")]


def test_head_click_before_first_sample_goes_to_serial():
    calls, fake = _fake(None)
    fake._head_click(1)
    assert calls["pane"] == [("serial", "/dev/con-1")] and calls["confirm"] == []


def test_action_refused_by_check_never_goes_background():
    calls, fake = _fake(_view(stop=1))
    iid = _view()["pending"][0]["id"]
    fake._serial_action("confirm", iid)              # 急停的时候挂起的已经丢了
    assert calls["bg"] == [] and calls["say"][-1][1] is False
    calls2, fake2 = _fake(None)                      # 还没采到状态: 确认不行, 急停可以
    fake2._serial_action("confirm", 1)
    fake2._serial_action("estop", None)
    assert len(calls2["bg"]) == 1 and calls2["bg"][0][2] == "serial-write"


def test_action_runs_human_action_in_background(monkeypatch):
    seen = []
    monkeypatch.setattr(kiosk.mcp_serial, "human_action",
                        lambda store, op, arg, by, slots, send: seen.append((op, arg, by)) or "已拒绝 #1")
    calls, fake = _fake(_view())
    iid = fake._sw_view["pending"][0]["id"]
    fake._serial_action("reject", iid)
    work, done, key = calls["bg"][0]
    assert seen == [] and key == "serial-write"       # 主线程不碰状态文件
    done(work())
    assert seen == [("reject", iid, cfc.BY_KIOSK)] and calls["say"][-1] == ("已拒绝 #1", True)
    done(sw.Refused("nope", "没有挂起的第 1 条"))
    assert calls["say"][-1] == ("不行: 没有挂起的第 1 条", False)


def test_busy_background_says_so():
    calls, fake = _fake(_view())
    fake.ui.run_bg = lambda *a, **k: False
    fake._serial_action("estop", 1)
    assert "处理中" in calls["say"][-1][0]


def test_lamp_paint_uses_agent_state():
    calls, fake = _fake(_view(pending_slot=2, stop=1))
    fake._lamp_slots = [{"dev": f"/dev/con-{n}", "present": True, "baud": "9600", "count": 0} for n in range(1, 5)]
    fake._lamps = {k: (_Lab(), _Lab(), _Lab(), [None]) for k in ["con-1", "con-2", "con-3", "con-4"] +
                   [key for _n, _l, key in kiosk.lc.LOWER]}
    fake._lamp_paint()
    assert fake._lamps["con-1"][2].cfg["text"] == "1\n急停"
    assert fake._lamps["con-2"][2].cfg["text"] == "2\n待确认 1"
    assert fake._lamps["con-3"][2].cfg["text"] == "3\n9600"
    assert fake._lamps["kvm"][2].cfg["text"] == "键鼠"
