#!/usr/bin/env python3
"""给 Tk 输入框加中文输入 (#54): 拦按键交给 ime_core, 输入框下面自绘一条候选栏, 右边一个 中/英 标记.

不走 X 输入法协议 (车机没有窗口管理器, 候选框位置不稳). 用法: ime_tk.attach(entry); 失败返回 None, 输入框照旧.
默认英文, 单按 Shift 切中文; 第一次切中文才在后台打开 librime (ime_rime.open_engine), 打不开就一直英文并提示原因.
只接给普通文字输入框; 别接确认码 / PIN / 密码 / 命令输入框 (候选上屏是"替人打字", 那几处要人亲手敲).
"""

import threading

import ime_core
import theme as th

_seq = [0]


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
    def __init__(self, entry, opener=None, label=None, spawn=_spawn, font=None, bg=None, fg=None):
        if label is None:
            import tkinter as tk
            label = tk.Label
        if opener is None:
            import ime_rime
            opener = ime_rime.open_engine
        self.entry, self.opener, self.spawn = entry, opener, spawn
        self.ime = ime_core.Ime()
        self._result, self._notice = None, ""          # _notice: 不可用的原因, 显示到下一次按键
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
        r = self.ime.press(ev.keysym, ev.char, ev.state)
        if r.commit:
            self.entry.insert("insert", r.commit)
        self._redraw()
        return "break" if r.consume else None

    def _release(self, ev):
        r = self.ime.release(ev.keysym)
        if r.need_engine:
            self.spawn(self._open)
            self.entry.after(100, self._poll)
        if r.need_engine or r.toggled:
            self._redraw()
        return None

    # ---------------------------------------------------------------- 后台开引擎 (librime 首次部署可能很慢)

    def _open(self):
        try:
            self._result = self.opener()
        except Exception as e:
            self._result = (None, f"中文输入不可用: {type(e).__name__}: {e}")

    def _poll(self):
        if self._result is None:
            self.entry.after(100, self._poll)
            return
        eng, why = self._result
        if eng is not None:
            self.ime.set_engine(eng)
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
        try:
            self.ime.close()
        except Exception:
            pass


def attach(entry, **kw):
    """给输入框接上中文输入; 出任何错返回 None, 输入框保持原样 (只打 ASCII)."""
    try:
        return ImeEntry(entry, **kw)
    except Exception:
        return None
