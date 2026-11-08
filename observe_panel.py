#!/usr/bin/env python3
"""串口观察模式的车机窗口 (#85, 半自动): 人在 console 上登录好, 点"开始观察", 确认设备名, 推车画前面板.

推车不碰登录, 只发只读命令 (逻辑在 observe_core.py). 串口面板选中一路点"观察"打开; 每一路同时只开一个窗口,
和巡检互斥 (observe_core.claim). 线程纪律同 serial_panel: 后台线程 (轮询) 只往 self.ui 放东西, 界面只在 _tick 里改.
观察全程占着车机串口面板对这一路的连接 (面板上显示"观察中"), 结束后接回. 有人 attach 着就不轮询.
"""

import functools
import os
import queue
from secrets import token_hex
import threading
import time
import weakref

import tkinter as tk

import atomicfile
import lamps_core
import observe_core as oc
import survey_core
import theme as th
from serial_core import match_service

BG, CARD, FG, DIM, OFF = th.follow(globals(), BG="PANEL", CARD="BG", FG="FG", DIM="DIM", OFF="BOX")
FONT, FONT_MONO = th.FONT_SMALL, th.FONT_MONO
CELL, GAP, PAD = 40, 8, 16              # 口的格子 / 间隔 / 边距 (像素); 列多了按画布宽度缩
WIN_W, WIN_H = 1100, 440
CANVAS_W = WIN_W - 2 * PAD              # 画布左右各让出 PAD
FRAME_MS = 60                           # 刷新灯 (闪烁) 的间隔
SNAP_S = 5                              # 多久给副屏写一次快照

_WINDOWS = weakref.WeakSet()            # 开着的观察窗口: 锁屏时隐藏 (置顶窗口别浮在锁屏上), 解锁后恢复


def hide_all():
    """锁屏: 等待中的设备确认作废; 正在跑的观察停掉 (轮询会替人给 console 保活); 窗口藏起来. 解锁后不自动恢复."""
    for w in list(_WINDOWS):
        if _alive(w):
            if getattr(w, "_confirming", None):
                w.cancel_confirm("锁屏了, 确认已取消; 解锁后请重新点 开始观察")
            worker = getattr(w, "worker", None)
            if worker is not None and worker.is_alive():
                w.stop_for_lock()
            w.withdraw()


def show_all():
    for w in list(_WINDOWS):
        if _alive(w):
            w.deiconify()
            w.lift()


def _alive(w):
    try:
        return bool(w.winfo_exists())
    except Exception:
        return False


class ConsoleHold:
    """观察全程借走车机串口面板对这一路的控制台 (它本身就是一个 attach 着的"人", 连着就永远不轮询).
    "观察中"登记在模块级 (observe_core.observing_*): kiosk 切页新建的面板也看得到, 不连、不发唤醒回车.
    pause: 登记, 并断开当前连着这一路的面板; resume: 撤销登记, 找当前活着、选着这一路又没连上的面板接回
    (不用 pause 时存下的旧面板: 它可能已经被 kiosk 销毁了)."""

    def __init__(self, name, panels=None):
        self.name, self.panels, self.active = name, panels or oc.live_panels, False

    def _mine(self, p):
        """面板是不是 (或者刚才是) 连着这一路: 连着的以 _connect 成功时记下的服务名为准 (list 一时返回空也不会错);
        没连着的看它选着的口 (记录里的口没变, 或者能从服务列表对上)."""
        if not _alive(p):
            return False
        rec, dev = getattr(p, "_conn_name", None), getattr(p, "_conn_dev", None)
        if getattr(p, "sock", None) and rec is not None:
            return rec == self.name
        if rec == self.name and dev is not None and p.port_var.get() == dev:
            return True
        return match_service(p.port_var.get(), p._serves) == self.name

    def pause(self):
        """登记并断开连着这一路的面板; 返回断没断开."""
        if self.active:
            return False
        self.active = True
        oc.observing_add(self.name)
        cut = False
        for p in self.panels():
            if not self._mine(p):
                continue
            if getattr(p, "sock", None):
                p._disconnect()
                cut = True
            show = getattr(p, "_show_held", None)
            if show:
                try:
                    show()
                except Exception:
                    pass
        return cut

    def drop(self):
        """只撤销登记, 不接回 (kiosk 退出的时候不必再去连面板)."""
        if self.active:
            self.active = False
            oc.observing_discard(self.name)

    def resume(self):
        """撤销登记; 选着这一路又没连上的面板接回 (标题跟着刷新). 接不回返回给人看的一句."""
        if not self.active:
            return None
        self.active = False
        oc.observing_discard(self.name)
        msg = None
        for p in self.panels():
            if not self._mine(p) or getattr(p, "sock", None):
                continue
            try:
                p._connect(wake=False)
            except Exception:
                pass
            if not getattr(p, "sock", None):
                msg = "车机控制台没接回来, 请在串口面板点 刷新 手动重连"
        return msg


class Life:
    """观察窗口和它的后台线程都结束了才放 claim: 线程还在跑 (可能还在一条 conbroker 交互里) 时,
    同一路不许开新的观察或巡检. 按线程计数, 先结束的那个不会把占用放掉."""

    def __init__(self, name, token):
        self.name, self.token, self._lock = name, token, threading.Lock()
        self._workers, self._window = 0, True

    def worker_started(self):
        with self._lock:
            self._workers += 1

    def worker_done(self):
        with self._lock:
            self._workers = max(0, self._workers - 1)
            if not self._window and not self._workers:
                oc.release(self.name, self.token)

    def window_gone(self):
        with self._lock:
            self._window = False
            if not self._workers:
                oc.release(self.name, self.token)


def _lit_color(state):
    return {"up": th.LAMP_OK, "admin": th.LAMP_WARN, "standby": th.DIM, "unknown": th.MARK}.get(state, OFF)


def panel_cells(panel, ports, now, cell=CELL, gap=GAP, pad=PAD, stale=False, width=None):
    """面板格子 + 当前端口状态 -> 要画的东西 [{name, box, fill, fg, shape, label}]. stale: 数据不可信, 全灰.
    width: 画布宽; 列多 (48 口 + 上行) 放不下就把格子等比缩小 (同 dash_render.front_boxes)."""
    cols = max((c["x"] for c in panel), default=0) + 1
    pitch = cell + gap
    if width:
        pitch = min(pitch, (width - 2 * pad) // cols)
        gap = max(2, pitch // 6)
        cell = pitch - gap
    out = []
    for c in panel:
        state, lit = ("unknown", False) if stale else oc.lamp(ports.get(c["name"]), now)
        x0, y0 = pad + c["x"] * pitch, pad + c["y"] * pitch
        out.append({"name": c["name"], "box": (x0, y0, x0 + cell, y0 + cell), "shape": c.get("shape", "rj45"),
                    "label": c.get("label", ""), "fill": _lit_color(state) if lit else OFF,
                    "fg": th.LAMP_LIT_FG if lit and state != "down" else DIM})
    return out


def draw(canvas, cells):
    """画到画布上, 返回 {图元 id: 接口名} 给悬停 / 点击查. rj45 画成带卡口的方块, 光口画成长方块."""
    canvas.delete("all")
    hits = {}
    for c in cells:
        x0, y0, x1, y1 = c["box"]
        if c["shape"] == "rj45":
            w = (x1 - x0) // 4
            pts = (x0, y0, x1, y0, x1, y1 - 8, x1 - w, y1 - 8, x1 - w, y1, x0 + w, y1, x0 + w, y1 - 8, x0, y1 - 8)
            i = canvas.create_polygon(*pts, fill=c["fill"], outline=DIM)
        else:
            i = canvas.create_rectangle(x0, y0 + 6, x1, y1 - 6, fill=c["fill"], outline=DIM)
        hits[i] = c["name"]
        hits[canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2 - 2, text=c["label"], fill=c["fg"], font=FONT)] = c["name"]
    return hits


def _pct(u):
    return "--" if u is None else f"{u:g}%"


def port_info(p):
    return f"{p['name']}   {p.get('phy', '?')} / {p.get('proto', '?')}   入 {_pct(p.get('in_uti'))}   出 {_pct(p.get('out_uti'))}"


class ObserveWindow(tk.Toplevel):
    def __init__(self, master, dev, name, vendor, ddir, token, panel=None, head=None):
        super().__init__(master, bg=BG, highlightthickness=3, highlightbackground=th.PRIMARY, **th.DIALOG)
        self.dev, self.name, self.vendor, self.ddir, self.token = dev, name, vendor, ddir, token
        self.head = head or survey_core.safe_name(os.path.basename(dev))
        self.ui, self.stop_evt = queue.Queue(), threading.Event()
        self.obs, self.worker, self.hits, self._snap_at, self._hover = None, None, {}, 0, None
        self._closing = False
        self.hold = ConsoleHold(name)             # 车机控制台本身是 attach 着的"人"
        self.life = Life(name, token)
        self._snap_err = None
        self._confirming = None             # (obs, 设备名): 等人在窗口里点 确认 / 取消
        self._stop_reason = None            # 窗口这边叫停的原因 (锁屏)
        self.overrideredirect(True)
        W, H = WIN_W, WIN_H
        self.geometry(f"{W}x{H}+{(self.winfo_screenwidth() - W) // 2}+{(self.winfo_screenheight() - H) // 2}")
        self.attributes("-topmost", True)
        self._build()
        _WINDOWS.add(self)
        self.after(FRAME_MS, self._tick)

    def _build(self):
        bar = tk.Frame(self, bg=th.TITLE)
        bar.pack(fill="x")
        self.title_lbl = tk.Label(bar, text=f"观察 {self.dev}", font=th.FONT_BIG, bg=th.TITLE, fg=th.WHITE, padx=12)
        self.title_lbl.pack(side="left")
        self.close_btn = tk.Button(bar, text="退出观察", font=FONT, command=self.close, bg=th.BTN2, fg=FG,
                                   **th.RAISED, cursor="hand2", padx=10)
        self.close_btn.pack(side="right", padx=6, pady=4)
        self.start_btn = tk.Button(bar, text="开始观察", font=FONT, command=self.begin, bg=th.PRIMARY, fg=FG,
                                   **th.RAISED, cursor="hand2", padx=12)
        self.start_btn.pack(side="right", padx=6, pady=4)
        self.confirm_row = tk.Frame(self, bg=BG)         # 不用模态框: 嵌套事件循环里能重入 begin
        self.confirm_lbl = tk.Label(self.confirm_row, text="", font=FONT, bg=BG, fg=FG)
        self.confirm_lbl.pack(side="left")
        tk.Button(self.confirm_row, text="取消", font=FONT, command=self.cancel_confirm, bg=th.BTN2, fg=FG,
                  **th.RAISED, cursor="hand2", padx=12).pack(side="right", padx=6)
        tk.Button(self.confirm_row, text="确认", font=FONT, command=self.confirm, bg=th.PRIMARY, fg=FG,
                  **th.RAISED, cursor="hand2", padx=12).pack(side="right", padx=6)
        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=PAD)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Button-1>", self._on_hover)
        self.status = tk.Label(self, text="先在 console 上登录, 停在 <设备名> 后点 开始观察", font=FONT_MONO, bg=BG,
                               fg=DIM, anchor="w", justify="left", wraplength=WIN_W - 2 * PAD)
        self.status.pack(fill="x", padx=PAD, pady=8)

    # ------------------------------------------------------------ 后台

    def _new_observer(self):
        log = oc.LogFile(os.path.join(self.ddir, "logs", f"{lamps_core._safe(self.name)}.log"))
        return oc.Observer(self.name, self.vendor, send=functools.partial(survey_core.conbroker_send, grace=oc.POLL_GRACE_S),
                           log=log, busy=lambda: oc.agent_busy(self.dev))

    def begin(self):
        """开始观察第一步: 看现场 (只认用户视图提示符), 在窗口里请人确认设备名. 这一步不往 console 发任何字节.
        确认期间 (_confirming) 再点直接返回; 不用模态确认框: 它的嵌套事件循环里能重入, 锁屏解锁后还会被盖住."""
        if self.worker or self._confirming:
            return
        obs = self._new_observer()
        name, why = obs.probe()
        if why:
            self.status.config(text=f"没开始: {why}", fg=th.WARN)
            return
        self._confirming = (obs, name)
        self.start_btn.config(state="disabled")
        self.status.config(text="请确认设备名", fg=DIM)       # 别残留上一次的失败原因
        self._show_confirm(confirm_text(name))

    def confirm(self):
        """人点了 确认: 再看一次现场 (还得是同一个 <设备名>), 后台线程开始轮询."""
        pending, self._confirming = self._confirming, None
        if not pending:
            return
        self._hide_confirm()
        obs, name = pending
        if self.worker:
            return
        got, why = obs.probe()
        if why or got != name:
            self.status.config(text=f"没开始: {why or f'现场变了 (<{name}> 变成了 <{got}>), 请重新点 开始观察'}",
                               fg=th.WARN)
            self.start_btn.config(state="normal")
            return
        self.obs = obs
        self._stop_reason = None
        self._closing = False               # 上次点过退出观察、留着窗口再开始: 按钮恢复
        self.close_btn.config(state="normal", text="退出观察", command=self.close)
        self.status.config(text=f"观察 <{name}>…", fg=DIM)
        self._snap_at = 0
        self.stop_evt.clear()
        self.hold.pause()                   # 观察全程占着车机控制台对这一路的连接 (有人 attach 就不轮询); 结束时接回
        self._spawn(obs, name)

    def cancel_confirm(self, why=None):
        if not self._confirming:
            return
        self._confirming = None
        self._hide_confirm()
        self.start_btn.config(state="normal")
        self.status.config(text=why or "已取消", fg=th.WARN if why else DIM)

    def _show_confirm(self, text):
        self.confirm_lbl.config(text=text)
        self.confirm_row.pack(fill="x", padx=PAD, pady=(8, 0), before=self.canvas)

    def _hide_confirm(self):
        self.confirm_row.pack_forget()

    def _spawn(self, obs, name):
        self.worker = threading.Thread(target=self._work, args=(obs, name), daemon=True)
        self.life.worker_started()
        self.worker.start()

    def _work(self, obs, name):
        try:
            ok = obs.start(name, cancelled=self.stop_evt.is_set)
            oc.record("start", head=self.head, device=obs.device, ok=ok, error=obs.error)
            while ok and not self.stop_evt.is_set() and obs.state == "polling":
                obs.tick(time.monotonic())
                self.stop_evt.wait(0.5)
            obs.stop()
            oc.record("stop", head=self.head, device=obs.device, state=obs.state, error=obs.error)
        except Exception as e:              # 意外出错: 别停在 polling (副屏会把旧数据当实时的画)
            obs.state, obs.error = "failed", f"观察出错, 已停止: {e or type(e).__name__}"
            oc.record("stop", head=self.head, device=getattr(obs, "device", None), state="failed", error=obs.error)
        finally:                            # 不管怎么结束: 放占用 (窗口已关的话) 并通知界面 (接回控制台在 _ended 里)
            life = getattr(self, "life", None)
            if life:
                life.worker_done()
            self.ui.put(self._ended)

    def stop_for_lock(self):
        """锁屏: 停止观察 (按正常退出收尾, 面板照常接回), 解锁后不自动恢复, 要人重新点 开始观察."""
        self._stop_reason = "锁屏, 观察已停止; 需要继续请解锁后重新点 开始观察"
        self.stop_evt.set()
        oc.record("stop", head=self.head, note="锁屏, 观察已停止")

    def _console_back(self):
        msg = self.hold.resume()
        if msg and _alive(self):
            self._snap_err = msg            # 借状态栏的提示位说一声
            self.status.config(text=msg, fg=th.WARN)

    def _ended(self):
        """后台线程结束: 出了事 (没开始 / 掉线 / 出错) 留着窗口说明, 可以再点开始观察; 点了退出且没事就关窗."""
        obs, self.worker = self.obs, None
        self._console_back()
        if self._closing and not obs.error:
            return self.destroy()
        self._write_snap()
        why = getattr(obs, "reason", None) or self._stop_reason
        self.status.config(text=obs.error or why or "已停止", fg=th.BAD if obs.error else th.WARN if why else DIM)
        self.start_btn.config(state="normal")
        self.close_btn.config(state="normal", text="关闭" if self._closing else "退出观察",
                              command=self.destroy if self._closing else self.close)

    # ------------------------------------------------------------ 界面

    def _tick(self):
        try:
            while True:
                self.ui.get_nowait()()
        except queue.Empty:
            pass
        if not self.winfo_exists():
            return
        obs = self.obs
        if obs and obs.panel:
            stale = obs.state != "polling" or bool(obs.error)
            width = self.canvas.winfo_width()
            self.hits = draw(self.canvas, panel_cells(obs.panel, obs.ports, time.time(), stale=stale,
                                                      width=width if width > 1 else CANVAS_W))
        if obs and self.worker and obs.state == "polling" and not self._closing:
            self._status(obs)
            self.title_lbl.config(text=f"观察 {self.dev} · {obs.device}" + (f" · {obs.model}" if obs.model else ""))
        if obs and time.monotonic() - self._snap_at > SNAP_S:
            self._write_snap()
        self.after(FRAME_MS, self._tick)

    def _write_snap(self):
        """准备中 / 观察中 / 没开始 / 掉线都写, 副屏照实显示; 关窗时删掉自己这份."""
        self._snap_at = time.monotonic()
        try:
            os.makedirs(oc.STATE_DIR, exist_ok=True)
            atomicfile.write_json(oc.snapshot_path(self.head), self.obs.snapshot(self.head, owner=self.token))
            self._snap_err = None
        except OSError as e:                # 不静默吞: 状态栏提示副屏看不到
            self._snap_err = f"副屏快照写不进去: {e.strerror or e}"

    def _status(self, obs):
        if self._hover and self._hover in obs.ports:
            self.status.config(text=port_info(obs.ports[self._hover]), fg=FG)
        elif obs.error:
            self.status.config(text=obs.error, fg=th.WARN)
        elif obs.warning:
            self.status.config(text=obs.warning, fg=th.WARN)
        elif self._snap_err:
            self.status.config(text=self._snap_err, fg=th.WARN)
        elif obs.pacer.paused(time.monotonic()):
            self.status.config(text="有人在用串口, 暂停", fg=DIM)
        else:
            self.status.config(text="", fg=DIM)

    def _on_hover(self, ev):
        items = self.canvas.find_overlapping(ev.x, ev.y, ev.x, ev.y)
        self._hover = next((self.hits[i] for i in reversed(items) if i in self.hits), None)

    def close(self):
        """后台线程在跑: 让它停下 (只停轮询, 不往 console 发东西), 结束后说明 console 的状态; 没在跑直接关."""
        self._closing = True
        if self.worker:
            self.stop_evt.set()
            self.close_btn.config(state="disabled")
            self.status.config(text="退出中…", fg=DIM)
            return
        self.destroy()

    def _shutdown(self, timeout=3):
        """窗口被硬关 (kiosk 退出) 时后台线程还在跑: 先叫停, 给正在进行的那条 conbroker 交互最多 timeout 秒收尾,
        同步记一笔 cartlog (之后进程可能就没了). 不等 stop 走完."""
        self.stop_evt.set()
        worker = self.worker
        if worker is None or not worker.is_alive():
            return False
        oc.record("interrupted", head=self.head, note="观察被 kiosk 退出打断")
        worker.join(timeout)
        return True

    def _release_console(self, interrupted):
        """kiosk 退出打断的: 只撤销"观察中"登记, 不在退出过程中接回面板; 否则正常接回."""
        (self.hold.drop if interrupted else self.hold.resume)()

    def destroy(self):
        self._release_console(self._shutdown())
        oc.remove_snapshot(self.token, oc.snapshot_path(self.head))
        self.life.window_gone()             # 后台线程还在跑就等它结束再放 claim
        _WINDOWS.discard(self)
        super().destroy()


def confirm_text(name):
    return f"检测到 <{name}>, 开始观察这台设备?"


def open_for(master, dev, name, vendor, ddir):
    """每一路只开一个; 已经开着就提到前面. 窗口挂在 kiosk 根窗口下: 切走串口面板不会把观察窗口一起销毁."""
    token = token_hex(4)
    root = master.winfo_toplevel()
    if not oc.claim(name, token):
        for w in root.winfo_children():
            if isinstance(w, ObserveWindow) and w.name == name:
                w.lift()
                return w
        return None
    try:
        return ObserveWindow(root, dev, name, vendor, ddir, token, panel=master)
    except Exception:
        oc.release(name, token)
        raise
