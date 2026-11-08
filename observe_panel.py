#!/usr/bin/env python3
"""串口观察模式的车机窗口 (#85): 输一次密码, 画设备前面板, 灯跟着真机亮灭闪. 逻辑在 observe_core.py.

串口面板选中一路点"观察"打开; 每一路同时只开一个窗口, 和巡检互斥 (observe_core.claim). 线程纪律同 serial_panel: 后台线程 (登录 / 轮询 / quit)
只往 self.ui 放东西, 界面只在 _tick 里改. 密码框读完立刻清空, 密码只经 Observer.start 传给登录状态机,
不落盘不进日志. 会话掉了 / 登录失败 / 退出时没敢 quit, 窗口留着, 写明原因, 登录行回来可以再登.
"""

import functools
import os
import queue
import secrets
import threading
import time

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

_LOGIN_SLOT = set()                     # 同一时间只许一个观察窗口处于登录阶段 (共用一个车机控制台, 不能互相暂停 / 接回)
_SLOT_LOCK = threading.Lock()


def login_slot(token):
    with _SLOT_LOCK:
        if _LOGIN_SLOT and token not in _LOGIN_SLOT:
            return False
        _LOGIN_SLOT.add(token)
        return True


def login_slot_done(token):
    with _SLOT_LOCK:
        _LOGIN_SLOT.discard(token)


def _alive(w):
    try:
        return bool(w.winfo_exists())
    except Exception:
        return False


class ConsoleHold:
    """登录 / 退出时借走车机串口面板的控制台 (它本身就是一个 attach 着的"人").
    只在面板确实连着被观察的这一路时才断开; 接回只接原来那一路, 面板没了 / 切到别的口 / 已经连上了就不动."""

    def __init__(self, panel, name):
        self.panel, self.name, self.dev = panel, name, None

    def pause(self):
        p = self.panel
        if self.dev is not None or p is None or not _alive(p) or not getattr(p, "sock", None):
            return False
        dev = p.port_var.get()
        if match_service(dev, p._serves) != self.name:
            return False
        self.dev = dev
        p._held = self.name                 # 面板这期间不自动接回、不发唤醒回车
        p._disconnect()
        return True

    def resume(self):
        """接回; 接不回返回给人看的一句."""
        dev, self.dev = self.dev, None
        p = self.panel
        if dev is not None and p is not None:
            p._held = None
        if dev is None or not _alive(p) or p.port_var.get() != dev or p.sock:
            return None
        try:
            p._connect(wake=False)
        except Exception:
            pass
        return None if p.sock else "车机控制台没接回来, 请在串口面板点 刷新 手动重连"


class Life:
    """观察窗口和它的后台线程谁后结束, 谁放 claim: 线程还在跑 (可能还要发 quit) 时, 同一路不许开新的观察或巡检."""

    def __init__(self, name, token):
        self.name, self.token, self._lock = name, token, threading.Lock()
        self._worker, self._window = False, True

    def worker_started(self):
        with self._lock:
            self._worker = True

    def worker_done(self):
        with self._lock:
            self._worker = False
            if not self._window:
                oc.release(self.name, self.token)

    def window_gone(self):
        with self._lock:
            self._window = False
            if not self._worker:
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
        self.hold = ConsoleHold(panel, name)      # 车机控制台本身是 attach 着的"人"
        self.life = Life(name, token)
        self._snap_err = None
        self.overrideredirect(True)
        W, H = WIN_W, WIN_H
        self.geometry(f"{W}x{H}+{(self.winfo_screenwidth() - W) // 2}+{(self.winfo_screenheight() - H) // 2}")
        self.attributes("-topmost", True)
        self._build()
        self.after(FRAME_MS, self._tick)

    def _build(self):
        bar = tk.Frame(self, bg=th.TITLE)
        bar.pack(fill="x")
        self.title_lbl = tk.Label(bar, text=f"观察 {self.dev}", font=th.FONT_BIG, bg=th.TITLE, fg=th.WHITE, padx=12)
        self.title_lbl.pack(side="left")
        self.close_btn = tk.Button(bar, text="退出观察", font=FONT, command=self.close, bg=th.BTN2, fg=FG,
                                   **th.RAISED, cursor="hand2", padx=10)
        self.close_btn.pack(side="right", padx=6, pady=4)
        self.login_row = tk.Frame(self, bg=BG)
        self.login_row.pack(fill="x", padx=PAD, pady=8)
        self.user_var, self.pass_var = tk.StringVar(), tk.StringVar()
        for label, var, show in (("用户名", self.user_var, ""), ("密码", self.pass_var, "•")):
            tk.Label(self.login_row, text=label, font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(0, 4))
            e = tk.Entry(self.login_row, textvariable=var, show=show, font=FONT, width=16, bg=CARD, fg=FG,
                         insertbackground=FG, **th.SUNKEN)
            e.pack(side="left", padx=(0, 12), ipady=4)
            e.bind("<Return>", lambda _e: self.login())
        self.login_btn = tk.Button(self.login_row, text="登录", font=FONT, command=self.login, bg=th.PRIMARY, fg=FG,
                                   **th.RAISED, cursor="hand2", padx=12)
        self.login_btn.pack(side="left")
        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=PAD)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Button-1>", self._on_hover)
        self.status = tk.Label(self, text="", font=FONT_MONO, bg=BG, fg=DIM, anchor="w")
        self.status.pack(fill="x", padx=PAD, pady=8)

    # ------------------------------------------------------------ 后台

    def login(self):
        user, pw = self.user_var.get().strip(), self.pass_var.get()
        self.pass_var.set("")                       # 读完就清
        if oc.bad_credential(user) or oc.bad_credential(pw):
            self.status.config(text="用户名或密码里有控制字符 (回车 / 换行等), 请重新输入", fg=th.WARN)
            return
        if self.worker or not user or not pw:
            return
        if not login_slot(self.token):
            self.status.config(text="另一个观察窗口正在登录, 等它登完再来", fg=th.WARN)
            return
        self.login_btn.config(state="disabled")
        log = oc.LogFile(os.path.join(self.ddir, "logs", f"{lamps_core._safe(self.name)}.log"))
        self.obs = oc.Observer(self.name, self.vendor,
                               exchange=functools.partial(oc.broker_exchange, self.ddir, self.name, vendor=self.vendor),
                               send=functools.partial(survey_core.conbroker_send, grace=oc.POLL_GRACE_S), log=log,
                               busy=lambda: oc.agent_busy(self.head))
        self.status.config(text="看现场、登录中…", fg=DIM)
        self._snap_at = 0
        self.stop_evt.clear()
        self.hold.pause()                   # 登录阶段不许有人 attach (#85); 车机控制台连着这一路就先断开, 登录完接回
        box = [pw]                          # 一次性容器: 线程取出后清空, 不在 Thread._args 里一直挂着
        del pw
        self.worker = threading.Thread(target=self._work, args=(self.obs, user, box), daemon=True)
        self.life.worker_started()
        self.worker.start()

    def _work(self, obs, user, box):
        pw = box.pop() if box else ""
        try:
            ok = obs.start(user, pw, cancelled=self.stop_evt.is_set)
        finally:
            del pw
            login_slot_done(self.token)
        self.ui.put(self._console_back)
        oc.record("start", head=self.head, device=obs.device, ok=ok, error=obs.error)
        if ok:
            self.ui.put(self._logged_in)
        while ok and not self.stop_evt.is_set() and obs.state == "polling":
            obs.tick(time.monotonic())
            self.stop_evt.wait(0.5)
        obs.stop(time.monotonic())
        oc.record("stop", head=self.head, device=obs.device, state=obs.state, error=obs.error, note=obs.note)
        life = getattr(self, "life", None)
        if life:
            life.worker_done()
        self.ui.put(self._ended)

    def _console_back(self):
        msg = self.hold.resume()
        if msg and _alive(self):
            self._snap_err = msg            # 借状态栏的提示位说一声
            self.status.config(text=msg, fg=th.WARN)

    def _logged_in(self):
        self.login_row.pack_forget()
        self.title_lbl.config(text=f"观察 {self.dev} · {self.obs.device}" + (f" · {self.obs.model}" if self.obs.model else ""))

    def _ended(self):
        """后台线程结束: 有话要交代 (console 还登录着 / 出错 / 掉线) 就留着窗口说明, 登录行回来可以再登; 没有就关窗."""
        obs, self.worker = self.obs, None
        self._console_back()
        if self._closing and not obs.note and not obs.error:
            return self.destroy()
        self._write_snap()
        text = "; ".join(x for x in (obs.error, obs.note) if x) or "已停止"
        self.status.config(text=text, fg=th.BAD if obs.error else th.WARN if obs.note else DIM, wraplength=WIN_W - 2 * PAD,
                           justify="left")
        if not self.login_row.winfo_ismapped():
            self.login_row.pack(fill="x", padx=PAD, pady=8, before=self.canvas)
        self.login_btn.config(state="normal")
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
        if obs and time.monotonic() - self._snap_at > SNAP_S:
            self._write_snap()
        self.after(FRAME_MS, self._tick)

    def _write_snap(self):
        """登录中 / 观察中 / 失败 / 掉线都写, 副屏照实显示; 关窗时删掉自己这份."""
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
            return
        oc.record("interrupted", head=self.head,
                  note="观察被 kiosk 退出打断, console 可能仍在登录状态或停在 Password:, " + oc.IDLE_LOGOUT)
        worker.join(timeout)

    def destroy(self):
        self._shutdown()
        self.hold.resume()
        oc.remove_snapshot(self.token, oc.snapshot_path(self.head))
        self.life.window_gone()             # 后台线程还在跑就等它结束再放 claim
        super().destroy()


def open_for(master, dev, name, vendor, ddir):
    """每一路只开一个; 已经开着就提到前面. 窗口挂在 kiosk 根窗口下: 切走串口面板不会把观察窗口一起销毁."""
    token = secrets.token_hex(4)
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
