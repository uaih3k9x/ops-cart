#!/usr/bin/env python3
"""运维推车 Kiosk GUI v2 (Tkinter + X, 无 WM 直跑).

v2: overrideredirect 全屏铺满; UVC 预览(mpv 嵌入); CH9329 键鼠透传
(点击画面捕获, Ctrl+Alt 释放, VMware 风格). 终端 root 敲 `kiosk` 启动.
"""

import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

import cartlog
import kiosk_core as kc
import kvm_core as kvm
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


class Kiosk(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("运维推车控制台")
        # 无 WM 环境: overrideredirect + 手动铺满, 不依赖 -fullscreen
        self.overrideredirect(True)
        self._fill_screen()
        self.configure(bg="#10141c", cursor="arrow")

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
        self._last_abs, self._abs_pos, self._sent_abs = None, None, None
        self._mouse_acc = [0, 0, 0]      # dx, dy, wheel
        self._mouse_btn = 0
        self._last_pos = None
        self.ch, self._ch_dev = None, None
        self.kvm = kvm.load()               # 画面源 <-> 键鼠绑定; 进透传时按当前画面源连

        kc.kp_enter_as_return(self)
        self._build()
        self.gate = kc.PinGate(kc.pin_conf_read())
        self._lock_win = None
        self._boot_win = None
        self._code = None
        try:
            kc.pxe_disarm()             # 开机即复位: PXE 默认只给救援菜单
        except OSError:
            pass
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
        self.net_label.bind("<Button-1>", lambda _e: self.toggle_wifi())
        self.clock = tk.Label(top, font=FONT, bg="#10141c", fg="#8b93a7")
        self.clock.pack(side="right")
        tk.Label(top, text=kc.version_string(), font=FONT, bg="#10141c",
                 fg="#5d6873").pack(side="right", padx=16)
        tk.Button(top, text="退出到终端", font=FONT, command=self.quit_kiosk,
                  bg="#2a3347", fg="#e8eaf0", activebackground="#3a4560",
                  relief="flat", padx=14, pady=4, cursor="hand2").pack(
                  side="right", padx=10)

        body = tk.Frame(self, bg="#10141c")
        body.pack(fill="both", expand=True, padx=12, pady=6)
        self.body = body

        # 左: 状态板 (预览/串口模式下换成对应面板)
        self.status_wrap = tk.Frame(body, bg="#161b26")
        self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.serial_panel = None
        tk.Label(self.status_wrap, text="状态", font=FONT_BIG, bg="#161b26",
                 fg="#8b93a7", anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        self.status_text = tk.Text(self.status_wrap, font=FONT_MONO, bg="#161b26",
                                   fg="#c8cede", relief="flat", wrap="word",
                                   state="disabled", padx=12, pady=6)
        self.status_text.pack(fill="both", expand=True, padx=4, pady=(0, 10))

        btns = tk.Frame(body, bg="#10141c")
        btns.pack(side="right", fill="y")
        self._build_menu(btns)
        self.video_dev = kc.default_video(kc.video_nodes()) or VIDEO_DEV
        self.btn_src.config(text=f"画面源:  {kc.video_friendly(self.video_dev)}")
        self._show_kvm()

    def _build_menu(self, btns):
        """右侧按钮按 kc.MENU 分页: 每页一个 Frame, 全部先建好, 切页只换显示哪一个."""
        colors = {"warn": ("#4a3320", "#e8eaf0"), "danger": ("#47202a", "#ffb3bd")}
        self.btns_wrap, self.pages = btns, {}

        def big(parent, text, cmd, bg="#1f2a3d", fg="#e8eaf0"):
            b = tk.Button(parent, text=text, font=FONT_BIG, command=cmd, bg=bg,
                          fg=fg, activebackground="#3a4560", relief="flat",
                          cursor="hand2", padx=30, pady=14, width=18)    # 定宽: 切页时左侧不跟着抖
            b.pack(fill="x", pady=6)
            return b

        for name, items in kc.MENU.items():
            page = tk.Frame(btns, bg="#10141c")
            self.pages[name] = page
            if name != "main":
                big(page, "←  返回", lambda: self.show_page("main"), bg="#2a3347", fg="#8b93a7")
            for it in items:
                if "page" in it:
                    cmd = lambda p=it["page"]: self.show_page(p)
                else:
                    cmd = getattr(self, it["do"])
                b = big(page, it["text"], cmd, *colors.get(it.get("style"), ()))
                if "attr" in it:
                    setattr(self, it["attr"], b)
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
        """状态板 = 实时自检: 跟开机自检同一张计划表 (selftest.json), 再加温度/风扇."""
        def checks():
            try:
                net = kc.net_probe()
                self.after(0, lambda: self._render_net(*net))
                self._check_rows = kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context())
            finally:
                self._checking = False

        def work():
            now = time.time()
            if now - self._ssd_ts > 60:          # smartctl 一分钟读一次就够
                self._ssd, self._ssd_ts = kc.smart_temp(), now
            temps = kc.temps_status(smart=lambda: self._ssd)
            fans = kc.fans_to_show(kc.fans_status(), self._fans_seen)
            lines = kc.status_board_lines(self._check_rows, temps, fans, kc.misc_status()["uptime"])
            self.after(0, lambda: self._render(lines))

        if not self._checking and time.time() - self._check_ts > self.CHECK_EVERY:
            self._checking, self._check_ts = True, time.time()
            threading.Thread(target=checks, daemon=True).start()
        threading.Thread(target=work, daemon=True).start()
        self.clock.config(text=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.after(2000, self.refresh)

    NET_COLORS = {"on": "#7ee787", "warn": "#f0b429", "off": "#ff7b72"}

    def _render_net(self, state, text):
        self.net_label.config(image=self._net_imgs.get(state, ""), text=text,
                              fg=self.NET_COLORS[state])

    def _render(self, lines):
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

    def toggle_serial(self):
        if self.serial_panel:
            self._stop_preview()          # 互斥: 预览与串口不同时占左侧
            self.serial_panel.destroy()
            self.serial_panel = None
            self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="")
        else:
            self._stop_preview()
            self._close_net_panels()
            self.status_wrap.pack_forget()
            self.serial_panel = SerialPanel(self.body)
            self.serial_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="串口会话 · conbroker (display 类只读命令可直接发, 改配置先问 op)")

    def toggle_ks(self):
        if getattr(self, "ks_panel", None):
            self._stop_preview()
            if self.serial_panel:
                self.toggle_serial()
            self.ks_panel.destroy()
            self.ks_panel = None
            self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="")
        else:
            self._stop_preview()
            if self.serial_panel:
                self.toggle_serial()
            self._close_net_panels()
            self.status_wrap.pack_forget()
            self.ks_panel = KsPanel(self.body)
            self.ks_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="Kickstart 编辑 · 保存自动备份, 修改立即对下次装机生效")

    def _close_net_panels(self):
        """无线 / 网口配置文件 / 网络唤醒几个网络面板, 开着哪个关哪个."""
        for attr in ("wifi_panel", "netprof_panel", "wol_panel"):
            if getattr(self, attr, None):
                getattr(self, attr).destroy()
                setattr(self, attr, None)
                self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
                self.banner.config(text="")

    def toggle_wifi(self):
        if getattr(self, "wifi_panel", None):
            self._close_net_panels()
            return
        self._close_net_panels()
        self._stop_preview()
        if self.serial_panel:
            self.toggle_serial()
        if getattr(self, "ks_panel", None):
            self.toggle_ks()
        self.status_wrap.pack_forget()
        self.wifi_panel = WifiPanel(self.body)
        self.wifi_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.banner.config(text="无线网络 · 优先级从上往下自动连, 再点一次按钮返回")

    def toggle_netprof(self):
        if getattr(self, "netprof_panel", None):
            self._close_net_panels()
            return
        self._close_net_panels()
        self._stop_preview()
        if self.serial_panel:
            self.toggle_serial()
        if getattr(self, "ks_panel", None):
            self.toggle_ks()
        self.status_wrap.pack_forget()
        self.netprof_panel = NetProfPanel(self.body)
        self.netprof_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.banner.config(text="网口配置文件 · 切换 / 回退 / 新建, 再点一次按钮返回")

    def toggle_wol(self):
        if getattr(self, "wol_panel", None):
            self._close_net_panels()
            return
        self._close_net_panels()
        self._stop_preview()
        if self.serial_panel:
            self.toggle_serial()
        if getattr(self, "ks_panel", None):
            self.toggle_ks()
        self.status_wrap.pack_forget()
        self.wol_panel = WolPanel(self.body)
        self.wol_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.banner.config(text="网络唤醒 · 双击清单里的机器直接唤醒, 再点一次按钮返回")

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

    def toggle_preview(self):
        if self.video_frame:
            self._stop_preview()
        else:
            self._start_preview()

    def _start_preview(self):
        if not os.path.exists(self.video_dev):
            messagebox.showwarning("UVC 预览", f"未检测到采集设备 {self.video_dev}")
            return
        self._close_net_panels()
        self.status_wrap.pack_forget()
        self.video_frame = tk.Frame(self.body, container=True, bg="#000000")
        self.video_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.video_frame.bind("<Button-1>", lambda e: self._engage_passthrough())
        self.update_idletasks()
        wid = self.video_frame.winfo_id()
        self._video_wh = kc.video_size(self.video_dev)     # 绝对鼠标按真实画面比例扣黑边
        os.makedirs("/run/ops-cart", exist_ok=True)        # mpv 的 IPC socket 放这 (cart_snap.py 用)
        self.mpv = subprocess.Popen(
            kc.mpv_cmd(wid, self.video_dev),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._banner_idle()

    def _stop_preview(self):
        self._release_passthrough()
        if self.mpv:
            self.mpv.terminate()
            self.mpv = None
        if self.video_frame:
            self.video_frame.destroy()
            self.video_frame = None
        self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.banner.config(text="")

    # ------------------------------------------------------------ 键鼠透传

    def _kvm_state(self):
        """当前画面源绑的键鼠 -> (串口路径, 序列号, ok/unbound/offline)."""
        return kvm.resolve(self.kvm, kc.video_key(self.video_dev), kvm.ports())

    def _close_ch(self):
        if self.ch:
            try:
                self.ch.close()
            except OSError:
                pass
        self.ch, self._ch_dev = None, None

    def _ensure_ch(self):
        """连当前画面源绑定的那块 CH9329 (按 CH343 序列号, 重插后 ttyACMn 变了也认得).
        没绑 / 不在线就不连 —— 绝不退回随便一块. 返回 ok/unbound/offline/error."""
        path, _serial, state = self._kvm_state()
        if state != "ok":
            self._close_ch()
            return state
        dev = os.path.realpath(path)
        if self.ch and dev == self._ch_dev:
            return "ok"
        self._close_ch()
        try:
            self.ch, self._ch_dev = Ch9329(dev, log=lambda *a: None), dev
        except OSError:
            return "error"
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
        cartlog.event("kvm", f"{src} ({key}) → {kvm.label(self.kvm, new)} {new or ''}".strip())
        self._show_kvm()
        others = kvm.shared_with(self.kvm, new, key) if new else []
        note = f" · 注意: 这块也绑在 {len(others)} 个别的画面源上" if others else ""
        self.banner.config(text=f"{src} 的键鼠: {kvm.label(self.kvm, new)}{note}")

    def _ch_send(self, fn, *args, **kw):
        """发送失败(线被拔)就丢掉句柄, 下次进透传再重连; 不让异常打断 Tk 的定时循环."""
        if not self.ch:
            return
        try:
            getattr(self.ch, fn)(*args, **kw)
        except OSError:
            self.ch, self._ch_dev = None, None
            self.banner.config(text="CH9329 断开 · 重新单击画面重连")

    def _banner_idle(self):
        _path, serial, state = self._kvm_state()
        src = kc.video_friendly(self.video_dev)
        if state != "ok":
            why = "绑定的键鼠不在线" if state == "offline" else "没绑定键鼠"
            self.banner.config(text=f"预览 {src} · {why}, 只看不操作 · 画面设置 ▸ 键鼠绑定")
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
        self._last_pos, self._abs_pos, self._last_abs = None, None, None
        self._mouse_acc, self._mouse_btn = [0, 0, 0], 0
        if self.mouse_abs:                  # 绝对: 不锁鼠标不藏光标, 指哪打哪
            self.video_frame.config(cursor="tcross")
        else:                               # 相对 (BIOS 用): 锁住鼠标, 藏本地光标
            self.video_frame.grab_set_global()
            self.video_frame.config(cursor="none")
        for seq in ("<Motion>", "<Button-1>", "<Button-2>", "<Button-3>",
                    "<ButtonRelease-1>", "<ButtonRelease-2>", "<ButtonRelease-3>",
                    "<Button-4>", "<Button-5>", "<MouseWheel>"):    # X11 滚轮 = 4/5 号键
            self.video_frame.bind(seq, self._on_passthrough_event)
        self.focus_force()
        mode = "绝对" if self.mouse_abs else "相对"
        self.banner.config(text=f"🔒 透传中 · 鼠标{mode} · 按一下 Ctrl+Alt 再松开 = 释放")
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
        self._mouse_acc, self._mouse_btn = [0, 0, 0], 0
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

    def _on_passthrough_event(self, ev):
        # 透传时这些事件绑定会一直留在 Frame 上: 释放后左键单击 = 重新进透传, 其余忽略
        # (否则第二次点不进去, 且释放期间的移动会攒成一大步甩给目标机)
        if not self.passthrough:
            if ev.type == "4" and str(ev.num) == "1":
                self._engage_passthrough()
            return
        if ev.type == "6":            # Motion
            if self.mouse_abs:
                self._abs_pos = (ev.x, ev.y)
            else:
                pos = (ev.x_root, ev.y_root)
                if self._last_pos:
                    self._mouse_acc[0] += pos[0] - self._last_pos[0]
                    self._mouse_acc[1] += pos[1] - self._last_pos[1]
                self._last_pos = pos
        elif step := kc.wheel_step(ev.type, ev.num, getattr(ev, "delta", 0)):
            self._mouse_acc[2] += step
            if self.mouse_abs:
                self._send_abs(force=True)          # 滚一格立刻发, 不等下一帧
        elif ev.type in ("4", "5") and kc.mouse_bit(ev.num):   # ButtonPress / Release
            bit = kc.mouse_bit(ev.num)
            if ev.type == "4":
                self._mouse_btn |= bit
            else:
                self._mouse_btn &= ~bit & 0xFF
            if self.mouse_abs:
                self._abs_pos = (ev.x, ev.y)
                self._send_abs(force=True)          # 点击立刻发, 不等下一帧

    def _send_abs(self, force=False):
        wheel = self._mouse_acc[2]
        if self._abs_pos:
            fw, fh = self.video_frame.winfo_width(), self.video_frame.winfo_height()
            pt = kc.abs_point(*self._abs_pos, fw, fh, *self._video_wh)
            if pt:
                self._last_abs = pt
            self._abs_pos = None
        if self._last_abs and (force or wheel or self._last_abs != self._sent_abs):
            self._ch_send("send_mouse_abs", self._mouse_btn, *self._last_abs, wheel)
            self._sent_abs = self._last_abs
            self._mouse_acc[2] = 0

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
            self._send_abs()
        else:
            dx, dy, wheel = self._mouse_acc
            if dx or dy or wheel or self._mouse_btn:
                self._ch_send("send_mouse", self._mouse_btn, dx, dy, wheel)
                self._mouse_acc = [0, 0, 0]
        self.after(16, self._flush_mouse)

    # ------------------------------------------------------------ 原有动作

    def toggle_pxe(self):
        _, active = kc.services_status(("pxe-server",))["pxe-server"]
        verb = "停止" if active else "启动"
        if not messagebox.askyesno("确认", f"确定要{verb} PXE 装机服务?"):
            return
        subprocess.run(["systemctl", "stop" if active else "start",
                        "pxe-server", "pxe-http-repo", "pxe-http-boot"],
                       capture_output=True)

    def cfg_lan1(self):
        cfg = kc.pxe_conf_read()
        ip = simpledialog.askstring("LAN1 装机口", "PXE 网卡地址 (CIDR):",
                                    initialvalue=f"{cfg['ip']}/{cfg['prefix']}")
        if not ip:
            return
        pool_s = simpledialog.askstring("地址池", "起始:", initialvalue=cfg["pool_start"]) or ""
        pool_e = simpledialog.askstring("地址池", "结束:", initialvalue=cfg["pool_end"]) or ""
        ip_body, _, prefix = ip.partition("/")
        new_cfg = {"ip": ip_body, "prefix": int(prefix or 24),
                   "pool_start": pool_s, "pool_end": pool_e}
        for cmd in kc.nmcli_set_static(kc.LAN1_CON, ip_body + "/" + (prefix or "24")):
            subprocess.run(cmd, capture_output=True)
        ok, msg = kc.apply_pxe_conf(new_cfg)
        (messagebox.showinfo if ok else messagebox.showerror)(
            "LAN1", "已应用:\n" + kc.render_pxe_service(new_cfg) if ok else f"失败: {msg}")

    def cfg_lan2(self):
        warn = ("注意: LAN2 是管理口(远程/SSH/tailscale 走这里)!\n"
                "修改后当前远程连接会断, 请确保你人在车边操作.\n\n要继续吗?")
        if not messagebox.askyesno("LAN2 管理口", warn, icon="warning"):
            return
        nics = {n["name"]: n for n in kc.nics_status()}
        nic = nics["enp3s0"]
        if messagebox.askyesno("LAN2", f"当前: {nic['ip']} ({nic['method']})\n\n改为 DHCP(自动获取)?"):
            cmds = kc.nmcli_set_dhcp(kc.LAN2_CON)
        else:
            ip = simpledialog.askstring("静态 IP", "IP/前缀:", initialvalue=nic["ip"])
            gw = simpledialog.askstring("网关", "网关(可空):")
            dns = simpledialog.askstring("DNS", "DNS(可空):")
            if not ip:
                return
            cmds = kc.nmcli_set_static(kc.LAN2_CON, ip, gw or "", dns or "")
        for cmd in cmds:
            subprocess.run(cmd, capture_output=True)
        messagebox.showinfo("LAN2", "已下发, 状态见左侧(远程会断属正常)")

    def confirm_power(self, off):
        verb = "关机" if off else "重启"
        if not self._confirm_pin(verb):
            return
        if messagebox.askyesno("确认", f"确定要{verb}推车?", icon="warning"):
            kc.power(off)

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
            rows = kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context())
            self.after(0, lambda: self._boot_show(box, rows, 0))

        threading.Thread(target=work, daemon=True).start()

    def _boot_show(self, box, rows, i):
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
            self._lock_win = None
            self._last_input = time.time()
        elif r == "locked":
            left = max(1, int(self.gate.locked_until - time.time()))
            self._lock_msg.config(text=f"错误次数过多，{left} 秒后再试", fg="#ff7b72")
        else:
            self._lock_msg.config(text="PIN 不对", fg="#ff7b72")

    def _confirm_pin(self, what):
        if not self.gate.required:
            return True
        pin = simpledialog.askstring("验证 PIN", f"{what}前请输入 PIN:", show="•", parent=self)
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
                kc.pxe_disarm()
                self._pxe_tick(reschedule=False)
            return
        self._arm_dialog()

    def _arm_dialog(self):
        """解锁: 选一台机器 + 照抄屏幕上的随机码. 码只显示在推车屏幕上, 远程开不了."""
        self._code = kc.new_code()
        w = tk.Toplevel(self, bg="#161b26", highlightthickness=3, highlightbackground="#7a1f2b")
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
        tk.Label(w, text=" ".join(self._code), font=("Noto Sans Mono CJK SC", 40, "bold"),
                 bg="#161b26", fg="#f0b429").pack()
        code_var = tk.StringVar()
        e = tk.Entry(w, textvariable=code_var, font=("Noto Sans Mono CJK SC", 22), width=10,
                     justify="center", bg="#1f2a3d", fg="#e8eaf0", insertbackground="#e8eaf0", relief="flat")
        e.pack(pady=8, ipady=6)
        msg = tk.Label(w, text="", font=FONT, bg="#161b26", fg="#ff7b72")
        msg.pack()

        def ok(_e=None):
            sel = lb.curselection()
            mac = mac_var.get().strip() or (lb.get(sel[0]) if sel else "")
            if not kc.code_matches(code_var.get(), self._code or ""):
                self._code = kc.new_code()          # 输错就换码, 不给试
                msg.config(text="验证码不对, 已换新码")
                w.destroy()
                self._arm_dialog()
                return
            try:
                kc.pxe_arm(mac)
            except ValueError as ex:
                msg.config(text=str(ex))
                return
            self._code = None
            w.destroy()
            self._pxe_tick(reschedule=False)

        def cancel():
            self._code = None
            w.destroy()

        row = tk.Frame(w, bg="#161b26")
        row.pack(pady=10)
        tk.Button(row, text="解锁", font=FONT_BIG, command=ok, bg="#47202a", fg="#ffb3bd",
                  relief="flat", padx=24).pack(side="left", padx=8)
        tk.Button(row, text="取消", font=FONT_BIG, command=cancel, bg="#2a3347", fg="#e8eaf0",
                  relief="flat", padx=24).pack(side="left", padx=8)
        e.bind("<Return>", ok)
        w.after(100, lambda: (w.focus_force(), e.focus_set()))

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
        """到期 / 选定机器取走了装机菜单 -> 自动上锁; 按钮显示当前状态."""
        st = kc.pxe_mode_read()
        if st["mode"] == "install" and kc.pxe_consumed(self._pxe_log_tail(), st["mac"], st["since"]):
            kc.pxe_disarm()
            self.banner.config(text=f"{st['mac']} 已取走装机菜单, 装机模式已自动上锁")
            st = {"mode": "rescue"}
        elif st["mode"] == "expired":
            kc.pxe_disarm()
            self.banner.config(text="装机模式 30 分钟到期, 已自动上锁")
            st = {"mode": "rescue"}
        if st["mode"] == "install":
            left = int(st["until"] - time.time())
            self.btn_install.config(text=f"⚠ 装机已解锁 {st['mac'][-8:]}  {left // 60:02d}:{left % 60:02d}",
                                    bg="#7a1f2b", fg="#ffffff")
            # 解锁是危险状态, 主页也得看得见
            self.btn_install_page.config(text=f"⚠ 装机已解锁  {left // 60:02d}:{left % 60:02d}  ▸",
                                         bg="#7a1f2b", fg="#ffffff")
        else:
            self.btn_install.config(text="装机模式:  救援 (安全)", bg="#1f2a3d", fg="#e8eaf0")
            self.btn_install_page.config(text="装机  ▸", bg="#1f2a3d", fg="#e8eaf0")
        if reschedule:
            self.after(2000, self._pxe_tick)

    def quit_kiosk(self):
        if not self._confirm_pin("退出到终端"):
            return
        self._stop_preview()
        self.destroy()


if __name__ == "__main__":
    app = Kiosk()
    app.mainloop()
