"""ime_tk 测试: 候选栏文字、中英标记、按键接线 (假输入框 / 假标签 / 同步 spawn), 不开真 Tk."""

import types

import pytest

import ime_core as ic
import ime_tk as it
from test_ime_core import FakeEngine


def _view(**kw):
    v = ic.Ime().view()
    v.update(kw)
    return v


# ------------------------------------------------------------ 纯文字

def test_bar_text_empty_when_not_composing():
    assert it.bar_text(_view()) == ""


def test_bar_text_numbers_and_page_marks():
    v = _view(mode="zh", composing=True, preedit="ni hao", candidates=["你好", "拟好"], page_no=0, last_page=False)
    assert it.bar_text(v) == "ni hao  1.你好  2.拟好  (=下页)"
    v.update(page_no=2, last_page=True)
    assert it.bar_text(v) == "ni hao  (-上页)  1.你好  2.拟好"
    v.update(candidates=[])
    assert it.bar_text(v) == "ni hao  (-上页)  (没有候选, 回车上英文)"


def test_bar_text_marks_highlighted():
    v = _view(mode="zh", composing=True, preedit="ni", candidates=["你", "尼"], highlighted=1, last_page=True)
    assert it.bar_text(v) == "ni  1.你  [2.尼]"


def test_mode_text():
    assert it.mode_text(_view()) == "英"
    assert it.mode_text(_view(mode="zh")) == "中"
    assert it.mode_text(_view(status="loading")) == "中…"
    assert it.mode_text(_view(status="unavailable")) == "英·无中文"


# ------------------------------------------------------------ 接线

class FakeEntry:
    def __init__(self, show=""):
        self.text, self.tags, self.class_binds, self.binds, self.afters = "", ("Entry", "."), {}, {}, []
        self.show, self.events = show, []

    def cget(self, key):
        assert key == "show"
        return self.show

    def event_generate(self, seq, **kw):
        self.events.append(seq)

    def bindtags(self, tags=None):
        if tags is None:
            return self.tags
        self.tags = tuple(tags)

    def bind_class(self, tag, seq, fn):
        self.class_binds[(tag, seq)] = fn

    def bind(self, seq, fn, add=None):
        self.binds[seq] = fn

    def insert(self, idx, s):
        self.text += s

    def after(self, ms, fn):
        self.afters.append(fn)

    def winfo_toplevel(self):
        return self


class FakeLabel:
    def __init__(self, master, **kw):
        self.kw, self.placed = dict(kw), False

    def config(self, **kw):
        self.kw.update(kw)

    configure = config

    def place(self, **kw):
        self.placed = True

    def place_forget(self):
        self.placed = False

    def lift(self):
        pass


def _ev(keysym, char="", state=0):
    return types.SimpleNamespace(keysym=keysym, char=char, state=state)


def _attach(opener, clock=None):
    e = FakeEntry()
    w = it.ImeEntry(e, "test.demo", opener=opener, label=FakeLabel, spawn=lambda f: f(), clock=clock or (lambda: 0.0))
    press, release = e.class_binds[(w.tag, "<KeyPress>")], e.class_binds[(w.tag, "<KeyRelease>")]
    return e, w, press, release


def _shift(e, w, press, release):
    press(_ev("Shift_L"))
    release(_ev("Shift_L"))
    while e.afters:                     # 跑掉后台加载的轮询
        e.afters.pop(0)()


def test_attach_puts_tag_first_and_does_not_open_engine():
    calls = []
    e, w, _p, _r = _attach(lambda: calls.append(1) or (FakeEngine(), ""))
    assert e.tags[0] == w.tag and e.tags[1:] == ("Entry", ".")      # 先于输入框自己的绑定 (如 <Return>)
    assert calls == [] and w.mode.kw["text"] == "英" and not w.bar.placed


def test_english_keys_pass_to_entry():
    e, w, press, _r = _attach(lambda: (FakeEngine(), ""))
    assert press(_ev("a", "a")) is None and press(_ev("Return", "\r")) is None


def test_shift_opens_engine_then_types_chinese():
    e, w, press, release = _attach(lambda: (FakeEngine(), ""))
    _shift(e, w, press, release)
    assert w.ime.zh and w.mode.kw["text"] == "中"
    for ch in "nihao":
        assert press(_ev(ch, ch)) == "break"
    assert w.bar.placed and "1.你好" in w.bar.kw["text"]
    assert press(_ev("Return", "\r")) == "break" and e.text == "nihao"     # 回车上英文, 没漏给 <Return> 绑定
    for ch in "ni":
        press(_ev(ch, ch))
    assert press(_ev("2", "2")) == "break" and e.text == "nihao尼" and not w.bar.placed


def test_unavailable_shows_reason_and_stays_english():
    e, w, press, release = _attach(lambda: (None, "中文输入不可用: 找不到 librime"))
    _shift(e, w, press, release)
    assert not w.ime.zh and w.mode.kw["text"] == "英·无中文"
    assert w.bar.placed and "找不到 librime" in w.bar.kw["text"]
    assert press(_ev("n", "n")) is None and e.text == ""                  # 照常打英文


def test_opener_exception_is_unavailable():
    def boom():
        raise RuntimeError("坏了")
    e, w, press, release = _attach(boom)
    _shift(e, w, press, release)
    assert w.ime.status == "unavailable" and "坏了" in w.ime.reason


def test_destroy_closes_engine():
    eng = FakeEngine()
    e, w, press, release = _attach(lambda: (eng, ""))
    _shift(e, w, press, release)
    e.binds["<Destroy>"](_ev("Destroy"))
    assert eng.closed


def test_attach_never_raises():
    class Bad:
        def bindtags(self, *a):
            raise RuntimeError("不是输入框")
    assert it.attach(Bad(), purpose="x.y") is None


# (文件, 用途) 白名单: 候选上屏等于替人打字, 只接普通文字输入框. 新接一处要改这里, 逼着看一眼是不是 PIN / 确认码 / 命令框
ALLOWED = {("wol_panel.py", "wol.name")}
_IME_FILES = {"ime_tk.py", "ime_core.py", "ime_proc.py", "ime_rime.py", "ime_worker.py"}


def _repo_py():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("tests", "__pycache__")]
        for f in files:
            if f.endswith(".py") and f not in _IME_FILES:
                p = os.path.join(d, f)
                yield os.path.relpath(p, root), open(p, encoding="utf-8").read()


def test_only_whitelisted_entries_get_ime():
    import re
    users, pairs = set(), set()
    for rel, src in _repo_py():
        if re.search(r"\bimport ime_tk\b|\bfrom ime_tk\b|\bime_tk\.|\bImeEntry\b|(?:import_module|__import__)\(\s*['\"]ime_", src):
            users.add(rel)
            for m in re.finditer(r"attach\(", src):
                call = src[m.start():m.start() + 200]
                pm = re.search(r"purpose=\"([\w.]+)\"", call)
                assert pm, (rel, call)                       # attach 必须带字面的 purpose=
                pairs.add((rel, pm.group(1)))
    assert users == {f for f, _p in ALLOWED}
    assert pairs == ALLOWED


def test_attach_requires_purpose():
    with pytest.raises(TypeError):
        it.attach(FakeEntry())                                  # noqa  (没有用途名)
    assert it.attach(FakeEntry(), purpose="") is None


def test_password_entry_refused():
    assert it.attach(FakeEntry(show="*"), purpose="x.y", opener=lambda: (FakeEngine(), ""), label=FakeLabel) is None
    assert it.attach(FakeEntry(), purpose="x.y", opener=lambda: (FakeEngine(), ""), label=FakeLabel) is not None


def test_consumed_keys_still_count_as_input():
    """拼音期间返回 break, kiosk 的 bind_all 收不到 -> 补发虚拟事件, 不然连续拼音会被判成空闲锁屏."""
    e, w, press, release = _attach(lambda: (FakeEngine(), ""))
    _shift(e, w, press, release)
    press(_ev("n", "n"))
    assert e.events == [it.INPUT_EVENT]
    press(_ev("Escape", "\x1b"))
    assert e.events == [it.INPUT_EVENT] * 2
    press(_ev("a", "a", 0x4))                                     # 没在拼 + Ctrl: 不吃, 不补发
    assert len(e.events) == 2


def test_late_engine_keeps_english():
    """加载慢: 切中文那一下之后过了几秒 / 已经接着打了英文, 引擎到了也不自动切中文."""
    t = {"v": 0.0}
    holder = {}
    e = FakeEntry()
    w = it.ImeEntry(e, "test.demo", opener=lambda: (FakeEngine(), ""), label=FakeLabel,
                    spawn=lambda f: holder.setdefault("f", f), clock=lambda: t["v"])
    press, release = e.class_binds[(w.tag, "<KeyPress>")], e.class_binds[(w.tag, "<KeyRelease>")]
    press(_ev("Shift_L"))
    release(_ev("Shift_L"))
    t["v"] = 10.0
    holder["f"]()
    while e.afters:
        e.afters.pop(0)()
    assert w.ime.status == "ready" and not w.ime.zh and w.mode.kw["text"] == "英"
    # 刚切就到了, 但中间打过字: 也不切
    w2 = it.ImeEntry(FakeEntry(), "test.demo", opener=lambda: (FakeEngine(), ""), label=FakeLabel,
                     spawn=lambda f: holder.__setitem__("g", f), clock=lambda: t["v"])
    e2 = w2.entry
    p2, r2 = e2.class_binds[(w2.tag, "<KeyPress>")], e2.class_binds[(w2.tag, "<KeyRelease>")]
    p2(_ev("Shift_L"))
    r2(_ev("Shift_L"))
    p2(_ev("x", "x"))
    holder["g"]()
    while e2.afters:
        e2.afters.pop(0)()
    assert not w2.ime.zh


def test_close_before_engine_arrives_closes_it():
    eng = FakeEngine()
    holder = {}
    e = FakeEntry()
    w = it.ImeEntry(e, "test.demo", opener=lambda: (eng, ""), label=FakeLabel,
                    spawn=lambda f: holder.setdefault("f", f), clock=lambda: 0.0)
    press, release = e.class_binds[(w.tag, "<KeyPress>")], e.class_binds[(w.tag, "<KeyRelease>")]
    press(_ev("Shift_L"))
    release(_ev("Shift_L"))
    w.close()                                                     # 输入框没了
    holder["f"]()
    while e.afters:
        e.afters.pop(0)()
    assert eng.closed and w.ime.engine is None


def test_kiosk_counts_ime_input_as_activity():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "kiosk.py"), encoding="utf-8").read()
    assert it.INPUT_EVENT in src and "_note_input" in src
