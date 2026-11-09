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

    def unbind_class(self, tag, seq):
        self.class_binds.pop((tag, seq), None)

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

    def destroy(self):
        self.destroyed = True


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
ALLOWED = {("wol_panel.py", "wol.name"), ("bigfish_panel.py", "bigfish.task")}
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


_MANIFESTS = {"scripts/accept_core.py"}       # 验收清单里列了模块名 "ime_tk"


def ime_refs(src):
    """-> (是否引用了 ime_tk, [(用途 或 None, 写法)], [违规写法]). 只认 `import ime_tk` + `ime_tk.attach(..., purpose="...")`;
    别名、from-import、动态导入 (含字符串拼接) 一律算违规, 白名单内的文件也不行. 这是提醒, 不是安全边界."""
    import re
    bad = []
    if re.search(r"\bimport\s+ime_tk\s+as\b", src):
        bad.append("import ime_tk as")
    if re.search(r"\bfrom\s+ime_tk\s+import\b", src):
        bad.append("from ime_tk import")
    lits = re.findall(r"[\'\"]([^\'\"\n]*)[\'\"]", src)
    head = any(x.startswith(("ime", "Ime")) or x in ("i", "im") for x in lits)
    tail = any("_tk" in x or x in ("tk", "e_tk") or "ImeEntry" in x for x in lits)
    if re.search(r"import_module|__import__|importlib|\bexec\(|\beval\(", src) and (head or tail):
        bad.append("动态导入 ime*")
    elif head and tail or any("ImeEntry" in x or "ime_tk" in x for x in lits):
        bad.append("字符串拼出 ime_tk")
    if re.search(r"\bImeEntry\b", src):
        bad.append("直接用 ImeEntry")
    used = bool(bad) or bool(re.search(r"\bime_tk\b", src))
    calls = []
    for m in re.finditer(r"\bime_tk\.(\w+)\(", src):
        if m.group(1) != "attach":
            bad.append(f"ime_tk.{m.group(1)}")
            continue
        pm = re.search(r"purpose=\"([\w.]+)\"", src[m.start():m.start() + 200])
        calls.append(pm.group(1) if pm else None)
    if re.search(r"(?<!ime_tk\.)\battach\(", src) and used:
        bad.append("attach 不经 ime_tk.")
    return used, calls, bad


def test_only_whitelisted_entries_get_ime():
    from collections import Counter
    users, pairs = set(), Counter()
    import re
    for rel, src in _repo_py():
        if rel in _MANIFESTS:       # 只是列模块名 (验收清单), 不许真用
            assert not re.search(r"\battach\(|^\s*(?:import|from)\s+ime_tk\b", src, re.M), rel
            continue
        used, calls, bad = ime_refs(src)
        assert not bad, (rel, bad)
        if used:
            users.add(rel)
            for purpose in calls:
                assert purpose, rel                          # attach 必须带字面的 purpose=
                pairs[(rel, purpose)] += 1
    assert users == {f for f, _p in ALLOWED}
    assert pairs == Counter(ALLOWED)                         # 每个 (文件, 用途) 恰好一次


@pytest.mark.parametrize("src", [
    "import ime_tk as k\nk.attach(e, purpose='x.y')\n",
    "from ime_tk import attach as a\na(e, purpose=\"x.y\")\n",
    "from ime_tk import ImeEntry\n",
    "import importlib\nm = importlib.import_module('ime' + '_tk')\n",
    "m = __import__(\"im\" \"e_tk\")\n",
    "import importlib\nm = importlib.import_module(\"ime_\" + \"tk\")\n",
    "name = 'ime' + '_tk'\n",
    "x = getattr(mod, 'ImeEntry')  # 'ime'\n",
    "import ime_tk\nime_tk.ImeEntry(e, 'x.y')\n",
    "import ime_tk\nf = ime_tk.attach\nf(e, purpose=\"x.y\")\n",
])
def test_ime_refs_catches_indirect_forms(src):
    used, calls, bad = ime_refs(src)
    assert bad or (used and None in calls) or (used and not calls), src


def test_ime_refs_same_purpose_twice_is_counted_twice():
    src = 'import ime_tk\nime_tk.attach(a, purpose="wol.name")\nime_tk.attach(b, purpose="wol.name")\n'
    used, calls, bad = ime_refs(src)
    from collections import Counter
    assert used and not bad and Counter(("wol_panel.py", p) for p in calls) != Counter(ALLOWED)


def test_no_ime_marked_entry_refused():
    kw = dict(opener=lambda: (FakeEngine(), ""), label=FakeLabel, spawn=lambda f: None)
    assert it.attach(FakeEntry(), purpose="x.y", **kw) is not None
    e = FakeEntry()
    e.no_ime = True
    assert it.attach(e, purpose="x.y", **kw) is None
    with pytest.raises(ValueError):
        it.ImeEntry(e, "x.y", **kw)


# kiosk.py 太大不好实例化, 还按源码数; 面板都实例化, 逐个看输入框
NO_IME_SRC = {"kiosk.py": 3}


def test_kiosk_safety_entries_are_marked_no_ime():
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, n in NO_IME_SRC.items():
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert len(re.findall(r"\.no_ime = True", src)) == n, name


@pytest.fixture
def tkroot(monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    r.withdraw()
    import wifi_panel
    monkeypatch.setattr(wifi_panel, "_run", lambda *a, **k: (1, "", "测试里不跑命令"))
    yield r
    r.destroy()


def _entries(w):
    import tkinter as tk
    out, stack = [], [w]
    while stack:
        x = stack.pop()
        stack.extend(x.winfo_children())
        if isinstance(x, tk.Entry):
            out.append(x)
    return out


def _panels():
    import ks_panel
    import netmode_panel
    import serial_panel
    import wifi_panel
    import wol_panel
    return {"wol": wol_panel.WolPanel, "ks": ks_panel.KsPanel, "wifi": wifi_panel.WifiPanel,
            "netmode": netmode_panel.NetModePanel, "serial": serial_panel.SerialPanel}


@pytest.mark.parametrize("name", ["wol", "ks", "wifi", "netmode", "serial"])
def test_panel_entries_are_password_ime_or_no_ime(tkroot, name):
    """面板里每个输入框: 密码框 (show=)、接了中文的那一个 (白名单), 其余必须标 no_ime; 接了中文的不许标."""
    panel = _panels()[name](tkroot)
    attached = getattr(getattr(panel, "name_ime", None), "entry", None)
    entries = _entries(panel)
    assert entries
    for e in entries:
        if e is attached:
            assert not getattr(e, "no_ime", False)
        elif not e.cget("show"):
            assert getattr(e, "no_ime", False) is True, (name, e.cget("textvariable"))
    if name == "wol":
        assert attached is not None and attached in entries


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


# ------------------------------------------------------------ #72 第三轮

def test_consumed_key_returns_break_even_if_insert_fails():
    e, w, press, release = _attach(lambda: (FakeEngine(), ""))
    _shift(e, w, press, release)
    for ch in "ni":
        press(_ev(ch, ch))

    def boom(idx, s):
        raise RuntimeError("Tcl 不收")
    e.insert = boom
    assert press(_ev("Return", "\r")) == "break"           # 回车绝不漏给输入框


def test_close_destroys_labels_and_unbinds():
    e, w, _p, _r = _attach(lambda: (FakeEngine(), ""))
    w.close()
    w.close()
    assert w.bar.destroyed and w.mode.destroyed
    assert (w.tag, "<KeyPress>") not in e.class_binds and (w.tag, "<KeyRelease>") not in e.class_binds


def test_reopening_panel_does_not_accumulate(tkroot):
    import tkinter as tk
    before = len(tkroot.winfo_children())
    tags = []
    for _ in range(5):
        f = tk.Frame(tkroot)
        ent = tk.Entry(f)
        w = it.attach(ent, purpose="test.demo", opener=lambda: (FakeEngine(), ""), spawn=lambda fn: None)
        assert w is not None
        tags.append(w.tag)
        f.destroy()                                         # 关面板 -> <Destroy> -> close
    tkroot.update_idletasks()
    assert len(tkroot.winfo_children()) == before
    assert all(tkroot.bind_class(t) == "" or tkroot.bind_class(t) == () for t in tags)


def test_close_after_engine_arrived_before_poll_closes_it_at_once():
    """引擎到了 (_result 写好) 但 _poll 还没跑, 这时关: close 当场关掉, 不靠之后的 _poll."""
    eng = FakeEngine()
    e = FakeEntry()
    w = it.ImeEntry(e, "test.demo", opener=lambda: (eng, ""), label=FakeLabel, spawn=lambda f: f(),
                    clock=lambda: 0.0)
    press, release = e.class_binds[(w.tag, "<KeyPress>")], e.class_binds[(w.tag, "<KeyRelease>")]
    press(_ev("Shift_L"))
    release(_ev("Shift_L"))                                 # _open 同步跑完, _poll 还在 afters 里
    w.close()
    assert eng.closed
    while e.afters:
        e.afters.pop(0)()
    assert w.ime.engine is None


@pytest.mark.parametrize("order", ["close_first", "result_first"])
def test_engine_closed_exactly_once(order):
    closes = []

    class E(FakeEngine):
        def close(self):
            closes.append(1)
    eng = E()
    holder = {}
    e = FakeEntry()
    w = it.ImeEntry(e, "test.demo", opener=lambda: (eng, ""), label=FakeLabel,
                    spawn=lambda f: holder.setdefault("f", f), clock=lambda: 0.0)
    press, release = e.class_binds[(w.tag, "<KeyPress>")], e.class_binds[(w.tag, "<KeyRelease>")]
    press(_ev("Shift_L"))
    release(_ev("Shift_L"))
    if order == "close_first":
        w.close()
        holder["f"]()
    else:
        holder["f"]()
        w.close()
    while e.afters:
        e.afters.pop(0)()
    assert closes == [1]
