#!/usr/bin/env python3
"""车机中文输入的状态机 (#54): 按键 -> 拼音缓冲 -> 候选 -> 上屏. 纯逻辑, 不碰 Tk, 不碰 librime.

引擎 (ime_rime.RimeEngine, 测试里是假的) 管拼音切分和查词; 这里只管哪些键交给引擎、哪些放回输入框:
  - 默认英文, 单按一下 Shift (按下到松开中间没有别的键) 切中 / 英. 第一次切中文才去要引擎 (need_engine),
    加载中、不可用都停在英文, 原输入框照常打 ASCII.
  - 中文且没在拼: 小写字母开始拼音, 其余键 (数字、标点、大写、回车、退格、Ctrl/Alt 组合) 原样放过.
  - 正在拼: 字母 / ' 继续拼, 退格删拼音, 空格上高亮的候选 (默认第一个), 1-9 选本页第几个,
    回车把敲的字母原样上屏 (上英文), Esc 取消, - = PageUp PageDown 翻页, 其余键吃掉 (不漏进输入框).
引擎任何一步抛异常: 记下原因, 退回英文且不再用它 (status=unavailable), 不让输入框跟着坏.
"""

from typing import List, NamedTuple, Optional

SHIFT, CONTROL, ALT = 0x1, 0x4, 0x8             # Tk event.state 的位 (X11: Mod1 = Alt)
_SHIFTS = ("Shift_L", "Shift_R")
_PAGE = {"Page_Up": True, "Prior": True, "minus": True, "Page_Down": False, "Next": False, "equal": False}


class Snapshot(NamedTuple):
    raw: str                    # 敲进去的字母原样 (回车上屏用)
    preedit: str                # 给人看的拼音 (引擎可能加了空格分词)
    candidates: List[str]       # 本页候选
    page_no: int
    last_page: bool
    highlighted: int


class Result(NamedTuple):
    consume: bool = False                   # True = 输入框别再处理这个键
    commit: Optional[str] = None            # 要插进输入框的字
    toggled: bool = False                   # 中英切了
    need_engine: bool = False               # 调用方该去 (后台) 打开引擎, 打开后 set_engine / set_unavailable


_EMPTY = Snapshot("", "", [], 0, True, 0)


class Ime:
    def __init__(self, engine=None):
        self.engine = engine
        self.zh = False
        self.status = "ready" if engine else "off"       # off / loading / ready / unavailable
        self.reason = ""
        self._shift_alone = False

    # ---------------------------------------------------------------- 引擎

    def set_engine(self, engine):
        self.engine, self.status, self.reason, self.zh = engine, "ready", "", True

    def set_unavailable(self, reason):
        self._drop(reason)

    def _drop(self, reason):
        eng, self.engine = self.engine, None
        self.status, self.reason, self.zh = "unavailable", str(reason) or "中文输入出错", False
        if eng is not None:
            try:
                eng.close()
            except Exception:
                pass

    def close(self):
        eng, self.engine = self.engine, None
        if eng is not None:
            eng.close()

    def _snap(self):
        return self.engine.snapshot() if self.engine else _EMPTY

    # ---------------------------------------------------------------- 按键

    def press(self, keysym, char, state):
        if keysym in _SHIFTS:
            self._shift_alone = True
            return Result()
        self._shift_alone = False
        if not self.zh or self.engine is None:
            return Result()
        try:
            return self._press(keysym, char or "", state)
        except Exception as e:                       # 引擎坏了: 退回英文, 这个键交还输入框
            self._drop(f"{type(e).__name__}: {e}")
            return Result()

    def _press(self, keysym, char, state):
        eng = self.engine
        snap = eng.snapshot()
        composing = bool(snap.preedit)
        letter = len(char) == 1 and "a" <= char <= "z"
        if state & (CONTROL | ALT):
            return Result(consume=composing)
        if not composing:
            if not letter:
                return Result()
            eng.feed(char)
        elif letter or char == "'":
            eng.feed(char)
        elif keysym == "BackSpace":
            eng.backspace()
        elif keysym == "Escape":
            eng.clear()
        elif keysym == "Return" or keysym == "KP_Enter":
            raw = snap.raw
            eng.clear()
            return Result(consume=True, commit=raw or None)
        elif keysym == "space":
            if snap.candidates:
                eng.select(snap.highlighted)
        elif len(char) == 1 and "1" <= char <= "9":
            i = int(char) - 1
            if i < len(snap.candidates):
                eng.select(i)
        elif keysym in _PAGE:
            eng.page(_PAGE[keysym])
        return Result(consume=True, commit=eng.take_commit())

    def release(self, keysym):
        if keysym not in _SHIFTS or not self._shift_alone:
            return Result()
        self._shift_alone = False
        if self.engine is None:
            if self.status == "off":
                self.status = "loading"
                return Result(need_engine=True)
            return Result()
        self.zh = not self.zh
        if not self.zh:
            try:
                self.engine.clear()
            except Exception as e:
                self._drop(f"{type(e).__name__}: {e}")
        return Result(toggled=True)

    # ---------------------------------------------------------------- 给界面画的

    def view(self):
        try:
            snap = self._snap() if self.zh else _EMPTY
        except Exception as e:
            self._drop(f"{type(e).__name__}: {e}")
            snap = _EMPTY
        return {"mode": "zh" if self.zh else "en", "status": self.status, "reason": self.reason,
                "composing": bool(snap.preedit), "preedit": snap.preedit, "candidates": list(snap.candidates),
                "page_no": snap.page_no, "last_page": snap.last_page, "highlighted": snap.highlighted}
