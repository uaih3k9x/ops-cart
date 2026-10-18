#!/usr/bin/env python3
"""Kiosk 串口面板: 嵌入 kiosk.py 的小部件, 后端走 conbroker 命令行.

职责: 列串口/开服务/发命令/看回显, 按 conbroker 文档约定的参数与守则.
所有动作经 conbroker.py 完成, 不直接碰串口设备 (独占规则交给 conbroker).
"""

import subprocess
import threading

import tkinter as tk
from tkinter import messagebox

CONBROKER = ["sudo", "-u", "uaih3k9x", "-H",
             "/home/user/conbroker/.venv/bin/python",
             "/home/user/conbroker/conbroker.py"]

BAUDS = ["9600", "115200", "57600", "38400", "4800"]


def _run(args, timeout=8):
    try:
        r = subprocess.run(CONBROKER + args, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
    except Exception as e:
        return f"[错误] {e}"


class SerialPanel(tk.Frame):
    """kiosk 里的 '串口会话' 页. kiosk.py 调 show()/hide() 切换."""

    def __init__(self, master):
        super().__init__(master, bg="#161b26")
        self.running_port = None
        self._build()
        self.refresh_ports()

    # ------------------------------------------------------------ UI

    def _build(self):
        bar = tk.Frame(self, bg="#161b26")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="串口:", font=("Noto Sans CJK SC", 13),
                 bg="#161b26", fg="#c8cede").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_menu = tk.OptionMenu(bar, self.port_var, "(无)")
        self.port_menu.config(font=("Noto Sans CJK SC", 12), bg="#1f2a3d",
                               fg="#e8eaf0", activebackground="#3a4560",
                               relief="flat", cursor="hand2")
        self.port_menu.pack(side="left", padx=6)

        tk.Label(bar, text="波特率:", font=("Noto Sans CJK SC", 13),
                 bg="#161b26", fg="#c8cede").pack(side="left", padx=(12, 0))
        self.baud_var = tk.StringVar(value="9600")
        self.baud_menu = tk.OptionMenu(bar, self.baud_var, *BAUDS)
        self.baud_menu.config(font=("Noto Sans CJK SC", 12), bg="#1f2a3d",
                              fg="#e8eaf0", activebackground="#3a4560",
                              relief="flat", cursor="hand2")
        self.baud_menu.pack(side="left", padx=6)

        for text, cmd in (("刷新", self.refresh_ports),
                          ("开服务", self.start_serve),
                          ("停服务", self.stop_serve),
                          ("唤醒(回车)", self.wake),
                          ("Ctrl+C", lambda: self.send("\\x03", raw=True))):
            tk.Button(bar, text=text, font=("Noto Sans CJK SC", 12), command=cmd,
                      bg="#1f2a3d", fg="#e8eaf0", activebackground="#3a4560",
                      relief="flat", cursor="hand2", padx=10).pack(
                      side="left", padx=4)

        self.serve_label = tk.Label(bar, text="服务: 未知", font=("Noto Sans CJK SC", 12),
                                    bg="#161b26", fg="#8b93a7")
        self.serve_label.pack(side="right")

        # 命令行 + 发送
        row = tk.Frame(self, bg="#161b26")
        row.pack(fill="x", padx=12, pady=4)
        self.cmd_var = tk.StringVar()
        self.cmd_entry = tk.Entry(row, textvariable=self.cmd_var, font=("Noto Sans Mono CJK SC", 14),
                                  bg="#10141c", fg="#e8eaf0", insertbackground="#e8eaf0",
                                  relief="flat")
        self.cmd_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.cmd_entry.bind("<Return>", lambda e: self.send_from_entry())
        tk.Button(row, text="发送", font=("Noto Sans CJK SC", 13),
                  command=self.send_from_entry, bg="#26577c", fg="#e8eaf0",
                  activebackground="#3a4560", relief="flat", cursor="hand2",
                  padx=18).pack(side="left", padx=8)

        # 输出区
        self.output = tk.Text(self, font=("Noto Sans Mono CJK SC", 13),
                              bg="#10141c", fg="#c8cede", relief="flat",
                              state="disabled", wrap="none")
        self.output.pack(fill="both", expand=True, padx=12, pady=(4, 10))

    # ------------------------------------------------------------ 后台执行

    def _async(self, fn, done=None):
        threading.Thread(target=self._work, args=(fn, done), daemon=True).start()

    def _work(self, fn, done):
        result = fn()
        if done:
            self.after(0, lambda: done(result))

    def _append(self, text, tag="out"):
        self.output.config(state="normal")
        self.output.insert("end", text.rstrip("\n") + "\n", tag)
        self.output.see("end")
        self.output.config(state="disabled")
        self.output.tag_config("out", foreground="#c8cede")
        self.output.tag_config("cmd", foreground="#7ec8e3")
        self.output.tag_config("err", foreground="#ff7b72")

    # ------------------------------------------------------------ 动作

    def refresh_ports(self):
        def work():
            return _run(["list"])
        def done(text):
            ports, serves = [], []
            serve_map = {}
            section = None
            for ln in text.splitlines():
                if ln.startswith("串口"):
                    section = "p"; continue
                if ln.startswith("服务"):
                    section = "s"; continue
                parts = ln.split()
                if section == "p" and parts:
                    ports.append(parts[0])
                elif section == "s" and parts:
                    serve_map[parts[0]] = True
            self._ports = ports
            self._serves = serve_map
            menu = self.port_menu["menu"]
            menu.delete(0, "end")
            for p in ports:
                menu.add_command(label=p, command=lambda v=p: self.port_var.set(v))
            if ports and self.port_var.get() not in ports:
                self.port_var.set(ports[0])
            running = ", ".join(serve_map) or "未开"
            self.serve_label.config(text=f"服务: {running}")
        self._async(work, done)

    def start_serve(self):
        port = self.port_var.get()
        if port == "(无)" or not port:
            return
        self._append(f"[开服务] {port} @ {self.baud_var.get()}", "cmd")
        # serve 是常驻前台设计: nohup+setsid 丢后台, 立刻返回, 用 list 验证
        cmd = (CONBROKER + ["serve", port, "-b", self.baud_var.get()])
        subprocess.Popen(["nohup", "setsid"] + cmd,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        self.after(1500, self.refresh_ports)

    def stop_serve(self):
        port = self.port_var.get()
        self._append(f"[停服务] {port}", "cmd")
        self._async(lambda: _run(["stop", port], timeout=30),
                   lambda t: (self._append(t or "[已停]"), self.refresh_ports()))

    def wake(self):
        self.send("\\r", raw=True)

    def send_from_entry(self):
        text = self.cmd_var.get()
        if not text:
            return
        self.send(text)
        self.cmd_var.set("")

    def send(self, text, raw=False, secret=False):
        port = self.port_var.get()
        if port == "(无)" or not port:
            return
        shown = "[密码]" if secret else text
        self._append(f"[发送] {shown}", "cmd")
        args = ["send", port]
        if raw:
            args += ["--raw", text]
        elif secret:
            args += [text, "--secret"]
        else:
            args += [text]
        def done(t):
            self._append(t, "err" if t.startswith("[") else "out")
            self.tail(60)
        self._async(lambda: _run(args, timeout=90), done)

    def tail(self, n=80):
        port = self.running_port or self.port_var.get()
        if not port or port == "(无)":
            return
        def done(t):
            # 全量替换式回显
            self.output.config(state="normal")
            self.output.delete("1.0", "end")
            self.output.insert("end", t, "out")
            self.output.see("end")
            self.output.config(state="disabled")
        self._async(lambda: _run(["tail", port, "-n", str(n)]), done)
