"""车机上的 Agent 写串口确认框 (#23): 点执行器条上黄闪 / 红 / 紫的操作头弹出.

只画 confirm_core.card 的结果, 按钮交给 on_action(op, arg) (kiosk 在后台线程里经 mcp_serial.human_action 执行),
这里不碰状态文件也不发串口. 高危词的命令点"确认发送"后再问一次.
refresh(view): kiosk 每次采样回来调; 挂起的条目 / 急停变了才重建按钮, 别的 (剩几秒、几秒前) 原地改字.
"""

import tkinter as tk
from tkinter import messagebox

import confirm_core as cc

FONT = ("Noto Sans CJK SC", 13)
FONT_BIG = ("Noto Sans CJK SC", 15)
FONT_CMD = ("Noto Sans Mono CJK SC", 20, "bold")
BG, CARD, FG, DIM = "#161b26", "#1f2633", "#e8eaf0", "#8b93a7"
BTN = {"go": ("#1f6f3f", "#e8ffe8"), "no": ("#47202a", "#ffb3bd"), "stop": ("#da3633", "#ffffff"),
       "plain": ("#2a3347", "#e8eaf0")}


class ConfirmDialog(tk.Toplevel):
    def __init__(self, master, n, view, on_action, on_serial=None, ask=None):
        super().__init__(master, bg=BG, highlightthickness=3, highlightbackground=cc.PENDING_COLOR)
        self.n, self.on_action, self.on_serial = n, on_action, on_serial
        self.ask = ask or (lambda title, msg: messagebox.askyesno(title, msg, parent=self))
        self.overrideredirect(True)
        W, H = 760, min(720, self.winfo_screenheight() - 40)
        self.geometry(f"{W}x{H}+{(self.winfo_screenwidth() - W) // 2}+{(self.winfo_screenheight() - H) // 2}")
        self.attributes("-topmost", True)
        self.title_lbl = tk.Label(self, font=FONT_BIG, bg=BG, fg=FG)
        self.title_lbl.pack(pady=(14, 2))
        self.status_lbl = tk.Label(self, font=FONT, bg=BG, fg=DIM, justify="center")
        self.status_lbl.pack()
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=18, pady=8)
        self.msg = tk.Label(self, text="", font=FONT, bg=BG, fg=DIM, wraplength=W - 40)
        self.msg.pack()
        self.foot = tk.Frame(self, bg=BG)
        self.foot.pack(pady=(6, 14))
        self._key, self._meta = None, {}
        self.refresh(view)

    # ---- 画

    def _btn(self, parent, text, kind, cmd, name=None):
        bg, fg = BTN[kind]
        b = tk.Button(parent, text=text, font=FONT_BIG, command=cmd, bg=bg, fg=fg, activebackground=bg,
                      relief="flat", padx=16, pady=4, cursor="hand2", name=name)
        b.pack(side="left", padx=6)
        return b

    def refresh(self, view):
        """视图读不懂 (None / 缺字段) 当全部急停画."""
        try:
            c = cc.card(view, self.n)
            states = cc.slot_states(view)
        except (KeyError, TypeError, StopIteration):
            view = cc.stopped_view()
            c, states = cc.card(view, self.n), cc.slot_states(view)
        self.title_lbl.config(text=c["title"])
        self.status_lbl.config(text="\n".join(c["status"]), fg=cc.ESTOP_COLOR if c["stopped"] else DIM)
        self.config(highlightbackground=cc.ESTOP_COLOR if c["stopped"] else cc.PENDING_COLOR)
        any_stop = any(s["estop"] for s in states.values())
        key = (c["stopped"], any_stop, tuple(it["id"] for it in c["items"]))
        if key == self._key:
            for it in c["items"]:
                self._meta[it["id"]].config(text=it["meta"])
            return
        self._key = key
        for w in list(self.body.winfo_children()) + list(self.foot.winfo_children()):
            w.destroy()
        self._meta = {}
        if not c["items"]:
            tk.Label(self.body, text="没有等确认的命令", font=FONT, bg=BG, fg=DIM).pack(pady=20)
        for it in c["items"]:
            self._item(it)
        if c["stopped"]:
            self._btn(self.foot, "解除本路急停", "plain", lambda: self.on_action("resume", self.n))
        else:
            self._btn(self.foot, "急停本路", "stop", lambda: self.on_action("estop", self.n))
        self._btn(self.foot, "全部急停", "stop", lambda: self.on_action("estop", None))
        if any_stop:
            self._btn(self.foot, "解除全部急停", "plain", lambda: self.on_action("resume", None))
        if self.on_serial:
            self._btn(self.foot, "串口会话", "plain", self.on_serial)
        self._btn(self.foot, "关闭", "plain", self.destroy)

    def _item(self, it):
        f = tk.Frame(self.body, bg=CARD, padx=12, pady=8)
        f.pack(fill="x", pady=4)
        tk.Label(f, text=it["head"], font=FONT_CMD, bg=CARD, fg=FG, anchor="w", justify="left",
                 wraplength=680).pack(fill="x")
        self._meta[it["id"]] = tk.Label(f, text=it["meta"], font=FONT, bg=CARD, fg=DIM, anchor="w")
        self._meta[it["id"]].pack(fill="x")
        tk.Label(f, text=it["reason"], font=FONT, bg=CARD, fg=FG, anchor="w", justify="left",
                 wraplength=680).pack(fill="x")
        if it["danger"]:
            tk.Label(f, text=it["danger"], font=FONT_BIG, bg=CARD, fg=cc.ESTOP_COLOR, anchor="w").pack(fill="x")
        row = tk.Frame(f, bg=CARD)
        row.pack(anchor="e", pady=(4, 0))
        self._btn(row, "确认发送", "go", lambda: self._confirm(it), name=f"confirm-{it['id']}")
        self._btn(row, "拒绝", "no", lambda: self.on_action("reject", it["id"]), name=f"reject-{it['id']}")

    def _confirm(self, it):
        if it["danger"] and not self.ask("高危命令", f"{it['head']}\n{it['danger']}\n\n确定发送到设备?"):
            return
        self.on_action("confirm", it["id"])

    def say(self, text, ok=True):
        self.msg.config(text=text, fg="#7ee787" if ok else "#ff7b72")
