"""车机打标签面板 (#104): 用假 self / 假 tk 测逻辑和接线, Mac 上不起真 Tk. 真 Tk 冒烟在有显示的机器上才跑."""

import gc
import os
from types import MethodType, SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

import label_core as lc  # noqa: E402
import label_panel as lp  # noqa: E402
import mcp_core as mc  # noqa: E402
import kiosk_ui_core as kui  # noqa: E402
import theme as th  # noqa: E402

P = lp.LabelPanel
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, v):
        self.value = v


def plan(fold=10.0, lines=("SW-A",)):
    return lc.build_plan(lc.Fit(20, list(lines), [lc.Ink(8, 2, b"\x01" * 16)], 40), fold)


def fake(run_bg=None, **over):
    """假 self: 所有方法绑上去, 控件换成记录器; run_bg 默认同步跑."""
    calls = SimpleNamespace(run=[], draw=[], clear=[], after=[], cancel=[])
    def sync(work, done, key=None):
        calls.run.append(key)
        try:
            res = work()
        except Exception as ex:             # noqa: BLE001
            res = ex
        done(res)
        return True

    f = SimpleNamespace(vars=[Var("SW-A"), Var("")], msg=Lab(), status=Lab(), btn_again=Lab(), copies_lab=Lab(),
                        btn_many=Lab(), cal_info=Lab(), cal_entry=Var(""), conf=lc.default_conf(), copies=1, last=None,
                        run_bg=run_bg or sync, render=lambda raw, fold: plan(fold), save=lambda c: None,
                        dev=SimpleNamespace(print_label=lambda raw, fold, copies=1: plan(fold),
                                            feed=lambda: None, ruler=lambda: None, query_status=lambda: {"ok": True, "flags": []}),
                        _preview_job=None, _status_job=None, _stale=False, calls=calls,
                        after=lambda ms, fn: calls.after.append((ms, fn)) or "job",
                        after_cancel=lambda j: calls.cancel.append(j), _printing=False)
    for name, fn in vars(P).items():
        if callable(fn) and not name.startswith("__"):
            setattr(f, name, MethodType(fn, f))
    f._alive = lambda: True
    f._draw = lambda p: calls.draw.append(p)
    f._clear_preview = lambda: calls.clear.append(True)
    f._render_recents = lambda: None
    for k, v in over.items():
        setattr(f, k, v)
    return f


# ---------------------------------------------------------------- 预览

def test_preview_runs_in_background_with_calibrated_fold():
    f = fake()
    f.conf = lc.set_fold(f.conf, lc.PAPER, 12.0)
    seen = []
    f.render = lambda raw, fold: seen.append((raw, fold)) or plan(fold)
    f._start_preview()
    assert seen == [(["SW-A", ""], 12.0)] and f.calls.draw and f.calls.run == [lp.KEY_RENDER]


def test_empty_text_clears_preview_without_spawning():
    f = fake()
    f.vars = [Var(" "), Var("")]
    f._start_preview()
    assert f.calls.clear == [True] and f.calls.run == [] and f.msg.cfg.get("text") == ""


def test_render_error_shown_not_raised():
    f = fake()

    def boom(raw, fold):
        raise lc.LabelError("字太多")
    f.render = boom
    f._start_preview()
    assert f.msg.cfg["text"] == "字太多" and f.msg.cfg["fg"] == th.BAD and f.calls.clear


def test_fold_warning_shown_after_preview(monkeypatch):
    f = fake()
    monkeypatch.setattr(lp.lc, "problems", lambda p: [lc.FOLD_WARNING])
    f._start_preview()
    assert lc.FOLD_WARNING in f.msg.cfg["text"] and f.msg.cfg["fg"] == th.WARN


def test_stale_preview_is_dropped_and_rerun():
    f = fake()
    f._previewed(plan(), ["old", ""])       # 输入框已经不是 old 了
    assert f.calls.draw == [] and f.calls.after and f.calls.after[0][0] == lp.DEBOUNCE_MS


def test_preview_busy_marks_stale():
    f = fake(run_bg=lambda work, done, key=None: False)
    f._start_preview()
    assert f._stale is True


def test_late_callback_after_close_is_ignored():
    f = fake()
    f._alive = lambda: False
    f._previewed(plan(), ["SW-A", ""])
    f._printed(plan(), ["SW-A", ""], 1)
    assert f.calls.draw == [] and "text" not in f.msg.cfg


# ---------------------------------------------------------------- 打印

def test_print_one_goes_to_background_and_remembers():
    f = fake()
    got = []
    f.dev.print_label = lambda raw, fold, copies=1: got.append((raw, fold, copies)) or plan(fold)
    f.print_one()
    assert got == [(["SW-A", ""], lc.FOLD_DEFAULT_MM, 1)]
    assert f.calls.run == [lp.KEY_IO] and f.last == ["SW-A", ""]
    assert f.btn_again.cfg["state"] == "normal" and "1" in f.msg.cfg["text"]


def test_print_copies_uses_selected_count():
    f = fake()
    got = []
    f.dev.print_label = lambda raw, fold, copies=1: got.append(copies) or plan(fold)
    f.step_copies(+1)
    f.step_copies(+1)
    assert f.copies == 3 and f.copies_lab.cfg["text"] == "3"
    f.print_copies()
    assert got == [3]


@pytest.mark.parametrize("start, delta, want", [(1, -1, 1), (lc.MAX_COPIES, 1, lc.MAX_COPIES)])
def test_copies_clamped(start, delta, want):
    f = fake()
    f.copies = start
    f.step_copies(delta)
    assert f.copies == want


def test_print_again_reuses_last_text_even_if_entry_changed():
    f = fake()
    got = []
    f.dev.print_label = lambda raw, fold, copies=1: got.append((raw, copies)) or plan(fold)
    f.last = ["SW-OLD", ""]
    f.vars[0].set("SW-NEW")
    f.print_again()
    assert got == [(["SW-OLD", ""], 1)]


def test_print_again_without_last_says_so():
    f = fake()
    f.print_again()
    assert f.calls.run == [] and f.msg.cfg["fg"] == th.BAD


@pytest.mark.parametrize("ex", [lc.LabelError("没打: 缺纸"), mc.ToolError("打印机没接 (/dev/usb/lp0 不在)"),
                                RuntimeError("???"), OSError("io")])
def test_print_failure_shown_never_raised(ex):
    f = fake()

    def boom(raw, fold, copies=1):
        raise ex
    f.dev.print_label = boom
    f.print_one()
    assert str(ex) in f.msg.cfg["text"] and f.msg.cfg["fg"] == th.BAD and f.last is None


def test_print_busy_says_so():
    f = fake(run_bg=lambda work, done, key=None: False)
    f.print_one()
    assert "忙" in f.msg.cfg["text"]


def test_printed_saves_recent_to():
    saved = []
    f = fake()
    f.save = saved.append
    f.vars[0].set("to SW-B")
    f.print_one()
    assert saved and saved[0]["recent_to"] == ["to SW-B"] and f.conf["recent_to"] == ["to SW-B"]


def test_recent_save_failure_only_says():
    f = fake()

    def bad(_c):
        raise OSError("ro")
    f.save = bad
    f.vars[0].set("to SW-B")
    f.print_one()                           # 打成功了, 记常用失败不算打印失败
    assert "常用" in f.msg.cfg["text"]


def test_to_button_prefixes_first_line():
    f = fake()
    f.vars[0].set("SW-B")
    f.fill_to()
    assert f.vars[0].get() == "to SW-B"


def test_recent_button_fills_first_line():
    f = fake()
    f.fill_recent("to SW-C")
    assert f.vars[0].get() == "to SW-C"


# ---------------------------------------------------------------- 状态

@pytest.mark.parametrize("st, word, color", [
    ({"ok": False, "flags": ["缺纸"]}, "缺纸", "BAD"),
    ({"ok": True, "flags": []}, "就绪", "OK"),
])
def test_status_shown(st, word, color):
    f = fake()
    f.dev.query_status = lambda: st
    f._status_tick()
    assert word in f.status.cfg["text"] and f.status.cfg["fg"] == getattr(th, color)
    assert f.calls.after and f.calls.after[-1][0] == lp.STATUS_MS


def test_status_error_shown_and_keeps_polling():
    f = fake()

    def gone():
        raise mc.ToolError("打印机没接 (/dev/usb/lp0 不在)")
    f.dev.query_status = gone
    f._status_tick()
    assert "没接" in f.status.cfg["text"] and f.status.cfg["fg"] == th.BAD and f.calls.after


def test_status_skipped_while_printing():
    f = fake()
    f._printing = True
    f._status_tick()
    assert f.calls.run == [] and f.calls.after


# ---------------------------------------------------------------- 走纸 / 校准

def test_feed_and_ruler_background():
    f = fake()
    done = []
    f.dev.feed = lambda: done.append("feed")
    f.dev.ruler = lambda: done.append("ruler")
    f.feed()
    f.ruler()
    assert done == ["feed", "ruler"] and f.calls.run == [lp.KEY_IO, lp.KEY_IO]


def test_feed_failure_is_message():
    f = fake()

    def bad():
        raise mc.ToolError("打印机没接")
    f.dev.feed = bad
    f.feed()
    assert "没接" in f.msg.cfg["text"]


def test_save_cal_saves_per_paper_and_replans():
    saved = []
    f = fake()
    f.save = saved.append
    f.cal_entry.set("11.5")
    f.save_cal()
    assert saved[0]["papers"]["cable"]["fold_mm"] == 11.5 and lc.fold_mm(f.conf) == 11.5
    assert "11.5" in f.cal_info.cfg["text"] and f.calls.after


@pytest.mark.parametrize("text", ["abc", "99"])
def test_save_cal_bad_reading_refused(text):
    saved = []
    f = fake()
    f.save = saved.append
    f.cal_entry.set(text)
    f.save_cal()
    assert saved == [] and f.msg.cfg["fg"] == th.BAD


def test_save_cal_write_failure_keeps_old_value():
    f = fake()

    def bad(_c):
        raise OSError("read-only")
    f.save = bad
    f.cal_entry.set("11.5")
    f.save_cal()
    assert lc.fold_mm(f.conf) == lc.FOLD_DEFAULT_MM and "read-only" in f.msg.cfg["text"]


# ---------------------------------------------------------------- 假 tk 跑 _build: 接线 / 中文输入白名单

class Stub:
    made = []

    def __init__(self, *a, **kw):
        self.a, self.kw = a, kw
        Stub.made.append(self)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_build_wires_ime_on_text_entries_only(monkeypatch):
    attached = []
    fake_tk = SimpleNamespace(Frame=Stub, Label=Stub, Button=Stub, Canvas=Stub, LabelFrame=Stub,
                              Entry=type("E", (Stub,), {}),
                              StringVar=lambda value="": SimpleNamespace(get=lambda: value, trace_add=lambda *a: None))
    Stub.made = []
    monkeypatch.setattr(lp, "tk", fake_tk)
    monkeypatch.setattr(lp.ime_tk, "attach", lambda e, purpose, **k: attached.append((e, purpose)) or object())
    f = fake()
    P._build(f)
    entries = [w for w in Stub.made if isinstance(w, fake_tk.Entry)]
    assert len(entries) == 3 and [p for _e, p in attached] == ["label.text", "label.text"]
    plain = [e for e in entries if e not in [a for a, _p in attached]]
    assert len(plain) == 1 and plain[0].no_ime is True           # 校准读数框


# ---------------------------------------------------------------- 约定

def test_panel_source_has_no_cross_thread_tk_or_symbols():
    with open(os.path.join(ROOT, "label_panel.py"), encoding="utf-8") as f:
        src = f.read()
    assert "after(0" not in src and "threading.Thread" not in src
    assert [ln for ln in src.splitlines() if kui.is_symbol_char(ln)] == []


def test_web_port_does_not_reach_printing():
    for name in ("webkiosk.py", "web_core.py"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            src = f.read()
        assert "label_device" not in src and "label_panel" not in src and "print_label" not in src


def test_kiosk_passes_run_bg_to_label_panel():
    with open(os.path.join(ROOT, "kiosk.py"), encoding="utf-8") as f:
        src = f.read()
    assert "LabelPanel(self.body, run_bg=self.ui.run_bg)" in src and "label" in kui.PANES


# ---------------------------------------------------------------- 真 Tk (有显示才跑)

def test_real_tk_panel_builds_and_previews():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    r.withdraw()
    p = plan()
    w = P(r, run_bg=lambda work, done, key=None: (done(work()), True)[1], load=lc.default_conf,
          save=lambda c: None, render=lambda raw, fold: p,
          device=SimpleNamespace(print_label=lambda *a, **k: p, feed=lambda: None, ruler=lambda: None,
                                 query_status=lambda: {"ok": True, "flags": []}))
    w._draw(p)
    assert th.unreadable(w) == []
    w.destroy()
    r.destroy()
    gc.collect()
