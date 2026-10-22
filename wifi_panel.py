#!/usr/bin/env python3
"""Kiosk 无线管理面板: 扫描/连接/输密码(可显示)/已保存网络按优先级排序/忘记;
底部是校园网 (CAMPUSNET) 认证: 改账号密码、立即认证、看最近一次结果.

nmcli 调用放后台线程, 结果用 after() 回到 Tk 线程. 规则见 wifi_core.
"""

import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox

import ruijie_portal as rp
import wifi_core as wc

FONT = ("Noto Sans CJK SC", 13)
FONT_MONO = ("Noto Sans Mono CJK SC", 14)
BG, FG, BOX = "#161b26", "#e8eaf0", "#1f2a3d"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as ex:
        return 1, str(ex)


def _bars(sig):
    return "▂▄▆█"[: max(1, min(4, sig // 25 + 1))].ljust(4)


class WifiPanel(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.nets, self.saved = [], []
        self._busy = False
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

        self.saved_lb, bar = self._list(cols, "已保存 · 优先级 高 → 低 (自动连接时从上往下试)")
        self._btn(bar, "↑ 上移", lambda: self.move(-1))
        self._btn(bar, "↓ 下移", lambda: self.move(+1))
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
        self.pw_entry = tk.Entry(form, textvariable=self.pw_var, width=24, show="•", font=FONT_MONO,
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
        box = tk.LabelFrame(self, text=" 校园网认证 (只在连着 CAMPUSNET 时自动跑, 每 2 分钟) ", font=FONT,
                            bg=BG, fg="#8b93a7", bd=1, relief="groove")
        box.pack(fill="x", padx=16, pady=(0, 10))
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=4)
        conf = rp._read_json(rp.CONF) or {}
        self.auth_user = tk.StringVar(value=conf.get("user", ""))
        self.auth_pw = tk.StringVar(value=conf.get("password", ""))
        tk.Label(row, text="账号:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(4, 0))
        tk.Entry(row, textvariable=self.auth_user, width=16, font=FONT_MONO, bg="#10141c", fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        tk.Label(row, text="密码:", font=FONT, bg=BG, fg="#c8cede").pack(side="left", padx=(10, 0))
        self.auth_pw_entry = tk.Entry(row, textvariable=self.auth_pw, width=20, show="•", font=FONT_MONO,
                                      bg="#10141c", fg=FG, insertbackground=FG, relief="flat")
        self.auth_pw_entry.pack(side="left", padx=6, ipady=3)
        self.auth_pw_entry.bind("<Return>", lambda _e: self.auth_save())
        self.auth_show = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="显示", variable=self.auth_show, font=FONT, bg=BG, fg="#c8cede",
                       selectcolor=BOX, activebackground=BG, activeforeground=FG,
                       command=lambda: self.auth_pw_entry.config(show="" if self.auth_show.get() else "•")
                       ).pack(side="left", padx=4)
        self._btn(row, "保存", self.auth_save)
        self._btn(row, "立即认证", self.auth_now, bg="#26577c")
        self.auth_lbl = tk.Label(box, text="", font=FONT, bg=BG, fg="#8b93a7", anchor="w")
        self.auth_lbl.pack(fill="x", padx=6, pady=(0, 6))
        self._auth_render()

    def _auth_render(self):
        st = rp._read_json(rp.STATUS)
        if not st:
            self.auth_lbl.config(text="最近一次: 还没跑过", fg="#8b93a7")
            return
        when = time.strftime("%H:%M:%S", time.localtime(st.get("time", 0)))
        text = {"online": "已在线", "login ok": "登录成功", "skip": "跳过", "login fail": "登录失败",
                "error": "出错", "down": "连不上网", "portal?": "认证页没认出来"}.get(st["state"], st["state"])
        self.auth_lbl.config(text=f"最近一次 {when}: {text} {st.get('message', '')}".rstrip(),
                             fg="#7ee787" if st.get("ok") else "#ff7b72")

    def auth_save(self):
        try:
            conf = rp.conf_update(rp._read_json(rp.CONF), self.auth_user.get(), self.auth_pw.get())
            rp.conf_save(conf)
        except (ValueError, OSError) as ex:
            return self._say(f"保存失败: {ex}", ok=False)
        self._say("认证账号已保存, 下次自动认证用新的; 要马上试点「立即认证」")

    def auth_now(self):
        def done(res):
            self._auth_render()
            ok = not isinstance(res, Exception) and res[0] == 0
            self._say("认证完成" if ok else "认证没成功, 原因见下面一行", ok)
        self._bg(lambda: _run(["systemctl", "start", "campusnet-auth.service"], timeout=60), done, "认证中…")

    def _toggle_show(self):
        self.pw_entry.config(show="" if self.show_var.get() else "•")

    def _say(self, text, ok=True):
        self.msg.config(text=text, fg="#7ee787" if ok else "#ff7b72")

    # ------------------------------------------------------------ 后台

    def _bg(self, work, done, note="处理中…"):
        """work() 在线程里跑, done(result) 回 Tk 线程."""
        if self._busy:
            return
        self._busy = True
        self._say(note)

        def wrap():
            try:
                res = work()
            except Exception as ex:          # noqa: BLE001  面板别因为一条命令挂掉
                res = ex

            def back():
                self._busy = False
                if self.winfo_exists():
                    done(res)
            self.after(0, back)
        threading.Thread(target=wrap, daemon=True).start()

    def reload(self, rescan=True):
        def work():
            _, scan = _run(wc.cmd_scan(rescan=rescan))
            _, saved = _run(wc.CMD_SAVED)
            saved = wc.parse_saved(saved)
            for s in saved:                 # 连接名和 SSID 可能不同 (campusnet / CAMPUSNET)
                s["ssid"] = _run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "uuid", s["uuid"]])[1]
            return wc.parse_scan(scan), saved

        def done(res):
            if isinstance(res, Exception):
                self._say(f"读取失败: {res}", ok=False)
                return
            self.nets, self.saved = res
            self._render()
            self._say(f"附近 {len(self.nets)} 个网络, 已保存 {len(self.saved)} 个")
        self._bg(work, done, "扫描中…" if rescan else "读取中…")

    def _render(self):
        known = {s["ssid"] for s in self.saved}
        self.scan_lb.delete(0, "end")
        for n in self.nets:
            lock = "🔒" if wc.key_mgmt(n["security"]) else "  "
            tag = " ● 在用" if n["in_use"] else (" 已保存" if n["ssid"] in known else "")
            self.scan_lb.insert("end", f"{_bars(n['signal'])} {n['signal']:>3} {lock} {n['ssid']}{tag}")
        self.saved_lb.delete(0, "end")
        for i, s in enumerate(self.saved, 1):
            flag = " ● 在用" if s["active"] else ("" if s["autoconnect"] else " (不自动连)")
            self.saved_lb.insert("end", f"{i:>2}. {s['ssid'] or s['name']}{flag}")

    def _sel(self, lb, items):
        cur = lb.curselection()
        return (cur[0], items[cur[0]]) if cur and cur[0] < len(items) else (None, None)

    def _after_cmd(self, cmds, ok_text, rescan=False):
        def work():
            for c in cmds:
                rc, out = _run(c, timeout=45)
                if rc:
                    return False, out
            return True, ""

        def done(res):
            ok, out = res if not isinstance(res, Exception) else (False, str(res))
            self._say(ok_text if ok else f"失败: {out[-160:]}", ok)
            self.after(300, lambda: self.reload(rescan=rescan))
        self._bg(work, done)

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
        old = [wc.cmd_delete(s["uuid"]) for s in self.saved if s["ssid"] == ssid]   # 重输密码 = 重建
        add = wc.cmd_add(ssid, pw, sec, wc.next_priority(self.saved))
        self._after_cmd(old + [add, ["nmcli", "con", "up", "id", ssid]], f"已连接并保存 {ssid} (排在最上面)")

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
        rc, out = _run(wc.cmd_show_psk(s["uuid"]))
        self.ssid_var.set(s["ssid"] or s["name"])
        self.pw_var.set(out if rc == 0 else "")
        self._say("开放网络, 没有密码" if rc == 0 and not out else
                  "已填入密码框 (勾「显示密码」查看)" if rc == 0 else f"读取失败: {out}", ok=rc == 0)

    def change_psk(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if not s:
            return self._say("先在右边选要改密码的网络", ok=False)
        if len(self.pw_var.get()) < 8:
            return self._say("WPA 密码至少 8 位", ok=False)
        self._after_cmd([wc.cmd_set_psk(s["uuid"], self.pw_var.get()), wc.cmd_up(s["uuid"])],
                        f"{s['ssid'] or s['name']} 密码已更新")

    def forget(self):
        _, s = self._sel(self.saved_lb, self.saved)
        if s and messagebox.askyesno("忘记网络", f"删除已保存的 {s['ssid'] or s['name']}?"):
            self._after_cmd([wc.cmd_delete(s["uuid"])], "已删除")
