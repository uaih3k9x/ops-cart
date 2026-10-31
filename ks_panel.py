#!/usr/bin/env python3
"""Kiosk Kickstart 面板: 表单改常用字段 + 全文编辑 + 备份轮换 + 保存前校验.
校验不通过要二次确认才写盘; 写盘/读备份出错弹提示; 算密码 hash (openssl) 放后台."""

import glob
import os
import tkinter as tk
from tkinter import messagebox

import kiosk_core as kc
import kiosk_ui_core as kui
from kiosk_ui_core import UiBridge
import theme as th

FONT, FONT_MONO = th.FONT_SMALL, th.FONT_MONO


class KsPanel(tk.Frame):
    """kiosk 里的「Kickstart 编辑」页. 修改先进 Text, 点保存才落盘(带备份)."""

    def __init__(self, master):
        super().__init__(master, bg=th.PANEL)
        self.ui = UiBridge(self)
        self.ui.start()
        self._build()
        self.reload()

    def destroy(self):
        self.ui.stop()
        super().destroy()

    # ------------------------------------------------------------ UI

    def _build(self):
        bar = tk.Frame(self, bg=th.PANEL)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        for text, cmd in (("重新加载", self.reload),
                          ("保存(自动备份)", self.save),
                          ("恢复最近备份", self.restore_backup)):
            tk.Button(bar, text=text, font=FONT, command=cmd, bg=th.BOX,
                      fg=th.FG, activebackground=th.ACTIVE, relief="flat",
                      cursor="hand2", padx=12).pack(side="left", padx=4)
        self.path_label = tk.Label(bar, text="", font=FONT, bg=th.PANEL, fg=th.DIM)
        self.path_label.pack(side="right")

        # 表单行: 主机名 / root 密码
        row = tk.Frame(self, bg=th.PANEL)
        row.pack(fill="x", padx=12, pady=2)
        tk.Label(row, text="主机名:", font=FONT, bg=th.PANEL,
                 fg=th.TEXT).pack(side="left")
        self.host_var = tk.StringVar()
        tk.Entry(row, textvariable=self.host_var, width=18, font=FONT_MONO,
                 bg=th.BG, fg=th.FG, insertbackground=th.FG,
                 relief="flat").pack(side="left", padx=4)
        tk.Button(row, text="改主机名", font=FONT, command=self.apply_hostname,
                  bg=th.PRIMARY, fg=th.FG, activebackground=th.ACTIVE,
                  relief="flat", cursor="hand2", padx=10).pack(side="left", padx=4)

        tk.Label(row, text="  root 密码:", font=FONT, bg=th.PANEL,
                 fg=th.TEXT).pack(side="left")
        self.pw_var = tk.StringVar()
        tk.Entry(row, textvariable=self.pw_var, width=18, show="*", font=FONT_MONO,
                 bg=th.BG, fg=th.FG, insertbackground=th.FG,
                 relief="flat").pack(side="left", padx=4)
        tk.Button(row, text="设密码(hash)", font=FONT, command=self.apply_password,
                  bg=th.PRIMARY, fg=th.FG, activebackground=th.ACTIVE,
                  relief="flat", cursor="hand2", padx=10).pack(side="left", padx=4)

        # 警告条
        self.warn = tk.Label(self, text="", font=FONT, bg=th.PANEL,
                             fg=th.NOTICE, anchor="w", wraplength=1100)
        self.warn.pack(fill="x", padx=12)

        # 全文编辑
        wrap = tk.Frame(self, bg=th.PANEL)
        wrap.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self.text = tk.Text(wrap, font=FONT_MONO, bg=th.BG, fg=th.TEXT,
                            insertbackground=th.FG, relief="flat",
                            wrap="none", undo=True)
        ysb = tk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 逻辑

    def _content(self):
        return self.text.get("1.0", "end").rstrip("\n") + "\n"

    def reload(self):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", kc.ks_read())
        self.path_label.config(text=kc.KS_PATH)
        self.host_var.set(self._current_hostname() or "")
        self._refresh_warn()

    def _current_hostname(self):
        for ln in self.text.get("1.0", "end").splitlines():
            if ln.strip().startswith("network") and "--hostname" in ln:
                import re
                m = re.search(r"--hostname\s+(\S+)", ln)
                return m.group(1) if m else ""
        return ""

    def apply_hostname(self):
        try:
            new = kc.ks_set_hostname(self._content(), self.host_var.get().strip())
            self.text.delete("1.0", "end")
            self.text.insert("1.0", new)
            self._refresh_warn()
        except ValueError as e:
            messagebox.showerror("主机名", str(e))

    def apply_password(self):
        pw = self.pw_var.get()
        if not pw or len(pw) < 8:
            messagebox.showerror("密码", "密码至少 8 位")
            return

        def done(hashed):                   # 回到主线程时再取正文, 期间的编辑不会被覆盖
            if isinstance(hashed, Exception):
                messagebox.showerror("密码", str(hashed))
                return
            try:
                new = kc.ks_set_rootpw(self._content(), hashed)
            except ValueError as e:
                messagebox.showerror("密码", str(e))
                return
            self.text.delete("1.0", "end")
            self.text.insert("1.0", new)
            self.pw_var.set("")
            self._refresh_warn()
        if not self.ui.run_bg(lambda: kc.ks_hash_password(pw), done, key="hash"):
            return
        self.warn.config(text="处理中…")

    def save(self):
        def confirm(warns):
            return messagebox.askyesno("校验没通过", "警告:\n" + "\n".join(warns) + "\n\n仍然保存?",
                                       icon="warning")
        ok, msg = kui.ks_save(self._content(), confirm)
        self._refresh_warn()
        if not ok:
            if msg != "已取消":
                messagebox.showerror("保存", msg)
            return
        baks = sorted(glob.glob(kc.KS_PATH + ".bak-*"))
        messagebox.showinfo("保存",
                            f"已写入 {kc.KS_PATH}\n备份: {os.path.basename(baks[-1]) if baks else '无'}")

    def restore_backup(self):
        baks = sorted(glob.glob(kc.KS_PATH + ".bak-*"))
        if not baks:
            messagebox.showinfo("恢复", "没有备份")
            return
        if not messagebox.askyesno("恢复", f"用 {os.path.basename(baks[-1])} 覆盖当前内容?"):
            return
        try:
            kc.ks_write(kui.read_text(baks[-1]))
        except (OSError, UnicodeDecodeError) as ex:
            messagebox.showerror("恢复", f"恢复失败: {ex}")
            return
        self.reload()

    def _refresh_warn(self):
        warns = kc.ks_validate(self._content())
        self.warn.config(text="警告: " + "  |  ".join(warns) if warns else "校验通过")
