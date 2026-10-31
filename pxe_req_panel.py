"""车机上的 Agent PXE 解锁申请框 (#24): 有新申请时 kiosk 弹出, 也可以点装机按钮打开.

只画 pxe_req_core.card 的结果 (谁申请、用途、清单、确认码), 按钮交给 on_action(op, arg)
(kiosk 在后台线程里经 mcp_pxe.human_confirm / human_reject 执行), 这里不碰状态文件也不碰 tftp.
确认交出去的是 (编号, 人敲的码, 界面上这份清单的摘要): 清单换了的申请拿旧界面确认不了.
refresh(view): kiosk 每次采样回来调; 换了申请 / 换了码才重建, 别的 (剩几分钟) 原地改字, 敲了一半的码不丢.
"""

import tkinter as tk

import pxe_req_core as pr
import theme as th

FONT, FONT_BIG, FONT_MONO = th.FONT, th.FONT_BIG, th.FONT_MONO
FONT_CODE = th.font(40, bold=True, mono=True)
BG, CARD, FG, DIM, RED, AMBER = th.PANEL, th.BOX, th.FG, th.DIM, th.BAD, th.WARN
EDGE = th.DANGER_EDGE            # 同手动解锁对话框: 解锁是危险操作
BTN = {"go": (th.DANGER_BG, th.DANGER_FG), "no": (th.BTN2, th.DANGER_FG), "plain": (th.BTN2, th.FG)}


class PxeRequestDialog(tk.Toplevel):
    def __init__(self, master, view, on_action, on_manual=None):
        super().__init__(master, bg=BG, highlightthickness=3, highlightbackground=EDGE)
        self.on_action, self.on_manual = on_action, on_manual
        self.overrideredirect(True)
        W, H = 780, min(720, self.winfo_screenheight() - 40)
        self.geometry(f"{W}x{H}+{(self.winfo_screenwidth() - W) // 2}+{(self.winfo_screenheight() - H) // 2}")
        self.attributes("-topmost", True)
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=12)
        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg=RED, wraplength=W - 40)
        self.msg.pack()
        self.foot = tk.Frame(self, bg=BG)
        self.foot.pack(pady=(6, 14))
        self._key, self._meta, self._entry = False, None, None
        self.refresh(view)

    def _btn(self, parent, text, kind, cmd):
        bg, fg = BTN[kind]
        b = tk.Button(parent, text=text, font=FONT_BIG, command=cmd, bg=bg, fg=fg, activebackground=bg,
                      relief="flat", padx=20, pady=4, cursor="hand2")
        b.pack(side="left", padx=6)
        return b

    def refresh(self, view):
        c = pr.card(view)
        key = (c["id"], c["code"], c["digest"]) if c else None
        if key == self._key:
            if c:
                self._meta.config(text=c["meta"])
            return
        self._key = key
        for w in list(self.body.winfo_children()) + list(self.foot.winfo_children()):
            w.destroy()
        self._meta = self._entry = None
        if c is None:
            tk.Label(self.body, text="没有在等确认的 PXE 申请", font=FONT_BIG, bg=BG, fg=DIM).pack(pady=40)
        else:
            self._draw(c)
        if self.on_manual:
            self._btn(self.foot, "手动解锁…", "plain", self.on_manual)
        self._btn(self.foot, "关闭", "plain", self.destroy)

    def _draw(self, c):
        tk.Label(self.body, text=c["title"], font=FONT_BIG, bg=BG, fg=FG).pack(pady=(4, 2))
        self._meta = tk.Label(self.body, text=c["meta"], font=FONT, bg=BG, fg=DIM)
        self._meta.pack()
        tk.Label(self.body, text=c["warn"], font=FONT, bg=BG, fg=RED, wraplength=720).pack(pady=(6, 6))
        box = tk.Frame(self.body, bg=CARD, padx=10, pady=6)
        box.pack(fill="x")
        for line in c["lines"]:
            tk.Label(box, text=line, font=FONT_MONO, bg=CARD, fg=AMBER, anchor="w").pack(fill="x")
        if c["more"]:
            tk.Label(self.body, text=f"还有 {c['more']} 条申请在等, 处理完这条接着显示", font=FONT, bg=BG,
                     fg=DIM).pack(pady=(4, 0))
        tk.Label(self.body, text="看过清单, 照抄确认码:", font=FONT, bg=BG, fg=DIM).pack(pady=(10, 0))
        tk.Label(self.body, text=c["code_text"], font=FONT_CODE, bg=BG, fg=AMBER).pack()
        e = tk.Entry(self.body, font=th.font(22, mono=True), width=10, justify="center", bg=CARD, fg=FG,
                     insertbackground=FG, relief="flat")
        e.pack(pady=6, ipady=6)
        e.bind("<Return>", lambda _e: self._confirm(c))
        self._entry = e
        self._btn(self.foot, "确认解锁", "go", lambda: self._confirm(c))
        self._btn(self.foot, "拒绝", "no", lambda: self.on_action("reject", c["id"]))
        self.after(100, lambda: self.winfo_exists() and e.winfo_exists() and e.focus_set())

    def _confirm(self, c):
        typed = self._entry.get()
        self._entry.delete(0, "end")
        self.on_action("confirm", (c["id"], typed, c["digest"]))

    def say(self, text, ok=True):
        self.msg.config(text=text, fg=th.OK if ok else RED)
