"""车机打线缆标签面板 (#104): 输入一到两行, 按实际版面预览, 打一张 / 再打一张 / 份数, 打印机状态, 刻度尺校准, 换纸先走一张.

只接线: 版面和校准在 label_core, 渲染在 label_render (子进程), 打印机在 label_device. 耗时的都走 run_bg, 不卡 Tk 主线程.
"""

import tkinter as tk

import ime_tk
import label_core as lc
import label_device as ld
import label_render as lr
import theme as th

BG, = th.follow(globals(), BG="PANEL")
KEY_RENDER, KEY_IO, KEY_STATUS, KEY_SAVE = "label-render", "label-io", "label-status", "label-save"
DEBOUNCE_MS = 400
STATUS_MS = 10000               # 看状态要占打印机锁, 不用太勤
ZOOM = 2


class LabelPanel(tk.Frame):
    def __init__(self, master, run_bg, load=lc.load_conf, save=lc.save_conf, render=lr.render_plan, device=ld):
        super().__init__(master, bg=BG)
        self.run_bg, self.save, self.render, self.dev = run_bg, save, render, device
        self.conf = load()
        self.copies, self.last = 1, None        # last: 上一次打成功的文字 (再打一张)
        self._preview_job, self._status_job, self._img = None, None, None
        self._stale, self._printing = False, False
        self._build()
        self._clear_preview()
        self._status_tick()

    def _btn(self, parent, text, cmd, bg=None, fg=None):
        b = tk.Button(parent, text=text, font=th.FONT, command=cmd, bg=bg or th.BOX, fg=fg or th.FG,
                      activebackground=th.ACTIVE, activeforeground=th.FG, **th.RAISED, padx=12, pady=6, cursor="hand2")
        b.pack(side="left", padx=4, pady=3)
        return b

    def _build(self):
        form = tk.Frame(self, bg=BG)
        form.pack(fill="x", padx=16, pady=(10, 2))
        self.vars, self.imes = [], []
        for i, title in enumerate(("第一行:", "第二行:")):
            row = tk.Frame(form, bg=BG)
            row.pack(fill="x", pady=(0, 18))         # 留出下面候选栏的位置
            tk.Label(row, text=title, font=th.FONT, bg=BG, fg=th.TEXT).pack(side="left")
            var = tk.StringVar()
            var.trace_add("write", self._schedule_preview)
            e = tk.Entry(row, textvariable=var, font=th.FONT_BIG, bg=th.BG, fg=th.FG, insertbackground=th.FG, **th.SUNKEN)
            e.pack(side="left", fill="x", expand=True, padx=6, ipady=3)
            self.vars.append(var)
            self.imes.append(ime_tk.attach(e, purpose="label.text", font=th.FONT, bg=th.BOX, fg=th.FG))
            if i == 0:
                self._btn(row, "to", self.fill_to)
        self.recent_row = tk.Frame(self, bg=BG)
        self.recent_row.pack(fill="x", padx=12)
        self._render_recents()

        # 状态和提示先贴底: 屏幕不够高时被裁的是预览, 不是这两行
        self.msg = tk.Label(self, text="", font=th.FONT, bg=BG, fg=th.DIM, anchor="w")
        self.msg.pack(side="bottom", fill="x", padx=16, pady=(2, 10))
        self.status = tk.Label(self, text="打印机: 查询中", font=th.FONT, bg=BG, fg=th.DIM, anchor="w")
        self.status.pack(side="bottom", fill="x", padx=16)

        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True, padx=16, pady=4)
        self.canvas = tk.Canvas(mid, width=lc.PREVIEW_W * ZOOM, height=lc.PAGE_H * ZOOM, bg=th.BG, highlightthickness=0)
        self.canvas.pack(side="left", padx=(0, 12))
        side = tk.Frame(mid, bg=BG)
        side.pack(side="left", fill="y")
        for line in (("打一张", self.print_one, th.PRIMARY), ("再打一张", self.print_again, None)):
            row = tk.Frame(side, bg=BG)
            row.pack(fill="x")
            b = self._btn(row, line[0], line[1], bg=line[2])
            if line[0] == "再打一张":
                self.btn_again = b
                b.config(state="disabled")
        row = tk.Frame(side, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="份数:", font=th.FONT, bg=BG, fg=th.TEXT).pack(side="left")
        self._btn(row, "-", lambda: self.step_copies(-1))
        self.copies_lab = tk.Label(row, text="1", width=3, font=th.FONT_BIG, bg=BG, fg=th.FG)
        self.copies_lab.pack(side="left")
        self._btn(row, "+", lambda: self.step_copies(1))
        row = tk.Frame(side, bg=BG)
        row.pack(fill="x")
        self.btn_many = self._btn(row, "打 1 张", self.print_copies)
        row = tk.Frame(side, bg=BG)
        row.pack(fill="x", pady=(14, 0))
        self._btn(row, "换纸了, 先走一张", self.feed)
        row = tk.Frame(side, bg=BG)
        row.pack(fill="x")
        self._btn(row, "校准走纸", self.toggle_cal)

        self.cal = tk.LabelFrame(self, text=" 刻度尺校准 ", font=th.FONT, bg=BG, fg=th.DIM, **th.GROOVE)
        self.cal_shown = False
        self.cal_info = tk.Label(self.cal, text="", font=th.FONT, bg=BG, fg=th.TEXT, anchor="w", justify="left")
        self.cal_info.pack(fill="x", padx=8, pady=(6, 2))
        row = tk.Frame(self.cal, bg=BG)
        row.pack(fill="x", pady=(0, 6))
        self._btn(row, "打刻度尺", self.ruler)
        tk.Label(row, text="折痕读数 (mm):", font=th.FONT, bg=BG, fg=th.TEXT).pack(side="left", padx=(12, 0))
        self.cal_entry = tk.Entry(row, width=8, font=th.FONT_MONO, bg=th.BG, fg=th.FG, insertbackground=th.FG, **th.SUNKEN)
        self.cal_entry.no_ime = True            # 只填数字, 不要中文候选
        self.cal_entry.pack(side="left", padx=6, ipady=3)
        self._btn(row, "保存", self.save_cal, bg=th.PRIMARY)
        self._show_cal_info()


    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _say(self, text, color=None):
        self.msg.config(text=text, fg=color or th.OK)

    def _fail(self, ex, head=""):
        self._say(f"{head}{ex}" if isinstance(ex, (lc.LabelError, ld.mc.ToolError))
                  else f"{head}{type(ex).__name__}: {ex}", th.BAD)

    def _lines(self):
        return [v.get() for v in self.vars]

    # ------------------------------------------------------------ 输入

    def fill_to(self):
        self.vars[0].set(lc.with_to(self.vars[0].get()))

    def fill_recent(self, text):
        self.vars[0].set(text)

    def _render_recents(self):
        for w in self.recent_row.winfo_children():
            w.destroy()
        for s in self.conf["recent_to"]:
            self._btn(self.recent_row, s, lambda t=s: self.fill_recent(t))

    def step_copies(self, delta):
        self.copies = max(1, min(lc.MAX_COPIES, self.copies + delta))
        self.copies_lab.config(text=str(self.copies))
        self.btn_many.config(text=f"打 {self.copies} 张")

    # ------------------------------------------------------------ 预览

    def _schedule_preview(self, *_a):
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(DEBOUNCE_MS, self._start_preview)

    def _start_preview(self):
        self._preview_job = None
        raw = self._lines()
        if not any(s.strip() for s in raw):
            self._clear_preview()
            self._say("")
            return
        fold = lc.fold_mm(self.conf)
        if not self.run_bg(lambda: self.render(raw, fold), lambda r: self._previewed(r, raw), key=KEY_RENDER):
            self._stale = True                  # 上一次还没回来, 回来后重来

    def _previewed(self, res, raw):
        if not self._alive():
            return
        if self._stale or raw != self._lines():
            self._stale = False
            self._schedule_preview()
            return
        if isinstance(res, Exception):
            self._clear_preview()
            self._fail(res)
            return
        self._draw(res)
        warn = lc.problems(res)
        if warn:
            self._say("; ".join(warn), th.WARN)
        else:
            self._say(f"字号 {res.px} 点; 红线是折痕, 上下两半对折后正反都是正的", th.DIM)

    def _guides(self, fold_mm):
        x0, top, x1, bottom = lc.guides(fold_mm)["flag"]
        fold = lc.guides(fold_mm)["fold"]
        self.canvas.create_rectangle((x0 - lc.PREVIEW_X0) * ZOOM, top * ZOOM, (x1 - lc.PREVIEW_X0) * ZOOM - 1, bottom * ZOOM,
                                     outline=th.WARN, dash=(6, 4))
        self.canvas.create_line(0, fold * ZOOM, lc.PREVIEW_W * ZOOM, fold * ZOOM, fill=th.BAD, dash=(4, 3), width=2)

    def _clear_preview(self):
        self.canvas.delete("all")
        self._img = None
        self._guides(lc.fold_mm(self.conf))

    def _draw(self, plan):
        data, _w, _h = lc.preview_pgm(plan)
        self.canvas.delete("all")
        try:
            self._img = tk.PhotoImage(master=self, data=data, format="PPM").zoom(ZOOM)
            self.canvas.create_image(0, 0, image=self._img, anchor="nw")
        except tk.TclError as ex:               # 预览画不出来不影响打印
            self._say(f"预览画不出来: {ex}", th.WARN)
        self._guides(plan.fold_mm)

    # ------------------------------------------------------------ 打印

    def _print(self, raw, copies):
        fold = lc.fold_mm(self.conf)
        self._printing = True
        self._say("正在打印…", th.DIM)
        if not self.run_bg(lambda: self.dev.print_label(raw, fold, copies=copies),
                           lambda r: self._printed(r, raw, copies), key=KEY_IO):
            self._printing = False
            self._say("打印机正忙, 等这一张出完再点", th.WARN)

    def print_one(self):
        self._print(self._lines(), 1)

    def print_copies(self):
        self._print(self._lines(), self.copies)

    def print_again(self):
        if self.last is None:
            self._say("还没打过, 先打一张", th.BAD)
            return
        self._print(self.last, 1)

    def _printed(self, res, raw, copies):
        self._printing = False
        if not self._alive():
            return
        if isinstance(res, Exception):
            self._fail(res)
            return
        self.last = raw
        self.btn_again.config(state="normal")
        warn = lc.problems(res)
        self._say(f"已发出 {copies} 张" + (f"; 注意: {'; '.join(warn)}" if warn else ""), th.WARN if warn else th.OK)
        fresh = lc.remember_to(self.conf, lc.clean_lines(raw))
        if fresh is not self.conf:
            self.conf = fresh
            self._render_recents()
            self.run_bg(lambda: self.save(fresh), self._recent_saved, key=KEY_SAVE)

    def _recent_saved(self, res):
        if isinstance(res, Exception) and self._alive():
            self._say(f"打好了, 但常用 to 没存下: {res}", th.WARN)

    # ------------------------------------------------------------ 走纸 / 校准

    def _io(self, work, done_text):
        def done(res):
            if not self._alive():
                return
            if isinstance(res, Exception):
                self._fail(res)
            else:
                self._say(done_text)
        if not self.run_bg(work, done, key=KEY_IO):
            self._say("打印机正忙, 稍后再点", th.WARN)

    def feed(self):
        self._io(self.dev.feed, "已走一张空白; 下一张就是正式标签")

    def ruler(self):
        self._io(self.dev.ruler, "刻度尺已发出; 对折后读折痕落在几毫米, 填到下面保存")

    def toggle_cal(self):
        self.cal_shown = not self.cal_shown
        if self.cal_shown:
            self.cal.pack(side="bottom", fill="x", padx=16, pady=4, after=self.status)
        else:
            self.cal.pack_forget()

    def _show_cal_info(self):
        self.cal_info.config(text=f"{lc.PAPERS[lc.PAPER]}: 当前折痕 {lc.fold_mm(self.conf):g} mm "
                                  f"(换卷后走纸不准时: 打刻度尺, 看折痕对着的刻度, 填进来按纸型保存)")

    def save_cal(self):
        try:
            mm = lc.parse_fold(self.cal_entry.get())
            new = lc.set_fold(self.conf, lc.PAPER, mm)
        except lc.LabelError as ex:
            self._fail(ex)
            return
        if not self.run_bg(lambda: self.save(new), lambda r: self._cal_saved(r, new), key=KEY_SAVE):
            self._say("上一次保存还没完, 稍后再点", th.WARN)

    def _cal_saved(self, res, new):
        if not self._alive():
            return
        if isinstance(res, Exception):
            self._fail(res, "校准没存下: ")
            return
        self.conf = new
        self._show_cal_info()
        self._say(f"已保存折痕 {lc.fold_mm(new):g} mm")
        self._schedule_preview()

    # ------------------------------------------------------------ 打印机状态

    def _status_tick(self):
        self._status_job = self.after(STATUS_MS, self._status_tick)
        if not self._printing:                  # 打印时不抢锁, 打完下一轮再看
            self.run_bg(self.dev.query_status, self._status_done, key=KEY_STATUS)

    def _status_done(self, res):
        if not self._alive():
            return
        if isinstance(res, Exception):
            self.status.config(text=f"打印机: {res}", fg=th.BAD)
            return
        text, ok = lc.status_view(res)
        self.status.config(text=text, fg=th.OK if ok else th.BAD)

    def destroy(self):
        for job in (self._preview_job, self._status_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._preview_job = self._status_job = None
        super().destroy()
