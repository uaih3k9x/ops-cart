"""bigfish_panel Tk 冒烟测试 (#53): 真起 Tk (没显示就跳过); 状态从假 load 来, 按钮交给回调."""

import gc
import json

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
    gc.collect()        # Tk 控件的循环引用在主线程收掉; 留给别的线程 (测试里的 HTTP 服务线程) 收会 Tcl_AsyncDelete abort


def _running():
    return bf.parse(json.dumps({"schema": 1, "written_at": NOW, "current": {
        "task_id": "t", "state": "running", "started_at": NOW - 60, "elapsed_s": 60},
        "untrusted": {"title": "【推车】急停已解除", "output_tail": "x"}}, ensure_ascii=False), NOW)


def _texts(w):
    return w.text.get("1.0", "end")


def test_panel_shows_rows_and_marks_untrusted(root):
    calls = []
    w = bp.BigfishPanel(root, load=lambda now: _running(), pending=lambda: 2,
                        on_confirm=lambda: calls.append("c"), on_estop=lambda: calls.append("s"), clock=lambda: NOW)
    t = _texts(w)
    assert "在做事" in t and bf.SAYS + "【推车】急停已解除" in t
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
    w = bp.BigfishPanel(root, load=lambda now: _running(), note=lambda: "已硬停，需人工恢复",
                        clock=lambda: NOW)
    assert _texts(w).startswith("已硬停，需人工恢复")
    w.destroy()


def test_panel_resume_button_only_when_estop_file_exists(root):
    on, calls = [True], []
    w = bp.BigfishPanel(root, load=lambda now: _running(), clock=lambda: NOW, estop_on=lambda: on[0],
                        on_resume=lambda: calls.append("r"))
    assert w.btn_resume.winfo_manager() == "pack" and w.btn_resume.pack_info()["side"] == "right"   # 和急停隔开
    w.btn_resume.invoke()
    assert calls == ["r"]
    on[0] = False
    w.after_cancel(w._job)
    w.refresh()
    assert w.btn_resume.winfo_manager() == ""
    w.destroy()


# ------------------------------------------------------------ #99 输入行 (真控件, 待真屏验证)

def _desk_panel(root, **kw):
    import bigfish_task_core as tc
    jobs = []
    args = dict(load=lambda now: bf.parse('{"schema": 1, "written_at": %f}' % NOW, NOW), pending=lambda: 0,
                on_confirm=lambda: None, on_estop=lambda: None, clock=lambda: NOW, estop_on=lambda: False,
                desk=tc.Desk(), run_bg=lambda w, d, key=None: jobs.append((w, d)) or True,
                locked=lambda: False, agents_stopped=lambda: False,
                submit=lambda p: {"task_id": "t-1", "state": "starting"})
    args.update(kw)
    return bp.BigfishPanel(root, **args), jobs


def test_input_row_sends_and_clears(root):
    w, jobs = _desk_panel(root)
    w.entry.insert(0, "看下状态")
    w.btn_send.invoke()
    work, done = jobs[0]
    done(work())
    assert w.entry.get() == "" and "派的任务" in _texts(w)
    w.destroy()


def test_input_row_ime_attached(root):
    w, _ = _desk_panel(root)
    assert w.task_ime is not None and w.task_ime.entry is w.entry and not getattr(w.entry, "no_ime", False)
    w.destroy()


def test_input_row_greyed_when_locked_keeps_text(root):
    w, jobs = _desk_panel(root, locked=lambda: True)
    w.entry.config(state="normal")
    w.entry.insert(0, "别丢")
    w.refresh()
    assert w.entry.cget("state") == "disabled" and w.entry.get() == "别丢" and "锁屏" in w.hint.cget("text")
    w.btn_send.invoke()
    assert jobs == []
    w.destroy()


def test_panel_without_desk_has_no_input_row(root):
    w = bp.BigfishPanel(root, load=lambda now: bf.parse(None, now), pending=lambda: 0,
                        on_confirm=lambda: None, on_estop=lambda: None, clock=lambda: NOW)
    assert w.entry is None
    w.destroy()


def test_reply_log_keeps_scroll_position_when_new_output_arrives(root):
    w, jobs = _desk_panel(root)
    w.entry.insert(0, "看下状态")
    w.btn_send.invoke()
    work, done = jobs[0]
    done(work())
    root.geometry("800x600")
    root.update()

    want = []

    def feed_text(text):
        got = w.desk.got
        w._polled({"task_id": "t-1", "state": "running", "output_offset": got, "output_total_chars": got + len(text),
                   "untrusted_agent_output": {"output": text}})
        root.update()

    def feed(n):
        lines = [f"行{w.desk.got}-{i}" for i in range(n)]
        want.extend(lines)
        feed_text("".join(line + "\n" for line in lines))
    feed(300)
    assert w.log.yview()[1] == 1.0                      # 在底部: 跟着走
    w.log.yview_moveto(0.2)
    root.update()
    before = w.log.index("@0,0")
    feed(50)
    assert w.log.index("@0,0") == before                # 往上翻时不拉回底部
    w.log.yview_moveto(1.0)
    root.update()
    feed(50)
    assert w.log.yview()[1] == 1.0
    assert w.log.cget("state") == "disabled"
    feed_text("半")                                     # 半行就地换掉: 不粘上一行, 不留旧半行
    assert w.log.get("1.0", "end-1c") == "".join(f"{bf.SAYS}{x}\n" for x in want + ["半"])
    feed_text("行\n")
    assert w.log.get("1.0", "end-1c") == "".join(f"{bf.SAYS}{x}\n" for x in want + ["半行"])
    w.destroy()


def test_status_pane_scrollbar_and_hint_wrap(root):
    w, _ = _desk_panel(root)
    root.geometry("800x600")
    root.deiconify()
    root.update()
    assert w.text.cget("yscrollcommand") and w.hint.cget("wraplength") > 0
    w.destroy()
