#!/usr/bin/env python3
"""给 Tk 输入框加中文输入 (#54): 拦按键交给 ime_core, 输入框下面自绘一条候选栏, 右边一个 中/英 标记.

不走 X 输入法协议 (车机没有窗口管理器, 候选框位置不稳). 用法: ime_tk.attach(entry, purpose="面板.字段");
失败返回 None, 输入框照旧. 默认英文, 单按 Shift 切中文; 第一次切中文才在后台起 librime 子进程 (ime_proc),
打不开就一直英文并提示原因. 本进程不加载 librime.
只接给普通文字输入框; 别接确认码 / PIN / 密码 / 命令输入框 (候选上屏是"替人打字", 那几处要人亲手敲):
设了 show= 的 (密码框) 直接拒; 接入点在 tests/test_ime_tk.py 的 (文件, 用途) 白名单里登记.
拼音期间吃掉的键不会冒泡到 bind_all, 所以每吃一个键补发虚拟事件 INPUT_EVENT, kiosk 拿它算"有人在操作".
"""

import threading
import time

import ime_core
import theme as th

_seq = [0]
INPUT_EVENT = "<<OpsCartImeInput>>"
AUTO_SWITCH_S = 3.0         # 引擎在切中文那一下之后这么久内到、且中间没打字, 才自动切到中文


def bar_text(view):
    """候选栏一行字; 不在拼就空串."""
    if not view.get("composing"):
        return ""
    parts = [view["preedit"]]
    if view.get("page_no", 0) > 0:
        parts.append("(-上页)")
    cands = view.get("candidates") or []
    hi = view.get("highlighted", 0)
    for i, c in enumerate(cands[:9]):
        s = f"{i + 1}.{c}"
        parts.append(f"[{s}]" if i == hi and i > 0 else s)
    if not cands:
        parts.append("(没有候选, 回车上英文)")
    if not view.get("last_page", True):
        parts.append("(=下页)")
    return "  ".join(parts)


def mode_text(view):
    if view.get("status") == "loading":
        return "中…"
    if view.get("status") == "unavailable":
        return "英·无中文"
    return "中" if view.get("mode") == "zh" else "英"


def _spawn(f):
    threading.Thread(target=f, daemon=True, name="ime-open").start()


class ImeEntry:
    def __init__(self, entry, purpose, opener=None, label=None, spawn=_spawn, font=None, bg=None, fg=None,
                 clock=time.monotonic):
        if not purpose:
            raise ValueError("要给用途名 purpose")
        if entry.cget("show"):
            raise ValueError("密码框不接中文输入")
        if label is None:
            import tkinter as tk
            label = tk.Label
        if opener is None:
            import ime_proc
            opener = ime_proc.open_engine
        self.entry, self.purpose, self.opener, self.spawn, self.clock = entry, purpose, opener, spawn, clock
        self.ime = ime_core.Ime()
        self._result, self._notice = None, ""          # _notice: 不可用的原因, 显示到下一次按键
        self._closed, self._asked_at, self._typed = False, 0.0, False
        _seq[0] += 1
        self.tag = f"OpsCartIme{_seq[0]}"
        top = entry.winfo_toplevel()
        kw = {"bg": bg or th.BOX, "fg": fg or th.FG, "anchor": "w", "padx": 6}
        if font is not None:
            kw["font"] = font
        self.bar = label(top, text="", **kw)
        self.mode = label(top, text="英", **dict(kw, padx=2))
        entry.bindtags((self.tag,) + tuple(entry.bindtags()))   # 先于输入框自己的 <Return> 之类
        entry.bind_class(self.tag, "<KeyPress>", self._press)
        entry.bind_class(self.tag, "<KeyRelease>", self._release)
        entry.bind("<Destroy>", lambda _e: self.close(), add="+")
        self._redraw()

    # ---------------------------------------------------------------- 按键

    def _press(self, ev):
        self._notice = ""
        if ev.keysym not in ("Shift_L", "Shift_R"):
            self._typed = True
        r = self.ime.press(ev.keysym, ev.char, ev.state)
        if r.commit:
            self.entry.insert("insert", r.commit)
        self._redraw()
        if not r.consume:
            return None
        try:
            self.entry.event_generate(INPUT_EVENT)
        except Exception:
            pass
        return "break"

    def _release(self, ev):
        r = self.ime.release(ev.keysym)
        if r.need_engine:
            self._asked_at, self._typed, self._result = self.clock(), False, None
            self.spawn(self._open)
            self.entry.after(100, self._poll)
        if r.need_engine or r.toggled:
            self._redraw()
        return None

    # ---------------------------------------------------------------- 后台开引擎 (librime 首次部署可能很慢)

    def _open(self):
        try:
            res = self.opener()
        except Exception as e:
            res = (None, f"中文输入不可用: {type(e).__name__}: {e}")
        if self._closed and res[0] is not None:      # 加载期间输入框没了: 拿到的引擎直接关, 不留子进程
            res[0].close()
            res = (None, "输入框已关闭")
        self._result = res

    def _poll(self):
        if self._result is None and not self._closed:
            self.entry.after(100, self._poll)
            return
        if self._closed:                    # 输入框已经没了: 引擎到了也直接关掉 (之后再到的由 _open 关)
            if self._result and self._result[0] is not None:
                self._result[0].close()
            return
        eng, why = self._result
        if eng is not None:
            # 加载慢的话用户可能早就接着打英文了: 只在刚切、中间没打字时才自动切到中文
            self.ime.set_engine(eng, zh=not self._typed and self.clock() - self._asked_at <= AUTO_SWITCH_S)
        else:
            self.ime.set_unavailable(why)
            self._notice = self.ime.reason
        self._redraw()

    # ---------------------------------------------------------------- 画

    def _redraw(self):
        v = self.ime.view()
        self.mode.config(text=mode_text(v))
        self.mode.place(in_=self.entry, relx=1.0, x=-2, rely=0.5, anchor="e")
        self.mode.lift()
        text = bar_text(v) or self._notice
        if text:
            self.bar.config(text=text)
            self.bar.place(in_=self.entry, x=0, rely=1.0, y=2, anchor="nw")
            self.bar.lift()
        else:
            self.bar.place_forget()

    def close(self):
        self._closed = True
        try:
            self.ime.close()
        except Exception:
            pass


def attach(entry, purpose, **kw):
    """给输入框接上中文输入; 出任何错 (含密码框、没给用途) 返回 None, 输入框保持原样 (只打 ASCII)."""
    try:
        return ImeEntry(entry, purpose, **kw)
    except Exception:
        return None
