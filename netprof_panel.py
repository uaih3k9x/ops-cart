#!/usr/bin/env python3
"""Kiosk 网口配置文件面板: 列出 LAN1/LAN2 的配置文件, 切换 / 一键回退 / 新建 DHCP 或静态 / 删除.

规则在 netprof_core: 发地址只在 LAN1, 切到发地址前先探测链路上有没有别人的 DHCP;
LAN1 切到别的配置时先停 PXE 服务. 每次切换记 events.log. nmcli 放后台线程, 同 wifi_panel.
LAN1 没插线时探测不算数 (收不到 OFFER 不代表对面没有 DHCP), 不启用发地址.
"""

import tkinter as tk
from tkinter import messagebox

import cartlog
import netprof_core as npc
from wifi_panel import BG, BOX, FG, FONT, FONT_MONO, BridgePanel, WifiPanel, _run


class NetProfPanel(BridgePanel):
    _btn = WifiPanel._btn
    _say = WifiPanel._say
    _sel = WifiPanel._sel

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.profs = []
        self.prev = {}                  # 网口 -> 切换前生效的配置, 给「回退」用
        self._build()
        self.reload()

    # ------------------------------------------------------------ UI

    def _radio(self, parent, var, options):
        for value, text in options:
            tk.Radiobutton(parent, text=text, value=value, variable=var, font=FONT, bg=BG, fg="#c8cede",
                           selectcolor=BOX, activebackground=BG, activeforeground=FG,
                           command=self._form_changed).pack(side="left", padx=4)

    def _build(self):
        tk.Label(self, text="网口配置文件 · 同一网口只生效一个 · 发地址只在 LAN1 · 无线不归这里管(留作后路)",
                 font=FONT, bg=BG, fg="#8b93a7", anchor="w").pack(fill="x", padx=16, pady=(10, 4))

        self.lb = tk.Listbox(self, font=FONT_MONO, bg=BOX, fg=FG, relief="flat", height=10,
                             selectbackground="#3a4560", exportselection=False, activestyle="none")
        self.lb.pack(fill="both", expand=True, padx=16, pady=4)
        self.lb.bind("<Double-Button-1>", lambda _e: self.switch())
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12)
        self._btn(bar, "切换到此", self.switch, bg="#26577c")
        self._btn(bar, "回退上一个", self.rollback)
        self._btn(bar, "刷新", self.reload)
        self._btn(bar, "删除", self.delete, bg="#47202a")

        form = tk.LabelFrame(self, text=" 新建 ", font=FONT, bg=BG, fg="#8b93a7", bd=1, relief="groove")
        form.pack(fill="x", padx=16, pady=(10, 4))
        row = tk.Frame(form, bg=BG)
        row.pack(fill="x", pady=4)
        self.iface_var = tk.StringVar(value="enp3s0")
        self._radio(row, self.iface_var, [(i, f"{n} ({i})") for i, n in npc.IFACES.items()])
        tk.Label(row, text="  |  ", bg=BG, fg="#6b7489").pack(side="left")
        self.kind_var = tk.StringVar(value="static")
        self._radio(row, self.kind_var, [("dhcp", npc.KINDS["dhcp"]), ("static", npc.KINDS["static"])])

        row = tk.Frame(form, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="模板:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        self.tpl_var = tk.StringVar(value="手填")
        names = ["手填"] + [t[0] for t in npc.STATIC_TEMPLATES]
        om = tk.OptionMenu(row, self.tpl_var, *names, command=lambda _v: self._use_template())
        om.config(font=FONT, bg=BOX, fg=FG, activebackground="#3a4560", relief="flat", highlightthickness=0)
        om["menu"].config(font=FONT, bg=BOX, fg=FG)
        om.pack(side="left", padx=6)
        tk.Label(row, text="名字:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(10, 0))
        self.label_var = tk.StringVar()
        tk.Entry(row, textvariable=self.label_var, width=14, font=FONT_MONO, bg="#10141c", fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        tk.Label(row, text="对端地址:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(10, 0))
        self.peer_var = tk.StringVar()
        self.peer_var.trace_add("write", lambda *_: self._form_changed())
        self.peer_entry = tk.Entry(row, textvariable=self.peer_var, width=18, font=FONT_MONO, bg="#10141c",
                                   fg=FG, insertbackground=FG, relief="flat")
        self.peer_entry.pack(side="left", padx=6, ipady=3)
        self.peer_entry.bind("<Return>", lambda _e: self.create())
        self._btn(row, "建好并切换", self.create, bg="#26577c")
        self.self_lbl = tk.Label(form, text="", font=FONT, bg=BG, fg="#8b93a7", anchor="w")
        self.self_lbl.pack(fill="x", padx=6, pady=(0, 6))

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))
        self._form_changed()

    def _use_template(self):
        hit = dict(npc.STATIC_TEMPLATES).get(self.tpl_var.get())
        if hit:
            self.kind_var.set("static")
            self.label_var.set(self.tpl_var.get())
            self.peer_var.set(hit)

    def _form_changed(self):
        if self.kind_var.get() != "static":
            self.peer_entry.config(state="disabled")
            self.self_lbl.config(text="DHCP 客户端: 插上就自动拿地址")
            return
        self.peer_entry.config(state="normal")
        peer = self.peer_var.get().strip()
        try:
            self.self_lbl.config(text=f"推车将用 {npc.pick_self(peer)} (不抢默认路由)" if peer else
                                 "填对端设备的地址, 比如交换机出厂管理地址; 推车自动挑同网段一个空地址")
        except ValueError as ex:
            self.self_lbl.config(text=str(ex))

    # ------------------------------------------------------------ 列表

    def reload(self):
        def done(res):
            if isinstance(res, Exception):
                return self._say(f"读取失败: {res}", ok=False)
            self.profs = res
            self._render()
            self._say(f"{len(self.profs)} 个配置文件")
        self._bg(lambda: npc.parse_cons(_run(npc.CMD_CONS)[1]), done, "读取中…")

    def _render(self):
        self.lb.delete(0, "end")
        link = {i: npc.carrier(i) for i in npc.IFACES}
        for p in self.profs:
            mark = "*" if p["active"] else " "
            tag = " (内置)" if p["builtin"] else ""
            cable = "" if link[p["iface"]] else "  [没插线]"
            self.lb.insert("end", f"{npc.IFACES[p['iface']]} {mark} {p['label']:<12} "
                                  f"{npc.KINDS[p['kind']]}{tag}{cable}")

    def _active(self, iface):
        return next((p for p in self.profs if p["iface"] == iface and p["active"]), None)

    # ------------------------------------------------------------ 动作

    def switch(self, target=None):
        if target is None:
            _, target = self._sel(self.lb, self.profs)
        if not target:
            return self._say("先在上面选一个配置文件", ok=False)
        cur = self._active(target["iface"])
        if cur and cur["uuid"] == target["uuid"]:
            return self._say("已经是这个了")
        plan = npc.plan_switch(cur, target)
        if plan["warn_remote"] and not messagebox.askyesno(
                "LAN2 管理口", "LAN2 是管理口, 切换后经它的远程连接会断 (无线和 tailscale 还在).\n要继续吗?",
                icon="warning"):
            return

        def work():
            if plan["probe"]:
                _run(["systemctl", "stop", "pxe-server"])           # 别听见自己
                offers = npc.dhcp_probe(target["iface"])
                if offers.unknown:
                    return False, "LAN1 没插线 (或读不到链路状态), 探测不了对面有没有 DHCP; 插好线再切"
                if offers:
                    return False, "对面已经有 DHCP 服务器 (" + ", ".join(o["server"] for o in offers) + \
                        "), 你插进别人的网络里了, 不启用发地址"
            if plan["stop_pxe"]:
                _run(["systemctl", "stop", *npc.PXE_UNITS])
            rc, out = _run(plan["up"], timeout=45)
            if rc and plan["rollback"]:
                _run(plan["rollback"], timeout=45)
                return False, f"切换失败, 已回到原配置: {out[-120:]}"
            return rc == 0, out[-160:]

        def done(res):
            ok, out = res if not isinstance(res, Exception) else (False, str(res))
            name = f"{npc.IFACES[target['iface']]} -> {target['label']}"
            cartlog.event("netprof", f"{name} {'成功' if ok else '失败: ' + out}")
            if ok:
                if cur:
                    self.prev[target["iface"]] = cur
                hint = " · PXE 服务没自动开, 要用去「装机」页打开" if target["kind"] == "serve" else ""
                self._say(f"已切换 {name}{hint}")
            else:
                self._say(out, ok=False)
            self.after(300, self.reload)
        self._bg(work, done, "探测对面有没有 DHCP…" if plan["probe"] else "切换中…")

    def rollback(self):
        _, sel = self._sel(self.lb, self.profs)
        iface = sel["iface"] if sel else next(iter(self.prev), None)
        prev = self.prev.get(iface)
        if not prev:
            return self._say("这个网口还没切换过, 没有可回退的", ok=False)
        self.switch(prev)

    def create(self):
        iface, kind, label = self.iface_var.get(), self.kind_var.get(), self.label_var.get().strip()
        try:
            addr = npc.pick_self(self.peer_var.get().strip()) if kind == "static" else None
            cmd = npc.cmd_create(iface, kind, label, addr=addr)
        except ValueError as ex:
            return self._say(str(ex), ok=False)
        name = npc.con_name(iface, kind, label)
        if any(p["name"] == name for p in self.profs):
            return self._say("同名配置已存在, 换个名字", ok=False)

        def work():
            rc, out = _run(cmd, timeout=30)
            return (rc == 0, out, npc.parse_cons(_run(npc.CMD_CONS)[1]))

        def done(res):
            if isinstance(res, Exception) or not res[0]:
                return self._say(f"新建失败: {res if isinstance(res, Exception) else res[1][-160:]}", ok=False)
            self.profs = res[2]
            self._render()
            cartlog.event("netprof", f"新建 {name} {addr or ''}")
            new = next((p for p in self.profs if p["name"] == name), None)
            if new:
                self.switch(new)
        self._bg(work, done, "新建中…")

    def delete(self):
        _, p = self._sel(self.lb, self.profs)
        if not p:
            return self._say("先选一个", ok=False)
        if p["builtin"]:
            return self._say("内置配置不能删", ok=False)
        if p["active"]:
            return self._say("正在用的不能删, 先切到别的", ok=False)
        if not messagebox.askyesno("删除", f"删除配置 {npc.IFACES[p['iface']]} · {p['label']}?"):
            return

        def done(res):
            ok = not isinstance(res, Exception) and res[0] == 0
            if ok:
                cartlog.event("netprof", f"删除 {p['name']}")
            self._say("已删除" if ok else f"删除失败: {res}", ok)
            self.after(300, self.reload)
        self._bg(lambda: _run(npc.cmd_delete(p["uuid"])), done)
