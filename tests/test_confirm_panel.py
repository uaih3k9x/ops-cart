"""confirm_panel Tk 冒烟测试 (#23): 真起 Tk (没显示就跳过), 按按钮看交出去的动作; 不碰状态文件和串口."""

import pytest

tk = pytest.importorskip("tkinter")

import confirm_panel as cp  # noqa: E402
import serial_write_core as sw  # noqa: E402

T0 = 1_700_000_000.0
DANGER = {"verdict": "confirm", "layer": 1, "reason": "带高危词", "danger": ["save"], "vendor": "huawei"}
HOLD = {"verdict": "confirm", "layer": 1, "reason": "不在只读白名单里", "danger": [], "vendor": "huawei"}


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    r.withdraw()
    yield r
    r.destroy()


def _view(stop=False, items=(("save", DANGER), ("undo shutdown", HOLD))):
    b = sw.Book.from_text(None, T0)
    tok = b.acquire(1, "stdio:ops@x", T0)["lease"]
    for cmd, gate in items:
        b.submit(1, tok, "stdio:ops@x", "line", cmd, gate, T0, 30, name="SW1")
    if stop:
        b.estop(1, "human:kiosk@local", "human", T0)
    return b.view(T0 + 2)


def _buttons(w):
    out, stack = {}, [w]
    while stack:
        x = stack.pop()
        stack.extend(x.winfo_children())
        if isinstance(x, tk.Button):
            out.setdefault(x.cget("text"), []).append(x)
    return out


def _labels(w):
    out, stack = [], [w]
    while stack:
        x = stack.pop()
        stack.extend(x.winfo_children())
        if isinstance(x, tk.Label):
            out.append(x.cget("text"))
    return out


def _dlg(root, view, ask=True):
    acts, asked = [], []
    d = cp.ConfirmDialog(root, 1, view, lambda op, arg: acts.append((op, arg)),
                         on_serial=lambda: acts.append(("serial", None)),
                         ask=lambda title, msg: asked.append(msg) or ask)
    d.update_idletasks()
    return d, acts, asked


def test_dialog_shows_what_agent_wants(root):
    d, _acts, _asked = _dlg(root, _view())
    text = "\n".join(_labels(d))
    assert "Agent 想执行：save" in text and "Agent 想执行：undo shutdown" in text
    assert "高危词：save" in text and "闸门理由：不在只读白名单里" in text and "操作头 1 · SW1" in text
    b = _buttons(d)
    assert len(b["确认发送"]) == 2 and len(b["拒绝"]) == 2 and "急停本路" in b and "全部急停" in b


def test_confirm_danger_asks_twice_then_hands_off(root):
    d, acts, asked = _dlg(root, _view(), ask=False)
    ids = [p["id"] for p in _view()["pending"]]
    b = _buttons(d)["确认发送"]
    by_id = {int(x.winfo_name().rsplit("-", 1)[1]): x for x in b}
    by_id[ids[0]].invoke()                           # save: 高危, 二次确认说不 -> 不交
    assert len(asked) == 1 and "save" in asked[0] and acts == []
    by_id[ids[1]].invoke()                           # 普通的不再问
    assert acts == [("confirm", ids[1])] and len(asked) == 1


def test_reject_estop_serial_close(root):
    d, acts, _asked = _dlg(root, _view())
    b = _buttons(d)
    b["拒绝"][0].invoke()
    b["急停本路"][0].invoke()
    b["全部急停"][0].invoke()
    b["串口会话"][0].invoke()
    assert [a[0] for a in acts] == ["reject", "estop", "estop", "serial"]
    assert acts[1] == ("estop", 1) and acts[2] == ("estop", None)
    b["关闭"][0].invoke()
    assert not d.winfo_exists()


def test_refresh_follows_state_and_stopped_offers_resume(root):
    d, acts, _asked = _dlg(root, _view())
    d.refresh(_view(stop=True))
    d.update_idletasks()
    b = _buttons(d)
    assert "确认发送" not in b and "急停本路" not in b and "解除本路急停" in b and "解除全部急停" in b
    assert any("急停中" in t for t in _labels(d))
    b["解除本路急停"][0].invoke()
    assert acts == [("resume", 1)]
    d.refresh(None)                                  # 读不懂: 当全部急停画, 不留旧的按钮
    d.update_idletasks()
    assert "确认发送" not in _buttons(d)


def test_message_line(root):
    d, _acts, _asked = _dlg(root, _view())
    d.say("已拒绝 #2", ok=True)
    assert "已拒绝 #2" in _labels(d)


@pytest.mark.parametrize("stop", [False, True])
def test_every_text_readable(root, stop):
    """#28 深色: 确认框里所有字 (含按钮按下、高危提示、急停状态、提示行) 对底都 >= 4.5."""
    import theme as th
    th.setup(root)
    d, _acts, _asked = _dlg(root, _view(stop=stop))
    d.say("不行: 急停中", ok=False)
    d.update_idletasks()
    assert not th.unreadable(d)
