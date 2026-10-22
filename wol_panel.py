#!/usr/bin/env python3
"""Kiosk 网络唤醒面板: 常用目标清单 (唤醒/添加/删除) + 临时输一个 MAC 直接唤醒.

魔术包从选定网口广播, 目标要和那个口在同一个二层里. 规则见 wol_core. 每次唤醒记 events.log.
"""

import tkinter as tk
from tkinter import messagebox

import cartlog
import wol_core as wol
from wifi_panel import BG, BOX, FG, FONT, FONT_MONO, WifiPanel


class WolPanel(tk.Frame):
    _btn = WifiPanel._btn
    _say = WifiPanel._say
    _sel = WifiPanel._sel

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.targets = wol.load()
        self._build()
        self._render()

    def _build(self):
        tk.Label(self, text="网络唤醒 (WOL) · 魔术包从选定网口广播, 目标要在同一个二层里 · 目标 BIOS/网卡要开 WOL",
                 font=FONT, bg=BG, fg="#8b93a7", anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        self.lb = tk.Listbox(self, font=FONT_MONO, bg=BOX, fg=FG, relief="flat", height=12,
                             selectbackground="#3a4560", exportselection=False, activestyle="none")
        self.lb.pack(fill="both", expand=True, padx=16, pady=4)
        self.lb.bind("<Double-Button-1>", lambda _e: self.wake_selected())
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._pick())
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12)
        self._btn(bar, "唤醒", self.wake_selected, bg="#26577c")
        self._btn(bar, "删除", self.delete, bg="#47202a")

        form = tk.LabelFrame(self, text=" 临时唤醒 / 加入清单 ", font=FONT, bg=BG, fg="#8b93a7", bd=1, relief="groove")
        form.pack(fill="x", padx=16, pady=(10, 4))
        row = tk.Frame(form, bg=BG)
        row.pack(fill="x", pady=6)
        self.name_var, self.mac_var = tk.StringVar(), tk.StringVar()
        for label, var, w in (("名字:", self.name_var, 14), ("MAC:", self.mac_var, 18)):
            tk.Label(row, text=label, font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(6, 0))
            e = tk.Entry(row, textvariable=var, width=w, font=FONT_MONO, bg="#10141c", fg=FG,
                         insertbackground=FG, relief="flat")
            e.pack(side="left", padx=6, ipady=3)
        e.bind("<Return>", lambda _e: self.wake_form())
        self.iface_var = tk.StringVar(value="enp3s0")
        for i, n in wol.IFACES.items():
            tk.Radiobutton(row, text=n, value=i, variable=self.iface_var, font=FONT, bg=BG, fg="#c8cede",
                           selectcolor=BOX, activebackground=BG, activeforeground=FG).pack(side="left", padx=2)
        self._btn(row, "唤醒", self.wake_form, bg="#26577c")
        self._btn(row, "加入清单", self.add)

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))

    def _render(self):
        self.lb.delete(0, "end")
        for t in self.targets:
            self.lb.insert("end", f"{t['name']:<16} {t['mac']}   {wol.IFACES.get(t['iface'], t['iface'])}")
        if not self.targets:
            self._say("清单是空的: 在下面填名字和 MAC, 点「加入清单」")

    def _pick(self):
        _, t = self._sel(self.lb, self.targets)
        if t:
            self.name_var.set(t["name"])
            self.mac_var.set(t["mac"])
            self.iface_var.set(t["iface"])

    def _wake(self, name, mac, iface):
        try:
            mac = wol.parse_mac(mac)
            wol.send(mac, iface)
        except (ValueError, OSError) as ex:
            return self._say(f"没发出去: {ex}", ok=False)
        cartlog.event("wol", f"{name or '(临时)'} {mac} 经 {wol.IFACES.get(iface, iface)}")
        self._say(f"已从 {wol.IFACES.get(iface, iface)} 发出魔术包 → {name or mac}; 一般 10-60 秒开机")

    def wake_selected(self):
        _, t = self._sel(self.lb, self.targets)
        if not t:
            return self._say("先在上面选一台", ok=False)
        self._wake(t["name"], t["mac"], t["iface"])

    def wake_form(self):
        self._wake(self.name_var.get().strip(), self.mac_var.get(), self.iface_var.get())

    def add(self):
        try:
            self.targets = wol.add(self.targets, self.name_var.get(), self.mac_var.get(), self.iface_var.get())
            wol.save(self.targets)
        except (ValueError, OSError) as ex:
            return self._say(f"没加进去: {ex}", ok=False)
        self._render()
        self._say(f"已加入清单: {self.name_var.get().strip()}")

    def delete(self):
        _, t = self._sel(self.lb, self.targets)
        if not t:
            return self._say("先选一台", ok=False)
        if not messagebox.askyesno("删除", f"从清单删除 {t['name']} ({t['mac']})?"):
            return
        self.targets = wol.remove(self.targets, t["mac"])
        try:
            wol.save(self.targets)
        except OSError as ex:
            return self._say(f"删除失败: {ex}", ok=False)
        self._render()
        self._say("已删除")
