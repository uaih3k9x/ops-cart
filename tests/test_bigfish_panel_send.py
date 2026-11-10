"""大肥鱼面板的发送接线 (#99): 假 self + 假控件, 不起 Tk 窗口 (真控件的冒烟在 test_bigfish_panel.py, 待真屏)."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import bigfish_core as bf  # noqa: E402
import bigfish_panel as bp  # noqa: E402
import bigfish_task_core as tc  # noqa: E402

NOW = 1_700_000_000.0
TID = "t-abc"


class _Entry:
    def __init__(self, text=""):
        self.value, self.state = text, "normal"

    def get(self):
        return self.value

    def config(self, **kw):
        self.state = kw.get("state", self.state)

    def delete(self, *_a):
        if self.state == "normal":
            self.value = ""

    def insert(self, _at, text):
        if self.state == "normal":
            self.value += text


class _Widget:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


def _online():
    return bf.parse('{"schema": 1, "written_at": %f}' % NOW, NOW)


def _fake(text="看下状态", locked=False, estop=False, stopped=False, view=None, run_ok=True):
    jobs = []

    def run_bg(work, done, key=None):
        if not run_ok:
            return False
        jobs.append((work, done, key))
        return True

    fake = SimpleNamespace(
        desk=tc.Desk(), entry=_Entry(text), btn_send=_Widget(), btn_cancel=_Widget(), hint=_Widget(), _desk_gen=0,
        locked=lambda: locked, estop_on=lambda: estop, agents_stopped=lambda: stopped,
        load=lambda now: view if view is not None else _online(), clock=lambda: NOW, run_bg=run_bg,
        submit=lambda p: {"task_id": TID, "state": "starting"}, get_task=lambda t, o: {"task": t, "offset": o},
        cancel_task=lambda t: {"cancelled": t}, _redraw=lambda: None, _alive=lambda: True)
    for name in ("_load", "_block", "_sync_desk", "_send", "_sent", "_cancel", "_cancelled", "_poll", "_polled",
                 "_desk_done"):
        setattr(fake, name, getattr(bp.BigfishPanel, name).__get__(fake))
    return fake, jobs


def _finish(job):
    work, done, _key = job
    done(work())


def test_send_goes_through_background_and_clears_on_success():
    fake, jobs = _fake()
    fake._send()
    assert fake.desk.sending and fake.entry.value == "看下状态" and fake.entry.state == "disabled"
    assert len(jobs) == 1 and jobs[0][2] == "bigfish-task-send"
    _finish(jobs[0])
    assert fake.entry.value == "" and fake.entry.state == "normal" and fake.desk.task_id == TID


def test_send_failure_keeps_text_and_reenables():
    fake, jobs = _fake()
    fake._send()
    jobs[0][1](tc.Offline("连不上"))
    assert fake.entry.value == "看下状态" and fake.entry.state == "normal"
    assert tc.OFFLINE in fake.hint.cfg["text"]


@pytest.mark.parametrize("kw,word", [({"locked": True}, "锁屏"), ({"estop": True}, "急停"), ({"stopped": True}, "急停"),
                                     ({"view": bf.parse(None, NOW)}, tc.OFFLINE)])
def test_send_blocked_never_starts_work_and_keeps_text(kw, word):
    fake, jobs = _fake(**kw)
    fake._send()
    assert jobs == [] and fake.entry.value == "看下状态" and word in fake.hint.cfg["text"]
    assert fake.entry.state == "disabled" and fake.btn_send.cfg["state"] == "disabled"


def test_blocked_state_reads_gates_at_press_time():
    fake, jobs = _fake()
    fake._sync_desk()
    assert fake.btn_send.cfg["state"] == "normal"
    fake.estop_on = lambda: True                        # 两次刷新之间急停了
    fake._send()
    assert jobs == []


def test_estop_on_failure_fails_closed():
    fake, jobs = _fake()

    def boom():
        raise OSError("x")
    fake.estop_on = boom
    fake._send()
    assert jobs == []


def test_send_when_background_busy_keeps_text():
    fake, _jobs = _fake(run_ok=False)
    fake._send()
    assert not fake.desk.sending and fake.entry.value == "看下状态" and fake.desk.bad


def test_sync_clears_text_after_success_in_rebuilt_panel():
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    other, _ = _fake()
    other.desk = fake.desk
    other.entry.config(state="disabled")                # 重开面板时还在发送中, 框是灰的
    other._sync_desk()
    assert other.entry.value == "" and other._desk_gen == fake.desk.gen


def test_sync_buttons_while_task_active():
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    assert fake.btn_cancel.cfg["state"] == "normal" and fake.btn_send.cfg["state"] == "disabled"
    assert fake.entry.state == "normal"                 # 可以先写下一条


def test_poll_runs_in_background_with_key():
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    jobs.clear()
    fake._poll()
    assert len(jobs) == 1 and jobs[0][2] == "bigfish-task-poll"
    assert jobs[0][0]() == {"task": TID, "offset": 0}


def test_poll_idle_does_nothing():
    fake, jobs = _fake()
    fake._poll()
    assert jobs == []


def test_cancel_only_cancels_own_task():
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    jobs.clear()
    fake._cancel()
    assert len(jobs) == 1 and jobs[0][2] == "bigfish-task-cancel"
    assert jobs[0][0]() == {"cancelled": TID}
    jobs[0][1]({"cancel_requested": True})
    assert not fake.desk.cancelling


def test_late_result_after_panel_destroyed_does_not_touch_widgets():
    """面板关了后台才回来: desk 照常更新, 控件不碰."""
    fake, jobs = _fake()
    fake._send()
    work, done, _ = jobs[0]
    fake._alive = lambda: False

    def explode(*_a, **_k):
        raise AssertionError("不该碰控件")
    fake._sync_desk = fake._redraw = explode
    done(work())
    assert fake.desk.task_id == TID


def test_no_confirm_path_from_send():
    """发送只有 submit 这一条路: 接线里不引用确认按钮的回调."""
    import inspect
    for name in ("_send", "_sent", "_cancel", "_cancelled", "_poll", "_polled", "_sync_desk"):
        src = inspect.getsource(getattr(bp.BigfishPanel, name))
        assert "on_confirm" not in src and "btn_confirm" not in src, name


def test_send_when_background_busy_says_previous_send_not_offline():
    fake, _jobs = _fake(run_ok=False)
    fake._send()
    assert "上一次发送" in fake.hint.cfg["text"] and tc.OFFLINE not in fake.hint.cfg["text"]


def test_cancel_when_background_busy_does_not_say_offline():
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    fake.run_bg = lambda *_a, **_k: False
    fake._cancel()
    assert not fake.desk.cancelling and tc.OFFLINE not in fake.hint.cfg["text"]


def _refreshable(fake):
    jobs = []
    fake.after = lambda ms, fn: jobs.append((ms, fn)) or "job"
    fake.note, fake.ready, fake.pending = (lambda: ""), (lambda: True), (lambda: 0)
    fake.btn_confirm = _Widget()
    fake.btn_resume = SimpleNamespace(winfo_manager=lambda: "", pack=lambda **_k: None, pack_forget=lambda: None)
    fake._paint = bp.BigfishPanel._paint.__get__(fake)
    fake.refresh = bp.BigfishPanel.refresh.__get__(fake)
    return jobs


def test_refresh_keeps_chain_and_polls_when_draw_raises(capsys):
    """画坏一次: 下一次 after 照排, 轮询照发 (下一个好回复才能把坏状态换掉), 不往外抛 (__init__ 也调 refresh)."""
    fake, jobs = _fake()
    fake._send()
    _finish(jobs[0])
    jobs.clear()
    after = _refreshable(fake)

    def boom(*_a):
        raise OverflowError("x")
    fake._draw = boom
    fake.refresh()
    assert after == [(bp.REFRESH_MS, fake.refresh)] and fake._job == "job"
    assert [j[2] for j in jobs] == ["bigfish-task-poll"]
    assert "OverflowError" in capsys.readouterr().err


def test_refresh_keeps_chain_when_poll_raises():
    fake, _jobs = _fake()
    after = _refreshable(fake)
    fake._draw = lambda *_a: None

    def boom():
        raise RuntimeError("x")
    fake._poll = boom
    fake.refresh()
    assert len(after) == 1


def test_refresh_without_desk_does_not_poll():
    fake, _jobs = _fake()
    after = _refreshable(fake)
    fake.desk = None
    fake._draw = lambda *_a: None
    fake._poll = bp.BigfishPanel._poll.__get__(fake)
    fake.refresh()
    assert len(after) == 1 and "text" not in fake.hint.cfg


# ------------------------------------------------------------ 回复滚动区: 假 Text, 不起 Tk

class _Log:
    """假 Text, 按字符建模, 守真 Tk 的 end 语义: 末尾有一个删不掉的换行, "end" 在它后面, "end-1c" 在它前面;
    删除范围碰到这个换行且起点在行首时, 起点前的换行也一起删 (Tk 把它当成"删整行")."""

    def __init__(self, view=(0.0, 1.0)):
        self.chars, self.view, self.seen, self.moved, self.state, self.writes = [], view, [], [], "disabled", 0

    @property
    def lines(self):
        text = "".join(c for c, _ in self.chars)
        out, at = [], 0
        for line in text.split("\n"):
            if at < len(self.chars):
                out.append((line, self.chars[at][1]))
            at += len(line) + 1
        return out

    def yview(self):
        return self.view

    def yview_moveto(self, fraction):
        self.moved.append(fraction)

    def config(self, **kw):
        self.state = kw.get("state", self.state)

    def _at(self, index):
        if index == "end":
            return len(self.chars) + 1
        if index == "end-1c":
            return len(self.chars)
        line, column = (int(x) for x in index.split("."))
        assert column == 0
        at = 0
        for _ in range(line - 1):
            found = next((i for i in range(at, len(self.chars)) if self.chars[i][0] == "\n"), None)
            if found is None:
                return len(self.chars)
            at = found + 1
        return at

    def delete(self, first, last):
        assert self.state == "normal"
        start, stop = self._at(first), self._at(last)
        if stop > len(self.chars):
            stop = len(self.chars)
            if 0 < start <= stop and self.chars[start - 1][0] == "\n":
                start -= 1
        del self.chars[start:stop]
        self.writes += 1

    def insert(self, _at, text, tag):
        assert self.state == "normal" and text.endswith("\n")
        self.chars.extend((c, tag) for c in text)
        self.writes += 1

    def see(self, where):
        self.seen.append(where)


def _log_fake(view=(0.0, 1.0)):
    fake, _jobs = _fake()
    fake.desk.begin("看下状态", "")
    fake.desk.sent({"task_id": TID, "state": "starting"})
    fake.log = _Log(view)
    fake._log_gen, fake._log_n, fake._log_live = None, 0, []
    fake._update_log = bp.BigfishPanel._update_log.__get__(fake)
    return fake


def _feed(fake, text):
    got = fake.desk.got
    fake.desk.polled({"task_id": TID, "state": "running", "output_offset": got, "output_total_chars": got + len(text),
                      "untrusted_agent_output": {"output": text}})


def _plain(fake):
    return [t[len(bf.SAYS):] for t, _ in fake.log.lines]


def test_log_first_paint_inserts_all_and_follows_bottom():
    fake = _log_fake()
    _feed(fake, "一\n二\n")
    fake._update_log()
    assert _plain(fake) == ["一", "二"] and fake.log.seen == ["end"] and fake.log.state == "disabled"
    assert all(tag == "untrusted" for _, tag in fake.log.lines)


def test_log_no_change_does_not_touch_widget():
    fake = _log_fake()
    _feed(fake, "一\n半")
    fake._update_log()
    writes, seen = fake.log.writes, list(fake.log.seen)
    fake._update_log()
    assert fake.log.writes == writes and fake.log.seen == seen


def test_log_only_appends_and_keeps_earlier_lines():
    fake = _log_fake()
    _feed(fake, "一\n二\n")
    fake._update_log()
    deletes = []
    real = fake.log.delete
    fake.log.delete = lambda a, b: deletes.append(a) or real(a, b)
    _feed(fake, "三\n")
    fake._update_log()
    assert _plain(fake) == ["一", "二", "三"] and deletes == ["3.0"]


def test_log_partial_line_is_replaced_in_place():
    fake = _log_fake()
    _feed(fake, "一\n前")
    fake._update_log()
    _feed(fake, "后")
    fake._update_log()
    assert _plain(fake) == ["一", "前后"]
    _feed(fake, "\n")
    fake._update_log()
    assert _plain(fake) == ["一", "前后"]


def test_log_scrolled_up_is_not_pulled_back():
    fake = _log_fake(view=(0.1, 0.5))
    _feed(fake, "一\n")
    fake._update_log()
    _feed(fake, "二\n")
    fake._update_log()
    assert fake.log.seen == [] and _plain(fake) == ["一", "二"]


def test_log_resumes_following_when_back_at_bottom():
    fake = _log_fake(view=(0.1, 0.5))
    _feed(fake, "一\n")
    fake._update_log()
    fake.log.view = (0.5, 1.0)
    _feed(fake, "二\n")
    fake._update_log()
    assert fake.log.seen == ["end"]


def test_log_new_task_starts_over():
    fake = _log_fake()
    _feed(fake, "旧\n")
    fake._update_log()
    fake.desk.begin("下一条", "")
    fake.desk.sent({"task_id": TID, "state": "starting"})
    _feed(fake, "新\n")
    fake._update_log()
    assert _plain(fake) == ["新"]


def test_log_new_task_with_no_output_yet_clears_old():
    fake = _log_fake()
    _feed(fake, "旧\n")
    fake._update_log()
    fake.desk.begin("下一条", "")
    fake.desk.sent({"task_id": TID, "state": "starting"})
    fake._update_log()
    assert fake.log.lines == []


def test_log_first_paint_is_batched_so_main_thread_stays_free():
    fake = _log_fake()
    fake.desk.lines.extend((f"{bf.SAYS}{i}", "untrusted") for i in range(bp.LOG_BATCH + 5))
    fake.desk.tail = "半"
    fake._update_log()
    assert len(fake.log.lines) == bp.LOG_BATCH and fake.log.lines[-1][0] == f"{bf.SAYS}{bp.LOG_BATCH - 1}"
    fake._update_log()
    assert len(fake.log.lines) == bp.LOG_BATCH + 6 and fake.log.lines[-1][0] == bf.SAYS + "半"


def test_draw_updates_log_and_status_pane():
    fake = _log_fake()
    fake.text = _Log()
    fake._shown = None
    fake.note, fake.ready = (lambda: ""), (lambda: True)
    _feed(fake, "一\n")
    bp.BigfishPanel._draw.__get__(fake)(_online(), NOW)
    assert _plain(fake) == ["一"] and any("派的任务" in t for t, _ in fake.text.lines)
    assert all("一" != t for t, _ in fake.text.lines)


def test_draw_skips_unchanged_status_pane():
    fake = _log_fake()
    fake.text = _Log()
    fake._shown = None
    fake.note, fake.ready = (lambda: ""), (lambda: True)
    draw = bp.BigfishPanel._draw.__get__(fake)
    draw(_online(), NOW)
    writes = fake.text.writes
    draw(_online(), NOW)
    assert fake.text.writes == writes


def test_status_pane_keeps_scroll_position_on_redraw():
    fake = _log_fake()
    fake.text = _Log(view=(0.4, 0.8))
    fake._shown = None
    fake.note, fake.ready = (lambda: ""), (lambda: True)
    bp.BigfishPanel._draw.__get__(fake)(_online(), NOW)
    assert fake.text.moved == [0.4]


class _Tk:
    def __init__(self, *_a, **kw):
        self.kw, self.packed, self.binds = dict(kw), None, {}

    def config(self, **kw):
        self.kw.update(kw)

    def pack(self, **kw):
        self.packed = kw

    def bind(self, event, fn):
        self.binds[event] = fn

    def set(self, *_a):
        pass

    def yview(self, *_a):
        pass


def _built(monkeypatch, desk):
    bars = []
    monkeypatch.setattr(bp.tk, "Frame", _Tk)
    monkeypatch.setattr(bp.tk, "Scrollbar", lambda *a, **k: bars.append(_Tk(*a, **k)) or bars[-1])
    fake = SimpleNamespace(desk=desk, _text=lambda _parent: _Tk())
    fake._scrolled = bp.BigfishPanel._scrolled.__get__(fake)
    bp.BigfishPanel._build_status.__get__(fake)()
    return fake, bars


def test_status_pane_scrolls_when_desk_present(monkeypatch):
    fake, bars = _built(monkeypatch, tc.Desk())
    assert len(bars) == 1 and fake.text.kw["height"] == bp.STATUS_LINES
    assert fake.text.kw["yscrollcommand"] == bars[0].set and bars[0].kw["command"] == fake.text.yview


def test_status_pane_without_desk_fills_panel(monkeypatch):
    fake, bars = _built(monkeypatch, None)
    assert bars == [] and fake.text.packed == {"fill": "both", "expand": True}


def test_hint_wraps_to_its_width(monkeypatch):
    monkeypatch.setattr(bp.tk, "Label", _Tk)
    fake = SimpleNamespace()
    bp.BigfishPanel._build_hint.__get__(fake)()
    assert fake.hint.kw["justify"] == "left"
    fake.hint.binds["<Configure>"](SimpleNamespace(width=300))
    assert 0 < fake.hint.kw["wraplength"] <= 300
    fake.hint.binds["<Configure>"](SimpleNamespace(width=1))
    assert fake.hint.kw["wraplength"] > 0               # 0 在 Tk 里是"不折行"


def test_draw_without_desk_has_no_log():
    fake = _log_fake()
    fake.text, fake._shown, fake.desk = _Log(), None, None
    fake.note, fake.ready = (lambda: ""), (lambda: True)
    fake._update_log = lambda: pytest.fail("没有 desk 不该碰滚动区")
    bp.BigfishPanel._draw.__get__(fake)(_online(), NOW)


def test_page_keys_scroll_log():
    calls = []
    fake = SimpleNamespace(log=SimpleNamespace(yview_scroll=lambda n, unit: calls.append((n, unit))))
    assert bp.BigfishPanel._page.__get__(fake)(-1) == "break"
    assert calls == [(-1, "pages")]


def test_container_down_greys_out_with_reason_and_keeps_text():
    view = _online()
    view["health"] = "container_down"
    fake, jobs = _fake(view=view)
    fake._sync_desk()
    assert fake.btn_send.cfg["state"] == "disabled" and fake.entry.state == "disabled"
    assert fake.hint.cfg["text"] == bf.HEALTH_TEXT["container_down"][1] and fake.entry.value == "看下状态"
    fake._send()
    assert jobs == []
