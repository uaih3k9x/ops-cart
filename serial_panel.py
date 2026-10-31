#!/usr/bin/env python3
"""Kiosk 串口面板: 嵌入 kiosk.py 的小部件, 后端走 conbroker.

左宽右窄分屏: 左边实时控制台 (直连 conbroker 的 human 通道, 键盘直通串口),
右边和 Claude 的留言板 (chat.log). Tk 8.6 接不上 ibus 的 XIM, 聊天先用英文/拼音.
开停服务/留言经 conbroker 命令行; 控制台只连它的本地 socket, 不直接碰串口设备.

线程纪律: 后台线程只往 self.ui / self.rx 两个队列放东西, 界面只在 _drain 里改 (Tk 不是线程安全的).
开服务优先用推车 hub 口别名 /dev/con-N (udev 按物理口起名; 那批 FT232R 仿片序列号全一样, by-id 只剩一个),
其次 /dev/serial/by-id 路径 (重插后 ttyUSBn 变了也认得); 板载 ttyS* 不列; 键鼠 CH9329 的口标出来, 不许当控制台.
纯逻辑在 serial_core.py; 这里重新导出那些名字, 老写法 (sp.serial_conf_read 等) 照样能用.
"""

import os
import queue
import socket
import subprocess
import tempfile
import threading
import time

import tkinter as tk

import kvm_core as kvm
import survey_core
import theme as th
from serial_core import (  # noqa: F401  界面要用的 + 给老调用方的兼容名
    ALIAS_GLOB, BAUDS, BYID_DIR, HIDDEN_PORT, SERIAL_CONF, TermFeed, _CTRL, _conbroker_cmd, _run,
    alias_paths, broker_port, byid_paths, console_ports, data_dir, key_bytes, kvm_devices, match_service,
    parse_chat_line, parse_list, paste_chunks, port_label, prefer_byid, select_port, serial_conf_read,
    serve_error, serve_target)

RECONNECT_MS = 3000         # 服务断了以后多久看一次它回来没有
PASTE_LINE_S = 0.05         # 粘贴按行发, 行间停一下, 交换机 CLI 吃得消


# ------------------------------------------------------------ UI

BG, BG2, FG, DIM = th.PANEL, th.BG, th.FG, th.DIM
FONT, FONT_MONO = th.FONT_SMALL, th.FONT_MONO
CHAT_WIDTH = 340


def _btn(parent, text, cmd, bg=th.BOX):
    return tk.Button(parent, text=text, font=FONT, command=cmd, bg=bg, fg=FG,
                     activebackground=th.ACTIVE, relief="flat", cursor="hand2", padx=10)


class SerialPanel(tk.Frame):
    """kiosk 里的 '串口会话' 页. 左控制台 (宽) | 右聊天 (窄), F2 切换焦点."""

    def __init__(self, master, want=None):
        super().__init__(master, bg=BG)
        self.cfg = serial_conf_read()
        self.ddir = data_dir(self.cfg)
        self.sock = None
        self.rx = queue.Queue()             # 读线程 -> (socket, 字节)
        self.ui = queue.Queue()             # 后台线程 -> 要在主线程里执行的函数
        self.term = TermFeed()
        self.chat_off = None
        self._serves, self._kvm_devs, self._byid, self._aliases = {}, set(), [], []
        self._busy = self._pasting = self._reconnect = self._surveying = False
        self._want = want                    # issue #5: 打开面板时点中的那一路, 列表回来后落位
        self._menu_ports = []                # 下拉列表当前列出的口
        self._build()
        self.refresh_ports()
        self.after(30, self._drain)
        self.after(200, self._chat_poll)
        self.after(300, self.console.focus_set)

    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="串口:", font=FONT, bg=BG, fg=th.TEXT).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_menu = tk.OptionMenu(bar, self.port_var, "(无)")
        self.port_menu.config(font=FONT, bg=th.BOX, fg=FG, activebackground=th.ACTIVE,
                              relief="flat", cursor="hand2")
        self.port_menu.pack(side="left", padx=6)
        tk.Label(bar, text="波特率:", font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(12, 0))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_menu = tk.OptionMenu(bar, self.baud_var, *BAUDS)
        self.baud_menu.config(font=FONT, bg=th.BOX, fg=FG, activebackground=th.ACTIVE,
                              relief="flat", cursor="hand2")
        self.baud_menu.pack(side="left", padx=6)
        for text, cmd in (("刷新", self.refresh_ports), ("开服务", self.start_serve),
                          ("停服务", self.stop_serve), ("唤醒(回车)", lambda: self._tx(b"\r")),
                          ("Ctrl+C", lambda: self._tx(b"\x03")), ("巡检", self.survey)):
            _btn(bar, text, cmd).pack(side="left", padx=4)
        self.serve_label = tk.Label(bar, text="服务: 未知", font=FONT, bg=BG, fg=DIM)
        self.serve_label.pack(side="right")

        pane = tk.PanedWindow(self, orient="horizontal", bg=BG, sashwidth=6,
                              sashrelief="flat", borderwidth=0)
        pane.pack(fill="both", expand=True, padx=12, pady=(4, 10))

        # 左: 控制台
        left = tk.Frame(pane, bg=BG)
        self.con_title = tk.Label(left, text="控制台 · 未连接", font=FONT, bg=BG, fg=DIM, anchor="w")
        self.con_title.pack(fill="x")
        self.console = tk.Text(left, font=FONT_MONO, bg=BG2, fg=th.TEXT, relief="flat",
                               insertbackground=FG, wrap="char", highlightthickness=2,
                               highlightbackground=BG2, highlightcolor=th.PRIMARY)
        self.console.pack(fill="both", expand=True)
        self.console.tag_config("mark", foreground=th.MARK)
        self.console.bind("<Key>", self._on_console_key)
        self.console.bind("<<Paste>>", self._on_paste)
        pane.add(left, stretch="always", minsize=400)

        # 右: 聊天
        right = tk.Frame(pane, bg=BG, width=CHAT_WIDTH)
        tk.Label(right, text="和 Claude 聊 · F2 切换", font=FONT, bg=BG, fg=DIM,
                 anchor="w").pack(fill="x")
        self.chat_log = tk.Text(right, font=FONT, bg=BG2, fg=th.TEXT, relief="flat",
                                wrap="word", state="disabled", padx=8, pady=6)
        self.chat_log.pack(fill="both", expand=True)
        self.chat_log.tag_config("ts", foreground=th.FAINTER)
        self.chat_log.tag_config("me", foreground=th.OK)
        self.chat_log.tag_config("them", foreground=th.NOTICE)
        row = tk.Frame(right, bg=BG)
        row.pack(fill="x", pady=(6, 0))
        self.chat_var = tk.StringVar()
        self.chat_entry = tk.Entry(row, textvariable=self.chat_var, font=FONT, bg=BG2, fg=FG,
                                   insertbackground=FG, relief="flat", highlightthickness=2,
                                   highlightbackground=BG2, highlightcolor=th.PRIMARY)
        self.chat_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.chat_entry.bind("<Return>", lambda e: self.chat_send())
        _btn(row, "发送", self.chat_send, bg=th.PRIMARY).pack(side="left", padx=(6, 0))
        pane.add(right, stretch="never", minsize=260, width=CHAT_WIDTH)

        for w in (self.console, self.chat_entry, self.chat_log):
            w.bind("<F2>", self._toggle_focus)

    # ------------------------------------------------------------ 焦点

    def _toggle_focus(self, _e=None):
        (self.chat_entry if self.focus_get() is self.console else self.console).focus_set()
        return "break"

    # ------------------------------------------------------------ 控制台

    def _on_console_key(self, ev):
        if ev.keysym == "F2":
            return self._toggle_focus()
        if ev.state & _CTRL and ev.keysym.lower() == "v":
            return self._on_paste()
        data = key_bytes(ev.keysym, ev.char, ev.state)
        if data:
            self._tx(data)
        return "break"

    def _on_paste(self, _e=None):
        """后台按行限速发, 大段粘贴不卡界面, 也不把设备的接收缓冲冲爆."""
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        sock, chunks = self.sock, paste_chunks(text)
        if not sock:
            return self._tx(b"") or "break"
        if self._pasting or not chunks:
            return "break"
        self._pasting = True

        def work():
            try:
                for c in chunks:
                    sock.sendall(c)
                    time.sleep(PASTE_LINE_S)
            except OSError:
                pass                        # 读线程会收到断开, 由 _drain 处理
            finally:
                self.ui.put(lambda: setattr(self, "_pasting", False))
        threading.Thread(target=work, daemon=True).start()
        return "break"

    def _tx(self, data):
        if not self.sock:
            self._con_write([("text", "\n[未连接: 先选串口点 开服务]\n")], "mark")
            return
        try:
            self.sock.sendall(data)
        except OSError:
            self._disconnect()

    def _connect(self, wake=True):
        self._disconnect()
        dev = self.port_var.get()
        name = match_service(dev, self._serves)
        port = broker_port(self.ddir, name) if name else None
        if not port:
            self.con_title.config(text=f"控制台 · {dev} 没有在跑的服务", fg=DIM)
            return
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.settimeout(None)
            s.sendall(b"HELLO human\n")
        except OSError as e:
            self.con_title.config(text=f"控制台 · 连接失败: {e}", fg=th.BAD)
            return
        self.sock, self._reconnect = s, False
        self.term = TermFeed()
        self.con_title.config(text=f"控制台 · {dev} @ {self._serves.get(name, '?')} · 键盘直通串口",
                              fg=th.OK)
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
        if wake:
            self._tx(b"\r")

    def _reader(self, s):
        while True:
            try:
                data = s.recv(4096)
            except OSError:
                data = b""
            self.rx.put((s, data))
            if not data:
                return

    def _disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.con_title.config(text="控制台 · 未连接", fg=DIM)

    def _run_ui(self):
        try:
            while True:
                self.ui.get_nowait()()
        except queue.Empty:
            pass

    def _drain(self):
        self._run_ui()
        try:
            while True:
                s, data = self.rx.get_nowait()
                if s is not self.sock:
                    continue
                if not data:
                    self._disconnect()
                    self._con_write([("text", "\n[服务已断开, 回来后自动重连]\n")], "mark")
                    self._reconnect = True
                    self.after(RECONNECT_MS, self._reconnect_tick)
                else:
                    self._con_write(self.term.feed(data))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(30, self._drain)

    def _reconnect_tick(self):
        if not self._reconnect or self.sock or not self.winfo_exists():
            return
        self.refresh_ports(wake=False)          # 服务回来了就会自动接上
        self.after(RECONNECT_MS, self._reconnect_tick)

    def _con_write(self, ops, tag=None):
        c = self.console
        for op, val in ops:
            if op == "text":
                c.insert("end-1c", val, tag or ())
            else:  # 退格只在当前行内删
                col = int(c.index("end-1c").split(".")[1])
                n = min(val, col)
                if n:
                    c.delete(f"end-1c -{n}c", "end-1c")
        if int(c.index("end-1c").split(".")[0]) > 5000:
            c.delete("1.0", "1000.0")
        c.mark_set("insert", "end-1c")
        c.see("end")

    # ------------------------------------------------------------ 聊天

    def _chat_path(self):
        return os.path.join(self.ddir, "chat.log")

    def _chat_poll(self):
        try:
            with open(self._chat_path(), "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if self.chat_off is None or size < self.chat_off:
                    self.chat_off = max(0, size - 3000)    # 首次: 带最近几条上下文
                f.seek(self.chat_off)
                data = f.read()
            end = data.rfind(b"\n") + 1
            self.chat_off += end
            for ln in data[:end].decode("utf-8", errors="replace").splitlines():
                p = parse_chat_line(ln)
                if p:
                    self._chat_show(*p)
        except OSError:
            pass
        if self.winfo_exists():
            self.after(500, self._chat_poll)

    def _chat_show(self, hhmm, who, text):
        log = self.chat_log
        log.config(state="normal")
        log.insert("end", f"{hhmm} ", "ts")
        log.insert("end", f"{who}\n", "me" if who == serial_conf_read()["chat_name"] else "them")
        log.insert("end", f"{text}\n\n")
        log.see("end")
        log.config(state="disabled")

    def chat_send(self):
        msg = self.chat_var.get().strip()
        if not msg:
            return
        self.chat_var.set("")
        # 经 conbroker say 写入 (以服务用户身份), 自己的消息由轮询显示出来
        threading.Thread(target=_run, args=(["say", serial_conf_read()["chat_name"], msg],), daemon=True).start()

    # ------------------------------------------------------------ 服务

    def _async(self, fn, done=None):
        """fn 在后台跑; done 经 self.ui 回到主线程执行 (后台线程不碰 Tk, 包括 after)."""
        def work():
            r = fn()
            if done:
                self.ui.put(lambda: done(r))
        threading.Thread(target=work, daemon=True).start()

    def _select(self, dev):
        self._reconnect = False
        self.port_var.set(dev)
        self._connect()

    def select_slot(self, dev):
        """issue #5: 执行器条点第 N 路时切到 /dev/con-N.

        列表里没有这个口 (没插线) 也要停在这上面, 并在控制台提示未插;
        有在跑的服务就顺手连上, 没有就只选中, 不自动开服务 (开服务会占串口).
        """
        shown_path, present = select_port(list(self._menu_ports), dev)
        self.port_var.set(shown_path)
        self._reconnect = False
        self._disconnect()
        if present:
            self._connect()
        else:
            self.con_title.config(text=f"控制台 · {dev} 未插 (这一路没有设备)", fg=DIM)

    def refresh_ports(self, wake=True):
        def fetch():
            return _run(["list"]), kvm_devices(kvm.ports()), byid_paths(), alias_paths()

        def done(res):
            text, self._kvm_devs, self._byid, self._aliases = res
            ports, self._serves = parse_list(text)
            ports = console_ports(ports, self._aliases)
            menu = self.port_menu["menu"]
            self._menu_ports = list(ports)
            menu.delete(0, "end")
            for p in ports:
                menu.add_command(label=port_label(p, self._kvm_devs), command=lambda v=p: self._select(v))
            want, self._want = self._want, None
            if want:
                shown_path, _present = select_port(ports, want)
                self.port_var.set(shown_path)
            elif ports and self.port_var.get() not in ports:
                free = [p for p in ports if p not in self._kvm_devs]
                self.port_var.set((free or ports)[0])
            shown = [f"{os.path.basename(n)} @ {b}" for n, b in self._serves.items()]
            self.serve_label.config(text=f"服务: {', '.join(shown) or '未开'}")
            if not self.sock and match_service(self.port_var.get(), self._serves):
                self._connect(wake=wake)
        self._async(fetch, done)

    def start_serve(self):
        """开服务: 防连点; 进程提前退出时把它的报错显示出来, 不再静默失败."""
        port = self.port_var.get()
        if self._busy or port == "(无)" or not port:
            return
        if os.path.realpath(port) in self._kvm_devs:
            self._con_write([("text", f"\n[{port} 是键鼠 CH9329 的口, 不能当控制台]\n")], "mark")
            return
        target = serve_target(port, self._aliases, self._byid)
        self._busy = True
        self._con_write([("text", f"\n[开服务] {target} @ {self.baud_var.get()}\n")], "mark")
        # serve 是常驻前台设计: nohup+setsid 丢后台, 立刻返回; stderr 落临时文件, 1.5 秒后看它死没死
        err = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(["nohup", "setsid"] + _conbroker_cmd() + ["serve", target, "-b", self.baud_var.get()],
                                    stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL)
        except OSError as e:
            err.close()
            self._busy = False
            self._con_write([("text", f"[开服务失败] {e}\n")], "mark")
            return
        self.after(1500, lambda: self._serve_check(proc, err))

    def _serve_check(self, proc, err):
        rc = proc.poll()
        if rc is not None:
            err.seek(0)
            msg = serve_error(rc, err.read().decode(errors="replace"))
            self._con_write([("text", f"[开服务失败] {msg}\n")], "mark")
        err.close()
        self._busy = False
        self.refresh_ports()

    def stop_serve(self):
        port = self.port_var.get()
        name = match_service(port, self._serves) or port
        self._reconnect = False
        self._disconnect()
        self._con_write([("text", f"\n[停服务] {name}\n")], "mark")
        self._async(lambda: _run(["stop", name], timeout=30), lambda t: self.refresh_ports())

    def survey(self):
        """#15 一键只读巡检: 后台跑 survey_core (走 conbroker send), 摘要经 self.ui 回主线程写进控制台.

        选中的口没有在跑的服务就只提示, 不开服务也不跑. 厂商取 serial.conf 的 survey_vendor, 默认 huawei.
        """
        if self._surveying:
            return
        dev = self.port_var.get()
        name = match_service(dev, self._serves)
        if not name:
            self._con_write([("text", f"\n[巡检] {dev} 上没有在跑的 conbroker 服务, 先点 开服务\n")], "mark")
            return
        self._surveying = True
        vendor = self.cfg.get("survey_vendor", "huawei")
        head = survey_core.safe_name(os.path.basename(dev))
        self._con_write([("text", f"\n[巡检] {dev} ({vendor}): 逐条发只读命令, 跑完前先别敲键\n")], "mark")

        def work():
            try:
                r = survey_core.run_survey(name, vendor, send=survey_core.conbroker_send, head=head)
            except Exception as e:                 # run_survey 本不抛; 兜住, 免得按钮一直锁着
                r = {"ok": False, "summary": f"巡检出错: {e}", "sections": [], "head": head, "error": str(e)}
            survey_core.record(r)
            return r

        def done(r):
            self._surveying = False
            self._con_write([("text", "\n[巡检] " + r["summary"].replace("\n", "\n       ") + "\n")], "mark")
        self._async(work, done)

    def destroy(self):
        self._disconnect()
        super().destroy()
