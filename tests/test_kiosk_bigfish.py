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


# ------------------------------------------------------------ 急停挂在现有"全部急停"上 (接口 v1)

import json  # noqa: E402
import os  # noqa: E402

import bigfish_core as bf  # noqa: E402
import confirm_core as cfc  # noqa: E402


def _ctl_fake(status=None):
    """带 _serial_action / _bigfish_ctl / 看门 / 硬停的假 self; after 和后台都记下来不真跑."""
    calls = {"bg": [], "after": [], "say": [], "hard": []}
    fake = SimpleNamespace(_sw_view=None, _kvm_view=None, _sw_store=object(), _confirm_win=None, banner=_Lab(),
                           _bf_watch=None, _bf_note="")
    fake.ui = SimpleNamespace(run_bg=lambda work, done, key=None: calls["bg"].append((work, done, key)) or True)
    fake.after = lambda ms, fn: calls["after"].append(fn)
    fake._serial_say = lambda text, ok=True: calls["say"].append((text, ok))
    for name in ("_serial_action", "_bigfish_ctl", "_bigfish_watch_tick", "_bigfish_hard", "_bigfish_estop"):
        setattr(fake, name, getattr(kiosk.Kiosk, name).__get__(fake))
    return calls, fake


def _bg(calls, key):
    return [c for c in calls["bg"] if c[2] == key]


def test_all_estop_writes_estop_file_and_starts_watch():
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)
    d = json.loads(open(bf.ESTOP_PATH).read())
    assert d["seq"] == 1 and d["by"] == "kiosk"
    assert _bg(calls, "serial-write") and fake._bf_watch is not None and calls["after"]
    fake._bigfish_estop()                                    # 面板按钮 = 同一个动作
    assert json.loads(open(bf.ESTOP_PATH).read())["seq"] == 2


def test_single_slot_estop_does_not_touch_bigfish():
    calls, fake = _ctl_fake()
    fake._serial_action("estop", 2)
    assert not os.path.exists(bf.ESTOP_PATH) and fake._bf_watch is None


def test_resume_all_deletes_estop_file_on_success(monkeypatch):
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)
    fake._sw_view = cfc.stopped_view()                       # 已经全部急停了, 才能解除
    fake._serial_action("resume", None)
    work, done, _key = _bg(calls, "serial-write")[-1]
    assert os.path.exists(bf.ESTOP_PATH)                     # 串口那边还没解除成功
    done("已解除 全部槽位 的急停")
    assert not os.path.exists(bf.ESTOP_PATH) and fake._bf_watch is None and fake._bf_note == ""


def test_resume_failure_keeps_estop_file():
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)
    fake._sw_view = cfc.stopped_view()
    fake._serial_action("resume", None)
    _work, done, _key = _bg(calls, "serial-write")[-1]
    done(RuntimeError("nope"))
    assert os.path.exists(bf.ESTOP_PATH)


def _status(monkeypatch, seen, online=True):
    v = bf.parse(json.dumps({"schema": 1, "written_at": 0, "estop": {"seen": seen}}), 0) if online \
        else bf.parse(None, 0)
    monkeypatch.setattr(kiosk.bfc, "load", lambda now: v)


def test_watch_tick_seen_stops_watching(monkeypatch):
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)
    _status(monkeypatch, True)
    fake._bigfish_watch_tick()
    assert fake._bf_watch is None and "已急停" in fake._bf_note and not _bg(calls, "bigfish-hard")


def test_watch_tick_escalates_to_hard_stop_after_deadline(monkeypatch):
    calls, fake = _ctl_fake()
    t = [1000.0]
    monkeypatch.setattr(kiosk.time, "time", lambda: t[0])
    fake._serial_action("estop", None)
    _status(monkeypatch, False)
    fake._bigfish_watch_tick()
    assert fake._bf_watch is not None and not _bg(calls, "bigfish-hard")
    t[0] += bf.HARD_AFTER_S
    fake._bigfish_watch_tick()
    assert fake._bf_watch is None
    ran = []
    monkeypatch.setattr(kiosk.bfc, "bigfish_uid", lambda user=bf.USER: 1001)
    monkeypatch.setattr(kiosk.kc, "run", lambda cmd, timeout=5: ran.append(cmd) or (0, ""))
    monkeypatch.setattr(kiosk.bfc, "cartlog_event", lambda kind, m: None)
    work, done, _key = _bg(calls, "bigfish-hard")[0]
    done(work())
    assert ran == [["loginctl", "terminate-user", "bigfish"], ["systemctl", "stop", "user@1001.service"]]
    assert fake._bf_note == "已硬停，需人工恢复"


def test_estop_without_channel_goes_hard_immediately(monkeypatch, tmp_path):
    monkeypatch.setattr(kiosk.bfc, "ESTOP_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(kiosk.bfc, "ESTOP_PATH", str(tmp_path / "nope" / "estop.json"))
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)
    assert _bg(calls, "bigfish-hard") and fake._bf_watch is None and _bg(calls, "serial-write")


# ------------------------------------------------------------ estop.json 残留: 解除大肥鱼急停 (中1)

def test_resume_all_allowed_when_only_estop_file_left():
    """串口那边已经没有急停 (终端解除 / 按路解除 / human_action 失败), 只剩 estop.json: 解除全部急停照样删它."""
    calls, fake = _ctl_fake()
    bf.estop_write(1.0)
    fake._sw_view = _view(None)                              # 没有一路在急停
    fake._serial_action("resume", None)
    assert not os.path.exists(bf.ESTOP_PATH) and not _bg(calls, "serial-write")


def test_resume_all_with_nothing_to_resume_still_refused():
    calls, fake = _ctl_fake()
    fake._sw_view = _view(None)
    fake._serial_action("resume", None)
    assert calls["say"][-1][1] is False and not _bg(calls, "serial-write")


def _resume_fake(pin_ok):
    calls, fake = _ctl_fake()
    fake._confirm_pin = lambda what: calls.setdefault("pin", []).append(what) or pin_ok
    fake._bigfish_resume = kiosk.Kiosk._bigfish_resume.__get__(fake)
    return calls, fake


def test_bigfish_resume_needs_pin_and_deletes_file():
    calls, fake = _resume_fake(True)
    bf.estop_write(1.0)
    fake._bigfish_resume()
    assert calls["pin"] and not os.path.exists(bf.ESTOP_PATH) and fake._bf_watch is None


def test_bigfish_resume_wrong_pin_keeps_file():
    calls, fake = _resume_fake(False)
    bf.estop_write(1.0)
    fake._bigfish_resume()
    assert os.path.exists(bf.ESTOP_PATH)


def test_watch_requires_echo_of_this_seq(monkeypatch):
    calls, fake = _ctl_fake()
    fake._serial_action("estop", None)                       # seq 1
    fake._serial_action("estop", None)                       # seq 2
    v = bf.parse(json.dumps({"schema": 1, "written_at": 0, "estop": {"seen": True, "seq": 1}}), 0)
    monkeypatch.setattr(kiosk.bfc, "load", lambda now: v)
    fake._bigfish_watch_tick()
    assert fake._bf_watch is not None                        # 回显的是上一次的 seq, 不算确认
