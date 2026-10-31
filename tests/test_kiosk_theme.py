"""kiosk 深浅切换 (#32): 只在锁屏且没人碰时重建主界面; 三档开关写 theme.json 并记日志.
判断逻辑在 kiosk_ui_core (另有单测); 这里用假 self 看车机把它接对了, 不起 Kiosk 窗口."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import kiosk  # noqa: E402
import kiosk_ui_core as kui  # noqa: E402
import theme as th  # noqa: E402

NOW = 1_782_043_200.0


class _Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


def _fake(**over):
    rebuilt = []
    f = SimpleNamespace(_theme="dark", _theme_want="light", _lock_win=object(), _last_input=NOW - 600,
                        pane=SimpleNamespace(current=None), passthrough=False, _boot_win=None, _arm_win=None,
                        ui=kui.UiBridge(None), banner=_Lab(), btn_theme=_Lab(), _other_windows=lambda: [])
    f.__dict__.update(over)
    f._rebuild_ui = lambda mode: rebuilt.append(mode)
    for name in ("_theme_tick", "_ui_busy", "cycle_theme", "_theme_setting"):
        setattr(f, name, getattr(kiosk.Kiosk, name).__get__(f))
    return f, rebuilt


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(kiosk.time, "time", lambda: NOW)


def test_rebuilds_when_locked_and_nobody_touches_it():
    f, rebuilt = _fake()
    f._theme_tick()
    assert rebuilt == ["light"]


@pytest.mark.parametrize("over", [
    {"_lock_win": None},                                  # 没锁: 有人在用, 绝不重建
    {"_last_input": NOW - 3},                             # 锁屏上正在输 PIN
    {"_theme_want": "dark"},                              # 没有要换的
    {"_theme_want": None},                                # 快照过期 / 读不懂
    {"pane": SimpleNamespace(current="serial")},          # 串口会话开着
    {"pane": SimpleNamespace(current="preview")},         # 预览 (mpv 嵌着)
    {"passthrough": True},
    {"_boot_win": object()},                              # 还在开机自检页
    {"_other_windows": lambda: [".confirm"]},             # 确认框 / 申请框 / messagebox
])
def test_never_rebuilds_while_in_use(over):
    f, rebuilt = _fake(**over)
    f._theme_tick()
    assert rebuilt == []


def test_open_arm_dialog_blocks_rebuild():
    f, rebuilt = _fake(_arm_win=SimpleNamespace(winfo_exists=lambda: True))
    f._theme_tick()
    assert rebuilt == []


def test_background_menu_action_blocks_rebuild_but_status_board_does_not():
    f, rebuilt = _fake()
    f.ui._busy.add("board")                               # 状态板刷新: 回调按名字找控件, 重建后照样能用
    f._theme_tick()
    assert rebuilt == ["light"]
    f, rebuilt = _fake()
    f.ui._busy.add("toggle_pxe")                          # 菜单动作回来要恢复旧按钮: 等它
    f._theme_tick()
    assert rebuilt == []


@pytest.fixture
def conf(tmp_path, monkeypatch):
    p = tmp_path / "theme.json"
    p.write_text(json.dumps({"mode": "auto", "lat": 23.1, "lon": 113.3}))
    monkeypatch.setattr(kiosk.sun_core, "CONF_PATH", str(p))
    events = []
    monkeypatch.setattr(kiosk.cartlog, "event", lambda kind, msg: events.append((kind, msg)))
    return p, events


def test_cycle_theme_writes_next_setting_and_logs(conf):
    p, events = conf
    f, _rebuilt = _fake()
    f.cycle_theme()
    assert json.loads(p.read_text()) == {"mode": "dark", "lat": 23.1, "lon": 113.3}
    assert f.btn_theme.cfg["text"] == kui.theme_button_text("dark")
    assert "下次锁屏" in f.banner.cfg["text"]
    assert events == [("theme", "界面档位 auto -> dark (车机设置)")]
    f.cycle_theme()
    f.cycle_theme()
    assert json.loads(p.read_text())["mode"] == "auto" and len(events) == 3


def test_cycle_theme_broken_conf_says_why_and_keeps_file(conf):
    p, events = conf
    p.write_text("{坏")
    f, _rebuilt = _fake()
    f.cycle_theme()
    assert p.read_text() == "{坏" and events == []
    assert "读不懂" in f.banner.cfg["text"]


def test_net_label_color_is_picked_when_painting():
    """网络标的颜色在画的时候按当前配色取: 重建换浅色后, 上一轮探测的结果不能还带着深色的红 (Xvfb 实测抓到过)."""
    f = SimpleNamespace(_net_imgs={}, net_label=_Lab(), _lock_net=None)
    for name in ("_render_net", "_paint_net"):
        setattr(f, name, getattr(kiosk.Kiosk, name).__get__(f))
    f._render_net("off", "未连接")
    assert f.net_label.cfg["fg"] == th.DARK["BAD"]
    th.use("light")
    try:
        f._paint_net(f.net_label)
        assert f.net_label.cfg["fg"] == th.LIGHT["BAD"] and f.net_label.cfg["text"] == "未连接"
    finally:
        th.use("dark")


def test_every_menu_action_is_a_kiosk_method():
    for items in kui.MENU.values():
        for it in items:
            if "do" in it:
                assert callable(getattr(kiosk.Kiosk, it["do"], None)), it["do"]


def test_kiosk_restores_dark_theme_module_state():
    """这个文件里的测试都不该把 theme 切走."""
    assert th.MODE == "dark"
