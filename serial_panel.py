#!/usr/bin/env python3
"""Kiosk 串口面板: 嵌入 kiosk.py 的小部件, 后端走 conbroker.

左宽右窄分屏: 左边实时控制台 (直连 conbroker 的 human 通道, 键盘直通串口),
右边和 Claude 的留言板 (chat.log). Tk 8.6 接不上 ibus 的 XIM, 聊天先用英文/拼音.
开停服务/留言经 conbroker 命令行; 控制台只连它的本地 socket, 不直接碰串口设备.
"""

import codecs
import json as _json
import os
import pwd
import queue
import re
import socket
import subprocess
import threading

import tkinter as tk

SERIAL_CONF = "/opt/pxe/serial.conf"
_SERIAL_DEFAULT = {"user": "ops", "conbroker_home": "/home/ops/conbroker"}


def serial_conf_read(path=None):
    """部署配置: 串口服务以哪个用户跑 conbroker (GUI 是 root, 须降权对齐数据目录)."""
    if path is None:
        path = SERIAL_CONF
    cfg = dict(_SERIAL_DEFAULT)
    try:
        cfg.update(_json.load(open(path)))
    except (OSError, ValueError):
        pass
    home = cfg["conbroker_home"].rstrip("/")
    cfg["bin"] = f"{home}/.venv/bin/python"
    cfg["script"] = f"{home}/conbroker.py"
    return cfg


def _conbroker_cmd():
    c = serial_conf_read()
    return ["sudo", "-u", c["user"], "-H", c["bin"], c["script"]]


# CONBROKER 不做模块级缓存: serial.conf 可能在运行中被改 (Kiosk 里保存)

BAUDS = ["9600", "115200", "57600", "38400", "4800"]


def _run(args, timeout=8):
    try:
        r = subprocess.run(_conbroker_cmd() + args, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
    except Exception as e:
        return f"[错误] {e}"


# ------------------------------------------------------------ 纯逻辑 (可测)

def data_dir(cfg):
    """conbroker 的数据目录 (run/ logs/ chat.log): 跑服务那个用户的 ~/.conbroker."""
    if cfg.get("data_dir"):
        return cfg["data_dir"]
    try:
        home = pwd.getpwnam(cfg["user"]).pw_dir
    except KeyError:
        home = f"/home/{cfg['user']}"
    return f"{home}/.conbroker"


def broker_port(ddir, name):
    """读 conbroker 的状态文件拿本地端口; 没在跑返回 None."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    try:
        return _json.load(open(os.path.join(ddir, "run", f"{safe}.json")))["port"]
    except (OSError, ValueError, KeyError):
        return None


_KEY_SEQ = {
    "Return": b"\r", "KP_Enter": b"\r", "BackSpace": b"\x7f", "Tab": b"\t",
    "Escape": b"\x1b", "Up": b"\x1b[A", "Down": b"\x1b[B", "Right": b"\x1b[C",
    "Left": b"\x1b[D", "Home": b"\x1b[H", "End": b"\x1b[F", "Delete": b"\x1b[3~",
    "Prior": b"\x1b[5~", "Next": b"\x1b[6~",
}
_CTRL = 0x4
_RESERVED = {"bracketright"}     # Ctrl+] 和 conbroker attach 一样, 不进串口


def key_bytes(keysym, char, state):
    """Tk 按键 -> 发给串口的字节; 不该发的 (单独修饰键/保留键) 返回 None."""
    if keysym in _KEY_SEQ:
        return _KEY_SEQ[keysym]
    if state & _CTRL:
        if keysym in _RESERVED:
            return None
        if len(keysym) == 1 and keysym.isalpha():
            return bytes([ord(keysym.lower()) - 96])
    if char and (char.isprintable() or char == "\x03"):
        return char.encode()
    return None


_CSI = re.compile(rb"\x1b\[([0-9;?]*)([A-Za-z])|\x1b[()][AB012]")
_CSI_TAIL = re.compile(rb"\x1b(\[[0-9;?]*)?$|\x1b[()]$")


class TermFeed:
    """串口原始字节 -> 文本/退格操作. 不做完整终端仿真, 够看交换机 CLI 就行."""

    def __init__(self):
        self._pending = b""
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data):
        data = self._pending + data
        m = _CSI_TAIL.search(data)
        self._pending, data = (data[m.start():], data[:m.start()]) if m else (b"", data)
        ops = []
        pos = 0
        for m in _CSI.finditer(data):
            self._text(data[pos:m.start()], ops)
            if m.group(2) == b"D":                     # 光标左移 n 格 = 退格 n 次
                self._bs(int(m.group(1) or b"1"), ops)
            pos = m.end()
        self._text(data[pos:], ops)
        return ops

    def _text(self, raw, ops):
        if not raw:
            return
        buf = ""
        for ch in self._dec.decode(raw):
            if ch == "\b":
                if buf:
                    ops.append(("text", buf)); buf = ""
                self._bs(1, ops)
            elif ch in "\n\t" or ch.isprintable():
                buf += ch
        if buf:
            if ops and ops[-1][0] == "text":
                ops[-1] = ("text", ops[-1][1] + buf)
            else:
                ops.append(("text", buf))

    @staticmethod
    def _bs(n, ops):
        if ops and ops[-1][0] == "bs":
            ops[-1] = ("bs", ops[-1][1] + n)
        else:
            ops.append(("bs", n))


_CHAT_LINE = re.compile(r"^\[\d{4}-\d\d-\d\d (\d\d:\d\d):\d\d (\S+)\] (.*)$")


def parse_chat_line(line):
    m = _CHAT_LINE.match(line)
    return m.groups() if m else None


# ------------------------------------------------------------ UI

BG, BG2, FG, DIM = "#161b26", "#10141c", "#e8eaf0", "#8b93a7"
FONT = ("Noto Sans CJK SC", 12)
FONT_MONO = ("Noto Sans Mono CJK SC", 13)
CHAT_WIDTH = 340


def _btn(parent, text, cmd, bg="#1f2a3d"):
    return tk.Button(parent, text=text, font=FONT, command=cmd, bg=bg, fg=FG,
                     activebackground="#3a4560", relief="flat", cursor="hand2", padx=10)


class SerialPanel(tk.Frame):
    """kiosk 里的 '串口会话' 页. 左控制台 (宽) | 右聊天 (窄), F2 切换焦点."""

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.cfg = serial_conf_read()
        self.ddir = data_dir(self.cfg)
        self.sock = None
        self.rx = queue.Queue()
        self.term = TermFeed()
        self.chat_off = None
        self._build()
        self.refresh_ports()
        self.after(30, self._drain)
        self.after(200, self._chat_poll)
        self.after(300, self.console.focus_set)

    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="串口:", font=FONT, bg=BG, fg="#c8cede").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_menu = tk.OptionMenu(bar, self.port_var, "(无)")
        self.port_menu.config(font=FONT, bg="#1f2a3d", fg=FG, activebackground="#3a4560",
                              relief="flat", cursor="hand2")
        self.port_menu.pack(side="left", padx=6)
        tk.Label(bar, text="波特率:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(12, 0))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_menu = tk.OptionMenu(bar, self.baud_var, *BAUDS)
        self.baud_menu.config(font=FONT, bg="#1f2a3d", fg=FG, activebackground="#3a4560",
                              relief="flat", cursor="hand2")
        self.baud_menu.pack(side="left", padx=6)
        for text, cmd in (("刷新", self.refresh_ports), ("开服务", self.start_serve),
                          ("停服务", self.stop_serve), ("唤醒(回车)", lambda: self._tx(b"\r")),
                          ("Ctrl+C", lambda: self._tx(b"\x03"))):
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
        self.console = tk.Text(left, font=FONT_MONO, bg=BG2, fg="#c8cede", relief="flat",
                               insertbackground=FG, wrap="char", highlightthickness=2,
                               highlightbackground=BG2, highlightcolor="#26577c")
        self.console.pack(fill="both", expand=True)
        self.console.tag_config("mark", foreground="#7ec8e3")
        self.console.bind("<Key>", self._on_console_key)
        self.console.bind("<<Paste>>", self._on_paste)
        pane.add(left, stretch="always", minsize=400)

        # 右: 聊天
        right = tk.Frame(pane, bg=BG, width=CHAT_WIDTH)
        tk.Label(right, text="和 Claude 聊 · F2 切换", font=FONT, bg=BG, fg=DIM,
                 anchor="w").pack(fill="x")
        self.chat_log = tk.Text(right, font=FONT, bg=BG2, fg="#c8cede", relief="flat",
                                wrap="word", state="disabled", padx=8, pady=6)
        self.chat_log.pack(fill="both", expand=True)
        self.chat_log.tag_config("ts", foreground="#5c6578")
        self.chat_log.tag_config("me", foreground="#7ee787")
        self.chat_log.tag_config("them", foreground="#ffb86c")
        row = tk.Frame(right, bg=BG)
        row.pack(fill="x", pady=(6, 0))
        self.chat_var = tk.StringVar()
        self.chat_entry = tk.Entry(row, textvariable=self.chat_var, font=FONT, bg=BG2, fg=FG,
                                   insertbackground=FG, relief="flat", highlightthickness=2,
                                   highlightbackground=BG2, highlightcolor="#26577c")
        self.chat_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.chat_entry.bind("<Return>", lambda e: self.chat_send())
        _btn(row, "发送", self.chat_send, bg="#26577c").pack(side="left", padx=(6, 0))
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
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        self._tx(text.replace("\r\n", "\r").replace("\n", "\r").encode())
        return "break"

    def _tx(self, data):
        if not self.sock:
            self._con_write([("text", "\n[未连接: 先选串口点 开服务]\n")], "mark")
            return
        try:
            self.sock.sendall(data)
        except OSError:
            self._disconnect()

    def _connect(self):
        self._disconnect()
        name = self.port_var.get()
        port = broker_port(self.ddir, name)
        if not port:
            self.con_title.config(text=f"控制台 · {name} 没有在跑的服务", fg=DIM)
            return
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.settimeout(None)
            s.sendall(b"HELLO human\n")
        except OSError as e:
            self.con_title.config(text=f"控制台 · 连接失败: {e}", fg="#ff7b72")
            return
        self.sock = s
        self.term = TermFeed()
        self.con_title.config(text=f"控制台 · {name} @ {self.baud_var.get()} · 键盘直通串口",
                              fg="#7ee787")
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
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

    def _drain(self):
        try:
            while True:
                s, data = self.rx.get_nowait()
                if s is not self.sock:
                    continue
                if not data:
                    self._disconnect()
                    self._con_write([("text", "\n[服务已断开]\n")], "mark")
                else:
                    self._con_write(self.term.feed(data))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(30, self._drain)

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
        log.insert("end", f"{who}\n", "me" if who == "op" else "them")
        log.insert("end", f"{text}\n\n")
        log.see("end")
        log.config(state="disabled")

    def chat_send(self):
        msg = self.chat_var.get().strip()
        if not msg:
            return
        self.chat_var.set("")
        # 经 conbroker say 写入 (以服务用户身份), 自己的消息由轮询显示出来
        threading.Thread(target=_run, args=(["say", "op", msg],), daemon=True).start()

    # ------------------------------------------------------------ 服务

    def _async(self, fn, done=None):
        def work():
            r = fn()
            if done:
                self.after(0, lambda: done(r))
        threading.Thread(target=work, daemon=True).start()

    def refresh_ports(self):
        def done(text):
            ports, serves, section = [], {}, None
            for ln in text.splitlines():
                if ln.startswith("串口"):
                    section = "p"; continue
                if ln.startswith("服务"):
                    section = "s"; continue
                parts = ln.split()
                if section == "p" and parts:
                    ports.append(parts[0])
                elif section == "s" and parts:
                    serves[parts[0]] = True
            menu = self.port_menu["menu"]
            menu.delete(0, "end")
            for p in ports:
                menu.add_command(label=p, command=lambda v=p: (self.port_var.set(v), self._connect()))
            if ports and self.port_var.get() not in ports:
                self.port_var.set(ports[0])
            self.serve_label.config(text=f"服务: {', '.join(serves) or '未开'}")
            if not self.sock and self.port_var.get() in serves:
                self._connect()
        self._async(lambda: _run(["list"]), done)

    def start_serve(self):
        port = self.port_var.get()
        if port == "(无)" or not port:
            return
        self._con_write([("text", f"\n[开服务] {port} @ {self.baud_var.get()}\n")], "mark")
        # serve 是常驻前台设计: nohup+setsid 丢后台, 立刻返回, 用 list 验证
        cmd = _conbroker_cmd() + ["serve", port, "-b", self.baud_var.get()]
        subprocess.Popen(["nohup", "setsid"] + cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.after(1500, self.refresh_ports)

    def stop_serve(self):
        port = self.port_var.get()
        self._disconnect()
        self._con_write([("text", f"\n[停服务] {port}\n")], "mark")
        self._async(lambda: _run(["stop", port], timeout=30), lambda t: self.refresh_ports())

    def destroy(self):
        self._disconnect()
        super().destroy()
