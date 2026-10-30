#!/usr/bin/env python3
"""Kiosk 无线管理面板: 扫描/连接/输密码(可显示)/已保存网络按优先级排序/忘记;
底部是校园网认证: 改账号密码、立即认证、看最近一次结果.

nmcli 调用放后台线程, 结果经 UiBridge 回到 Tk 线程. 规则见 wifi_core.
推车眼下靠无线上网时 (网线拔了), 忘记 / 改密码 / 重输密码都先弹确认; 重输密码先建后删, 连不上就回滚.
"""

import subprocess
import time
import tkinter as tk
import uuid
from tkinter import messagebox

import ruijie_portal as rp
import wifi_core as wc
from kiosk_ui_core import UiBridge

FONT = ("Noto Sans CJK SC", 13)
FONT_MONO = ("Noto Sans Mono CJK SC", 14)
BG, FG, BOX = "#161b26", "#e8eaf0", "#1f2a3d"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as ex:
        return 1, str(ex)


AUTH_STATES = {"online": "已在线", "login ok": "登录成功", "skip": "跳过", "login fail": "登录失败",
               "error": "出错", "down": "连不上网", "portal?": "认证页没认出来",
               "untrusted": "认证页主机不在白名单, 没发密码"}
EGRESS_WARN = "{name} 是推车眼下的上网出口 (网线没插), {what}后远程 (SSH / tailscale) 可能会断.\n要继续吗?"


class BridgePanel(tk.Frame):
    """面板基类: 自带一个 UiBridge, 后台命令只经它回到 Tk 线程; 面板销毁时停掉轮询."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.ui = UiBridge(self)
        self.ui.start()

    def destroy(self):
        self.ui.stop()
        super().destroy()

    def _bg(self, work, done, note="处理中…"):
        """work() 在线程里跑, done(result) 回 Tk 线程 (异常对象也当结果传). 同一面板同时只跑一件事."""
        def back(res):
            if self.winfo_exists():
                done(res)
        if self.ui.run_bg(work, back, key="panel"):
            self._say(note)
        else:
            self._say("上一步还在处理中…", ok=False)


class WifiPanel(BridgePanel):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.nets, self.saved, self.routes = [], [], ""
        self._build()
        self.reload(rescan=True)        # 缓存里往往只有当前连着的那个

    # ------------------------------------------------------------ UI

    def _btn(self, parent, text, cmd, bg=BOX):
        b = tk.Button(parent, text=text, font=FONT, command=cmd, bg=bg, fg=FG,
                      activebackground="#3a4560", relief="flat", cursor="hand2", padx=12, pady=4)
        b.pack(side="left", padx=4)
        return b

    def _list(self, parent, title):
        col = tk.Frame(parent, bg=BG)
        col.pack(side="left", fill="both", expand=True, padx=8)
        tk.Label(col, text=title, font=FONT, bg=BG, fg="#8b93a7", anchor="w").pack(fill="x")
        lb = tk.Listbox(col, font=FONT_MONO, bg=BOX, fg=FG, relief="flat", height=14,
                        selectbackground="#3a4560", exportselection=False, activestyle="none")
        lb.pack(fill="both", expand=True, pady=4)
        bar = tk.Frame(col, bg=BG)
        bar.pack(fill="x")
        return lb, bar

    def _build(self):
        tk.Label(self, text="无线网络 (wlp4s0) · 插着网线时永远走有线, 无线只当备胎",
                 font=FONT, bg=BG, fg="#8b93a7", anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        cols = tk.Frame(self, bg=BG)
        cols.pack(fill="both", expand=True, padx=8)

        self.scan_lb, bar = self._list(cols, "附近的网络")
        self._btn(bar, "刷新扫描", lambda: self.reload(rescan=True))
        self._btn(bar, "连接", self.connect_selected, bg="#26577c")
        self.scan_lb.bind("<Double-Button-1>", lambda _e: self.connect_selected())

        self.saved_lb, bar = self._list(cols, "已保存 · 优先级从高到低 (自动连接时从上往下试)")
        self._btn(bar, "上移", lambda: self.move(-1))
        self._btn(bar, "下移", lambda: self.move(+1))
        self._btn(bar, "连接", self.up_saved, bg="#26577c")
        self._btn(bar, "显示已存密码", self.load_psk)
        self._btn(bar, "忘记", self.forget, bg="#47202a")
        self.saved_lb.bind("<<ListboxSelect>>", lambda _e: self._pick_saved())

        form = tk.Frame(self, bg=BG)
        form.pack(fill="x", padx=16, pady=(8, 4))
        tk.Label(form, text="网络:", font=FONT, bg=BG, fg="#c8cede").pack(side="left")
        self.ssid_var = tk.StringVar()
        tk.Entry(form, textvariable=self.ssid_var, width=20, font=FONT_MONO, bg="#10141c", fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        tk.Label(form, text="密码:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(10, 0))
        self.pw_var = tk.StringVar()
        self.pw_entry = tk.Entry(form, textvariable=self.pw_var, width=24, show="*", font=FONT_MONO,
                                 bg="#10141c", fg=FG, insertbackground=FG, relief="flat")
        self.pw_entry.pack(side="left", padx=6, ipady=3)
        self.pw_entry.bind("<Return>", lambda _e: self.connect_form())
        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text="显示密码", variable=self.show_var, command=self._toggle_show,
                       font=FONT, bg=BG, fg="#c8cede", selectcolor=BOX, activebackground=BG,
                       activeforeground=FG).pack(side="left", padx=6)
        self._btn(form, "连接并保存", self.connect_form, bg="#26577c")
        self._btn(form, "只改密码", self.change_psk)

        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg="#f0b429", anchor="w")
        self.msg.pack(fill="x", padx=16, pady=(2, 10))
        self._build_auth()

    def _build_auth(self):
        conf = rp._read_json(rp.CONF) or {}
        title = f" 校园网认证 (只在连着 {conf.get('ssid') or '配置的无线'} 时自动跑, 每 2 分钟) "
        box = tk.LabelFrame(self, text=title, font=FONT, bg=BG, fg="#8b93a7", bd=1, relief="groove")
        box.pack(fill="x", padx=16, pady=(0, 10))
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=4)
        self.auth_user = tk.StringVar(value=conf.get("user", ""))
        self.auth_pw = tk.StringVar(value=conf.get("password", ""))
        tk.Label(row, text="账号:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        tk.Entry(row, textvariable=self.auth_user, width=16, font=FONT_MONO, bg="#10141c", fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        tk.Label(row, text="密码:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(10, 0))
        self.auth_pw_entry = tk.Entry(row, textvariable=self.auth_pw, width=20, show="*", font=FONT_MONO,
                                      bg="#10141c", fg=FG, insertbackground=FG, relief="flat")
        self.auth_pw_entry.pack(side="left", padx=6, ipady=3)
        self.auth_pw_entry.bind("<Return>", lambda _e: self.auth_save())
        self.auth_show = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="显示", variable=self.auth_show, font=FONT, bg=BG, fg="#c8cede",
                       selectcolor=BOX, activebackground=BG, activeforeground=FG,
                       command=lambda: self.auth_pw_entry.config(show="" if self.auth_show.get() else "*")
                       ).pack(side="left", padx=4)
        self._btn(row, "保存", self.auth_save)
        self._btn(row, "立即认证", self.auth_now, bg="#26577c")
        self.auth_lbl = tk.Label(box, text="", font=FONT, bg=BG, fg="#8b93a7", anchor="w")
        self.auth_lbl.pack(fill="x", padx=6, pady=(0, 6))
        self.auth_trust_row = tk.Frame(box, bg=BG)         # 认证页换了主机时才露出来
        self.auth_trust_btn = self._btn(self.auth_trust_row, "", self.auth_trust, bg="#7c5a26")
        self._auth_render()

    def _auth_render(self):
        st = rp._read_json(rp.STATUS)
        if not st:
            self.auth_lbl.config(text="最近一次: 还没跑过", fg="#8b93a7")
            return
        when = time.strftime("%H:%M:%S", time.localtime(st.get("time", 0)))
        text = AUTH_STATES.get(st["state"], st["state"])
        self.auth_lbl.config(text=f"最近一次 {when}: {text} {st.get('message', '')}".rstrip(),
                             fg="#7ee787" if st.get("ok") else "#ff7b72")
        host = rp.pending_host(st)
        if host:
            self.auth_trust_btn.config(text=f"信任认证页 {host}")
            self.auth_trust_row.pack(fill="x", padx=6, pady=(0, 6))
        else:
            self.auth_trust_row.pack_forget()

    def auth_trust(self):
        """认证页换了主机 (学校换入口): 人确认后加进白名单, 再认证一次. 原来的主机留着."""
        host = rp.pending_host(rp._read_json(rp.STATUS))
        if not host:
            return self._auth_render()
        conf = rp._read_json(rp.CONF) or {}
        if not messagebox.askyesno(
                "信任新的认证页",
                f"这次被跳到的认证页是\n\n    {host}\n\n原来信任的是: {', '.join(conf.get('portal_hosts') or []) or '(无)'}\n\n"
                f"信任以后, 连着 {conf.get('ssid') or '配置的无线'} 时会把校园网账号密码发给它.\n"
                "只有确定推车连的是真的校园网 (不是同名的假热点) 才点「是」.", parent=self):
            return

        def work():
            rp.conf_save(rp.conf_trust(rp._read_json(rp.CONF) or {}, host))
            return _run(["systemctl", "start", "campus-auth.service"], timeout=60)

        def done(res):
            if isinstance(res, Exception):
                return self._say(f"没改成: {type(res).__name__}: {res}", ok=False)
            self._auth_render()
            self._say(f"已信任 {host}, 认证完成" if res[0] == 0 else f"已信任 {host}, 但认证没成功, 原因见下面一行",
                      res[0] == 0)
        self._bg(work, done, "保存并认证中…")

    def auth_save(self):
        user, pw = self.auth_user.get(), self.auth_pw.get()

        def work():                         # 配置里没写 ssid 时要跑 iw 看当前连着哪个, 放后台
            old = rp._read_json(rp.CONF) or {}
            ssid = None if old.get("ssid") else rp.current_ssid(old.get("iface", "wlp4s0"))
            rp.conf_save(rp.conf_update(old, user, pw, ssid=ssid))

        def done(res):
            if isinstance(res, (ValueError, OSError)):
                return self._say(f"保存失败: {res}", ok=False)
            if isinstance(res, Exception):
                return self._say(f"保存失败: {type(res).__name__}: {res}", ok=False)
            self._say("认证账号已保存, 下次自动认证用新的; 要马上试点「立即认证」")
        self._bg(work, done, "保存中…")

    def auth_now(self):
        def done(res):
            self._auth_render()
            ok = not isinstance(res, Exception) and res[0] == 0
            self._say("认证完成" if ok else "认证没成功, 原因见下面一行", ok)
        self._bg(lambda: _run(["systemctl", "start", "campus-auth.service"], timeout=60), done, "认证中…")

    def _toggle_show(self):
        self.pw_entry.config(show="" if self.show_var.get() else "*")

    def _say(self, text, ok=True):
        self.msg.config(text=text, fg="#7ee787" if ok else "#ff7b72")

    # ------------------------------------------------------------ 后台

    def reload(self, rescan=True):
        def work():
            _, scan = _run(wc.cmd_scan(rescan=rescan))
            _, saved = _run(wc.CMD_SAVED)
            saved = wc.parse_saved(saved)
            for s in saved:                 # 连接名和 SSID 可能不同 (大小写、改过名)
                s["ssid"] = _run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "uuid", s["uuid"]])[1]
            routes = _run(["ip", "route", "show", "default"])[1]
            return wc.parse_scan(scan), saved, routes

        def done(res):
            if isinstance(res, Exception):
                self._say(f"读取失败: {res}", ok=False)
                return
            self.nets, self.saved, self.routes = res
            self._render()
            self._say(f"附近 {len(self.nets)} 个网络, 已保存 {len(self.saved)} 个")
        self._bg(work, done, "扫描中…" if rescan else "读取中…")

    def _render(self):
        known = {s["ssid"] for s in self.saved}
        self.scan_lb.delete(0, "end")
        for n in self.nets:
            lock = "[加密]" if wc.key_mgmt(n["security"]) else "[开放]"
            tag = " [在用]" if n["in_use"] else (" [已保存]" if n["ssid"] in known else "")
            self.scan_lb.insert("end", f"{n['signal']:>3}% {lock} {n['ssid']}{tag}")
        self.saved_lb.delete(0, "end")
        for i, s in enumerate(self.saved, 1):
            flag = " [在用]" if s["active"] else ("" if s["autoconnect"] else " (不自动连)")
            self.saved_lb.insert("end", f"{i:>2}. {s['ssid'] or s['name']}{flag}")

    def _sel(self, lb, items):
        cur = lb.curselection()
        return (cur[0], items[cur[0]]) if cur and cur[0] < len(items) else (None, None)

    def _after_cmd(self, cmds, ok_text, rescan=False):
        self._after_plan({"do": cmds, "rollback": []}, ok_text, rescan)

    def _after_plan(self, plan, ok_text, rescan=False, prepare=None):
        """按 wifi_core 的计划执行 (失败自动回滚). prepare() 在后台先跑, 返回计划或错误字符串."""
        def work():
            p = prepare() if prepare else plan
            if isinstance(p, str):
                return False, p, False
            return wc.run_plan(p, lambda c: _run(c, timeout=45))

        def done(res):
            ok, out, rolled = res if not isinstance(res, Exception) else (False, str(res), False)
            tail = ", 已回到原来的设置" if rolled else ""
            self._say(ok_text if ok else f"失败{tail}: {out[-160:]}", ok)
            self.after(300, lambda: self.reload(rescan=rescan))
        self._bg(work, done)

    def _egress_ok(self, s, what):
        """对当前出口连接动手前确认; 不是出口直接放行."""
        if not s or not wc.is_egress(s, self.routes):
            return True
        return messagebox.askyesno("远程会断", EGRESS_WARN.format(name=s["ssid"] or s["name"], what=what),
                                   icon="warning")

    # ------------------------------------------------------------ 动作

    def connect_selected(self):
        _, n = self._sel(self.scan_lb, self.nets)
        if not n:
            return self._say("先在左边选一个网络", ok=False)
        saved = next((s for s in self.saved if s["ssid"] == n["ssid"]), None)
        if saved:
            return self._after_cmd([wc.cmd_up(saved["uuid"])], f"已连接 {n['ssid']}")
        self.ssid_var.set(n["ssid"])
        self.pw_var.set("")
        if wc.key_mgmt(n["security"]):
            self._say(f"{n['ssid']} 需要密码: 在下面输入后按回车或「连接并保存」")
            self.pw_entry.focus_set()
        else:
            self.connect_form()

    def connect_form(self):
        ssid, pw = self.ssid_var.get().strip(), self.pw_var.get()
        if not ssid:
            return self._say("网络名不能为空", ok=False)
        sec = next((n["security"] for n in self.nets if n["ssid"] == ssid), "WPA2" if pw else "")
        if wc.key_mgmt(sec) and len(pw) < 8:
            return self._say("WPA 密码至少 8 位", ok=False)
        cur = next((s for s in self.saved if s["ssid"] == ssid and s["active"]), None)
        if not self._egress_ok(cur, "重建这个连接"):
            return
        # 重输密码 = 重建: 先建新的并连上, 成功了才删旧的; 连不上就删掉新的、回到旧的
        plan = wc.plan_replace(self.saved, ssid, pw, sec, wc.next_priority(self.saved), str(uuid.uuid4()))
        self._after_plan(plan, f"已连接并保存 {ssid} (排在最上面)")

    def _pick_saved(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if s:
            self.ssid_var.set(s["ssid"] or s["name"])
            self.pw_var.set("")

    def move(self, delta):
        i, s = self._sel(self.saved_lb, self.saved)
        if s is None:
            return self._say("先在右边选一个已保存的网络", ok=False)
        order = wc.move(self.saved, i, delta)
        cmds = [wc.cmd_priority(x["uuid"], p) for x, p in zip(order, wc.priorities(len(order)))]
        self.saved = order
        self._render()
        self.saved_lb.selection_set(max(0, min(len(order) - 1, i + delta)))
        self._after_cmd(cmds, "优先级已更新")

    def up_saved(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if s:
            self._after_cmd([wc.cmd_up(s["uuid"])], f"已连接 {s['ssid'] or s['name']}")

    def load_psk(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if not s:
            return self._say("先在右边选一个已保存的网络", ok=False)

        def done(res):
            rc, out = res if not isinstance(res, Exception) else (1, str(res))
            self.ssid_var.set(s["ssid"] or s["name"])
            self.pw_var.set(out if rc == 0 else "")
            self._say("开放网络, 没有密码" if rc == 0 and not out else
                      "已填入密码框 (勾「显示密码」查看)" if rc == 0 else f"读取失败: {out}", ok=rc == 0)
        self._bg(lambda: _run(wc.cmd_show_psk(s["uuid"])), done, "读取中…")

    def change_psk(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if not s:
            return self._say("先在右边选要改密码的网络", ok=False)
        new = self.pw_var.get()
        if len(new) < 8:
            return self._say("WPA 密码至少 8 位", ok=False)
        if not self._egress_ok(s, "改密码"):
            return

        def prepare():                      # 先记下旧密码, 新密码连不上就写回去
            rc, old = _run(wc.cmd_show_psk(s["uuid"]))
            if rc:
                return f"读不到旧密码, 没改: {old[-120:]}"
            return wc.plan_change_psk(s["uuid"], old, new)
        self._after_plan(None, f"{s['ssid'] or s['name']} 密码已更新", prepare=prepare)

    def forget(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if not s or not self._egress_ok(s, "忘记它"):
            return
        if messagebox.askyesno("忘记网络", f"删除已保存的 {s['ssid'] or s['name']}?"):
            self._after_cmd([wc.cmd_delete(s["uuid"])], "已删除")
