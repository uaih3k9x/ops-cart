#!/usr/bin/env python3
"""运维推车 Kiosk GUI v2 (Tkinter + X, 无 WM 直跑).

v2: overrideredirect 全屏铺满; UVC 预览(mpv 嵌入); CH9329 键鼠透传
(点击画面捕获, Ctrl+Alt 释放, VMware 风格). 终端 root 敲 `kiosk` 启动.

线程纪律: 外部命令 (systemctl/nmcli/v4l2-ctl/smartctl...) 一律经 self.ui (UiBridge) 放到后台,
结果回主线程才碰界面; CH9329 发送走 ChWorker 自己的线程. 主线程只起 mpv (Popen 不等它).
"""

import os
import sys
import time
import tkinter as tk
from subprocess import DEVNULL, Popen       # 只给 mpv 用
from tkinter import messagebox, simpledialog

import cartlog
import kiosk_core as kc
import kiosk_ui_core as kui
import kvm_core as kvm
import lamps_core as lc
import serial_panel as sp
from ch9329 import Ch9329
from serial_panel import SerialPanel
from ks_panel import KsPanel
from wifi_panel import WifiPanel
from netprof_panel import NetProfPanel
from wol_panel import WolPanel

FONT = ("Noto Sans CJK SC", 13)
FONT_BIG = ("Noto Sans CJK SC", 15)
FONT_MONO = ("Noto Sans Mono CJK SC", 13)
BOOT_BG = "#ffffff"     # 开机自检页白底, 接 Plymouth 的白底
VIDEO_DEV = "/dev/video0"
IDLE_SECONDS = 180
BOOT_WATCHDOG_MS = 30000    # 自检 30 秒还没完就强制进主界面 (锁屏)
PANE_CLASSES = {"serial": SerialPanel, "ks": KsPanel, "wifi": WifiPanel,
                "netprof": NetProfPanel, "wol": WolPanel}


class Kiosk(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("运维推车控制台")
        # 无 WM 环境: overrideredirect + 手动铺满, 不依赖 -fullscreen
        self.overrideredirect(True)
        self._fill_screen()
        self.configure(bg="#10141c", cursor="arrow")
        self.ui = kui.UiBridge(self)
        kui.main_thread_gc(self)            # 后台线程里不许触发 GC (会析构 Tk 对象等主线程 -> 死锁)
        self.ui.start()
        self.pane = kui.LeftPane(kui.PANES)
        self.panels = {}                    # 面板名 -> 正开着的面板 (预览除外)

        self.mpv = None
        self.video_frame = None
        self.passthrough = False
        self._ssd, self._ssd_ts = None, 0.0
        self._check_rows, self._check_ts, self._checking = None, 0.0, False
        self._fans_seen = set()
        self._combo = kc.ReleaseCombo()
        self._kb = kc.HidKeyboard()
        self.mouse_abs = True               # 默认绝对鼠标; BIOS 里切相对
        self._video_wh = (1920, 1080)
        self._last_abs, self._pending_abs, self._sent_abs = None, None, None
        self._mouse_acc = [0, 0, 0]      # dx, dy, wheel
        self._mouse_btn = 0
        self._center = None              # 相对模式: 指针每帧 warp 回画面中心
        self.chw, self._ch_dev = None, None
        self._kvm_count = 0                 # 键鼠灯的活动计数: 入队成功一次加一
        self._activity, self._lamp_slots, self._active = lc.Activity(), None, set()
        self.kvm = kvm.load()               # 画面源 <-> 键鼠绑定; 进透传时按当前画面源连

        kc.kp_enter_as_return(self)
        kui.kp_digits_as_digits(self)
        self._menu_btns = {}
        self._net_view, self._lock_net = None, None     # 最近一次网络探测; 锁屏上的网络标
        self._build()
        self.gate = kc.PinGate(kc.pin_conf_read(), state_path=kc.PIN_STATE)
        self._lock_win = None
        self._boot_win = None
        self._arm_win = None
        self._code = None
        self._boot_once = kui.Once(self._boot_finish)
        self._exit_once = kui.Once(self._disarm_quietly)
        self._disarm_quietly()          # 开机即复位: PXE 默认只给救援菜单
        self.withdraw()                 # 主界面先藏着: 自检 -> 锁屏之间不能露出来
        self.after(50, self._boot_selftest)
        self._last_input = time.time()
        for seq in ("<Any-KeyPress>", "<Any-Motion>", "<Button>"):
            self.bind_all(seq, self._note_input, add="+")
        # 透传的键盘走全局: Tk 把按键投给有焦点的窗口, 不是嵌着 mpv 的那个 Frame
        self.bind_all("<KeyPress>", lambda e: self.passthrough and self._on_key(e, True), add="+")
        self.bind_all("<KeyRelease>", lambda e: self.passthrough and self._on_key(e, False), add="+")
        self.after(5000, self._idle_tick)
        self.after(2000, self._pxe_tick)
        self.after(100, self.refresh)
        self.ui.repeat_bg(self._lamp_sample, self._lamp_sampled, every=lc.SAMPLE_MS / 1000)
        self.after(lc.SAMPLE_MS // 2, self._lamp_tick)

    def _fill_screen(self):
        self.update_idletasks()
        w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+0+0")

    # ------------------------------------------------------------ UI

    def _build(self):
        top = tk.Frame(self, bg="#10141c")
        top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(top, text="运维推车控制台", font=FONT_BIG,
                 bg="#10141c", fg="#e8eaf0").pack(side="left")
        self.banner = tk.Label(top, text="", font=FONT_BIG, bg="#10141c", fg="#ffb86c")
        self.banner.pack(side="left", padx=20)
        # 网络标: 最右边常驻, 点一下开无线面板; 图标缺了就只显示文字
        self._net_imgs = {}
        for state in ("on", "warn", "off"):
            try:
                self._net_imgs[state] = tk.PhotoImage(file=kc.net_icon(state))
            except tk.TclError:
                pass
        self.net_label = tk.Label(top, text="检查中", font=FONT, bg="#10141c", fg="#6b7489",
                                  compound="left", padx=6, cursor="hand2")
        self.net_label.pack(side="right", padx=(12, 0))
        self.net_label.bind("<Button-1>", lambda _e: self.toggle_pane("wifi"))
        self.clock = tk.Label(top, font=FONT, bg="#10141c", fg="#8b93a7")
        self.clock.pack(side="right")
        tk.Label(top, text=kc.version_string(), font=FONT, bg="#10141c",
                 fg="#5d6873").pack(side="right", padx=16)
        tk.Button(top, text="退出到终端", font=FONT, command=self.quit_kiosk,
                  bg="#2a3347", fg="#e8eaf0", activebackground="#3a4560",
                  relief="flat", padx=14, pady=4, cursor="hand2").pack(
                  side="right", padx=10)

        self._build_lamps()

        body = tk.Frame(self, bg="#10141c")
        body.pack(fill="both", expand=True, padx=12, pady=6)
        self.body = body

        # 左: 状态板 (开了面板/预览就换成它, 同时只有一个, 见 kui.LeftPane)
        self.status_wrap = tk.Frame(body, bg="#161b26")
        self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
        tk.Label(self.status_wrap, text="状态", font=FONT_BIG, bg="#161b26",
                 fg="#8b93a7", anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        self.status_text = tk.Text(self.status_wrap, font=FONT_MONO, bg="#161b26",
                                   fg="#c8cede", relief="flat", wrap="word",
                                   state="disabled", padx=12, pady=6)
        self.status_text.pack(fill="both", expand=True, padx=4, pady=(0, 10))

        btns = tk.Frame(body, bg="#10141c")
        btns.pack(side="right", fill="y")
        self._build_menu(btns)
        nodes = kc.video_nodes()            # 只读 sysfs; 挑有信号的那路要跑 v4l2-ctl, 推迟到自检线程
        self.video_dev = nodes[0]["dev"] if nodes else VIDEO_DEV
        self.btn_src.config(text=f"画面源:  {kc.video_friendly(self.video_dev)}")
        self._show_kvm()

    def _build_menu(self, btns):
        """右侧按钮按 kui.MENU 分页: 每页一个 Frame, 全部先建好, 切页只换显示哪一个."""
        colors = {"warn": ("#4a3320", "#e8eaf0"), "danger": ("#47202a", "#ffb3bd")}
        self.btns_wrap, self.pages = btns, {}

        def big(parent, text, cmd, bg="#1f2a3d", fg="#e8eaf0"):
            b = tk.Button(parent, text=text, font=FONT_BIG, command=cmd, bg=bg,
                          fg=fg, activebackground="#3a4560", relief="flat",
                          cursor="hand2", padx=30, pady=14, width=18)    # 定宽: 切页时左侧不跟着抖
            b.pack(fill="x", pady=6)
            return b

        for name, items in kui.MENU.items():
            page = tk.Frame(btns, bg="#10141c")
            self.pages[name] = page
            if name != "main":
                big(page, "返回主页", lambda: self.show_page("main"), bg="#2a3347", fg="#8b93a7")
            for it in items:
                if "page" in it:
                    cmd = lambda p=it["page"]: self.show_page(p)
                elif "arg" in it:
                    cmd = lambda f=getattr(self, it["do"]), a=it["arg"]: f(a)
                else:
                    cmd = getattr(self, it["do"])
                b = big(page, it["text"], cmd, *colors.get(it.get("style"), ()))
                if "attr" in it:
                    setattr(self, it["attr"], b)
                if "do" in it and "arg" not in it:
                    self._menu_btns[it["do"]] = b
        self.show_page("main")

    def show_page(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="x")

    def reboot(self):
        self.confirm_power(False)

    def poweroff(self):
        self.confirm_power(True)

    # ------------------------------------------------------------ 状态刷新

    CHECK_EVERY = 10        # 自检项 (ping/PCI/nmcli...) 慢一点, 10 s 一轮; 温度风扇照旧 2 s

    def refresh(self):
        """状态板 = 实时自检: 跟开机自检同一张计划表 (selftest.json), 再加温度/风扇.
        采集都在后台 (UiBridge), 上一轮没回来就不再起新的, 线程不会越攒越多."""
        def checks():
            return kc.net_probe(), kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context())

        def checks_done(res):
            self._checking = False
            if not isinstance(res, Exception):
                self._render_net(*res[0])
                self._check_rows = res[1]

        rows = self._check_rows

        def board():
            now = time.time()
            if now - self._ssd_ts > 60:          # smartctl 一分钟读一次就够 (只在这个线程里读写)
                self._ssd, self._ssd_ts = kc.smart_temp(), now
            temps = kc.temps_status(smart=lambda: self._ssd)
            fans = kc.fans_to_show(kc.fans_status(), self._fans_seen)
            return kc.status_board_lines(rows, temps, fans, kc.misc_status()["uptime"])

        if not self._checking and time.time() - self._check_ts > self.CHECK_EVERY:
            self._checking, self._check_ts = True, time.time()
            self.ui.run_bg(checks, checks_done)
        self.ui.run_bg(board, self._render, key="board")
        self.clock.config(text=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.after(2000, self.refresh)

    # ------------------------------------------------------------ 执行器条 (issue #2)

    def _build_lamps(self):
        """顶栏下一行 grandMA3 执行器风格的方块: 左边 4 个串口操作头, 右边键鼠/PXE/网口.
        左边色带 = 自检健康, 整块闪 = 计数器在动."""
        bar = tk.Frame(self, bg="#10141c")
        bar.pack(fill="x", padx=12, pady=(0, 4))
        self._lamps = {}

        def block(key, width, pane):
            f = tk.Frame(bar, bg=lc.EMPTY_BODY)
            f.pack(side="left", padx=(0, 6))
            stripe = tk.Frame(f, bg=lc.HEALTH_COLOR[None], width=5)
            stripe.pack(side="left", fill="y")
            lab = tk.Label(f, text="", font=FONT, width=width, height=2, justify="left", anchor="w",
                           bg=lc.EMPTY_BODY, fg=lc.EMPTY_FG, padx=6, cursor="hand2")
            lab.pack(side="left")
            for w in (f, lab):
                w.bind("<Button-1>", lambda _e: self._open_pane(pane))
            self._lamps[key] = (f, stripe, lab, [None])

        for n in range(1, len(lc.SLOTS) + 1):
            block(f"con-{n}", 8, "serial")
        tk.Frame(bar, bg="#2a3347", width=2).pack(side="left", fill="y", padx=(4, 10))
        pane_of = {"kvm": "preview", "pxe": "ks", "net:eno1": "netprof", "net:enp3s0": "netprof",
                   "net:wlp4s0": "wifi"}
        for name, _label, key in lc.LOWER:
            block(key, 5, pane_of[key])
            self._lamps[key][2].config(text=name)

    def _open_pane(self, name):
        if self.pane.current != name:
            self.toggle_pane(name)

    def _lamp_sample(self):
        """常驻采样线程里跑: 只读文件和 /proc, 不碰 Tk (_kvm_count 是 int, 读一下是原子的)."""
        return lc.sample(sp.data_dir(sp.serial_conf_read()), self._kvm_count)

    def _lamp_tick(self):
        """采样在常驻线程里 (见 __init__), 这里只画; 每半个采样周期重画一次, 闪烁才跟得上."""
        self._lamp_paint()
        self.after(lc.SAMPLE_MS // 2, self._lamp_tick)

    def _lamp_sampled(self, res):
        if isinstance(res, Exception):
            return
        self._lamp_slots, counters = res
        self._active = self._activity.update(counters, time.time())

    def _lamp_paint(self):
        on = lc.blink_on(time.time())
        views = {f"con-{i + 1}": lc.slot_view(i + 1, slot) for i, slot in enumerate(self._lamp_slots or [])}
        for name, label, key in lc.LOWER:
            views[key] = (name, lc.health_of(self._check_rows, label))
        for key, (text, health) in views.items():
            f, stripe, lab, last = self._lamps[key]
            v = lc.lamp_view(health, key in self._active, on)
            state = (text, v["bg"], v["fg"], v["bar"])
            if state == last[0]:
                continue
            last[0] = state
            f.config(bg=v["bg"])
            stripe.config(bg=v["bar"])
            lab.config(text=text, bg=v["bg"], fg=v["fg"])

    def _render_net(self, state, text):
        """顶栏和锁屏的网络标用同一份探测结果, 锁着屏也照常刷新."""
        self._net_view = kui.net_view(state, text)
        for label in (self.net_label, self._lock_net):
            if label is not None:
                self._paint_net(label)

    def _paint_net(self, label):
        v = self._net_view
        label.config(image=self._net_imgs.get(v["image"], ""), text=v["text"], fg=v["fg"])

    def _render(self, lines):
        if isinstance(lines, Exception):
            return
        if self.status_text.winfo_ismapped():
            self.status_text.config(state="normal")
            self.status_text.delete("1.0", "end")
            for text, tag in lines:
                self.status_text.insert("end", text + "\n", tag)
            self.status_text.config(state="disabled")
            self.status_text.tag_config("ok", foreground="#7ee787")
            self.status_text.tag_config("bad", foreground="#ff7b72")
            self.status_text.tag_config("t_ok", foreground="#9aa5b0")
            self.status_text.tag_config("t_warn", foreground="#f0b429")
            self.status_text.tag_config("t_hot", foreground="#ff7b72")
            self.status_text.tag_config("dim", foreground="#6b7489")

    # ------------------------------------------------------------ 左侧面板

    def toggle_pane(self, name):
        """左侧面板 (预览/串口/ks/无线/网口配置/WOL) 同时只开一个; 再点一次回状态板."""
        if name == "preview" and self.pane.current != "preview" and not os.path.exists(self.video_dev):
            messagebox.showwarning("UVC 预览", f"未检测到采集设备 {self.video_dev}")
            return
        close, opening = self.pane.toggle(name)
        self._pane_close(close)
        if opening:
            self._pane_open(opening)
        else:
            self._show_status()

    def _close_pane(self):
        self._pane_close(self.pane.close())
        self._show_status()

    def _pane_close(self, name):
        if name == "preview":
            self._stop_preview()
        elif name:
            w = self.panels.pop(name, None)
            if w is not None:
                w.destroy()

    def _pane_open(self, name):
        self.status_wrap.pack_forget()
        if name == "preview":
            self._start_preview()
            return
        try:
            w = PANE_CLASSES[name](self.body)
        except Exception as ex:                 # noqa: BLE001  面板起不来就回状态板, 不留半截状态
            self.pane.close()
            self._show_status()
            self.banner.config(text=f"面板打不开: {type(ex).__name__}: {ex}")
            return
        w.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.panels[name] = w
        self.banner.config(text=kui.PANES[name])

    def _show_status(self):
        self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.banner.config(text="")

    # ------------------------------------------------------------ UVC 预览

    def switch_source(self):
        """在能出画面的采集节点之间轮换 (跳过 UVC 元数据节点); 正在预览就换源重开."""
        self._release_passthrough()        # 别带着上一路的透传切过去
        self.video_dev = kc.next_video_device([x["dev"] for x in kc.video_nodes()], self.video_dev)
        self.btn_src.config(text=f"画面源:  {kc.video_friendly(self.video_dev)}")
        self._show_kvm()
        if self.video_frame:
            self._stop_preview()
            self._start_preview()

    def _set_video_dev(self, dev):
        """自检线程挑出的默认画面源 (有信号的优先); 用户已经在看预览就不动."""
        if dev and dev != self.video_dev and not self.video_frame:
            self.video_dev = dev
            self.btn_src.config(text=f"画面源:  {kc.video_friendly(dev)}")
            self._show_kvm()

    def _start_preview(self):
        """只由 toggle_pane / switch_source 调. 分辨率 (v4l2-ctl) 后台读, 回来之前按 16:9 算."""
        self.video_frame = tk.Frame(self.body, container=True, bg="#000000")
        self.video_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.video_frame.bind("<Button-1>", lambda e: self._engage_passthrough())
        self.update_idletasks()
        wid = self.video_frame.winfo_id()
        dev = self.video_dev
        self._video_wh = (1920, 1080)

        def got_size(wh):
            if not isinstance(wh, Exception) and self.video_dev == dev:
                self._video_wh = wh                         # 绝对鼠标按真实画面比例扣黑边
        self.ui.run_bg(lambda: kc.video_size(dev), got_size)
        try:
            os.makedirs("/run/ops-cart", exist_ok=True)    # mpv 的 IPC socket 放这 (cart_snap.py 用)
            self.mpv = Popen(kc.mpv_cmd(wid, dev), stdout=DEVNULL, stderr=DEVNULL)
        except OSError as ex:
            self.banner.config(text=f"mpv 起不来: {ex}")
            return
        self._banner_idle()

    def _stop_preview(self):
        self._release_passthrough()
        if self.mpv:
            self.mpv.terminate()
            self.mpv = None
        if self.video_frame:
            self.video_frame.destroy()
            self.video_frame = None

    # ------------------------------------------------------------ 键鼠透传

    def _kvm_state(self):
        """当前画面源绑的键鼠 -> (串口路径, 序列号, ok/unbound/offline)."""
        return kvm.resolve(self.kvm, kc.video_key(self.video_dev), kvm.ports())

    def _close_ch(self):
        if self.chw:
            self.chw.close()
        self.chw, self._ch_dev = None, None

    def _ensure_ch(self):
        """连当前画面源绑定的那块 CH9329 (按 CH343 序列号, 重插后 ttyACMn 变了也认得).
        没绑 / 不在线就不连 —— 绝不退回随便一块. 返回 ok/unbound/offline/error."""
        path, _serial, state = self._kvm_state()
        if state != "ok":
            self._close_ch()
            return state
        dev = os.path.realpath(path)
        if self.chw and not self.chw.dead and dev == self._ch_dev:
            return "ok"
        self._close_ch()
        try:
            ch = Ch9329(dev, log=lambda *a: None)
        except OSError:
            return "error"
        self.chw = kui.ChWorker(ch, on_gone=lambda ex: self.ui.post(self._ch_gone, ex))
        self._ch_dev = dev
        return "ok"

    def _show_kvm(self):
        _path, serial, state = self._kvm_state()
        text = kvm.label(self.kvm, serial) + {"offline": " (不在线)"}.get(state, "")
        self.btn_kvm.config(text=f"键鼠绑定:  {text}")

    def cycle_kvm(self):
        """给当前画面源换绑: 不绑定 -> 在线的第一块 -> 第二块 -> ... (按序列号排)."""
        key = kc.video_key(self.video_dev)
        if not key:
            return messagebox.showwarning("键鼠绑定", "当前没有画面源")
        self._release_passthrough()
        new = kvm.next_choice(sorted(kvm.ports()), self.kvm["bind"].get(key))
        self.kvm = kvm.bind(self.kvm, key, new)
        try:
            kvm.save(self.kvm)
        except OSError as ex:
            return messagebox.showerror("键鼠绑定", f"保存失败: {ex}")
        src = kc.video_friendly(self.video_dev)
        cartlog.event("kvm", f"{src} ({key}) -> {kvm.label(self.kvm, new)} {new or ''}".strip())
        self._show_kvm()
        others = kvm.shared_with(self.kvm, new, key) if new else []
        note = f" · 注意: 这块也绑在 {len(others)} 个别的画面源上" if others else ""
        self.banner.config(text=f"{src} 的键鼠: {kvm.label(self.kvm, new)}{note}")

    def _ch_send(self, fn, *args, droppable=False):
        """只入队, 真正的发送在 ChWorker 线程; 返回是否入队 (满了/断了 = False)."""
        ok = bool(self.chw) and self.chw.send(fn, *args, droppable=droppable)
        self._kvm_count += bool(ok)
        return ok

    def _ch_gone(self, _ex):
        """工作线程报拔线 (句柄已关): 退出透传, 下次单击画面重连."""
        self._close_ch()
        self._release_passthrough()
        self.banner.config(text="CH9329 断开 · 重新单击画面重连")

    def _banner_idle(self):
        _path, serial, state = self._kvm_state()
        src = kc.video_friendly(self.video_dev)
        if state != "ok":
            why = "绑定的键鼠不在线" if state == "offline" else "没绑定键鼠"
            self.banner.config(text=f"预览 {src} · {why}, 只看不操作 · 去「画面设置」里绑定键鼠")
            return
        mode = "绝对" if self.mouse_abs else "相对"
        self.banner.config(text=f"预览 {src} · 键鼠 {kvm.label(self.kvm, serial)} · 鼠标{mode} · 单击画面进入透传")

    def _engage_passthrough(self):
        if self.passthrough:
            return
        state = self._ensure_ch()
        if state in ("unbound", "offline"):
            self._banner_idle()                     # 只看不操作: 横幅说明原因, 不弹窗打断
            return
        if state != "ok":
            messagebox.showwarning("键鼠透传", "绑定的 CH9329 打不开 (被占用或刚拔掉?)")
            return
        self.passthrough = True
        self._combo = kc.ReleaseCombo()
        self._kb = kc.HidKeyboard()
        self._mute_tk_keys()
        self._pending_abs, self._last_abs, self._sent_abs = None, None, None
        self._mouse_acc, self._mouse_btn = [0, 0, 0], 0
        if self.mouse_abs:                  # 绝对: 不锁鼠标不藏光标, 指哪打哪
            self.video_frame.config(cursor="tcross")
        else:                               # 相对 (BIOS 用): 锁住鼠标, 藏本地光标, 指针钉在中心
            self.video_frame.grab_set_global()
            self.video_frame.config(cursor="none")
            self._center = (self.video_frame.winfo_width() // 2, self.video_frame.winfo_height() // 2)
            self._warp_center()
        for seq in ("<Motion>", "<Button-1>", "<Button-2>", "<Button-3>",
                    "<ButtonRelease-1>", "<ButtonRelease-2>", "<ButtonRelease-3>",
                    "<Button-4>", "<Button-5>", "<MouseWheel>"):    # X11 滚轮 = 4/5 号键
            self.video_frame.bind(seq, self._on_passthrough_event)
        self.focus_force()
        mode = "绝对" if self.mouse_abs else "相对"
        self.banner.config(text=f"[透传中] 鼠标{mode} · 按一下 Ctrl+Alt 再松开 = 释放")
        self.after(16, self._flush_mouse)

    def _release_passthrough(self):
        if not self.passthrough:
            return
        self.passthrough = False
        self._restore_tk_keys()
        try:
            self.video_frame.grab_release()
            self.video_frame.config(cursor="arrow")
        except tk.TclError:
            pass
        self._banner_idle()
        self._mouse_acc, self._mouse_btn, self._center = [0, 0, 0], 0, None
        if self.mouse_abs and self._last_abs:
            self._ch_send("send_mouse_abs", 0, *self._last_abs, 0)   # 松开鼠标键, 光标留原地
        else:
            self._ch_send("send_mouse", 0, 0, 0, 0)
        self._ch_send("send_report", self._kb.release_all())         # 松开所有键盘键

    def toggle_mouse_mode(self):
        self._release_passthrough()
        self.mouse_abs = not self.mouse_abs
        self.btn_mouse.config(text="鼠标:  绝对 (点哪是哪)" if self.mouse_abs else "鼠标:  相对 (BIOS 用)")
        if self.video_frame:
            self._banner_idle()

    def _warp_center(self):
        if self._center and self.video_frame:
            self.video_frame.event_generate("<Motion>", warp=True, x=self._center[0], y=self._center[1])

    def _abs_at(self, ev):
        fw, fh = self.video_frame.winfo_width(), self.video_frame.winfo_height()
        return kc.abs_point(ev.x, ev.y, fw, fh, *self._video_wh)

    def _on_passthrough_event(self, ev):
        # 透传时这些事件绑定会一直留在 Frame 上: 释放后左键单击 = 重新进透传, 其余忽略
        # (否则第二次点不进去, 且释放期间的移动会攒成一大步甩给目标机)
        if not self.passthrough:
            if ev.type == "4" and str(ev.num) == "1":
                self._engage_passthrough()
            return
        if ev.type == "6":            # Motion
            if self.mouse_abs:
                self._pending_abs = self._abs_at(ev) or self._pending_abs
            else:
                d = kui.rel_motion((ev.x, ev.y), self._center)
                if d:
                    self._mouse_acc[0] += d[0]
                    self._mouse_acc[1] += d[1]
                    self._warp_center()
            return
        step = kc.wheel_step(ev.type, ev.num, getattr(ev, "delta", 0))
        bit = kc.mouse_bit(ev.num) if ev.type in ("4", "5") else None
        if not step and not bit:
            return
        if not self.mouse_abs:
            if step:
                self._mouse_acc[2] += step
            else:
                self._mouse_btn = self._mouse_btn | bit if ev.type == "4" else self._mouse_btn & ~bit & 0xFF
            self._flush_rel(force=True)                 # 点击/滚轮立刻发, 不等下一帧
            return
        kind = "wheel" if step else ("press" if ev.type == "4" else "release")
        pt = self._abs_at(ev)
        if not kui.abs_should_send(kind, pt, self._last_abs is not None):
            return                                      # 黑边上按下/滚轮: 不发, 免得点到上一个位置
        if kind == "press":
            self._mouse_btn |= bit
        elif kind == "release":
            self._mouse_btn &= ~bit & 0xFF
        self._last_abs = pt or self._last_abs
        self._send_abs(wheel=step)

    def _send_abs(self, wheel=0, droppable=False):
        if self._ch_send("send_mouse_abs", self._mouse_btn, *self._last_abs, wheel, droppable=droppable):
            self._sent_abs = self._last_abs

    def _flush_rel(self, force=False):
        """相对位移每帧最多 ±127, 多的留到下一帧; 位移帧只在工作线程空闲时发 (攒着不丢)."""
        dx, dy, wheel = self._mouse_acc
        if not (dx or dy or wheel or self._mouse_btn or force):
            return
        if not force and not (self.chw and self.chw.idle()):
            return
        send, rest = kui.rel_split(dx, dy, wheel)
        if self._ch_send("send_mouse", self._mouse_btn, *send, droppable=not force):
            self._mouse_acc = list(rest)

    def _on_key(self, ev, down):
        """物理键码同步按住/松开 (X 键码 = evdev + 8), 不看 keysym, 不受 Shift/输入法影响."""
        name = kc.mod_name(ev.keycode) or f"k{ev.keycode}"
        if self._combo.key(name, down):      # Ctrl+Alt 按下又松开、中间没按别的 -> 释放
            self._release_passthrough()
            return
        rep = self._kb.press(ev.keycode) if down else self._kb.release(ev.keycode)
        if rep is not None:
            self._ch_send("send_report", rep)

    # Tk 在 all 标签上默认绑了 Tab/Shift-Tab(切焦点)、Alt+键(跳菜单)、F10(菜单),
    # 比我们的 <KeyPress> 更具体, 会把这些键截走 -> 透传期间临时撤掉, 释放时原样装回
    _TK_STEALS = ("<<NextWindow>>", "<<PrevWindow>>", "<Alt-KeyPress>", "<Key-F10>")

    def _mute_tk_keys(self):
        self._tk_saved = {seq: self.tk.call("bind", "all", seq) for seq in self._TK_STEALS}
        for seq in self._TK_STEALS:
            self.tk.call("bind", "all", seq, "")

    def _restore_tk_keys(self):
        for seq, script in getattr(self, "_tk_saved", {}).items():
            self.tk.call("bind", "all", seq, script)
        self._tk_saved = {}

    def _flush_mouse(self):
        if not self.passthrough:
            return
        if self.mouse_abs:
            pt, self._pending_abs = self._pending_abs, None
            if pt and pt != self._sent_abs:
                self._last_abs = pt
                self._send_abs(droppable=True)      # 移动帧可丢: 下一帧会带上最新位置
        else:
            self._flush_rel()
        self.after(16, self._flush_mouse)

    # ------------------------------------------------------------ 原有动作

    def _bg_action(self, key, work, done, note="处理中…"):
        """菜单动作的外部命令放后台: 横幅显示 note, 对应按钮置灰; 上一次没回来就不重复起."""
        btn = self._menu_btns.get(key)

        def finish(res):
            if btn is not None:
                btn.config(state="normal")
            done(res)
        if not self.ui.run_bg(work, finish, key=key):
            self.banner.config(text="上一次还在处理中…")
            return
        self.banner.config(text=note)
        if btn is not None:
            btn.config(state="disabled")

    def toggle_pxe(self):
        units = ("pxe-server", "pxe-http-repo", "pxe-http-boot")

        def asked(res):
            if isinstance(res, Exception):
                return self.banner.config(text=f"读不到 PXE 服务状态: {res}")
            active = res["pxe-server"][1]
            verb = "停止" if active else "启动"
            self.banner.config(text="")
            if not messagebox.askyesno("确认", f"确定要{verb} PXE 装机服务?"):
                return
            cmd = ["systemctl", "stop" if active else "start", *units]
            self._bg_action("toggle_pxe", lambda: kc.run(cmd, timeout=30), lambda r: self.banner.config(
                text=f"PXE 装机服务已{verb}" if not isinstance(r, Exception) and r[0] == 0
                else f"PXE 装机服务{verb}失败, 详见 journalctl"), f"正在{verb} PXE 装机服务…")
        self._bg_action("toggle_pxe", lambda: kc.services_status(("pxe-server",)), asked, "读取服务状态…")

    def cfg_lan2(self):
        warn = ("注意: LAN2 是管理口(远程/SSH/tailscale 走这里)!\n"
                "修改后当前远程连接会断, 请确保你人在车边操作.\n\n要继续吗?")
        if not messagebox.askyesno("LAN2 管理口", warn, icon="warning"):
            return

        def apply(cmds):
            for cmd in cmds:
                rc, _ = kc.run(cmd, timeout=30)
                if rc:
                    return f"失败: {' '.join(cmd[:4])} 返回 {rc}"
            return "已下发, 状态见左侧(远程会断属正常)"

        def got(res):
            self.banner.config(text="")
            nic = None if isinstance(res, Exception) else next((n for n in res if n["name"] == "enp3s0"), None)
            if nic is None:
                return messagebox.showerror("LAN2", "读不到 enp3s0 的状态")
            if messagebox.askyesno("LAN2", f"当前: {nic['ip']} ({nic['method']})\n\n改为 DHCP(自动获取)?"):
                cmds = kc.nmcli_set_dhcp(kc.LAN2_CON)
            else:
                ip = simpledialog.askstring("静态 IP", "IP/前缀:", initialvalue=nic["ip"])
                if not ip:
                    return
                gw = simpledialog.askstring("网关", "网关(可空):")
                dns = simpledialog.askstring("DNS", "DNS(可空):")
                cmds = kc.nmcli_set_static(kc.LAN2_CON, ip, gw or "", dns or "")
            self._bg_action("cfg_lan2", lambda: apply(cmds), lambda r: messagebox.showinfo(
                "LAN2", str(r)), "正在下发 LAN2 配置…")
        self._bg_action("cfg_lan2", kc.nics_status, got, "读取网口状态…")

    def confirm_power(self, off):
        verb = "关机" if off else "重启"
        if not self._confirm_pin(verb):
            return
        if messagebox.askyesno("确认", f"确定要{verb}推车?", icon="warning"):
            self._disarm_quietly()
            self.ui.run_bg(lambda: kc.power(off))

    # ------------------------------------------------------------ 开机自检(街机风)

    def _boot_selftest(self):
        """全屏: Logo/标题 + 自检逐行出现; 完了进锁屏. 换皮见 /opt/pxe/branding/."""
        b = kc.branding_read()
        w = tk.Toplevel(self, bg=BOOT_BG)
        w.overrideredirect(True)
        w.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        w.attributes("-topmost", True)
        self._boot_win = w
        top = tk.Frame(w, bg=BOOT_BG)
        top.place(relx=0.5, rely=0.22, anchor="center")
        if b["logo"]:
            try:
                img = tk.PhotoImage(file=b["logo"])
                k = max(1, -(-img.width() // 320))     # 缩到 320 px 以内
                self._boot_logo = img.subsample(k, k) if k > 1 else img
                tk.Label(top, image=self._boot_logo, bg=BOOT_BG).pack(pady=(0, 10))
            except tk.TclError:
                pass
        tk.Label(top, text=b["title"], font=("Noto Sans CJK SC", 40, "bold"),
                 bg=BOOT_BG, fg="#14161a").pack()
        tk.Label(top, text=b["subtitle"], font=FONT_BIG, bg=BOOT_BG, fg="#6b7280").pack()
        tk.Label(top, text=kc.version_string(), font=FONT_MONO, bg=BOOT_BG, fg="#9ca3af").pack(pady=(6, 0))
        box = tk.Frame(w, bg=BOOT_BG)
        box.place(relx=0.5, rely=0.43, anchor="n")     # 从标题下方往下排: 检查项多了也不盖住标题
        tk.Label(box, text="Step 3/3   System check", font=FONT_BIG, bg=BOOT_BG,
                 fg="#2a2e36", anchor="w").pack(anchor="w", pady=(0, 10))   # 接 Plymouth 的 1/3 2/3

        def work():
            rows = kui.boot_rows_safe(lambda: kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context()))
            try:
                video = kc.default_video(kc.video_nodes())
            except Exception:                   # noqa: BLE001  挑不出就用 _build 时的第一路
                video = None
            return rows, video

        def done(res):
            rows, video = (kui.fail_rows(res), None) if isinstance(res, Exception) else res
            self._set_video_dev(video)
            self._boot_show(box, rows, 0)

        self.after(BOOT_WATCHDOG_MS, self._boot_done)   # 看门狗: 自检卡住也要能进锁屏
        self.ui.run_bg(work, done)

    def _boot_show(self, box, rows, i):
        if self._boot_once.done:                # 看门狗已经放行, 自检页没了
            return
        if i < len(rows):
            r = rows[i]
            color = {"OK": "#15803d", "WARN": "#b45309", "FAIL": "#dc2626"}.get(r["state"], "#9ca3af")
            tk.Label(box, text=kc.selftest_line(r, width=26), font=FONT_MONO, bg=BOOT_BG,
                     fg=color, anchor="w", width=56).pack(anchor="w")
            self.after(300, lambda: self._boot_show(box, rows, i + 1))
            return
        failed = any(r["state"] == "FAIL" for r in rows)
        tk.Label(box, text="SELF CHECK FAILED" if failed else "SYSTEM READY", font=FONT_BIG,
                 bg=BOOT_BG, fg="#dc2626" if failed else "#15803d").pack(pady=(14, 0))
        self.after(6000 if failed else 2000, self._boot_done)

    def _boot_done(self):
        """幂等: 正常结束和看门狗谁先到算谁."""
        self._boot_once()

    def _boot_finish(self):
        """先盖锁屏, 再撤自检页, 最后在锁屏底下放出主界面 -- 中间不留空档."""
        self._last_input = time.time()
        if self.gate.required:
            self.lock()
        if self._boot_win:
            self._boot_win.destroy()
            self._boot_win = None
        self.deiconify()
        if self._lock_win:
            self._lock_win.lift()

    # ------------------------------------------------------------ PIN 锁屏

    def lock(self):
        """全屏锁屏; 已锁则只提到最前."""
        if self._lock_win:
            self._lock_win.lift()
            return
        self._release_passthrough()
        w = tk.Toplevel(self, bg="#10141c")
        w.overrideredirect(True)
        w.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        w.attributes("-topmost", True)
        # 上: 时钟; 中: 公告牌(/opt/pxe/notice.txt, 推车客串引导牌); 下: PIN
        self._lock_clock = tk.Label(w, font=("Noto Sans CJK SC", 64), bg="#10141c", fg="#e8eaf0")
        self._lock_clock.place(relx=0.5, rely=0.13, anchor="center")
        self._lock_date = tk.Label(w, font=FONT_BIG, bg="#10141c", fg="#8b93a7")
        self._lock_date.place(relx=0.5, rely=0.23, anchor="center")
        self._lock_notice = tk.Label(w, font=("Noto Sans CJK SC", 28, "bold"), bg="#10141c", fg="#ffb86c",
                                     justify="center", wraplength=int(self.winfo_screenwidth() * 0.92))
        self._lock_notice.place(relx=0.5, rely=0.48, anchor="center")
        # 右上角网络标 (#1): 只看不点, 锁着屏不开无线面板
        self._lock_net = tk.Label(w, text="检查中", font=FONT, bg="#10141c", fg="#6b7489",
                                  compound="left", padx=6)
        self._lock_net.place(relx=0.985, rely=0.02, anchor="ne")
        if self._net_view:
            self._paint_net(self._lock_net)
        box = tk.Frame(w, bg="#10141c")
        box.place(relx=0.5, rely=0.8, anchor="center")
        tk.Label(box, text="运维推车已锁定", font=FONT_BIG, bg="#10141c",
                 fg="#e8eaf0").pack(pady=(0, 12))
        self._pin_var = tk.StringVar()
        e = tk.Entry(box, textvariable=self._pin_var, show="*", font=("Noto Sans Mono CJK SC", 22),
                     width=14, justify="center", bg="#1f2a3d", fg="#e8eaf0",
                     insertbackground="#e8eaf0", relief="flat")
        e.pack(ipady=8)
        e.bind("<Return>", lambda _e: self._try_unlock())
        self._lock_msg = tk.Label(box, text="输入 PIN 后回车", font=FONT, bg="#10141c", fg="#8b93a7")
        self._lock_msg.pack(pady=12)
        self._lock_win = w
        self._lock_tick()
        w.after(100, lambda: (w.focus_force(), e.focus_set()))
        try:
            w.grab_set()
        except tk.TclError:
            pass

    def lock_now(self):
        if not self.gate.required:
            messagebox.showinfo("锁屏", "还没设 PIN, 先在终端跑 sudo kiosk-setpin")
            return
        self.lock()

    def _lock_tick(self):
        """锁屏每秒刷新时钟和公告; 解锁后自动停."""
        if not self._lock_win:
            return
        self._lock_clock.config(text=time.strftime("%H:%M"))
        self._lock_date.config(text=time.strftime("%Y-%m-%d  星期") + "一二三四五六日"[time.localtime().tm_wday])
        text = kc.notice_read()
        if text != self._lock_notice.cget("text"):     # 字号自适应: 时钟和 PIN 之间那块塞满
            px = kc.notice_font_px(text, self.winfo_screenwidth() * 0.9, self.winfo_screenheight() * 0.36)
            self._lock_notice.config(text=text, font=("Noto Sans CJK SC", -px, "bold"))
        self._lock_win.after(1000, self._lock_tick)

    def _try_unlock(self):
        pin = self._pin_var.get()
        self._pin_var.set("")
        r = self.gate.attempt(pin)
        if r == "ok":
            self._lock_win.destroy()
            self._lock_win, self._lock_net = None, None
            self._last_input = time.time()
        elif r == "locked":
            left = max(1, int(self.gate.locked_until - time.time()))
            self._lock_msg.config(text=f"错误次数过多，{left} 秒后再试", fg="#ff7b72")
        else:
            self._lock_msg.config(text="PIN 不对", fg="#ff7b72")

    def _confirm_pin(self, what):
        if not self.gate.required:
            return True
        pin = simpledialog.askstring("验证 PIN", f"{what}前请输入 PIN:", show="*", parent=self)
        if pin is None:
            return False
        r = self.gate.attempt(pin)
        if r != "ok":
            messagebox.showerror("PIN", "PIN 不对" if r == "bad" else "错误次数过多，稍后再试")
        return r == "ok"

    # ------------------------------------------------------------ 闲置自动锁屏

    def _note_input(self, _event=None):
        self._last_input = time.time()

    def _idle_tick(self):
        """闲置 IDLE_SECONDS 直接进锁屏 (时钟 + 公告 + PIN), 不再放屏保盖住公告."""
        if self.gate.required and kc.screensaver_due(self._last_input, time.time(), IDLE_SECONDS,
                                                     showing=self._lock_win is not None):
            self.lock()
        self.after(5000, self._idle_tick)

    # ------------------------------------------------------------ 装机模式

    def toggle_install_mode(self):
        st = kc.pxe_mode_read()
        if st["mode"] == "install":
            if messagebox.askyesno("装机模式", f"提前上锁? (当前只对 {st['mac']} 开放装机)"):
                if not self._disarm_quietly():
                    messagebox.showerror("装机模式", "上锁失败 (写不了 tftp 目录?), 请在终端检查")
                self._pxe_tick(reschedule=False)
            return
        self._arm_dialog()

    def _arm_dialog(self):
        """解锁: 选一台机器 + 照抄屏幕上的随机码. 码只显示在推车屏幕上, 远程开不了.
        同时只有一个对话框 (再点只提到最前); 抢键盘焦点; 输错原地换码, 提示保留."""
        if self._arm_win is not None and self._arm_win.winfo_exists():
            self._arm_win.lift()
            self._arm_win.focus_force()
            return
        self._code = kc.new_code()
        w = tk.Toplevel(self, bg="#161b26", highlightthickness=3, highlightbackground="#7a1f2b")
        self._arm_win = w
        w.overrideredirect(True)
        W, H = 780, 760
        w.geometry(f"{W}x{H}+{(self.winfo_screenwidth() - W) // 2}+{(self.winfo_screenheight() - H) // 2}")
        w.attributes("-topmost", True)
        tk.Label(w, text="解锁装机模式", font=FONT_BIG, bg="#161b26", fg="#e8eaf0").pack(pady=(18, 4))
        tk.Label(w, text="选定的机器下次 PXE 会自动安装 Rocky 并清空整块硬盘 (clearpart --all)。\n"
                         "只对这一台开放, 30 分钟或它取走装机菜单后自动上锁。",
                 font=FONT, bg="#161b26", fg="#ff7b72", justify="center").pack(pady=(0, 10))
        tk.Label(w, text="最近来要过地址的机器 (或在下面手填 MAC):", font=FONT, bg="#161b26",
                 fg="#8b93a7").pack(anchor="w", padx=24)
        lb = tk.Listbox(w, font=FONT_MONO, height=5, bg="#1f2a3d", fg="#e8eaf0", relief="flat",
                        selectbackground="#3a4560", exportselection=False)
        for mac in kc.recent_pxe_clients(self._pxe_log_tail()):
            lb.insert("end", mac)
        if lb.size():
            lb.selection_set(0)
        lb.pack(fill="x", padx=24)
        mac_var = tk.StringVar()
        tk.Entry(w, textvariable=mac_var, font=FONT_MONO, bg="#1f2a3d", fg="#e8eaf0",
                 insertbackground="#e8eaf0", relief="flat").pack(fill="x", padx=24, pady=(6, 12), ipady=4)
        tk.Label(w, text="照抄验证码:", font=FONT, bg="#161b26", fg="#8b93a7").pack()
        code_lbl = tk.Label(w, text=" ".join(self._code), font=("Noto Sans Mono CJK SC", 40, "bold"),
                            bg="#161b26", fg="#f0b429")
        code_lbl.pack()
        code_var = tk.StringVar()
        e = tk.Entry(w, textvariable=code_var, font=("Noto Sans Mono CJK SC", 22), width=10,
                     justify="center", bg="#1f2a3d", fg="#e8eaf0", insertbackground="#e8eaf0", relief="flat")
        e.pack(pady=8, ipady=6)
        msg = tk.Label(w, text="", font=FONT, bg="#161b26", fg="#ff7b72")
        msg.pack()

        def close():
            self._code = None
            self._arm_win = None
            w.destroy()

        def ok(_e=None):
            sel = lb.curselection()
            mac = mac_var.get().strip() or (lb.get(sel[0]) if sel else "")
            if not kc.code_matches(code_var.get(), self._code or ""):
                self._code = kc.new_code()          # 输错就换码, 不给试
                code_lbl.config(text=" ".join(self._code))
                code_var.set("")
                msg.config(text="验证码不对, 已换新码, 请照抄上面的新码")
                e.focus_set()
                return
            try:
                kc.pxe_arm(mac)
            except (ValueError, OSError) as ex:
                msg.config(text=str(ex))
                return
            close()
            self._pxe_tick(reschedule=False)

        row = tk.Frame(w, bg="#161b26")
        row.pack(pady=10)
        tk.Button(row, text="解锁", font=FONT_BIG, command=ok, bg="#47202a", fg="#ffb3bd",
                  relief="flat", padx=24).pack(side="left", padx=8)
        tk.Button(row, text="取消", font=FONT_BIG, command=close, bg="#2a3347", fg="#e8eaf0",
                  relief="flat", padx=24).pack(side="left", padx=8)
        e.bind("<Return>", ok)
        w.after(100, lambda: (w.focus_force(), e.focus_set()))
        try:
            w.grab_set()                            # 对话框开着时主界面点不动
        except tk.TclError:
            pass

    @staticmethod
    def _pxe_log_tail(n=256 * 1024):
        try:
            with open(kc.PXE_LOG, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - n))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _pxe_tick(self, reschedule=True):
        """到期 / 选定机器取走了装机菜单 -> 自动上锁; 按钮显示当前状态. 出什么错都要排下一轮."""
        try:
            st = kc.pxe_mode_read()
            log = self._pxe_log_tail() if st["mode"] == "install" else ""
            d = kui.pxe_tick_decide(st, log, time.time())
            if d["disarm"]:
                kc.pxe_disarm()
            if d["banner"]:
                self.banner.config(text=d["banner"])
            sub, main = kui.pxe_button_texts(d["view"])
            danger = d["view"]["mode"] == "install"     # 解锁是危险状态, 主页也得看得见
            colors = {"bg": "#7a1f2b", "fg": "#ffffff"} if danger else {"bg": "#1f2a3d", "fg": "#e8eaf0"}
            self.btn_install.config(text=sub, **colors)
            self.btn_install_page.config(text=main, **colors)
        except Exception as ex:                         # noqa: BLE001
            print(f"_pxe_tick: {ex!r}", file=sys.stderr)
        finally:
            if reschedule:
                self.after(2000, self._pxe_tick)

    def _disarm_quietly(self):
        """装机模式上锁; 失败只记 stderr (开机/退出路径上不能因为它卡住). -> 是否成功."""
        try:
            kc.pxe_disarm()
            return True
        except OSError as ex:
            print(f"pxe_disarm: {ex!r}", file=sys.stderr)
            return False

    def quit_kiosk(self):
        if not self._confirm_pin("退出到终端"):
            return
        self.destroy()

    def destroy(self):
        """不管怎么退 (按钮/窗口被关), 先上锁装机模式, 再停预览和键鼠线程."""
        self._exit_once()
        self._pane_close(self.pane.close())
        self._close_ch()
        self.ui.stop()
        super().destroy()


if __name__ == "__main__":
    app = Kiosk()
    app.mainloop()
