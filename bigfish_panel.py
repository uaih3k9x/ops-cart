"""车机大肥鱼面板 (#53): 看它在做什么; 确认 / 拒绝打开现有确认框, 急停就是全部急停 (顺带写大肥鱼的急停信号).
#99: 给了 desk 就多一行输入框, 能派任务、看这个任务、取消它. 逻辑全在 bigfish_task_core, 这里只接线.

回复单独放滚动区, 只追加新内容, 人往上翻时不拉回底部.

数据只来自 bigfish_core.load (大肥鱼导出的白名单状态文件) 和任务接口. 大肥鱼说的话用单独的颜色、等宽字、SAYS 前缀,
Text 只插纯文字 (不认链接、不执行). 这里不审批、不写任何文件; 输入框没有任何通往批准的路.
"""

import sys
import time
import tkinter as tk

import bigfish_core as bf
import bigfish_task_core as tc
import ime_tk
import theme as th

BG, = th.follow(globals(), BG="PANEL")
REFRESH_MS = 2000
STATUS_LINES = 9                    # 有滚动区时上面状态区的高度
LOG_BATCH = 2000                    # 一次刷新最多插这么多行 (重开面板补历史也不卡主线程), 剩下的下次接着插
HINT_PAD = 8                        # 折行宽度比控件窄这么多, 留给 Label 的内边距
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
        self.entry = self.log = None
        self._shown, self._log_gen, self._log_n, self._log_live = None, None, 0, []
        self._follow, self._log_placed = True, False    # 新内容是否跟到底; 控件拿到真实大小后才量得准
        self._build_status()
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

    def _text(self, parent):
        w = tk.Text(parent, font=th.FONT_BIG, bg=BG, fg=th.FG, **th.FLAT, wrap="word",
                    state="disabled", padx=16, pady=12, cursor="arrow")
        for tag, color in TAGS.items():
            w.tag_config(tag, foreground=getattr(th, color))
        w.tag_config("untrusted", font=th.FONT_MONO, lmargin1=24, lmargin2=24)
        return w

    def _btn(self, parent, text, bg, fg, cmd):
        b = tk.Button(parent, text=text, font=th.FONT_BIG, command=cmd or (lambda: None), bg=bg, fg=fg,
                      activebackground=bg, activeforeground=fg, **th.RAISED, padx=24, pady=10, cursor="hand2")
        b.pack(side="left", padx=6)
        return b

    def _scrolled(self):
        wrap = tk.Frame(self, bg=BG)
        text = self._text(wrap)
        bar = tk.Scrollbar(wrap, orient="vertical", width=24, command=text.yview)
        text.config(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return wrap, text

    def _build_status(self):
        if self.desk is None:
            self.text = self._text(self)
            self.text.pack(fill="both", expand=True)
            return
        wrap, self.text = self._scrolled()  # 固定高度放不下 Mac 派来任务的全部步骤和输出尾巴
        self.text.config(height=STATUS_LINES)
        wrap.pack(fill="x")

    def _build_log(self):
        wrap, self.log = self._scrolled()
        self.log.config(height=1)           # 只占 expand 分到的地方, 不把下面的输入行挤出屏
        self.log.bind("<Map>", self._log_shown)        # 第一次画在控件上屏之前, 那时的 see 不生效
        self.log.bind("<Configure>", self._log_shown)
        wrap.pack(fill="both", expand=True)

    def _build_hint(self):
        self.hint = tk.Label(self, text="", font=th.FONT, bg=BG, fg=th.DIM, anchor="w", justify="left")
        self.hint.pack(fill="x", padx=18, pady=(0, 8))
        self.hint.bind("<Configure>", lambda e: self.hint.config(wraplength=max(e.width - HINT_PAD, 1)))

    def _log_shown(self, _e=None):
        self._log_placed = True
        if self._follow:
            self.log.see("end")

    def _log_at_bottom(self):
        """末行完整可见才算在底部. 不用 yview 比例: 几千行后上翻几行比例也接近 1; dlineinfo 半截露出时也有值, 要比到底边."""
        info = self.log.dlineinfo("end-1c")
        return info is not None and info[1] + info[3] <= self.log.winfo_height() - int(self.log.cget("pady"))

    def _page(self, n):
        self.log.yview_scroll(n, "pages")
        return "break"

    def _build_desk(self):
        self._build_log()
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=18, pady=(0, 4))
        self.entry = tk.Entry(row, font=th.FONT_BIG, bg=th.BG, fg=th.FG, insertbackground=th.FG, **th.SUNKEN)
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.entry.insert(0, self.desk.draft)
        self.task_ime = ime_tk.attach(self.entry, purpose="bigfish.task", font=th.FONT, bg=th.BOX, fg=th.FG)
        self.entry.bind("<Return>", lambda _e: self._send())   # 拼音期间的回车被中文输入吃掉, 到不了这里
        self.entry.bind("<Prior>", lambda _e: self._page(-1))  # 焦点总在输入框, 翻页键直接翻回复
        self.entry.bind("<Next>", lambda _e: self._page(1))
        self.btn_send = self._btn(row, "发送", th.PRIMARY, th.WHITE, self._send)
        self.btn_cancel = self._btn(row, "取消任务", th.BTN2, th.FG, self._cancel)
        self._build_hint()
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
            self._sent(tc.Busy())

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
            self._cancelled(tc.Busy())

    def _cancelled(self, res):
        self._desk_done(self.desk.cancelled, res)

    def _poll(self):
        args = self.desk.poll_args() if self.desk is not None else None
        if args:
            self.run_bg(lambda: self.get_task(*args), self._polled, key="bigfish-task-poll")

    def _polled(self, res):
        self._desk_done(self.desk.polled, res)

    def _draw(self, view, now):
        rows = bf.panel_rows(view, now, note=self.note(), ready=self.ready())
        if self.desk is not None and self.desk.rows():
            rows = self.desk.rows() + [("", "dim")] + rows      # 人刚派的任务放最上面, 不被状态行挤出屏
        if rows != self._shown:
            self._shown = rows
            top = self.text.yview()[0]      # 计时每秒都在变, 整段重画不能把人翻到的位置拉回顶上
            self.text.config(state="normal")
            self.text.delete("1.0", "end")
            for line, tag in rows:
                self.text.insert("end", line + "\n", tag)
            self.text.config(state="disabled")
            self.text.yview_moveto(top)
        if self.desk is not None:
            self._update_log()

    def _update_log(self):
        """回复滚动区的增量刷新: 定稿的行只追加, 只重写最后还会变的几行; 人往上翻时不动视图."""
        d, log = self.desk, self.log
        if self._log_gen != d.gen:
            self._log_gen, self._log_n, self._log_live = d.gen, 0, []
            log.config(state="normal")
            log.delete("1.0", "end")
            log.config(state="disabled")
            self._follow = True
        if self._log_placed and log.winfo_viewable():   # 没上屏时量出来的不算数, 保持原值
            self._follow = self._log_at_bottom()
        done, live = d.log_rows()
        fresh = done[self._log_n:self._log_n + LOG_BATCH]
        n = self._log_n + len(fresh)
        live = live if n == len(done) else []       # 没追上时半行先不画
        if not fresh and live == self._log_live:
            return
        log.config(state="normal")
        log.delete(f"{self._log_n + 1}.0", "end-1c")   # 不能用 end: Tk 会连前一行的换行一起删, 下一行就粘上去了
        for line, tag in fresh + live:
            log.insert("end", line + "\n", tag)
        log.config(state="disabled")
        self._log_n, self._log_live = n, live
        if self._follow:
            log.see("end")

    def _redraw(self):
        now = self.clock()
        self._draw(self._load(now), now)

    def _paint(self):
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

    def refresh(self):
        """画坏一次不能断刷新链, 也不能停轮询: 下一个好回复才能把坏状态换掉."""
        for step in (self._paint, self._poll):
            try:
                step()
            except Exception as ex:         # noqa: BLE001
                print(f"大肥鱼面板刷新出错: {ex!r}", file=sys.stderr)
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
