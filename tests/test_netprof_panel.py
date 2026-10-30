"""netprof_panel 测试: 只测不起窗口的那部分 (输入框重问逻辑), 对话框全部打桩."""

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
