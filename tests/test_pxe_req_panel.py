"""pxe_req_panel Tk 冒烟测试 (#24): 真起 Tk (没显示就跳过), 按按钮看交出去的动作; 不碰状态文件和 tftp."""

import pytest

tk = pytest.importorskip("tkinter")

import pxe_req_core as pr  # noqa: E402
import pxe_req_panel as pp  # noqa: E402

T0 = 1_700_000_000.0
MAC = "52:54:00:aa:bb:01"


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    r.withdraw()
    yield r
    r.destroy()


def _view(codes=("111111",), n=1):
    it = iter(codes)
    b = pr.Book.from_text(None, T0, code=lambda: next(it))
    for i in range(n):
        mac = f"52:54:00:aa:bb:0{i + 1}"
        b.submit("stdio:ops@x", "rocky", mac, False, pr.simple_plan("rocky", mac), T0, stopped=False)
    return b.view(T0 + 5, secret=True)


def _walk(w, cls):
    out, stack = [], [w]
    while stack:
        x = stack.pop()
        stack.extend(x.winfo_children())
        if isinstance(x, cls):
            out.append(x)
    return out


def _buttons(w):
    out = {}
    for b in _walk(w, tk.Button):
        out.setdefault(b.cget("text"), []).append(b)
    return out


def _labels(w):
    return [x.cget("text") for x in _walk(w, tk.Label)]


def _dlg(root, view, manual=True):
    acts = []
    d = pp.PxeRequestDialog(root, view, lambda op, arg: acts.append((op, arg)),
                            on_manual=(lambda: acts.append(("manual", None))) if manual else None)
    d.update_idletasks()
    return d, acts


def test_shows_request_and_code(root):
    v = _view(codes=("111111", "333333"), n=2)
    d, _acts = _dlg(root, v)
    text = "\n".join(_labels(d))
    assert "Agent 申请 PXE 解锁 #1" in text and "stdio:ops@x" in text and "1 1 1 1 1 1" in text
    assert "清空整块硬盘" in text and MAC in text and "还有 1 条" in text
    assert {"确认解锁", "拒绝", "手动解锁…", "关闭"} <= set(_buttons(d))


def test_confirm_hands_off_typed_code_and_shown_digest(root):
    v = _view()
    d, acts = _dlg(root, v)
    entry = _walk(d, tk.Entry)[0]
    entry.insert(0, "11 11 11")
    _buttons(d)["确认解锁"][0].invoke()
    assert acts == [("confirm", (1, "11 11 11", v["pending"][0]["digest"]))]
    assert entry.get() == ""                         # 交出去就清空, 不留在框里


def test_reject_manual_close(root):
    d, acts = _dlg(root, _view())
    b = _buttons(d)
    b["拒绝"][0].invoke()
    b["手动解锁…"][0].invoke()
    assert acts == [("reject", 1), ("manual", None)]
    b["关闭"][0].invoke()
    assert not d.winfo_exists()


def test_refresh_new_code_rebuilds_and_empty_has_no_confirm(root):
    d, acts = _dlg(root, _view(), manual=False)
    d.refresh(_view(codes=("222222",)))
    d.update_idletasks()
    assert "2 2 2 2 2 2" in _labels(d) and "1 1 1 1 1 1" not in _labels(d)
    assert "手动解锁…" not in _buttons(d)
    d.refresh({"pending": [], "history": []})
    d.update_idletasks()
    assert "确认解锁" not in _buttons(d) and any("没有在等" in t for t in _labels(d))
    d.refresh(None)                                  # 读不懂: 当没有
    d.update_idletasks()
    assert "确认解锁" not in _buttons(d)


def test_message_line(root):
    d, _acts = _dlg(root, _view())
    d.say("确认码不对", ok=False)
    assert "确认码不对" in _labels(d)
