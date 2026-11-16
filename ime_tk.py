#!/usr/bin/env python3
"""给 Tk 输入框加中文输入 (#54): 拦按键交给 ime_core, 输入框下面自绘一条候选栏, 右边一个 中/英 标记.

不走 X 输入法协议 (车机没有窗口管理器, 候选框位置不稳). 用法: ime_tk.attach(entry, purpose="面板.字段");
失败返回 None, 输入框照旧. 默认英文, 单按 Shift 切中文; 第一次切中文才在后台起 librime 子进程 (ime_proc),
打不开就一直英文并提示原因. 本进程不加载 librime.
只接给普通文字输入框; 别接确认码 / PIN / 密码 / 命令输入框 (候选上屏是"替人打字", 那几处要人亲手敲):
设了 show= 的 (密码框) 和标了 no_ime 的直接拒; 接入点在 tests/test_ime_tk.py 的 (文件, 用途) 白名单里登记
(那个测试是提醒, 不是边界: 静态扫描挡不住所有写法, 运行时兜底靠 show= / no_ime).
拼音期间吃掉的键不会冒泡到 bind_all, 所以每吃一个键补发虚拟事件 INPUT_EVENT, kiosk 拿它算"有人在操作".
当前焦点框的状态给指示灯 (#100): focus_state() 取, watch(fn) 订阅; 都在 Tk 主线程. 只给 mode / status, 不给拼音和候选.
"""

import threading
import time

import ime_core
import theme as th

_seq = [0]
INPUT_EVENT = "<<OpsCartImeInput>>"
NO_IME = "no_ime"           # 输入框上 `e.no_ime = True`: 要人亲手敲的框 (确认码 / PIN / 串口命令 / MAC / 网络参数), attach 见到就拒
AUTO_SWITCH_S = 3.0         # 引擎在切中文那一下之后这么久内到、且中间没打字, 才自动切到中文
WATCH_MS = 1000             # 焦点在框上时多久看一眼引擎还活着没有
_focus, _published, _watchers = [None], [None], []     # 有焦点的 ImeEntry; 上次发出去的状态; 订阅者


def focus_state():
    """当前焦点框的 {"mode": "en"/"zh", "status": ...}; 焦点不在接了中文的框里 -> None."""
    w = _focus[0]
    if w is None:
        return None
    v = w.ime.view()
    return {"mode": v["mode"], "status": v["status"]}


def watch(fn):
    """焦点框状态变了就调 fn(focus_state()). 订阅者出错不影响打字."""
    _watchers.append(fn)


def _publish():
    state = focus_state()
    if state == _published[0]:
        return
    _published[0] = state
    for fn in list(_watchers):
        try:
            fn(state)
        except Exception:
            pass


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
        if getattr(entry, NO_IME, False):
            raise ValueError("这个输入框标了 no_ime (确认码 / PIN / 命令), 不接中文输入")
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
        self._watching = False                         # 存活检查的定时器排着没有
        self._lock = threading.Lock()                  # _result / _closed: 后台 _open 和主线程 close 之间
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
        entry.bind_class(self.tag, "<FocusIn>", self._focus_in)
        entry.bind_class(self.tag, "<FocusOut>", self._focus_out)
        entry.bind("<Destroy>", lambda _e: self.close(), add="+")
        self._redraw()

    # ---------------------------------------------------------------- 按键

    def _press(self, ev):
        self._notice = ""
        if ev.keysym not in ("Shift_L", "Shift_R"):
            self._typed = True
        r = self.ime.press(ev.keysym, ev.char, ev.state)
        try:
            if r.commit:
                self.entry.insert("insert", r.commit)
            self._redraw()
            if r.consume:
                self.entry.event_generate(INPUT_EVENT)
        except Exception:
            if not r.consume:
                raise
        return "break" if r.consume else None      # 吃掉的键 (含回车) 出了错也绝不漏给输入框

    def _release(self, ev):
        r = self.ime.release(ev.keysym)
        if r.need_engine:
            self._asked_at, self._typed, self._result = self.clock(), False, None
            self.spawn(self._open)
            self.entry.after(100, self._poll)
        if r.need_engine or r.toggled:
            self._redraw()
        return None

    # ---------------------------------------------------------------- 焦点 (#100)

    def _focus_in(self, _ev):
        _focus[0] = self
        self._check_engine()                # 没焦点时引擎可能已经死了
        _publish()
        if not self._watching:
            self._watching = True
            self.entry.after(WATCH_MS, self._watch)

    def _focus_out(self, _ev):
        if _focus[0] is self:               # 别的框先收到 In 的话, 这个迟到的 Out 不算数
            _focus[0] = None
            _publish()

    def _check_engine(self):
        if self.ime.check_engine():
            self._redraw()

    def _watch(self):
        if self._closed or _focus[0] is not self:
            self._watching = False
            return
        self._check_engine()
        self.entry.after(WATCH_MS, self._watch)

    # ---------------------------------------------------------------- 后台开引擎 (librime 首次部署可能很慢)

    def _open(self):
        try:
            res = self.opener()
        except Exception as e:
            res = (None, f"中文输入不可用: {type(e).__name__}: {e}")
        with self._lock:                             # 先写结果再看 _closed: close 在哪一刻发生都只关一次
            self._result = res
            closed = self._closed
        if closed:
            self._discard()

    def _discard(self):
        """关掉已经到了、还没交给 ime 的引擎 (只关一次)."""
        with self._lock:
            res, self._result = self._result, (None, "输入框已关闭")
        if res and res[0] is not None:
            try:
                res[0].close()
            except Exception:
                pass

    def _poll(self):
        if self._result is None and not self._closed:
            self.entry.after(100, self._poll)
            return
        if self._closed:                    # 输入框已经没了: close / _open 已经关掉引擎
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
        if _focus[0] is self:               # 在动控件之前: 控件出错灯也要对
            _publish()
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
        with self._lock:
            already, self._closed = self._closed, True
        if _focus[0] is self:
            _focus[0] = None
            _publish()
        self._discard()
        try:
            self.ime.close()
        except Exception:
            pass
        if already:
            return
        for seq in ("<KeyPress>", "<KeyRelease>", "<FocusIn>", "<FocusOut>"):     # 面板反复开关不累积绑定和控件
            try:
                self.entry.unbind_class(self.tag, seq)
            except Exception:
                pass
        for w in (self.bar, self.mode):
            try:
                w.destroy()
            except Exception:
                pass


def attach(entry, purpose, **kw):
    """给输入框接上中文输入; 出任何错 (含密码框、没给用途) 返回 None, 输入框保持原样 (只打 ASCII)."""
    try:
        return ImeEntry(entry, purpose, **kw)
    except Exception:
        return None
