"""ime_core 测试: 按键 -> 拼音缓冲 -> 候选 -> 上屏 的状态机. 引擎用假的, 不碰 librime / Tk."""

import pytest

import ime_core as ic

SHIFT, CTRL, ALT = 0x1, 0x4, 0x8

_DICT = {"ni": ["你", "尼", "泥"], "hao": ["好", "号"], "nihao": ["你好", "拟好"],
         "a": ["啊", "阿", "吖", "嗄", "腌", "锕", "安", "爱", "按", "暗", "案"]}


class FakeEngine:
    """够状态机用的假引擎: 整串查表, 每页 5 个, 选词就整串上屏."""

    PAGE = 5

    def __init__(self):
        self.raw, self.page_no, self.commits, self.closed = "", 0, [], False
        self.calls = []

    def _all(self):
        return _DICT.get(self.raw.replace("'", ""), [])

    def feed(self, ch):
        self.calls.append(("feed", ch))
        self.raw += ch
        self.page_no = 0
        return True

    def backspace(self):
        self.calls.append(("backspace",))
        self.raw = self.raw[:-1]
        self.page_no = 0

    def select(self, i):
        self.calls.append(("select", i))
        page = self._all()[self.page_no * self.PAGE:(self.page_no + 1) * self.PAGE]
        if 0 <= i < len(page):
            self.commits.append(page[i])
            self.raw, self.page_no = "", 0
            return True
        return False

    def page(self, backward):
        self.calls.append(("page", backward))
        n = len(self._all())
        if backward and self.page_no > 0:
            self.page_no -= 1
        elif not backward and (self.page_no + 1) * self.PAGE < n:
            self.page_no += 1

    def clear(self):
        self.calls.append(("clear",))
        self.raw, self.page_no = "", 0

    def snapshot(self):
        allc = self._all()
        page = allc[self.page_no * self.PAGE:(self.page_no + 1) * self.PAGE]
        return ic.Snapshot(raw=self.raw, preedit=self.raw, candidates=page, page_no=self.page_no,
                           last_page=(self.page_no + 1) * self.PAGE >= len(allc), highlighted=0)

    def take_commit(self):
        return self.commits.pop(0) if self.commits else None

    def close(self):
        self.closed = True


def _zh():
    ime = ic.Ime()
    ime.set_engine(FakeEngine(), zh=True)
    return ime


def _type(ime, text):
    out = []
    for ch in text:
        r = ime.press(ch if ch.isalnum() else {" ": "space", "'": "apostrophe"}.get(ch, ch), ch, 0)
        out.append(r)
    return out


# ------------------------------------------------------------ 英文模式 / 没有引擎: 一律放过

def test_default_is_english_and_passes_everything():
    ime = ic.Ime()
    assert not ime.zh and ime.status == "off"
    for ks, ch in (("a", "a"), ("Return", "\r"), ("space", " "), ("1", "1"), ("BackSpace", "\b")):
        r = ime.press(ks, ch, 0)
        assert not r.consume and r.commit is None


def test_english_mode_with_engine_passes():
    ime = _zh()
    ime.zh = False
    assert not ime.press("n", "n", 0).consume
    assert ime.engine.calls == []


# ------------------------------------------------------------ 中文: 拼音 -> 候选 -> 上屏

def test_letters_build_buffer_and_candidates():
    ime = _zh()
    rs = _type(ime, "nihao")
    assert all(r.consume and r.commit is None for r in rs)
    v = ime.view()
    assert v["composing"] and v["preedit"] == "nihao" and v["candidates"] == ["你好", "拟好"]


def test_space_commits_highlighted():
    ime = _zh()
    _type(ime, "nihao")
    r = ime.press("space", " ", 0)
    assert r.consume and r.commit == "你好" and not ime.view()["composing"]


@pytest.mark.parametrize("key,want", [("1", "你"), ("2", "尼"), ("3", "泥")])
def test_digit_selects(key, want):
    ime = _zh()
    _type(ime, "ni")
    r = ime.press(key, key, 0)
    assert r.consume and r.commit == want


def test_digit_out_of_range_is_eaten_without_commit():
    ime = _zh()
    _type(ime, "ni")
    r = ime.press("9", "9", 0)
    assert r.consume and r.commit is None and ime.view()["composing"]
    r = ime.press("0", "0", 0)
    assert r.consume and r.commit is None


def test_return_commits_raw_letters_as_english():
    ime = _zh()
    _type(ime, "ni'hao")
    r = ime.press("Return", "\r", 0)
    assert r.consume and r.commit == "ni'hao" and not ime.view()["composing"]


def test_escape_cancels():
    ime = _zh()
    _type(ime, "nihao")
    r = ime.press("Escape", "\x1b", 0)
    assert r.consume and r.commit is None and not ime.view()["composing"]


def test_backspace_edits_then_passes_when_empty():
    ime = _zh()
    _type(ime, "ni")
    assert ime.press("BackSpace", "\b", 0).consume
    assert ime.view()["preedit"] == "n"
    assert ime.press("BackSpace", "\b", 0).consume
    assert not ime.view()["composing"]
    assert not ime.press("BackSpace", "\b", 0).consume            # 没在拼: 交给输入框删字


@pytest.mark.parametrize("ks,backward", [("Page_Down", False), ("equal", False), ("Next", False),
                                         ("Page_Up", True), ("minus", True), ("Prior", True)])
def test_paging_keys(ks, backward):
    ime = _zh()
    _type(ime, "a")
    if backward:
        ime.press("Page_Down", "", 0)
    r = ime.press(ks, "", 0)
    assert r.consume and ime.engine.calls[-1] == ("page", backward)


def test_paging_and_select_on_second_page():
    ime = _zh()
    _type(ime, "a")
    ime.press("Page_Down", "", 0)
    v = ime.view()
    assert v["page_no"] == 1 and v["candidates"][0] == "锕" and v["last_page"] is False
    assert ime.press("2", "2", 0).commit == "安"


def test_not_composing_non_letters_pass():
    ime = _zh()
    for ks, ch in (("1", "1"), ("space", " "), ("Return", "\r"), ("comma", ","), ("A", "A"),
                   ("Escape", "\x1b"), ("apostrophe", "'")):
        assert not ime.press(ks, ch, SHIFT if ch == "A" else 0).consume, ks
    assert ime.engine.calls == []


def test_composing_other_keys_are_eaten():
    ime = _zh()
    _type(ime, "ni")
    for ks, ch in (("comma", ","), ("Left", ""), ("Up", ""), ("Tab", "\t"), ("A", "A")):
        r = ime.press(ks, ch, 0)
        assert r.consume and r.commit is None, ks
    assert ime.view()["preedit"] == "ni"


def test_ctrl_alt_pass_when_idle_eaten_when_composing():
    ime = _zh()
    assert not ime.press("a", "\x01", CTRL).consume
    assert not ime.press("a", "a", ALT).consume
    _type(ime, "ni")
    assert ime.press("c", "\x03", CTRL).consume and ime.view()["preedit"] == "ni"


# ------------------------------------------------------------ Shift 切中英

def test_shift_alone_toggles():
    ime = _zh()
    assert not ime.press("Shift_L", "", 0).consume
    r = ime.release("Shift_L")
    assert r.toggled and not ime.zh
    ime.press("Shift_R", "", 0)
    assert ime.release("Shift_R").toggled and ime.zh


def test_shift_with_other_key_does_not_toggle():
    ime = _zh()
    ime.press("Shift_L", "", 0)
    ime.press("A", "A", SHIFT)
    assert not ime.release("Shift_L").toggled and ime.zh
    assert not ime.release("a").toggled


def test_toggle_to_english_while_composing_clears():
    ime = _zh()
    _type(ime, "ni")
    ime.press("Shift_L", "", 0)
    ime.release("Shift_L")
    assert not ime.zh and not ime.view()["composing"] and ("clear",) in ime.engine.calls


# ------------------------------------------------------------ 引擎生命周期 / 降级

def test_first_toggle_without_engine_asks_for_one():
    ime = ic.Ime()
    ime.press("Shift_L", "", 0)
    r = ime.release("Shift_L")
    assert r.need_engine and not ime.zh and ime.status == "loading"
    ime.press("Shift_L", "", 0)                       # 还在加载: 再按不重复要
    assert not ime.release("Shift_L").need_engine
    ime.set_engine(FakeEngine(), zh=True)
    assert ime.zh and ime.status == "ready"


def test_unavailable_stays_english_and_reports():
    ime = ic.Ime()
    ime.press("Shift_L", "", 0)
    ime.release("Shift_L")
    ime.set_unavailable("找不到 librime")
    assert not ime.zh and ime.status == "unavailable" and ime.reason == "找不到 librime"
    ime.press("Shift_L", "", 0)
    r = ime.release("Shift_L")
    assert not r.need_engine and not ime.zh                 # 不可用就一直英文, 不反复去加载
    assert not ime.press("n", "n", 0).consume


def test_engine_errors_degrade_to_unavailable():
    class Broken(FakeEngine):
        def feed(self, ch):
            raise OSError("段错误前的最后一声")
    ime = ic.Ime()
    ime.set_engine(Broken(), zh=True)
    r = ime.press("n", "n", 0)
    assert not r.consume and ime.status == "unavailable" and not ime.zh and "最后一声" in ime.reason


def test_close_closes_engine():
    ime = _zh()
    eng = ime.engine
    ime.close()
    assert eng.closed and ime.engine is None


def test_view_english():
    v = ic.Ime().view()
    assert v["mode"] == "en" and not v["composing"] and v["candidates"] == []


# ------------------------------------------------------------ #72 审查: 吞键 / 默认不自动切 / 崩了能重开

class Dead:
    """方案没加载: process_key 不认, 不进拼音, 也不上屏."""

    def __init__(self, handled=False):
        self.handled, self.closed = handled, False

    def feed(self, c):
        return self.handled

    def snapshot(self):
        return ic.Snapshot("", "", [], 0, True, 0)

    def take_commit(self):
        return None

    def clear(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("handled", [False, True])
def test_dead_engine_does_not_swallow_letters(handled):
    ime = ic.Ime()
    eng = Dead(handled)
    ime.set_engine(eng, zh=True)
    r = ime.press("a", "a", 0)
    assert not r.consume and ime.status == "unavailable" and not ime.zh and eng.closed
    assert "方案" in ime.reason
    assert not ime.press("b", "b", 0).consume


def test_set_engine_keeps_mode_by_default():
    ime = ic.Ime()
    ime.set_engine(FakeEngine())
    assert not ime.zh and ime.status == "ready"


def test_crash_allows_limited_reopen():
    class Crash(FakeEngine):
        def feed(self, ch):
            raise OSError("子进程退出了")
    ime = ic.Ime()
    for n in range(ic.MAX_REOPEN):
        ime.set_engine(Crash(), zh=True)
        assert not ime.press("n", "n", 0).consume and ime.status == "unavailable"
        ime.press("Shift_L", "", 0)
        assert ime.release("Shift_L").need_engine, n          # 运行时崩了: 下次 Shift 再开
    ime.set_engine(Crash(), zh=True)
    ime.press("n", "n", 0)
    ime.press("Shift_L", "", 0)
    assert not ime.release("Shift_L").need_engine             # 次数用完: 一直英文


def test_open_failure_does_not_reopen():
    ime = ic.Ime()
    ime.press("Shift_L", "", 0)
    ime.release("Shift_L")
    ime.set_unavailable("没有词库")
    ime.press("Shift_L", "", 0)
    assert not ime.release("Shift_L").need_engine


# ------------------------------------------------------------ #72 第二轮: 拼音中引擎出错, 键不漏进输入框

class FailMid(FakeEngine):
    """拼了几个字母之后坏掉."""

    def __init__(self, broken_op):
        super().__init__()
        self.broken_op = broken_op

    def _boom(self, op):
        if op == self.broken_op:
            raise OSError("子进程退出了")

    def feed(self, ch):
        if self.raw:
            self._boom("feed")
        return super().feed(ch)

    def clear(self):
        self._boom("clear")
        super().clear()

    def backspace(self):
        self._boom("backspace")
        super().backspace()


@pytest.mark.parametrize("key", ["Return", "KP_Enter"])
def test_enter_while_composing_and_engine_fails_commits_raw(key):
    ime = ic.Ime()
    ime.set_engine(FailMid("clear"), zh=True)
    for ch in "ni":
        ime.press(ch, ch, 0)
    r = ime.press(key, "\r", 0)
    assert r.consume and r.commit == "ni"                  # 回车绝不漏给输入框 (会触发它的 <Return> 动作)
    assert ime.status == "unavailable" and not ime.zh


@pytest.mark.parametrize("op,key,char", [("feed", "h", "h"), ("backspace", "BackSpace", "\b")])
def test_other_keys_while_composing_and_engine_fails_are_consumed(op, key, char):
    ime = ic.Ime()
    ime.set_engine(FailMid(op), zh=True)
    ime.press("n", "n", 0)
    r = ime.press(key, char, 0)
    assert r.consume and r.commit is None and ime.status == "unavailable"


# ------------------------------------------------------------ 闲着时引擎死了 (#100)

class _Mortal(FakeEngine):
    alive_now = True

    def alive(self):
        return self.alive_now


def test_check_engine_alive_changes_nothing():
    ime = ic.Ime()
    ime.set_engine(_Mortal(), zh=True)
    assert ime.check_engine() is False and ime.zh and ime.status == "ready"


def test_check_engine_without_probe_or_engine_is_noop():
    ime = ic.Ime()
    assert ime.check_engine() is False and ime.status == "off"
    ime.set_engine(FakeEngine(), zh=True)
    assert ime.check_engine() is False and ime.zh


def test_check_engine_dead_falls_back_and_can_reopen():
    eng = _Mortal()
    ime = ic.Ime()
    ime.set_engine(eng, zh=True)
    eng.alive_now = False
    assert ime.check_engine() is True
    assert ime.status == "unavailable" and not ime.zh and ime.engine is None and eng.closed
    assert ime.check_engine() is False                       # 只报一次
    ime.press("Shift_L", "", 0)
    assert ime.release("Shift_L").need_engine                # 运行中死的, 下次 Shift 可以重开
