#!/usr/bin/env python3
"""Kiosk 网口模式面板 (#12): 上面按网口 (LAN1 / LAN2) 列当前模式、实际地址、是不是默认出口;
选中一个口, 下面选模式 (关闭 / DHCP / 静态 / 交还旧配置), 静态一页填完 (地址/前缀、网关、DNS、出口优先级).

规则都在 netmode_core: 切换 = undo 当前 -> apply 新, 失败自动撤并恢复原来的; 确认框直接列出
"将执行的命令"和"出问题时的 undo 命令". 每次 apply / undo 写 events.log ([netmode]).
取代旧的「网口配置文件」面板和「LAN2 管理口配置」两个入口 (#8: 两套入口改同一个口, 手感不对).
交还装机配置 (Lan1-pxe) 前照旧先探测链路上有没有别人的 DHCP. 无线不归这里管, 永远留作后路.
nmcli 放后台线程, 同 wifi_panel.
"""

import tkinter as tk
import uuid
from tkinter import messagebox

import cartlog
import netmode_core as nmc
import netprof_core as npc
from wifi_panel import BG, BOX, FG, FONT, FONT_MONO, BridgePanel, WifiPanel, _run

UP_TIMEOUT = 90         # nmcli con up 等 DHCP 最长 45 秒左右, 留余量


class NetModePanel(BridgePanel):
    _btn = WifiPanel._btn
    _say = WifiPanel._say

    def __init__(self, master, want=None, state_path=nmc.STATE_PATH):
        super().__init__(master, bg=BG)
        self.state_path = state_path
        self.state, self.cons_text = nmc.empty_state(), ""
        self.views = nmc.port_views(self.state, "", "", "", {})
        self.port = want if want in nmc.PORTS else nmc.LAN2
        self._build()
        self.reload()

    # ------------------------------------------------------------ UI

    def _entry(self, parent, text, var, width):
        tk.Label(parent, text=text, font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        var.trace_add("write", lambda *_: self._form_changed())
        e = tk.Entry(parent, textvariable=var, width=width, font=FONT_MONO, bg="#10141c", fg=FG,
                     insertbackground=FG, relief="flat", disabledbackground=BOX)
        e.pack(side="left", padx=6, ipady=3)
        e.bind("<Return>", lambda _e: self.apply())
        return e

    def _radio(self, parent, var, value, text):
        b = tk.Radiobutton(parent, text=text, value=value, variable=var, font=FONT, bg=BG, fg="#c8cede",
                           selectcolor=BOX, activebackground=BG, activeforeground=FG,
                           disabledforeground="#4b5368", command=self._form_changed)
        b.pack(side="left", padx=4)
        return b

    def _build(self):
        tk.Label(self, text="网口模式 · 每个口同一时间一个模式, 都能撤 · 无线不归这里管(留作后路)",
                 font=FONT, bg=BG, fg="#8b93a7", anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        self.lb = tk.Listbox(self, font=FONT_MONO, bg=BOX, fg=FG, relief="flat", height=2,
                             selectbackground="#3a4560", exportselection=False, activestyle="none")
        self.lb.pack(fill="x", padx=16, pady=4)
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._on_select())
        self.drift_lbl = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w", justify="left")
        self.drift_lbl.pack(fill="x", padx=16)

        self.box = tk.LabelFrame(self, text="", font=FONT, bg=BG, fg="#8b93a7", bd=1, relief="groove")
        self.box.pack(fill="x", padx=16, pady=(8, 4))
        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="模式:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        self.mode_var = tk.StringVar(value="dhcp")
        self.mode_btns = {m: self._radio(row, self.mode_var, m, nmc.MODES[m]["title"]) for m in nmc.MODES}
        self.mode_btns[nmc.LEGACY] = self._radio(row, self.mode_var, nmc.LEGACY, "交还旧配置:")
        self.legacy_var = tk.StringVar(value="")
        self.legacy_om = tk.OptionMenu(row, self.legacy_var, "")
        self.legacy_om.config(font=FONT, bg=BOX, fg=FG, activebackground="#3a4560", relief="flat",
                              highlightthickness=0)
        self.legacy_om["menu"].config(font=FONT, bg=BOX, fg=FG)
        self.legacy_om.pack(side="left", padx=4)

        # 静态: 本机 地址/前缀 是主输入 (不猜掩码, #13); 对端可选, 只防撞址 / 按它预填; 模板 = 设备出厂地址
        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="模板:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        self.tpl_var = tk.StringVar(value="手填")
        self.tpl_om = tk.OptionMenu(row, self.tpl_var, *(["手填"] + [t[0] for t in npc.STATIC_TEMPLATES]),
                                    command=lambda _v: self._use_template())
        self.tpl_om.config(font=FONT, bg=BOX, fg=FG, activebackground="#3a4560", relief="flat",
                           highlightthickness=0)
        self.tpl_om["menu"].config(font=FONT, bg=BOX, fg=FG)
        self.tpl_om.pack(side="left", padx=6)
        self.addr_var, self.peer_var = tk.StringVar(), tk.StringVar()
        self.addr_e = self._entry(row, "本机 地址/前缀:", self.addr_var, 18)
        self.peer_e = self._entry(row, "对端(可选):", self.peer_var, 16)
        self.prefill_btn = self._btn(row, "按对端预填", self.prefill)

        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        self.gw_var, self.dns_var = tk.StringVar(), tk.StringVar()
        self.gw_e = self._entry(row, "网关:", self.gw_var, 16)
        self.dns_e = self._entry(row, "DNS(可空, 逗号分隔):", self.dns_var, 24)

        row = tk.Frame(self.box, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="出口:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        self.egress_var = tk.StringVar(value="never")
        self.egress_btns = {k: self._radio(row, self.egress_var, k, t) for k, t in nmc.EGRESS.items()}

        self.hint = tk.Label(self.box, text="", font=FONT, bg=BG, fg="#8b93a7", anchor="w", justify="left")
        self.hint.pack(fill="x", padx=6, pady=(0, 4))
        bar = tk.Frame(self.box, bg=BG)
        bar.pack(fill="x", padx=2, pady=(0, 6))
        self._btn(bar, "应用", self.apply, bg="#26577c")
        self._btn(bar, "撤到干净状态", self.undo_clean, bg="#47202a")
        self._btn(bar, "回到上一个", self.back)
        self._btn(bar, "刷新", self.reload)

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w", justify="left")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))
        self._form_changed()

    def _form(self):
        return {"mode": self.mode_var.get(), "addr": self.addr_var.get(), "peer": self.peer_var.get(),
                "gateway": self.gw_var.get(), "dns": self.dns_var.get(), "egress": self.egress_var.get(),
                "legacy": self.legacy_var.get()}

    def _form_changed(self):
        mode = self.mode_var.get()
        st = nmc.form_state(self.port, mode, self.egress_var.get())
        if not st["egress"].get(self.egress_var.get()) and mode == "static":
            self.egress_var.set("never")                   # LAN1 不许当出口: 选不了的项落回"不当出口"
            st = nmc.form_state(self.port, mode, "never")
        for w in (self.addr_e, self.peer_e, self.prefill_btn, self.tpl_om):
            w.config(state="normal" if st["addr"] else "disabled")
        for w in (self.gw_e,):
            w.config(state="normal" if st["gateway"] else "disabled")
        self.dns_e.config(state="normal" if st["addr"] else "disabled")
        for k, b in self.egress_btns.items():
            b.config(state="normal" if st["egress"][k] else "disabled")
        self.legacy_om.config(state="normal" if mode == nmc.LEGACY else "disabled")
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
            return st, cons, nmc.port_views(st, cons, _run(npc.CMD_IP_BR)[1], _run(nmc.CMD_ROUTES)[1], links)

        def done(res):
            if isinstance(res, Exception):
                return self._say(f"读取失败: {res}", ok=False)
            self.state, self.cons_text, self.views = res
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
        """选中一个口: 表单按它当前的模式预填 (未托管就预选交还给正在用的那条旧连接)."""
        self.port = iface
        v = self._view(iface)
        idx = [x["iface"] for x in self.views].index(iface)
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(idx)
        self.box.config(text=f" {v['port']} ({iface}) 改模式 ")
        self.drift_lbl.config(text=v["drift"])
        for m, b in self.mode_btns.items():
            b.config(state="disabled" if nmc.check_mode(iface, m) or (m == nmc.LEGACY and not v["legacy"])
                     else "normal")
        menu = self.legacy_om["menu"]
        menu.delete(0, "end")
        for c in v["legacy"]:
            menu.add_command(label=c["name"], command=lambda n=c["name"]: self.legacy_var.set(n))
        active = next((c["name"] for c in v["legacy"] if c["active"]), v["legacy"][0]["name"] if v["legacy"] else "")
        self.legacy_var.set(active)
        p = v["rec"]["params"]
        self.mode_var.set(v["mode"] if v["managed"] or v["legacy"] else nmc.CLEAN[iface])
        self.addr_var.set(p.get("addr", ""))
        self.gw_var.set(p.get("gateway", ""))
        self.dns_var.set(p.get("dns", ""))
        self.egress_var.set(p.get("egress", "never"))
        self._form_changed()

    # ------------------------------------------------------------ 动作

    def apply(self):
        try:
            new = nmc.target(self.port, self._form(), self._view(self.port)["legacy"])
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
        self._switch(prev)

    def _switch(self, new):
        iface = new["ports"][0]
        view = self._view(iface)
        if nmc.same(view["rec"], new) and not view["drift"]:
            return self._say("已经是这个了")
        try:
            plan = nmc.plan_switch(self.state, new, self.cons_text, lambda: str(uuid.uuid4()))
        except ValueError as ex:
            return self._say(str(ex), ok=False)
        ip = view["ip"] if view["ip"] != "-" else ""
        if not messagebox.askyesno("切换网口模式", nmc.confirm_text(plan, ip), icon="warning", parent=self):
            return self._say("没确认, 网口没动", ok=False)
        state, path = self.state, self.state_path

        def log(msg):
            cartlog.event("netmode", msg)

        def work():
            if plan["probe"]:
                _run(["systemctl", "stop", "pxe-server"])           # 别听见自己
                offers = npc.dhcp_probe(iface)
                why = ("LAN1 没插线 (或读不到链路状态), 探测不了对面有没有 DHCP; 插好线再切" if offers.unknown else
                       "对面已经有 DHCP 服务器 (" + ", ".join(o["server"] for o in offers) +
                       "), 你插进别人的网络里了, 不交还装机配置" if offers else "")
                if why:
                    log(f"{nmc.PORTS[iface]} 交还 {new['params'].get('name')} 未切: {why}")
                    return False, None, why
            return nmc.run_switch(plan, state, lambda c: _run(c, timeout=UP_TIMEOUT),
                                  lambda s: nmc.save_state(path, s), log)

        def done(res):
            if isinstance(res, Exception):
                ok, msg = False, f"出错: {res}"
            else:
                ok, st, msg = res
                if st is not None:
                    self.state = st
            if ok and new["mode"] == nmc.LEGACY and plan["probe"]:
                msg += " · PXE 服务没自动开, 要用去「装机」页打开"
            self._say(msg, ok)
            self.after(300, self.reload)
        self._bg(work, done, "探测对面有没有 DHCP…" if plan["probe"] else "切换中…")
