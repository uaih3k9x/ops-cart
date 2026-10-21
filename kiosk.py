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

import kiosk_core as kc
from ch9329 import Ch9329, KEYS
from serial_panel import SerialPanel
from ks_panel import KsPanel

FONT = ("Noto Sans CJK SC", 13)
FONT_BIG = ("Noto Sans CJK SC", 15)
FONT_MONO = ("Noto Sans Mono CJK SC", 13)
BOOT_BG = "#ffffff"     # 开机自检页白底, 接 Plymouth 的白底
VIDEO_DEV = "/dev/video0"
IDLE_SECONDS = 180

# Tk keysym -> ch9329 键名
KEYSYM_SPECIAL = {
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc",
    "BackSpace": "backspace", "Tab": "tab", "space": "space",
    "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "Prior": "pgup", "Next": "pgdn", "Left": "left", "Right": "right",
    "Up": "up", "Down": "down",
    "F1": "f1", "F2": "f2", "F3": "f3", "F4": "f4", "F5": "f5", "F6": "f6",
    "F7": "f7", "F8": "f8", "F9": "f9", "F10": "f10", "F11": "f11", "F12": "f12",
    "KP_1": "1", "KP_2": "2", "KP_3": "3", "KP_4": "4", "KP_5": "5",
    "KP_6": "6", "KP_7": "7", "KP_8": "8", "KP_9": "9", "KP_0": "0",
    "KP_Add": "+", "KP_Subtract": "-", "KP_Multiply": "*", "KP_Divide": "/",
    "KP_Decimal": ".", "KP_Delete": "delete",
}
MODIFIER_KEYS = {"Control_L": "ctrl", "Control_R": "ctrl", "Shift_L": "shift",
                 "Shift_R": "shift", "Alt_L": "alt", "Alt_R": "alt", "Meta_L": "alt", "Meta_R": "alt",
                 "Super_L": "gui", "Super_R": "gui"}


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
        self._fans_seen = set()
        self._mods = set()
        self._combo = kc.ReleaseCombo()
        self._mouse_acc = [0, 0, 0]      # dx, dy, wheel
        self._mouse_btn = 0
        self._last_pos = None
        self.ch, self._ch_dev = None, None
        self._ensure_ch()

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

        def big(text, cmd, bg="#1f2a3d", fg="#e8eaf0"):
            b = tk.Button(btns, text=text, font=FONT_BIG, command=cmd, bg=bg,
                          fg=fg, activebackground="#3a4560", relief="flat",
                          cursor="hand2", padx=30, pady=14)
            b.pack(fill="x", pady=6)
            return b

        self.btn_video = big("UVC 预览:  开 / 关", self.toggle_preview)
        self.video_dev = (kc.video_devices() or [VIDEO_DEV])[0]
        self.btn_src = big(f"画面源:  {os.path.basename(self.video_dev)}", self.switch_source)
        self.btn_serial = big("串口会话:  开 / 关", self.toggle_serial)
        self.btn_ks = big("Kickstart 编辑", self.toggle_ks)
        big("PXE 装机服务:  开 / 关", self.toggle_pxe)
        self.btn_install = big("装机模式:  救援 (安全)", self.toggle_install_mode)
        big("LAN1 装机口配置", self.cfg_lan1)
        big("LAN2 管理口配置", self.cfg_lan2)
        big("锁屏", self.lock_now)
        big("重启推车", lambda: self.confirm_power(False), bg="#4a3320")
        big("关机", lambda: self.confirm_power(True), bg="#47202a", fg="#ffb3bd")

    # ------------------------------------------------------------ 状态刷新

    def refresh(self):
        def work():
            svcs = kc.services_status()
            nics = kc.nics_status()
            misc = kc.misc_status()
            now = time.time()
            if now - self._ssd_ts > 60:          # smartctl 一分钟读一次就够
                self._ssd, self._ssd_ts = kc.smart_temp(), now
            temps = kc.temps_status(smart=lambda: self._ssd)
            fans = kc.fans_to_show(kc.fans_status(), self._fans_seen)
            lines = []
            for n, (en, ac) in svcs.items():
                lines.append(f"{'● 正常' if ac else '● 停止':<8} {n}"
                             + ("  (未自启)" if not en else ""))
            lines.append("")
            for nic in nics:
                lines.append(f"{nic['role']}  {nic['name']}")
                lines.append(f"    {nic['ip']:<20} {nic['method']}"
                             f"  {'已插线' if nic['link'] else '未插线'}")
            lines.append("")
            lines.append(f"tailscale: {misc['tailscale_ip']}")
            lines.append(f"CH9329 键鼠线: {'已连接' if misc['ch9329'] else '未连接'}")
            lines.append(misc["uptime"])
            if temps:
                lines.append("")
                for name, c in temps:
                    lines.append((f"{name:<6}{c:>5.0f}°C", "t_" + kc.temp_level(c)))
            for name, rpm in fans:
                lines.append((f"{name:<6}{rpm:>5} RPM", "t_ok" if rpm else "t_hot"))
            self.after(0, lambda: self._render(lines))

        threading.Thread(target=work, daemon=True).start()
        self.clock.config(text=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.after(2000, self.refresh)

    def _render(self, lines):
        if self.status_text.winfo_ismapped():
            self.status_text.config(state="normal")
            self.status_text.delete("1.0", "end")
            for ln in lines:
                if isinstance(ln, tuple):       # (文本, tag)
                    self.status_text.insert("end", ln[0] + "\n", ln[1])
                    continue
                tag = "ok" if ln.startswith("● 正常") else (
                      "bad" if ln.startswith("● 停止") else None)
                self.status_text.insert("end", ln + "\n", tag or ())
            self.status_text.config(state="disabled")
            self.status_text.tag_config("ok", foreground="#7ee787")
            self.status_text.tag_config("bad", foreground="#ff7b72")
            self.status_text.tag_config("t_ok", foreground="#9aa5b0")
            self.status_text.tag_config("t_warn", foreground="#f0b429")
            self.status_text.tag_config("t_hot", foreground="#ff7b72")

    def toggle_serial(self):
        if self.serial_panel:
            self._stop_preview()          # 互斥: 预览与串口不同时占左侧
            self.serial_panel.destroy()
            self.serial_panel = None
            self.status_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="")
        else:
            self._stop_preview()
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
            self.status_wrap.pack_forget()
            self.ks_panel = KsPanel(self.body)
            self.ks_panel.pack(side="left", fill="both", expand=True, padx=(0, 10))
            self.banner.config(text="Kickstart 编辑 · 保存自动备份, 修改立即对下次装机生效")

    # ------------------------------------------------------------ UVC 预览

    def switch_source(self):
        """video0 -> video1 -> ... 轮换; 正在预览就换源重开."""
        self.video_dev = kc.next_video_device(kc.video_devices(), self.video_dev)
        self.btn_src.config(text=f"画面源:  {os.path.basename(self.video_dev)}")
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
        self.status_wrap.pack_forget()
        self.video_frame = tk.Frame(self.body, container=True, bg="#000000")
        self.video_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.video_frame.bind("<Button-1>", lambda e: self._engage_passthrough())
        self.update_idletasks()
        wid = self.video_frame.winfo_id()
        self.mpv = subprocess.Popen(
            kc.mpv_cmd(wid, self.video_dev),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.banner.config(text=f"预览 {os.path.basename(self.video_dev)} · 单击画面进入键鼠透传 (Ctrl+Alt 释放)")

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

    def _ensure_ch(self):
        """CH9329 串口按 by-id 找; 重插后 ttyACMn 变了就重连. 返回能不能用."""
        dev = os.path.realpath(kc.ch9329_port())
        if self.ch and dev == self._ch_dev:
            return True
        if self.ch:
            try:
                self.ch.close()
            except OSError:
                pass
        try:
            self.ch, self._ch_dev = Ch9329(dev, log=lambda *a: None), dev
        except OSError:
            self.ch, self._ch_dev = None, None
        return self.ch is not None

    def _ch_send(self, fn, *args, **kw):
        """发送失败(线被拔)就丢掉句柄, 下次进透传再重连; 不让异常打断 Tk 的定时循环."""
        if not self.ch:
            return
        try:
            getattr(self.ch, fn)(*args, **kw)
        except OSError:
            self.ch, self._ch_dev = None, None
            self.banner.config(text="CH9329 断开 · 重新单击画面重连")

    def _engage_passthrough(self):
        if self.passthrough:
            return
        if not self._ensure_ch():
            messagebox.showwarning("键鼠透传", f"CH9329 未连接 ({kc.ch9329_port()})")
            return
        self.passthrough = True
        self._combo = kc.ReleaseCombo()
        self._mute_tk_keys()
        self._mods.clear()
        self._last_pos = None
        self._mouse_acc, self._mouse_btn = [0, 0, 0], 0
        self.video_frame.grab_set_global()
        self.video_frame.config(cursor="none")
        for seq in ("<Motion>", "<Button-1>", "<Button-2>", "<Button-3>",
                    "<ButtonRelease-1>", "<ButtonRelease-2>", "<ButtonRelease-3>",
                    "<MouseWheel>"):
            self.video_frame.bind(seq, self._on_passthrough_event)
        self.focus_force()
        self.banner.config(text="🔒 透传中 · 按一下 Ctrl+Alt 再松开 = 释放")
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
        self.banner.config(text=f"预览 {os.path.basename(self.video_dev)} · 单击画面进入键鼠透传 (Ctrl+Alt 释放)")
        self._mouse_acc, self._mouse_btn, self._mods = [0, 0, 0], 0, set()
        self._ch_send("send_mouse", 0, 0, 0, 0)   # 松开所有鼠标键
        self._ch_send("send_keys", [], press_ms=0)   # 松开所有键盘键

    def _on_passthrough_event(self, ev):
        # 透传时这些事件绑定会一直留在 Frame 上: 释放后左键单击 = 重新进透传, 其余忽略
        # (否则第二次点不进去, 且释放期间的移动会攒成一大步甩给目标机)
        if not self.passthrough:
            if ev.type == "4" and str(ev.num) == "1":
                self._engage_passthrough()
            return
        if ev.type == "6":            # Motion
            pos = (ev.x_root, ev.y_root)
            if self._last_pos:
                self._mouse_acc[0] += pos[0] - self._last_pos[0]
                self._mouse_acc[1] += pos[1] - self._last_pos[1]
            self._last_pos = pos
        elif ev.type in ("4", "5"):   # ButtonPress / Release
            bit = {"1": 0x01, "2": 0x04, "3": 0x02}[ev.num]   # 左/中/右
            if ev.type == "4":
                self._mouse_btn |= bit
            else:
                self._mouse_btn &= ~bit & 0xFF
        elif ev.type == "38":         # MouseWheel
            self._mouse_acc[2] += max(-1, min(1, ev.delta // 120))

    def _on_key(self, ev, down):
        mod = MODIFIER_KEYS.get(ev.keysym)
        name = mod or ev.keysym
        if self._combo.key(name, down):      # Ctrl+Alt 按下又松开、中间没按别的 -> 释放
            self._release_passthrough()
            return
        if mod:
            if down:
                self._mods.add(mod)
            else:
                self._mods.discard(mod)
            return
        if not down:
            return
        key = None
        if len(ev.char) == 1 and ev.char in KEYS:
            key = ev.char
        elif ev.keysym in KEYSYM_SPECIAL:
            key = KEYSYM_SPECIAL[ev.keysym]
        if key:
            try:
                self._ch_send("send_keys", list(self._mods) + [key], press_ms=20)
            except ValueError:      # 组合里有 CH9329 不认的键
                pass

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
        box.place(relx=0.5, rely=0.62, anchor="center")
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
        else:
            self.btn_install.config(text="装机模式:  救援 (安全)", bg="#1f2a3d", fg="#e8eaf0")
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
