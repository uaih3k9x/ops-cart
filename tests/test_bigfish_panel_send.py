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
