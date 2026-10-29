#!/usr/bin/env python3
"""Kiosk 串口面板: 嵌入 kiosk.py 的小部件, 后端走 conbroker.

左宽右窄分屏: 左边实时控制台 (直连 conbroker 的 human 通道, 键盘直通串口),
右边和 Claude 的留言板 (chat.log). Tk 8.6 接不上 ibus 的 XIM, 聊天先用英文/拼音.
开停服务/留言经 conbroker 命令行; 控制台只连它的本地 socket, 不直接碰串口设备.

线程纪律: 后台线程只往 self.ui / self.rx 两个队列放东西, 界面只在 _drain 里改 (Tk 不是线程安全的).
开服务优先用推车 hub 口别名 /dev/con-N (udev 按物理口起名; 那批 FT232R 仿片序列号全一样, by-id 只剩一个),
其次 /dev/serial/by-id 路径 (重插后 ttyUSBn 变了也认得); 板载 ttyS* 不列; 键鼠 CH9329 的口标出来, 不许当控制台.
"""

import codecs
import glob
import json as _json
import os
import pwd
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time

import tkinter as tk

import kvm_core as kvm

SERIAL_CONF = "/opt/pxe/serial.conf"
_SERIAL_DEFAULT = {"user": "ops", "conbroker_home": "/home/ops/conbroker", "chat_name": "operator"}


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
BYID_DIR = "/dev/serial/by-id"
ALIAS_GLOB = "/dev/con-*"   # /etc/udev/rules.d/99-cart-console.rules
HIDDEN_PORT = re.compile(r"^/dev/ttyS\d+$")   # 板载 COM 口, 推车上不接线
RECONNECT_MS = 3000         # 服务断了以后多久看一次它回来没有
PASTE_LINE_S = 0.05         # 粘贴按行发, 行间停一下, 交换机 CLI 吃得消


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


def parse_list(text):
    """conbroker list 的输出 -> (串口列表, {活着的服务名: 波特率}). 已死的残留状态不算服务."""
    ports, serves, section = [], {}, None
    for ln in text.splitlines():
        if ln.startswith("串口"):
            section = "p"
            continue
        if ln.startswith("服务"):
            section = "s"
            continue
        if not ln.startswith(" "):           # [stderr] / [错误] 之类
            section = None if ln.strip() else section
            continue
        parts = ln.split()
        if section == "p" and parts:
            ports.append(parts[0])
        elif section == "s" and len(parts) >= 3 and parts[2] == "在跑":
            serves[parts[0]] = parts[1]
    return ports, serves


def byid_paths(byid_dir=BYID_DIR):
    try:
        return [os.path.join(byid_dir, n) for n in sorted(os.listdir(byid_dir))]
    except OSError:
        return []


def prefer_byid(dev, paths, realpath=os.path.realpath):
    """同一个口有 by-id 路径就用它 (按序列号认口, 重插不变)."""
    r = realpath(dev)
    return next((p for p in sorted(paths) if realpath(p) == r), dev)


def alias_paths(pattern=ALIAS_GLOB):
    return sorted(glob.glob(pattern))


def console_ports(ports, aliases, realpath=os.path.realpath):
    """下拉列表: 有 hub 口别名的换成别名, 板载口去掉; 别名排前面."""
    out = []
    for p in ports:
        if HIDDEN_PORT.match(p):
            continue
        out.append(next((a for a in aliases if realpath(a) == realpath(p)), p))
    return sorted(out, key=lambda p: (p not in aliases, p))


def serve_target(port, aliases, byid, realpath=os.path.realpath):
    """开服务用的路径: hub 口别名原样用, 别的口换成 by-id."""
    return port if port in aliases else prefer_byid(port, byid, realpath)


def match_service(dev, serves, realpath=os.path.realpath):
    """选中的口对应哪个在跑的服务 (服务名可能是 by-id 路径, 也可能是 ttyUSBn)."""
    if dev in serves:
        return dev
    r = realpath(dev)
    return next((n for n in serves if realpath(n) == r), None)


def select_port(ports, want, realpath=os.path.realpath):
    """issue #5: 把点中那一路口对齐到下拉列表里的条目.

    返回 (要显示的路径, 是否在列表里). 不在列表里说明这一路没插线,
    仍然返回 want, 由面板提示"未插"; 指向同一设备时优先用列表里的写法.
    """
    if want in ports:
        return want, True
    r = realpath(want)
    hit = next((p for p in ports if realpath(p) == r), None)
    return (hit, True) if hit else (want, False)


def kvm_devices(kvm_ports, realpath=os.path.realpath):
    """键鼠 CH9329 占着的真实设备 (kvm_core.ports() 的 by-id 路径解析出来)."""
    return {realpath(p) for p in kvm_ports.values()}


def port_label(dev, kvm_devs):
    return f"{dev} (键鼠 CH9329)" if dev in kvm_devs else dev


def paste_chunks(text):
    """粘贴的文字 -> 按行切的字节块, 换行统一成回车 (串口设备认 \\r)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = [ln.encode() + b"\r" for ln in lines[:-1]]
    return out + ([lines[-1].encode()] if lines[-1] else [])


def serve_error(rc, stderr):
    """开服务的进程提前退出时给人看的一句: stderr 最后一行, 没有就退出码."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else f"退出码 {rc}"


_KEY_SEQ = {
    "Return": b"\r", "KP_Enter": b"\r", "BackSpace": b"\x08", "Tab": b"\t",
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
        self._busy = self._pasting = self._reconnect = False
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
            self.con_title.config(text=f"控制台 · 连接失败: {e}", fg="#ff7b72")
            return
        self.sock, self._reconnect = s, False
        self.term = TermFeed()
        self.con_title.config(text=f"控制台 · {dev} @ {self._serves.get(name, '?')} · 键盘直通串口",
                              fg="#7ee787")
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

    def destroy(self):
        self._disconnect()
        super().destroy()
