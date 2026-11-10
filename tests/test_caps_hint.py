"""#88 大写锁定提示: 打码输入框右侧的 ⇪、串口面板标题栏的 ⇪. 全用假控件, 不起真 Tk."""

import os
import types

import pytest

import serial_panel as sp
import theme as th

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = 0x2
SHIFT = 0x1


def _ev(keysym="a", state=0):
    return types.SimpleNamespace(keysym=keysym, state=state)


# ------------------------------------------------------------ 纯判断

@pytest.mark.parametrize("keysym,state,want", [
    ("a", 0, False),
    ("a", SHIFT, False),
    ("A", LOCK, True),
    ("a", LOCK | SHIFT, True),
    # 按大写锁定键这一下, 事件里的 state 还是按键之前的: 要反过来算
    ("Caps_Lock", 0, True),
    ("Caps_Lock", LOCK, False),
])
def test_caps_on(keysym, state, want):
    assert th.caps_on(state, keysym) is want


# ------------------------------------------------------------ 打码输入框

class FakeEntry:
    def __init__(self, show="*"):
        self.opts = {"show": show, "background": th.BG, "font": th.FONT_MONO}
        self.binds = {}

    def cget(self, key):
        return self.opts[key]

    def bind(self, seq, fn, add=None):
        assert add == "+", "输入框上可能已有别的绑定, 只能追加"
        self.binds[seq] = fn


class FakeLabel:
    def __init__(self, master, **kw):
        self.master, self.kw, self.placed = master, kw, False

    def place(self, **kw):
        self.placed = True

    def place_forget(self):
        self.placed = False


def _hint(show="*"):
    made = []
    entry = FakeEntry(show)
    refresh = th.caps_hint(entry, label=lambda *a, **kw: made.append(FakeLabel(*a, **kw)) or made[-1])
    return entry, made[0], refresh


def test_hint_starts_hidden():
    _e, mark, _r = _hint()
    assert mark.placed is False


def test_hint_mark_is_the_icon_on_the_entry_background():
    entry, mark, _r = _hint()
    assert mark.master is entry
    assert mark.kw["text"] == th.CAPS_MARK == "⇪"
    assert mark.kw["bg"] == entry.cget("background")
    assert mark.kw["font"] == entry.cget("font")
    assert mark.kw["fg"] == th.WARN


def test_hint_shows_with_lock_bit_and_hides_without():
    e, mark, _r = _hint()
    e.binds["<KeyPress>"](_ev("A", LOCK))
    assert mark.placed is True
    e.binds["<KeyPress>"](_ev("a", 0))
    assert mark.placed is False


def test_hint_follows_the_caps_lock_key_itself():
    e, mark, _r = _hint()
    e.binds["<KeyPress>"](_ev("Caps_Lock", 0))
    assert mark.placed is True
    e.binds["<KeyPress>"](_ev("Caps_Lock", LOCK))
    assert mark.placed is False


def test_hint_hides_on_focus_out_and_waits_for_a_key_after_focus_in():
    """FocusIn 事件没有修饰键状态, 焦点回来后等第一次按键才知道."""
    e, mark, _r = _hint()
    e.binds["<KeyPress>"](_ev("A", LOCK))
    e.binds["<FocusOut>"](_ev())
    assert mark.placed is False
    assert "<FocusIn>" not in e.binds
    e.binds["<KeyPress>"](_ev("A", LOCK))
    assert mark.placed is True


def test_hint_never_shows_on_plain_entry():
    e, mark, _r = _hint(show="")
    e.binds["<KeyPress>"](_ev("A", LOCK))
    assert mark.placed is False


def test_hint_refresh_follows_show_password_toggle():
    e, mark, refresh = _hint()
    e.binds["<KeyPress>"](_ev("A", LOCK))
    e.opts["show"] = ""                         # 勾上"显示密码"
    refresh()
    assert mark.placed is False
    e.opts["show"] = "*"                        # 取消勾选
    refresh()
    assert mark.placed is True


def test_hint_binding_lets_the_key_through():
    """不能 return "break", 不然字打不进框."""
    e, _m, _r = _hint()
    assert e.binds["<KeyPress>"](_ev("A", LOCK)) is None


@pytest.mark.parametrize("name,calls", [("kiosk.py", 1), ("ks_panel.py", 1), ("wifi_panel.py", 2)])
def test_masked_entries_are_wired(name, calls):
    """车机打码框 (simpledialog 那处先跳过, 见 #88) 都接上了; 真控件这里起不了, 静态数一下."""
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        assert f.read().count("th.caps_hint(") == calls


# ------------------------------------------------------------ 串口面板标题栏

@pytest.mark.parametrize("on,line,want", [
    (False, "Password:", ""),
    (False, "<SW>", ""),
    (True, "<SW>", th.CAPS_MARK),
    (True, "Password:", th.CAPS_MARK),
])
def test_serial_caps_view_text(on, line, want):
    assert sp.caps_view(on, line)[0] == want


@pytest.mark.parametrize("mode", th.MODES)
def test_serial_caps_view_is_louder_at_password(mode):
    th.use(mode)
    try:
        assert sp.caps_view(True, "<SW>")[1] == th.FG
        for line in ("Password:", "password: ", "Password："):
            assert sp.caps_view(True, line)[1] == th.BAD
        assert th.contrast(th.BAD, th.PANEL) >= 4.5
    finally:
        th.use("dark")


class _Console:
    def __init__(self, line=""):
        self.line, self.asked = line, None

    def get(self, a, b):
        self.asked = (a, b)
        return self.line


class _Mark:
    def __init__(self):
        self.kw = {}

    def config(self, **kw):
        self.kw.update(kw)


class _Panel:
    """不起 Tk, 只验证状态怎么落到标题栏的 ⇪ 上."""

    _note_caps = sp.SerialPanel._note_caps
    _paint_caps = sp.SerialPanel._paint_caps

    def __init__(self, line=""):
        self.console, self.caps_mark, self._caps = _Console(line), _Mark(), False


def test_panel_note_caps_paints_title():
    p = _Panel("<SW>")
    p._note_caps(_ev("A", LOCK))
    assert p.caps_mark.kw == {"text": th.CAPS_MARK, "fg": th.FG}
    p._note_caps(_ev("a", 0))
    assert p.caps_mark.kw["text"] == ""


def test_panel_reads_only_the_cursor_line():
    p = _Panel("Password:")
    p._note_caps(_ev("Caps_Lock", 0))
    assert p.console.asked == ("end-1c linestart", "end-1c")
    assert p.caps_mark.kw == {"text": th.CAPS_MARK, "fg": th.BAD}


def test_console_key_updates_caps_and_still_sends():
    sent = []
    p = types.SimpleNamespace(_note_caps=lambda ev: sent.append("caps"), _tx=lambda d: sent.append(d))
    ev = types.SimpleNamespace(keysym="A", char="A", state=LOCK)
    assert sp.SerialPanel._on_console_key(p, ev) == "break"
    assert sent == ["caps", b"A"]


def test_con_write_repaints_caps(monkeypatch):
    """设备回 Password: 时不用等下一次按键, ⇪ 自己变色."""
    painted = []

    class Text:
        def insert(self, *a):
            pass

        def index(self, _i):
            return "1.0"

        def mark_set(self, *a):
            pass

        def see(self, *a):
            pass

    p = types.SimpleNamespace(console=Text(), _paint_caps=lambda: painted.append(1))
    sp.SerialPanel._con_write(p, [("text", "Password:")])
    assert painted == [1]
