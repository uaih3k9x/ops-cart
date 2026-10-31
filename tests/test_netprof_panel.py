"""netprof_panel 测试: 只测不起窗口的那部分 (输入框重问逻辑), 对话框全部打桩."""

from types import SimpleNamespace

import pytest

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


# ------------------------------------------------- 静态模式主输入 = 本机 地址/前缀 (#13)

class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _form_panel(monkeypatch, addr="", peer="", yes=True):
    """假面板: 只带 create/prefill 用到的字段; 记下弹了哪些确认框、进没进后台、拼了什么命令."""
    calls = {"bg": [], "say": [], "ask": [], "cmd": []}
    monkeypatch.setattr(panel.messagebox, "askyesno", lambda t, m, **k: calls["ask"].append(m) or yes)

    def no_dialog(*a, **k):
        raise AssertionError("#13: 地址在表单里填, 不再弹框猜")
    monkeypatch.setattr(panel.simpledialog, "askstring", no_dialog)
    real = npc.cmd_create
    monkeypatch.setattr(panel.npc, "cmd_create",
                        lambda *a, **k: calls["cmd"].append(k.get("addr")) or real(*a, **k))
    fake = SimpleNamespace(
        iface_var=_Var("enp3s0"), kind_var=_Var("static"), label_var=_Var("现场"),
        self_var=_Var(addr), peer_var=_Var(peer), profs=[],
        _say=lambda *a, **k: calls["say"].append(a), _bg=lambda *a, **k: calls["bg"].append(a))
    return calls, fake


@pytest.mark.parametrize("addr", ["172.16.0.250/16", "192.0.2.250/23", "192.0.2.126/25", "192.0.2.6/30"])
def test_create_static_takes_prefix_as_typed(monkeypatch, addr):
    calls, fake = _form_panel(monkeypatch, addr=addr)
    panel.NetProfPanel.create(fake)
    assert calls["cmd"] == [addr]                  # 掩码按人填的来, 不改成 /24
    assert len(calls["ask"]) == 1 and addr in calls["ask"][0]
    assert len(calls["bg"]) == 1


def test_create_static_without_prefix_errors_not_guess(monkeypatch):
    calls, fake = _form_panel(monkeypatch, addr="192.0.2.5")
    panel.NetProfPanel.create(fake)
    assert calls["cmd"] == [] and calls["ask"] == [] and calls["bg"] == []
    assert "前缀" in calls["say"][-1][0]


def test_create_static_peer_only_checks_collision(monkeypatch):
    calls, fake = _form_panel(monkeypatch, addr="192.0.2.1/25", peer="192.0.2.1")
    panel.NetProfPanel.create(fake)
    assert calls["bg"] == [] and "对端" in calls["say"][-1][0]

    calls, fake = _form_panel(monkeypatch, addr="192.0.2.9/25", peer="192.0.2.1")   # 对端不带前缀也只防撞
    panel.NetProfPanel.create(fake)
    assert calls["cmd"] == ["192.0.2.9/25"] and len(calls["bg"]) == 1


def test_create_static_not_confirmed_writes_nothing(monkeypatch):
    calls, fake = _form_panel(monkeypatch, addr="192.0.2.5/24", yes=False)
    panel.NetProfPanel.create(fake)
    assert calls["bg"] == []


def test_create_dhcp_ignores_address_fields(monkeypatch):
    calls, fake = _form_panel(monkeypatch, addr="乱填", peer="乱填")
    fake.kind_var.set("dhcp")
    panel.NetProfPanel.create(fake)
    assert calls["cmd"] == [None] and calls["ask"] == [] and len(calls["bg"]) == 1


def test_prefill_from_peer_needs_prefix(monkeypatch):
    calls, fake = _form_panel(monkeypatch, addr="192.0.2.5/24", peer="192.0.2.1")
    panel.NetProfPanel.prefill(fake)
    assert fake.self_var.get() == "192.0.2.5/24"   # 不猜 /24, 原样不动
    assert "前缀" in calls["say"][-1][0]

    calls, fake = _form_panel(monkeypatch, peer="192.0.2.1/25")
    panel.NetProfPanel.prefill(fake)
    assert fake.self_var.get() == "192.0.2.126/25"
