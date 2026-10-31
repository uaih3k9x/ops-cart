"""kiosk 的 Agent PXE 申请部分 (#24): 不起 Kiosk 窗口, 用假 self 调方法; 后台、申请框、状态都打桩."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import confirm_core as cfc  # noqa: E402
import kiosk  # noqa: E402
import mcp_pxe  # noqa: E402
import pxe_req_core as pr  # noqa: E402

T0 = 1_700_000_000.0


def _view(n=1):
    b = pr.Book.from_text(None, T0, code=lambda: "111111")
    for i in range(n):
        mac = f"52:54:00:aa:bb:0{i + 1}"
        b.submit("a", "rocky", mac, False, pr.simple_plan("rocky", mac), T0, stopped=False)
    return b.view(T0, secret=True)


class _Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


class _Win:
    def __init__(self):
        self.views, self.said, self.alive = [], [], True

    def winfo_exists(self):
        return self.alive

    def refresh(self, view):
        self.views.append(view)

    def say(self, text, ok=True):
        self.said.append((text, ok))

    def destroy(self):
        self.alive = False

    def lift(self):
        pass


def _fake(locked=False):
    calls = {"bg": [], "open": [], "arm": []}
    fake = SimpleNamespace(_pxe_view=None, _pxe_req_win=None, _pxe_seen=set(), _pxe_store=object(),
                           _lock_win=object() if locked else None, _boot_win=None, banner=_Lab())
    fake.ui = SimpleNamespace(run_bg=lambda work, done, key=None: calls["bg"].append((work, done, key)) or True)
    fake._open_pxe_req = lambda: calls["open"].append(True)
    fake._arm_dialog = lambda: calls["arm"].append(True)
    for name in ("_pxe_req_update", "_pxe_req_action", "_pxe_req_say", "_close_pxe_req"):
        setattr(fake, name, getattr(kiosk.Kiosk, name).__get__(fake))
    return calls, fake


def test_new_request_pops_once():
    calls, fake = _fake()
    fake._pxe_req_update(_view())
    assert calls["open"] == [True] and fake._pxe_seen == {1}
    fake._pxe_req_update(_view())                      # 同一条, 人关掉了: 不再弹
    assert calls["open"] == [True]
    fake._pxe_req_update(_view(n=2))                   # 来了新的: 再弹
    assert len(calls["open"]) == 2 and fake._pxe_seen == {1, 2}
    fake._pxe_req_update({"pending": [], "history": []})
    assert fake._pxe_seen == set()


def test_locked_does_not_pop_until_unlocked():
    calls, fake = _fake(locked=True)
    fake._pxe_req_update(_view())
    assert calls["open"] == [] and fake._pxe_seen == set()
    fake._lock_win = None
    fake._pxe_req_update(_view())
    assert calls["open"] == [True]


def test_open_dialog_gets_refreshed_not_reopened():
    calls, fake = _fake()
    fake._pxe_req_win = w = _Win()
    v = _view()
    fake._pxe_req_update(v)
    assert w.views == [v] and calls["open"] == []


def test_confirm_runs_in_background_as_kiosk(monkeypatch):
    calls, fake = _fake()
    fake._pxe_req_win = w = _Win()
    seen = []
    monkeypatch.setattr(mcp_pxe, "human_confirm", lambda *a, **k: seen.append(a) or "#1 已解锁")
    fake._pxe_req_action("confirm", (1, "111111", "d" * 16))
    work, done, key = calls["bg"][0]
    assert key == "pxe-req" and seen == []             # 主线程只交出去
    res = work()
    assert seen == [(fake._pxe_store, 1, "111111", "d" * 16, cfc.BY_KIOSK)]
    done(res)
    assert w.said[-1] == ("#1 已解锁", True)


def test_reject_and_refused(monkeypatch):
    calls, fake = _fake()
    fake._pxe_req_win = w = _Win()
    monkeypatch.setattr(mcp_pxe, "human_reject", lambda store, iid, by: f"已拒绝 #{iid}")
    fake._pxe_req_action("reject", 3)
    work, done, _key = calls["bg"][0]
    done(work())
    assert w.said[-1] == ("已拒绝 #3", True)
    done(pr.Refused("code", "确认码不对, 已换新码"))
    assert w.said[-1] == ("不行: 确认码不对, 已换新码", False)


def test_busy_background():
    calls, fake = _fake()
    fake._pxe_req_win = w = _Win()
    fake.ui = SimpleNamespace(run_bg=lambda work, done, key=None: False)
    fake._pxe_req_action("reject", 1)
    assert "还在处理" in w.said[-1][0]


def test_lamp_sample_passes_estop_all_to_pxe_peek(monkeypatch):
    import lamps_core as lc
    import serial_core as sc
    got = []
    monkeypatch.setattr(sc, "serial_conf_read", lambda: {})
    monkeypatch.setattr(sc, "data_dir", lambda conf: "/nope")
    monkeypatch.setattr(lc, "sample", lambda *a, **k: ([], {}))
    monkeypatch.setattr(kiosk.mcp_serial, "peek", lambda: cfc.stopped_view())
    monkeypatch.setattr(mcp_pxe, "peek", lambda stopped: got.append(stopped()) or {"pending": [], "history": []})
    fake = SimpleNamespace(_beeper=None, _kiosk_report=SimpleNamespace(update=lambda *a, **k: None),
                           _lock_win=None, _kvm_count=0, _bells=None, _ups_since=None)
    res = kiosk.Kiosk._lamp_sample(fake)
    assert len(res) == 4 and res[3] == {"pending": [], "history": []} and got == [True]
