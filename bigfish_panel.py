"""车机大肥鱼面板 (#53): 看它在做什么; 确认 / 拒绝打开现有确认框, 急停就是全部急停 (顺带写大肥鱼的急停信号).
#99: 给了 desk 就多一行输入框, 能派任务、看这个任务、取消它. 逻辑全在 bigfish_task_core, 这里只接线.

数据只来自 bigfish_core.load (大肥鱼导出的白名单状态文件) 和任务接口. 大肥鱼说的话用单独的颜色、等宽字、SAYS 前缀,
Text 只插纯文字 (不认链接、不执行). 这里不审批、不写任何文件; 输入框没有任何通往批准的路.
"""

import time
import tkinter as tk

import bigfish_core as bf
import bigfish_task_core as tc
import ime_tk
import theme as th

BG, = th.follow(globals(), BG="PANEL")
REFRESH_MS = 2000
TAGS = {"own": "FG", "dim": "DIM", "bad": "BAD", "untrusted": "AGENT"}


class BigfishPanel(tk.Frame):
    def __init__(self, master, load=bf.load, pending=lambda: 0, on_confirm=None, on_estop=None, clock=time.time,
                 note=lambda: "", ready=bf.channel_ready, estop_on=bf.estop_active, on_resume=None,
                 desk=None, run_bg=None, locked=lambda: True, agents_stopped=lambda: True,
                 submit=tc.submit, get_task=tc.get_task, cancel_task=tc.cancel_task):
        super().__init__(master, bg=BG)
        self.load, self.pending, self.clock, self.note, self.ready = load, pending, clock, note, ready
        self.estop_on = estop_on
        self.desk, self.run_bg, self.locked, self.agents_stopped = desk, run_bg, locked, agents_stopped
        self.submit, self.get_task, self.cancel_task = submit, get_task, cancel_task
        self.entry = None
        self.text = tk.Text(self, font=th.FONT_BIG, bg=BG, fg=th.FG, **th.FLAT, wrap="word",
                            state="disabled", padx=16, pady=12, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        for tag, color in TAGS.items():
            self.text.tag_config(tag, foreground=getattr(th, color))
        self.text.tag_config("untrusted", font=th.FONT_MONO, lmargin1=24, lmargin2=24)
        if desk is not None:
            self._build_desk()
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

    def _build_desk(self):
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=18, pady=(0, 4))
        self.entry = tk.Entry(row, font=th.FONT_BIG, bg=th.BG, fg=th.FG, insertbackground=th.FG, **th.SUNKEN)
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.entry.insert(0, self.desk.draft)
        self.task_ime = ime_tk.attach(self.entry, purpose="bigfish.task", font=th.FONT, bg=th.BOX, fg=th.FG)
        self.entry.bind("<Return>", lambda _e: self._send())   # 拼音期间的回车被中文输入吃掉, 到不了这里
        self.btn_send = self._btn(row, "发送", th.PRIMARY, th.WHITE, self._send)
        self.btn_cancel = self._btn(row, "取消任务", th.BTN2, th.FG, self._cancel)
        self.hint = tk.Label(self, text="", font=th.FONT, bg=BG, fg=th.DIM, anchor="w")
        self.hint.pack(fill="x", padx=18, pady=(0, 8))
        self._desk_gen = self.desk.gen

    def _load(self, now):
        try:
            return self.load(now)
        except Exception:                   # noqa: BLE001  读坏了也只是"不在线"
            return bf.parse(None, now)

    def _block(self, view):
        return tc.send_block(self.locked, lambda: self.estop_on() or self.agents_stopped(), view)

    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _sync_desk(self, view=None):
        """输入行跟着闸门和 desk 走. 字只在发送成功后清, 置灰不动字."""
        block = self._block(view if view is not None else self._load(self.clock()))
        d = self.desk
        if self._desk_gen != d.gen:
            self._desk_gen = d.gen
            self.entry.config(state="normal")
            self.entry.delete(0, "end")
        self.entry.config(state="disabled" if block or d.sending else "normal")
        self.btn_send.config(state="disabled" if block or d.sending or d.active else "normal")
        self.btn_cancel.config(state="normal" if d.active and not d.cancelling else "disabled")
        self.hint.config(text=block or d.msg, fg=th.BAD if block or d.bad else th.DIM)

    def _send(self):
        view = self._load(self.clock())
        prompt = self.desk.begin(self.entry.get(), self._block(view))
        self._sync_desk(view)
        if prompt is not None and not self.run_bg(lambda: self.submit(prompt), self._sent, key="bigfish-task-send"):
            self._sent(tc.Offline("后台忙"))

    def _desk_done(self, fn, res):
        """后台结果回来 (主线程). desk 在 kiosk 上, 面板关了也要更新."""
        fn(res)
        if self._alive():
            self._redraw()
            self._sync_desk()

    def _sent(self, res):
        self._desk_done(self.desk.sent, res)

    def _cancel(self):
        task_id = self.desk.begin_cancel()
        if task_id is None:
            return
        self._sync_desk()
        if not self.run_bg(lambda: self.cancel_task(task_id), self._cancelled, key="bigfish-task-cancel"):
            self._cancelled(tc.Offline("后台忙"))

    def _cancelled(self, res):
        self._desk_done(self.desk.cancelled, res)

    def _poll(self):
        args = self.desk.poll_args()
        if args:
            self.run_bg(lambda: self.get_task(*args), self._polled, key="bigfish-task-poll")

    def _polled(self, res):
        self._desk_done(self.desk.polled, res)

    def _draw(self, view, now):
        rows = bf.panel_rows(view, now, note=self.note(), ready=self.ready())
        if self.desk is not None and self.desk.rows():
            rows = self.desk.rows() + [("", "dim")] + rows      # 人刚派的任务放最上面, 不被状态行挤出屏
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for line, tag in rows:
            self.text.insert("end", line + "\n", tag)
        self.text.config(state="disabled")

    def _redraw(self):
        now = self.clock()
        self._draw(self._load(now), now)

    def refresh(self):
        now = self.clock()
        view = self._load(now)
        self._draw(view, now)
        n = self.pending()
        self.btn_confirm.config(text=f"确认 / 拒绝  {n}" if n else "确认 / 拒绝",
                                state="normal" if n else "disabled")
        try:
            on = bool(self.estop_on())
        except Exception:                   # noqa: BLE001
            on = False
        if on and not self.btn_resume.winfo_manager():
            self.btn_resume.pack(side="right", padx=6)      # 和急停隔开, 放另一头
        elif not on and self.btn_resume.winfo_manager():
            self.btn_resume.pack_forget()
        if self.desk is not None:
            self._sync_desk(view)
            self._poll()
        self._job = self.after(REFRESH_MS, self.refresh)

    def destroy(self):
        if self.entry is not None:
            try:
                self.desk.draft = self.entry.get()      # 面板重开 / 换主题重建时字不丢
            except tk.TclError:
                pass
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        super().destroy()
