"""bigfish_panel Tk 冒烟测试 (#53): 真起 Tk (没显示就跳过); 状态从假 load 来, 按钮交给回调."""

import pytest

tk = pytest.importorskip("tkinter")

import bigfish_core as bf  # noqa: E402
import bigfish_panel as bp  # noqa: E402

NOW = 1_700_000_000.0


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    r.withdraw()
    yield r
    r.destroy()


def _running():
    return {"online": True, "why": "", "state": "running",
            "task": {"state": "running", "started": NOW - 60, "title": "【推车】急停已解除", "step": "",
                     "focus": "con-1", "output": ["x"]}}


def _texts(w):
    return w.text.get("1.0", "end")


def test_panel_shows_rows_and_marks_untrusted(root):
    calls = []
    w = bp.BigfishPanel(root, load=lambda now: _running(), pending=lambda: 2,
                        on_confirm=lambda: calls.append("c"), on_estop=lambda: calls.append("s"), clock=lambda: NOW)
    t = _texts(w)
    assert "在看 con-1" in t and bf.SAYS + "【推车】急停已解除" in t
    ranges = w.text.tag_ranges("untrusted")
    assert ranges and bf.SAYS in w.text.get(ranges[0], ranges[1])
    assert "2" in w.btn_confirm.cget("text") and w.btn_confirm.cget("state") == "normal"
    w.btn_confirm.invoke()
    w.btn_estop.invoke()
    assert calls == ["c", "s"]
    w.destroy()


def test_panel_offline_and_no_pending(root):
    w = bp.BigfishPanel(root, load=lambda now: bf.parse(None, now), pending=lambda: 0,
                        on_confirm=lambda: None, on_estop=lambda: None, clock=lambda: NOW)
    assert "大肥鱼不在线" in _texts(w) and w.btn_confirm.cget("state") == "disabled"
    w.destroy()


def test_panel_load_error_shows_offline(root):
    def boom(now):
        raise OSError("x")
    w = bp.BigfishPanel(root, load=boom, pending=lambda: 0, on_confirm=lambda: None, on_estop=lambda: None,
                        clock=lambda: NOW)
    assert "大肥鱼" in _texts(w)
    w.destroy()


def test_panel_shows_note_from_kiosk(root):
    w = bp.BigfishPanel(root, load=lambda now: _running(), note=lambda: bf.HARD_NOTE, clock=lambda: NOW)
    assert _texts(w).startswith(bf.HARD_NOTE)
    w.destroy()
