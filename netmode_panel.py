#!/usr/bin/env python3
"""Kiosk 网口模式面板 (#12): 上面按网口 (LAN1 / LAN2) 列当前模式、实际地址、是不是默认出口;
选中一个口, 下面选模式 (关闭 / DHCP / 静态), 静态一页填完 (地址/前缀、网关、经网关去的网段、DNS、出口).

规则都在 netmode_core: 切换 = undo 当前 -> apply 新, 失败自动撤并恢复原来的; 确认框直接列出
"将执行的命令"、"将删除旧配置"和"出问题时的 undo 命令". 每次 apply / undo / 删旧配置写 events.log ([netmode]).
取代旧的「网口配置文件」面板和「LAN2 管理口配置」两个入口 (#8: 两套入口改同一个口, 手感不对).
10/31: 网关一直能填 (LAN2 填了自动备用出口, LAN1 只给经网关的网段用); 「交还旧配置」去掉,
第一次接管一个口时新模式起来后备份再删它的旧配置. 无线不归这里管, 永远留作后路.
nmcli 放后台线程, 同 wifi_panel.
"""

import tkinter as tk
import uuid
from tkinter import messagebox

import cartlog
import netmode_core as nmc
import netprof_core as npc
from wifi_panel import BG, BOX, FG, FONT, FONT_MONO, BridgePanel, WifiPanel, _run
import theme as th

UP_TIMEOUT = 90         # nmcli con up 等 DHCP 最长 45 秒左右, 留余量


class NetModePanel(BridgePanel):
    _btn = WifiPanel._btn
    _say = WifiPanel._say

    def __init__(self, master, want=None, state_path=nmc.STATE_PATH, backup_root=nmc.BACKUP_DIR,
                 nm_dir=nmc.NM_DIR):
        super().__init__(master, bg=BG)
        self.state_path, self.backup_root, self.nm_dir = state_path, backup_root, nm_dir
        self.state, self.cons_text, self.details, self.pxe_active = nmc.empty_state(), "", {}, False
        self.views = nmc.port_views(self.state, "", "", "", {})
        self.port = want if want in nmc.PORTS else nmc.LAN2
        self._build()
        self.reload()

    # ------------------------------------------------------------ UI

    def _entry(self, parent, text, var, width, on_change=None):
        tk.Label(parent, text=text, font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(4, 0))
        var.trace_add("write", lambda *_: (on_change or self._form_changed)())
        e = tk.Entry(parent, textvariable=var, width=width, font=FONT_MONO, bg=th.BG, fg=FG,
                     insertbackground=FG, **th.SUNKEN, disabledbackground=BOX)
        e.pack(side="left", padx=6, ipady=3)
        e.bind("<Return>", lambda _e: self.apply())
        return e

    def _radio(self, parent, var, value, text):
        b = tk.Radiobutton(parent, text=text, value=value, variable=var, font=FONT, bg=BG, fg=th.TEXT,
                           selectcolor=BOX, activebackground=BG, activeforeground=FG,
                           disabledforeground=th.DISABLED, command=self._form_changed)
        b.pack(side="left", padx=4)
        return b

    def _build(self):
        tk.Label(self, text="网口模式 · 每个口同一时间一个模式, 都能撤 · 无线不归这里管(留作后路)",
                 font=FONT, bg=BG, fg=th.DIM, anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        self.lb = tk.Listbox(self, font=FONT_MONO, bg=th.BG, fg=FG, **th.SUNKEN, height=2,
                             selectbackground=th.ACTIVE, exportselection=False, activestyle="none")
        self.lb.pack(fill="x", padx=16, pady=4)
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._on_select())
        self.drift_lbl = tk.Label(self, text="", font=FONT, bg=BG, fg=th.WARN, anchor="w", justify="left")
        self.drift_lbl.pack(fill="x", padx=16)

        self.box = tk.LabelFrame(self, text="", font=FONT, bg=BG, fg=th.DIM, **th.GROOVE)
        self.box.pack(fill="x", padx=16, pady=(8, 4))
        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="模式:", font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(4, 0))
        self.mode_var = tk.StringVar(value="dhcp")
        self.mode_btns = {m: self._radio(row, self.mode_var, m, nmc.MODES[m]["title"]) for m in nmc.MODES}

        # 静态: 本机 地址/前缀 是主输入 (不猜掩码, #13); 对端可选, 只防撞址 / 按它预填; 模板 = 设备出厂地址
        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="模板:", font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(4, 0))
        self.tpl_var = tk.StringVar(value="手填")
        self.tpl_om = tk.OptionMenu(row, self.tpl_var, *(["手填"] + [t[0] for t in npc.STATIC_TEMPLATES]),
                                    command=lambda _v: self._use_template())
        self.tpl_om.config(font=FONT, bg=BOX, fg=FG, activebackground=th.ACTIVE, **th.RAISED,
                           highlightthickness=0)
        self.tpl_om["menu"].config(font=FONT, bg=BOX, fg=FG)
        self.tpl_om.pack(side="left", padx=6)
        self.addr_var, self.peer_var = tk.StringVar(), tk.StringVar()
        self.addr_e = self._entry(row, "本机 地址/前缀:", self.addr_var, 18)
        self.peer_e = self._entry(row, "对端(可选):", self.peer_var, 16)
        self.prefill_btn = self._btn(row, "按对端预填", self.prefill)

        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        self.gw_var, self.dns_var, self.routes_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.gw_e = self._entry(row, "网关:", self.gw_var, 16, on_change=self._gateway_changed)
        self.dns_e = self._entry(row, "DNS(可空, 逗号分隔):", self.dns_var, 24)

        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        self.routes_e = self._entry(row, "经网关去的网段(可空, 逗号分隔):", self.routes_var, 36)

        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="出口:", font=FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(4, 0))
        self.egress_var = tk.StringVar(value="never")
        self.egress_btns = {k: self._radio(row, self.egress_var, k, t) for k, t in nmc.EGRESS.items()}

        self.hint = tk.Label(self.box, text="", font=FONT, bg=BG, fg=th.DIM, anchor="w", justify="left")
        self.hint.pack(fill="x", padx=6, pady=(0, 4))
        bar = tk.Frame(self.box, bg=BG)
        bar.pack(fill="x", padx=2, pady=(0, 6))
        self._btn(bar, "应用", self.apply, bg=th.PRIMARY)
        self._btn(bar, "撤到干净状态", self.undo_clean, bg=th.DANGER_BG)
        self._btn(bar, "回到上一个", self.back)
        self._btn(bar, "刷新", self.reload)

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg=th.WARN, anchor="w", justify="left")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))
        self._form_changed()

    def _form(self):
        return {"mode": self.mode_var.get(), "addr": self.addr_var.get(), "peer": self.peer_var.get(),
                "gateway": self.gw_var.get(), "dns": self.dns_var.get(), "egress": self.egress_var.get(),
                "routes": self.routes_var.get()}

    def _gateway_changed(self):
        """10/31: LAN2 填了网关还是「不当出口」就自动改备用出口, 清空网关回到不当出口; LAN1 永远不当出口.
        只在网关框变化时纠正, 用户自己点的出口照旧 (提示行说明会怎样)."""
        self.egress_var.set(nmc.egress_for_gateway(self.port, self.egress_var.get(), self.gw_var.get()))
        self._form_changed()

    def _form_changed(self):
        mode = self.mode_var.get()
        st = nmc.form_state(self.port, mode, self.egress_var.get())
        if not st["egress"].get(self.egress_var.get()) and mode == "static":
            self.egress_var.set("never")                   # LAN1 不当出口 / 老记录的主出口: 落回"不当出口"
            st = nmc.form_state(self.port, mode, "never")
        for w in (self.addr_e, self.peer_e, self.prefill_btn, self.tpl_om):
            w.config(state="normal" if st["addr"] else "disabled")
        self.gw_e.config(state="normal" if st["gateway"] else "disabled")
        self.routes_e.config(state="normal" if st["routes"] else "disabled")
        self.dns_e.config(state="normal" if st["addr"] else "disabled")
        for k, b in self.egress_btns.items():
            b.config(state="normal" if st["egress"][k] else "disabled")
        self.hint.config(text=nmc.form_hint(self.port, mode, self._form()))

    def _use_template(self):
        """模板 = 设备出厂管理地址: 填进对端, 再按它预填本机 (预填仍可改)."""
        hit = dict(npc.STATIC_TEMPLATES).get(self.tpl_var.get())
        if hit:
            self.mode_var.set("static")
            self.peer_var.set(hit)
            self.prefill()

    def prefill(self):
        """#13: 按对端算一个同网段空地址填进本机; 对端必须带前缀, 不猜 /24."""
        try:
            self.addr_var.set(npc.pick_self(self.peer_var.get()))
        except ValueError as ex:
            self._say(str(ex), ok=False)

    # ------------------------------------------------------------ 列表

    def _view(self, iface):
        return next(v for v in self.views if v["iface"] == iface)

    def reload(self):
        def work():
            st = nmc.load_state(self.state_path)
            cons = _run(npc.CMD_CONS)[1]
            links = {i: npc.carrier(i) for i in nmc.PORTS}
            # 以太网连接的详情: 认 interface-name (接管时删哪些旧配置), 没 keyfile 时也当备份文本
            details = {u: _run(nmc.cmd_con_detail(u))[1] for u in nmc.eth_uuids(cons)}
            pxe = _run(["systemctl", "is-active", "--quiet", "pxe-server"])[0] == 0
            return st, cons, details, pxe, nmc.port_views(st, cons, _run(npc.CMD_IP_BR)[1],
                                                          _run(nmc.CMD_ROUTES)[1], links)

        def done(res):
            if isinstance(res, Exception):
                return self._say(f"读取失败: {res}", ok=False)
            self.state, self.cons_text, self.details, self.pxe_active, self.views = res
            self._render()
            self._pick(self.port)
            self._say(self.state.get("broken") or "选一个网口, 在下面改模式")
        self._bg(work, done, "读取中…")

    def _render(self):
        self.lb.delete(0, "end")
        for v in self.views:
            link = {True: "已插线", False: "没插线"}.get(v["link"], "链路未知")
            note = "  [注意]" if v["drift"] else ""
            self.lb.insert("end", f"{v['port']} {v['iface']:<7} {v['title']:<28} {v['ip']:<18} "
                                  f"{link}  {v['egress'] or '不出网'}{note}")

    def select_slot(self, iface):
        """顶栏 LAN1 / LAN2 灯点进来 (面板已开着): 选中那个口. 和串口面板同名, kiosk 统一调."""
        if iface in nmc.PORTS:
            self._pick(iface)

    def _on_select(self):
        cur = self.lb.curselection()
        if cur and cur[0] < len(self.views):
            self._pick(self.views[cur[0]]["iface"])

    def _pick(self, iface):
        """选中一个口: 表单按它当前的模式预填 (未托管就预选干净状态, 不自动改任何东西)."""
        self.port = iface
        v = self._view(iface)
        idx = [x["iface"] for x in self.views].index(iface)
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(idx)
        self.box.config(text=f" {v['port']} ({iface}) 改模式 ")
        self.drift_lbl.config(text=v["drift"])
        for m, b in self.mode_btns.items():
            b.config(state="disabled" if nmc.check_mode(iface, m) else "normal")
        p = v["rec"]["params"]
        self.mode_var.set(v["mode"] if v["managed"] else nmc.CLEAN[iface])
        self.addr_var.set(p.get("addr", ""))
        self.gw_var.set(p.get("gateway", ""))           # 先填网关 (会自动纠正出口), 再按记录落出口
        self.routes_var.set(p.get("routes", ""))
        self.dns_var.set(p.get("dns", ""))
        self.egress_var.set(p.get("egress", "never"))
        self._form_changed()

    # ------------------------------------------------------------ 动作

    def apply(self):
        try:
            new = nmc.target(self.port, self._form())
        except ValueError as ex:
            return self._say(str(ex), ok=False)
        self._switch(new)

    def undo_clean(self):
        """撤掉当前模式, 回到这个口的干净状态 (LAN1 关闭 / LAN2 DHCP)."""
        self._switch(nmc.rec(nmc.CLEAN[self.port], self.port))

    def back(self):
        prev = self.state.get("prev", {}).get(self.port)
        if not prev:
            return self._say("这个口还没切换过, 没有上一个", ok=False)
        if prev["mode"] == nmc.LEGACY:                  # 1.09.00 记下的旧连接
            return self._say(nmc.NO_HANDBACK, ok=False)
        self._switch(prev)

    def _switch(self, new):
        iface = new["ports"][0]
        view = self._view(iface)
        if nmc.same(view["rec"], new) and not view["drift"]:
            return self._say("已经是这个了")
        try:
            plan = nmc.plan_switch(self.state, new, self.cons_text, lambda: str(uuid.uuid4()),
                                   links={v["iface"]: v["link"] for v in self.views},
                                   details=self.details, pxe_active=self.pxe_active)
        except ValueError as ex:
            return self._say(str(ex), ok=False)
        ip = view["ip"] if view["ip"] != "-" else ""
        if not messagebox.askyesno("切换网口模式", nmc.confirm_text(plan, ip, self.backup_root),
                                   icon="warning", parent=self):
            return self._say("没确认, 网口没动", ok=False)
        state, path, root, nm_dir = self.state, self.state_path, self.backup_root, self.nm_dir

        def log(msg):
            cartlog.event("netmode", msg)

        def work():
            return nmc.run_switch(plan, state, lambda c: _run(c, timeout=UP_TIMEOUT),
                                  lambda s: nmc.save_state(path, s), log,
                                  backup=lambda cons: nmc.backup_cons(cons, root=root, nm_dir=nm_dir))

        def done(res):
            if isinstance(res, Exception):
                ok, msg = False, f"出错: {res}"
            else:
                ok, st, msg = res
                if st is not None:
                    self.state = st
            self._say(msg, ok)
            self.after(300, self.reload)
        self._bg(work, done, "切换中…")
