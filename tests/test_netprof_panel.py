"""netprof_panel 测试: 只测不起窗口的那部分 (输入框重问逻辑), 对话框全部打桩."""

from types import SimpleNamespace

import netprof_core as npc
import netprof_panel as panel


def _stub(monkeypatch, answers):
    asked, errors = [], []

    def askstring(title, prompt, initialvalue=None, parent=None):
        asked.append(initialvalue)
        return answers.pop(0)
    monkeypatch.setattr(panel.simpledialog, "askstring", askstring)
    monkeypatch.setattr(panel.messagebox, "showerror", lambda t, m, parent=None: errors.append(m))
    return asked, errors


def test_ask_valid_reasks_with_last_input_until_valid(monkeypatch):
    asked, errors = _stub(monkeypatch, ["192.0.2.5", "192.0.2.5/24"])
    form = panel.ask_valid(None, "t", "p", "192.0.2.250/24", npc.static_form)
    assert form["addr"] == "192.0.2.5/24"
    assert asked == ["192.0.2.250/24", "192.0.2.5"]       # 预填建议值; 填错了带着刚填的重问
    assert len(errors) == 1 and "前缀" in errors[0]


def test_ask_valid_cancel_returns_none(monkeypatch):
    _, errors = _stub(monkeypatch, [None])
    assert panel.ask_valid(None, "t", "p", "", npc.static_form) is None
    assert errors == []


# ------------------------------------------------- 切换前确认 (#8): LAN1 也要, 点否什么都不动

CUR = {"name": "Lan1-pxe", "uuid": "a737-4", "iface": "eno1", "kind": "serve",
       "label": "装机", "active": True, "builtin": True}
NEW = {"name": "cart-eno1-dhcp-临时", "uuid": "dddd-7", "iface": "eno1", "kind": "dhcp",
       "label": "临时", "active": False, "builtin": False}


def _fake_panel():
    calls = {"bg": [], "say": []}
    return calls, SimpleNamespace(
        _active=lambda iface: CUR, _ips={"eno1": "192.0.2.5/24"}, prev={},
        _say=lambda *a, **k: calls["say"].append(a), _bg=lambda *a, **k: calls["bg"].append(a))


def test_switch_lan1_no_means_nothing_happens(monkeypatch):
    shown = []
    monkeypatch.setattr(panel.messagebox, "askyesno", lambda t, m, **k: shown.append(m) or False)
    calls, fake = _fake_panel()
    panel.NetProfPanel.switch(fake, NEW)
    assert len(shown) == 1 and "LAN1" in shown[0] and "出问题怎么回滚" in shown[0]
    assert "192.0.2.5/24" in shown[0]
    assert calls["bg"] == []                       # 没进后台 = 没跑 nmcli / systemctl


def test_switch_lan1_yes_goes_to_background(monkeypatch):
    monkeypatch.setattr(panel.messagebox, "askyesno", lambda t, m, **k: True)
    calls, fake = _fake_panel()
    panel.NetProfPanel.switch(fake, NEW)
    assert len(calls["bg"]) == 1
