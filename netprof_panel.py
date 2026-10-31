#!/usr/bin/env python3
"""Kiosk 网口配置文件面板: 列出 LAN1/LAN2 的配置文件, 切换 / 一键回退 / 新建 DHCP 或静态 / 删除.

规则在 netprof_core: 发地址只在 LAN1, 切到发地址前先探测链路上有没有别人的 DHCP;
LAN1 切到别的配置时先停 PXE 服务. 每次切换记 events.log. nmcli 放后台线程, 同 wifi_panel.
LAN1 没插线时探测不算数 (收不到 OFFER 不代表对面没有 DHCP), 不启用发地址.
静态地址由人填 (建议值只做预填); LAN1/LAN2 切换前都弹确认框, 写明原来/改为/回滚方式 (#8).
静态模式主输入是推车自己的 地址/前缀, 掩码按人填的来; 对端地址可选, 只防撞址和预填 (#13).
"""

import tkinter as tk
from tkinter import messagebox, simpledialog

import cartlog
import netprof_core as npc
from wifi_panel import BG, BOX, FG, FONT, FONT_MONO, BridgePanel, WifiPanel, _run


def ask_valid(parent, title, prompt, initial, check):
    """弹输入框让人填; check(text) 抛 ValueError 就把原因告诉人, 带着刚填的值重问. 取消返回 None."""
    text = initial
    while True:
        text = simpledialog.askstring(title, prompt, initialvalue=text, parent=parent)
        if text is None:
            return None
        try:
            return check(text)
        except ValueError as ex:
            messagebox.showerror(title, str(ex), parent=parent)


class NetProfPanel(BridgePanel):
    _btn = WifiPanel._btn
    _say = WifiPanel._say
    _sel = WifiPanel._sel

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.profs = []
        self._cons_text = ""
        self._ips = {}                  # 网口 -> 当前地址, 给切换确认框用 (reload 后台读)
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

        # #13: 主输入是推车自己的 地址/前缀 (不替人猜掩码); 对端可选, 只防撞址 / 按它预填
        row = tk.Frame(form, bg=BG)
        row.pack(fill="x", pady=4)
        self.self_var = tk.StringVar()
        self.peer_var = tk.StringVar()
        self.addr_entries = []
        for text, var, width in (("本机 地址/前缀:", self.self_var, 18), ("对端地址(可选):", self.peer_var, 18)):
            tk.Label(row, text=text, font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
            var.trace_add("write", lambda *_: self._form_changed())
            e = tk.Entry(row, textvariable=var, width=width, font=FONT_MONO, bg="#10141c",
                         fg=FG, insertbackground=FG, relief="flat")
            e.pack(side="left", padx=6, ipady=3)
            e.bind("<Return>", lambda _e: self.create())
            self.addr_entries.append(e)
        self._btn(row, "按对端预填", self.prefill)
        self._btn(row, "建好并切换", self.create, bg="#26577c")
        self.self_lbl = tk.Label(form, text="", font=FONT, bg=BG, fg="#8b93a7", anchor="w")
        self.self_lbl.pack(fill="x", padx=6, pady=(0, 6))

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))
        self._form_changed()

    def _lan2_note(self):
        """#8: LAN2 有两个入口能改, 直接标出来, 别让同一接口出现两份说法."""
        v = npc.lan2_view("enp3s0", getattr(self, "_cons_text", ""), ())
        return v["warning"] if v["conflict"] else ""

    def _use_template(self):
        """模板 = 设备出厂管理地址: 填进对端, 再按它预填本机 (预填仍可改)."""
        hit = dict(npc.STATIC_TEMPLATES).get(self.tpl_var.get())
        if hit:
            self.kind_var.set("static")
            self.label_var.set(self.tpl_var.get())
            self.peer_var.set(hit)
            self.prefill()

    def prefill(self):
        """#13: 按对端算一个同网段空地址填进本机; 对端必须带前缀, 不猜 /24."""
        try:
            self.self_var.set(npc.pick_self(self.peer_var.get()))
        except ValueError as ex:
            self._say(str(ex), ok=False)

    def _form_changed(self):
        static = self.kind_var.get() == "static"
        for e in self.addr_entries:
            e.config(state="normal" if static else "disabled")
        self.self_lbl.config(text=npc.static_hint(self.self_var.get(), self.peer_var.get()) if static
                             else "DHCP 客户端: 插上就自动拿地址")

    # ------------------------------------------------------------ 列表

    def reload(self):
        def work():
            text = _run(npc.CMD_CONS)[1]
            return npc.parse_cons(text), text, npc.parse_ip_br(_run(npc.CMD_IP_BR)[1])

        def done(res):
            if isinstance(res, Exception):
                return self._say(f"读取失败: {res}", ok=False)
            self.profs, self._cons_text, self._ips = res
            self._render()
            note = self._lan2_note()
            self._say(note if note else f"{len(self.profs)} 个配置文件")
        self._bg(work, done, "读取中…")

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
        # #8: LAN1/LAN2 都先确认 (原来/改为/回滚); 点否什么都不动. 自动回滚、探测这些保护照旧
        if not messagebox.askyesno(
                "切换网口配置", npc.switch_confirm_text(cur, target, plan, self._ips.get(target["iface"], "")),
                icon="warning", parent=self):
            return self._say("没确认, 未切换", ok=False)

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
        name = npc.con_name(iface, kind, label)
        try:
            why = npc.check(iface, kind) or npc.check_label(label)
            if why:
                raise ValueError(why)
            if any(p["name"] == name for p in self.profs):
                raise ValueError("同名配置已存在, 换个名字")
            addr = None
            if kind == "static":
                # #8/#13: 地址由人在表单里填 地址/前缀 (预填也只是起点); 对端填了只防撞址; 校验过了还要再确认一次
                addr = npc.static_form(self.self_var.get(), avoid=self.peer_var.get().strip())["addr"]
                if not messagebox.askyesno(
                        "新建静态地址",
                        f"将在 {iface} 上新建配置「{label}」\n地址: {addr}（不抢默认路由）\n\n"
                        f"确认写入 NetworkManager 吗？（切换前还会再确认一次，带回滚方式）",
                        icon="question", parent=self):
                    return self._say("没确认, 未写入", ok=False)
            cmd = npc.cmd_create(iface, kind, label, addr=addr, confirmed=True)
        except ValueError as ex:
            return self._say(str(ex), ok=False)

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
