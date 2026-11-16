#!/usr/bin/env python3
"""车机中文输入的状态机 (#54): 按键 -> 拼音缓冲 -> 候选 -> 上屏. 纯逻辑, 不碰 Tk, 不碰 librime.

引擎 (kiosk 里是 ime_proc 的子进程引擎, 测试里是假的) 管拼音切分和查词; 这里只管哪些键交给引擎、哪些放回输入框:
  - 默认英文, 单按一下 Shift (按下到松开中间没有别的键) 切中 / 英. 第一次切中文才去要引擎 (need_engine),
    加载中、不可用都停在英文, 原输入框照常打 ASCII.
  - 中文且没在拼: 小写字母开始拼音, 其余键 (数字、标点、大写、回车、退格、Ctrl/Alt 组合) 原样放过.
  - 正在拼: 字母 / ' 继续拼, 退格删拼音, 空格上高亮的候选 (默认第一个), 1-9 选本页第几个,
    回车把敲的字母原样上屏 (上英文), Esc 取消, - = PageUp PageDown 翻页, 其余键吃掉 (不漏进输入框).
引擎任何一步抛异常: 记下原因, 退回英文且不再用它 (status=unavailable), 不让输入框跟着坏; 正在拼时出错
那个键照样吃掉 (回车把敲的字母上屏), 不漏进输入框; 运行中崩的
(子进程退出 / 超时) 下次 Shift 可以重开, 最多 MAX_REOPEN 次; 打开就失败的 (没库 / 没词库) 不重开.
引擎不认第一个字母 (方案没加载: 不进拼音也不上屏) 同样当坏了处理, 那个字母还给输入框, 不吞键.
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
MAX_REOPEN = 2


class Ime:
    def __init__(self, engine=None):
        self.engine = engine
        self.zh = False
        self.status = "ready" if engine else "off"       # off / loading / ready / unavailable
        self.reason = ""
        self._shift_alone = False
        self._reopen = False                # 上次是运行中坏的 (可以重开) 还是打开就失败
        self._reopened = 0

    # ---------------------------------------------------------------- 引擎

    def set_engine(self, engine, zh=False):
        """引擎就绪; 默认保持当前中英 (加载慢, 用户可能已经接着打英文了), zh=True 才切中文."""
        self.engine, self.status, self.reason = engine, "ready", ""
        self.zh = bool(zh)

    def set_unavailable(self, reason):
        self._drop(reason, reopen=False)

    def _drop(self, reason, reopen=True):
        self._reopen = reopen
        eng, self.engine = self.engine, None
        self.status, self.reason, self.zh = "unavailable", str(reason) or "中文输入出错", False
        if eng is not None:
            try:
                eng.close()
            except Exception:
                pass

    def check_engine(self):
        """闲着时引擎也会死 (子进程被 RuntimeMaxSec 收掉), 不按键发现不了. 死了按运行中崩溃处理, 下次 Shift 可重开.
        状态因此变了 -> True; 引擎没有 alive() 或还活着 -> False."""
        alive = getattr(self.engine, "alive", None)
        if alive is None:
            return False
        try:
            if alive():
                return False
        except Exception:
            pass
        self._drop("中文引擎已退出")
        return True

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
            snap = self.engine.snapshot()
        except Exception:
            snap = _EMPTY
        try:
            return self._press(keysym, char or "", state)
        except Exception as e:                       # 引擎坏了: 退回英文
            self._drop(f"{type(e).__name__}: {e}")
            if not snap.preedit:                     # 没在拼: 这个键交还输入框
                return Result()
            if keysym in ("Return", "KP_Enter"):     # 在拼: 回车照样把字母上屏, 绝不漏给输入框 (会触发它的回车动作)
                return Result(consume=True, commit=snap.raw or None)
            return Result(consume=True)

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
            handled = eng.feed(char)
            commit = eng.take_commit()
            if handled is False or not (commit or eng.snapshot().preedit):
                self._drop("中文引擎不认按键 (方案没加载或没部署?), 已回英文", reopen=False)
                return Result()
            return Result(consume=True, commit=commit)
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
            if self.status == "off" or (self.status == "unavailable" and self._reopen
                                        and self._reopened < MAX_REOPEN):
                if self.status == "unavailable":
                    self._reopened += 1
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
