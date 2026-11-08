"""车机大肥鱼面板 (#53): 只读看它在做什么; 确认 / 拒绝打开现有确认框, 急停就是全部急停 (顺带写大肥鱼的急停信号).

数据只来自 bigfish_core.load (大肥鱼导出的白名单状态文件). 大肥鱼说的话用单独的颜色、等宽字、SAYS 前缀,
Text 只插纯文字 (不认链接、不执行). 这里不审批、不写任何文件.
"""

import time
import tkinter as tk

import bigfish_core as bf
import theme as th

BG, = th.follow(globals(), BG="PANEL")
REFRESH_MS = 2000
TAGS = {"own": "FG", "dim": "DIM", "bad": "BAD", "untrusted": "AGENT"}


class BigfishPanel(tk.Frame):
    def __init__(self, master, load=bf.load, pending=lambda: 0, on_confirm=None, on_estop=None, clock=time.time,
                 note=lambda: "", ready=bf.channel_ready, estop_on=bf.estop_active, on_resume=None):
        super().__init__(master, bg=BG)
        self.load, self.pending, self.clock, self.note, self.ready = load, pending, clock, note, ready
        self.estop_on = estop_on
        self.text = tk.Text(self, font=th.FONT_BIG, bg=BG, fg=th.FG, **th.FLAT, wrap="word",
                            state="disabled", padx=16, pady=12, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        for tag, color in TAGS.items():
            self.text.tag_config(tag, foreground=getattr(th, color))
        self.text.tag_config("untrusted", font=th.FONT_MONO, lmargin1=24, lmargin2=24)
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        self.btn_confirm = self._btn(bar, "", th.GO_BG, th.GO_FG, on_confirm)
        self.btn_estop = self._btn(bar, "全部急停", th.ALERT_BG, th.WHITE, on_estop)
        # estop.json 还在就显示: 终端 / 按路解除、串口急停失败后它会留下, 大肥鱼一直拒任务 (#97 审查中1)
        self.btn_resume = self._btn(bar, "解除大肥鱼急停", th.BTN2, th.FG, on_resume)
        self.btn_resume.pack_forget()
        self._job = None
        self.refresh()

    def _btn(self, parent, text, bg, fg, cmd):
        b = tk.Button(parent, text=text, font=th.FONT_BIG, command=cmd or (lambda: None), bg=bg, fg=fg,
                      activebackground=bg, activeforeground=fg, **th.RAISED, padx=24, pady=10, cursor="hand2")
        b.pack(side="left", padx=6)
        return b

    def refresh(self):
        now = self.clock()
        try:
            view = self.load(now)
        except Exception:                   # noqa: BLE001  读坏了也只是"不在线"
            view = bf.parse(None, now)
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for line, tag in bf.panel_rows(view, now, note=self.note(), ready=self.ready()):
            self.text.insert("end", line + "\n", tag)
        self.text.config(state="disabled")
        n = self.pending()
        self.btn_confirm.config(text=f"确认 / 拒绝  {n}" if n else "确认 / 拒绝",
                                state="normal" if n else "disabled")
        try:
            on = bool(self.estop_on())
        except Exception:                   # noqa: BLE001
            on = False
        if on and not self.btn_resume.winfo_manager():
            self.btn_resume.pack(side="left", padx=6)
        elif not on and self.btn_resume.winfo_manager():
            self.btn_resume.pack_forget()
        self._job = self.after(REFRESH_MS, self.refresh)

    def destroy(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        super().destroy()
