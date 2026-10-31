"""车机 Tk 界面的纯逻辑: UiBridge / 装机定时器 / 自检兜底 / 左侧面板状态机 / 菜单 /
CH9329 工作线程与鼠标决策 / 小键盘数字 / ks 保存. 不起 Tk, 用假 root."""

import ast
import os
import queue
import threading
import time

import pytest

import kiosk_ui_core as ui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeRoot:
    """只记 after 排期, 不真的跑; tick() 手动执行一轮到期的回调."""

    def __init__(self):
        self.jobs, self.binds, self.dead = [], {}, False
        self._n = 0

    def after(self, ms, fn=None):
        if self.dead:
            raise RuntimeError("窗口已销毁")
        self._n += 1
        self.jobs.append((self._n, ms, fn))
        return f"after#{self._n}"

    def after_cancel(self, job):
        self.jobs = [j for j in self.jobs if f"after#{j[0]}" != job]

    def tick(self):
        jobs, self.jobs = self.jobs, []
        for _n, _ms, fn in jobs:
            fn()

    def bind_class(self, cls, seq, fn, add=None):
        self.binds[(cls, seq)] = fn


def _sync_spawn(fn):
    fn()


# ------------------------------------------------------------ B1 UiBridge

def test_bridge_runs_callbacks_only_in_drain():
    root = FakeRoot()
    b = ui.UiBridge(root, spawn=_sync_spawn)
    got = []
    b.post(got.append, 1)
    b.run_bg(lambda: 2, got.append)
    assert got == []                         # 后台只入队, 不碰界面
    b.drain()
    assert got == [1, 2]


def test_bridge_from_real_thread_runs_on_drain_thread():
    root = FakeRoot()
    b = ui.UiBridge(root)
    seen = []
    done = threading.Event()
    b.run_bg(lambda: threading.get_ident(), lambda r: (seen.append((r, threading.get_ident())), done.set()))
    for _ in range(200):
        b.drain()
        if done.is_set():
            break
        time.sleep(0.005)
    worker, main = seen[0]
    assert main == threading.get_ident() and worker != main


def test_bridge_tick_reschedules_with_interval_and_survives_errors():
    root = FakeRoot()
    errs = []
    b = ui.UiBridge(root, interval=50, spawn=_sync_spawn, on_error=errs.append)
    b.start()
    assert [ms for _n, ms, _f in root.jobs] == [50]
    b.post(lambda: 1 / 0)
    root.tick()                              # 回调炸了也要继续排下一轮
    assert len(errs) == 1 and isinstance(errs[0], ZeroDivisionError)
    assert [ms for _n, ms, _f in root.jobs] == [50]


def test_bridge_stops_when_root_destroyed():
    root = FakeRoot()
    b = ui.UiBridge(root, spawn=_sync_spawn)
    b.start()
    root.dead = True
    root.tick()
    assert root.jobs == [] and b.stopped


def test_bridge_work_exception_is_passed_to_done():
    b = ui.UiBridge(FakeRoot(), spawn=_sync_spawn)
    got = []
    b.run_bg(lambda: (_ for _ in ()).throw(OSError("x")), got.append)
    b.drain()
    assert isinstance(got[0], OSError)


def test_bridge_busy_key_guards_until_done_runs():
    b = ui.UiBridge(FakeRoot(), spawn=_sync_spawn)
    got = []
    assert b.run_bg(lambda: 1, got.append, key="pxe") is True
    assert b.busy("pxe")
    assert b.run_bg(lambda: 2, got.append, key="pxe") is False    # 上一次还没回到主线程
    b.drain()
    assert got == [1] and not b.busy("pxe")
    assert b.run_bg(lambda: 3, got.append, key="pxe") is True


def test_bridge_drain_limit_leaves_rest_for_next_tick():
    b = ui.UiBridge(FakeRoot(), spawn=_sync_spawn)
    got = []
    for i in range(5):
        b.post(got.append, i)
    b.drain(limit=3)
    assert got == [0, 1, 2]
    b.drain()
    assert got == [0, 1, 2, 3, 4]


# ------------------------------------------------------------ B2 装机定时器

MAC = "02:00:00:00:00:0a"


def test_pxe_tick_decide_rescue_is_quiet():
    d = ui.pxe_tick_decide({"mode": "rescue"}, "", 100)
    assert d == {"disarm": False, "banner": None, "view": {"mode": "rescue"}}


def test_pxe_tick_decide_install_counts_down():
    st = {"mode": "install", "mac": MAC, "since": 10, "until": 10 + 1800}
    d = ui.pxe_tick_decide(st, "", 10 + 65)
    assert d["disarm"] is False and d["banner"] is None
    assert d["view"] == {"mode": "install", "mac": MAC, "left": 1735}


def test_pxe_tick_decide_expired_disarms():
    d = ui.pxe_tick_decide({"mode": "expired", "mac": MAC, "since": 0, "until": 1}, "", 5)
    assert d["disarm"] is True and "到期" in d["banner"] and d["view"] == {"mode": "rescue"}


def test_pxe_tick_decide_consumed_disarms():
    st = {"mode": "install", "mac": MAC, "since": 10, "until": 9999}
    d = ui.pxe_tick_decide(st, "log", 20, consumed=lambda log, mac, since: (log, mac, since) == ("log", MAC, 10))
    assert d["disarm"] is True and MAC in d["banner"] and d["view"] == {"mode": "rescue"}


MAC2 = "02:00:00:00:00:0b"


def test_pxe_tick_decide_batch_waits_for_everyone_and_retires_consumed():
    """恢复一批 (#18): 名单里全都取走菜单才上锁; 取走的那几台先把菜单删掉 (关机后再 PXE 不会再来一遍)."""
    st = {"mode": "install", "mac": MAC, "macs": [MAC, MAC2], "since": 10, "until": 9999}
    took = {MAC}
    d = ui.pxe_tick_decide(st, "log", 20, consumed=lambda log, mac, since: mac in took)
    assert d["disarm"] is False and d["retire"] == [MAC]
    assert d["view"] == {"mode": "install", "mac": MAC, "left": 9979, "count": 2}
    took.add(MAC2)
    d = ui.pxe_tick_decide(st, "log", 20, consumed=lambda log, mac, since: mac in took)
    assert d["disarm"] is True and "2 台" in d["banner"] and d["view"] == {"mode": "rescue"}


def test_pxe_button_texts_batch():
    a, b = ui.pxe_button_texts({"mode": "install", "mac": MAC, "left": 125, "count": 3})
    assert "3 台" in a and "02:05" in a and "02:05" in b


def test_pxe_tick_decide_left_never_negative():
    st = {"mode": "install", "mac": MAC, "since": 0, "until": 10}
    assert ui.pxe_tick_decide(st, "", 10.5)["view"]["left"] == 0


def test_pxe_button_texts_have_no_symbols():
    a, b = ui.pxe_button_texts({"mode": "install", "mac": MAC, "left": 125})
    assert "02:05" in a and "00:0a" in a and "02:05" in b
    assert ui.pxe_button_texts({"mode": "rescue"}) == ("装机模式:  救援 (安全)", "装机")
    for s in a + b:
        assert not ui.is_symbol_char(s)


# ------------------------------------------------------------ B3 开机自检兜底

def test_boot_rows_safe_turns_exception_into_fail_row():
    def boom():
        raise RuntimeError("坏了")
    rows = ui.boot_rows_safe(boom)
    assert rows == [{"label": "SELFTEST", "state": "FAIL", "detail": "RuntimeError", "check": None}]


def test_boot_rows_safe_passes_rows_through():
    rows = [{"label": "A", "state": "OK", "detail": "", "check": "x"}]
    assert ui.boot_rows_safe(lambda: rows) is rows


def test_once_runs_first_call_only():
    calls = []
    once = ui.Once(lambda: calls.append(1))
    assert once() is True and once() is False and once.done
    assert calls == [1]


# ------------------------------------------------------------ B4 LeftPane

def test_leftpane_show_and_toggle():
    p = ui.LeftPane(ui.PANES)
    assert p.toggle("wifi") == (None, "wifi")
    assert p.toggle("serial") == ("wifi", "serial")
    assert p.toggle("serial") == ("serial", None) and p.current is None


def test_leftpane_any_two_switches_leave_one():
    names = list(ui.PANES)
    for a in names:
        for b in names:
            p = ui.LeftPane(ui.PANES)
            opened = set()
            for n in (a, b):
                close, op = p.toggle(n)
                opened.discard(close)
                if op:
                    opened.add(op)
            assert len(opened) <= 1 and (p.current in opened or p.current is None)


def test_leftpane_close_and_unknown():
    p = ui.LeftPane(ui.PANES)
    p.toggle("preview")
    assert p.close() == "preview" and p.current is None and p.close() is None
    with pytest.raises(KeyError):
        p.toggle("nope")


def test_panes_include_preview():
    assert {"preview", "serial", "ks", "wifi", "netprof", "wol"} == set(ui.PANES)


# ------------------------------------------------------------ B4 菜单

def _actions(menu):
    return [(it["do"], it.get("arg")) for items in menu.values() for it in items if "do" in it]


def test_menu_pages_reachable_and_actions_unique():
    links = {it["page"] for items in ui.MENU.values() for it in items if "page" in it}
    assert links == set(ui.MENU) - {"main"}
    acts = _actions(ui.MENU)
    assert len(acts) == len(set(acts)) and all(ui.MENU.values())


def test_menu_keeps_every_action_and_panes_are_parameterized():
    acts = set(_actions(ui.MENU))
    assert {a for a, _ in acts} == {"toggle_pane", "switch_source", "toggle_mouse_mode", "cycle_kvm",
                                    "toggle_pxe", "toggle_install_mode", "cfg_lan2", "lock_now",
                                    "reboot", "poweroff"}
    assert {arg for a, arg in acts if a == "toggle_pane"} == set(ui.PANES)


def test_menu_has_no_lan1_config_and_no_symbols():
    for items in ui.MENU.values():
        for it in items:
            assert it.get("do") != "cfg_lan1" and "LAN1" not in it["text"]
            assert not ui.is_symbol_char(it["text"]), it["text"]


def test_menu_main_keeps_hot_buttons():
    main = [(it.get("do"), it.get("arg")) for it in ui.MENU["main"]]
    assert ("toggle_pane", "preview") in main and ("toggle_pane", "serial") in main and ("lock_now", None) in main


# ------------------------------------------------------------ B6 键鼠

def test_rel_split_clamps_and_keeps_remainder():
    send, rest = ui.rel_split(300, -300, 0)
    assert send == (127, -127, 0) and rest == (173, -173, 0)


def test_rel_split_conserves_total_over_frames():
    acc, total = (300, -300, 5), [0, 0, 0]
    for _ in range(10):
        send, acc = ui.rel_split(*acc)
        total = [t + s for t, s in zip(total, send)]
    assert total == [300, -300, 5] and acc == (0, 0, 0)


def test_rel_motion_ignores_warp_to_center():
    assert ui.rel_motion((100, 50), (100, 50)) is None
    assert ui.rel_motion((103, 48), (100, 50)) == (3, -2)


@pytest.mark.parametrize("kind,send", [("press", False), ("wheel", False), ("move", False), ("release", True)])
def test_abs_on_border(kind, send):
    assert ui.abs_should_send(kind, None, have_last=True) is send


def test_abs_inside_always_sends_and_release_needs_last():
    for kind in ("press", "wheel", "move", "release"):
        assert ui.abs_should_send(kind, (1, 2), have_last=False) is True
    assert ui.abs_should_send("release", None, have_last=False) is False


class FakeCh:
    def __init__(self, fail_after=None):
        self.sent, self.closed, self.fail_after = [], False, fail_after

    def send_report(self, rep):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError("拔线")
        self.sent.append(("send_report", rep))

    def send_mouse(self, *a):
        self.sent.append(("send_mouse",) + a)

    def close(self):
        self.closed = True


def test_chworker_sends_in_order_off_main_thread():
    ch = FakeCh()
    w = ui.ChWorker(ch, on_gone=lambda ex: None)
    for i in range(5):
        assert w.send("send_report", i)
    w.close(wait=True)
    assert [x[1] for x in ch.sent] == [0, 1, 2, 3, 4] and ch.closed


def test_chworker_closes_handle_on_error_and_reports_once():
    ch, gone = FakeCh(fail_after=1), []
    w = ui.ChWorker(ch, on_gone=gone.append)
    w.send("send_report", 1)
    w.send("send_report", 2)
    w.join(1)
    assert ch.closed and len(gone) == 1 and isinstance(gone[0], OSError)
    assert w.dead and w.send("send_report", 3) is False


def test_chworker_bounded_queue_drops_droppable_when_full():
    gate = threading.Event()

    class Slow(FakeCh):
        def send_mouse(self, *a):
            gate.wait(2)
            super().send_mouse(*a)

    ch = Slow()
    w = ui.ChWorker(ch, on_gone=lambda ex: None, maxsize=2)
    w.send("send_mouse", 0)                   # 工作线程卡在这
    time.sleep(0.05)
    assert w.send("send_mouse", 1, droppable=True) and w.send("send_mouse", 2, droppable=True)
    assert w.send("send_mouse", 3, droppable=True) is False      # 满了: 位移帧丢掉, 不堵主线程
    assert not w.idle()
    gate.set()
    w.close(wait=True)
    assert [x[1] for x in ch.sent] == [0, 1, 2]


def test_chworker_idle_when_queue_empty():
    w = ui.ChWorker(FakeCh(), on_gone=lambda ex: None)
    w.join_queue(1)
    assert w.idle()
    w.close(wait=True)


# ------------------------------------------------------------ B8 小键盘数字

class FakeEntry:
    def __init__(self):
        self.text = ""

    def insert(self, index, s):
        assert index == "insert"
        self.text += s


def test_kp_digits_insert_by_keycode_regardless_of_numlock():
    root = FakeRoot()
    ui.kp_digits_as_digits(root)
    seqs = {seq for (cls, seq) in root.binds if cls == "Entry"}
    assert "<KeyPress-KP_Home>" in seqs and "<KeyPress-KP_Insert>" in seqs
    fn = root.binds[("Entry", "<KeyPress-KP_Home>")]
    w = FakeEntry()

    class Ev:
        widget = w
        keycode = 79                        # X keycode = evdev 71 (KP7) + 8
    assert fn(Ev()) == "break"
    Ev.keycode = 90
    fn(Ev())
    assert w.text == "70"


def test_kp_digits_unknown_keycode_passes_through():
    root = FakeRoot()
    ui.kp_digits_as_digits(root)
    fn = root.binds[("Entry", "<KeyPress-KP_Up>")]
    w = FakeEntry()

    class Ev:
        widget = w
        keycode = 111                       # 方向键上, 不是小键盘
    assert fn(Ev()) is None and w.text == ""


def test_kp_keycode_table_complete():
    assert sorted(ui.KP_DIGITS.values()) == sorted("0123456789.")


# ------------------------------------------------------------ B9 ks 保存/恢复

def test_ks_save_writes_when_valid():
    wrote = []
    ok, msg = ui.ks_save("t", confirm=lambda w: pytest.fail("不该问"), write=wrote.append, validate=lambda t: [])
    assert ok and wrote == ["t"] and msg == ""


def test_ks_save_asks_when_invalid_and_respects_no():
    wrote, asked = [], []
    ok, msg = ui.ks_save("t", confirm=lambda w: asked.append(w) or False, write=wrote.append,
                         validate=lambda t: ["没有 rootpw"])
    assert not ok and wrote == [] and asked == [["没有 rootpw"]] and msg == "已取消"


def test_ks_save_invalid_but_confirmed_writes():
    wrote = []
    ok, _ = ui.ks_save("t", confirm=lambda w: True, write=wrote.append, validate=lambda t: ["x"])
    assert ok and wrote == ["t"]


def test_ks_save_reports_oserror():
    def bad(_t):
        raise PermissionError(13, "没权限")
    ok, msg = ui.ks_save("t", confirm=lambda w: True, write=bad, validate=lambda t: [])
    assert not ok and "没权限" in msg


def test_read_text_closes_file_and_reports(tmp_path):
    p = tmp_path / "a"
    p.write_text("hello")
    assert ui.read_text(str(p)) == "hello"
    with pytest.raises(OSError):
        ui.read_text(str(tmp_path / "missing"))


# ------------------------------------------------------------ B5/B15 车机源码约束 (静态检查, 不起 Tk)

def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_kiosk_runs_no_external_command_on_main_thread():
    tree = ast.parse(_src("kiosk.py"))
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == "subprocess" for a in n.names):
            bad.append("import subprocess")
        if isinstance(n, ast.ImportFrom) and n.module == "subprocess":
            if {a.name for a in n.names} - {"Popen", "DEVNULL"}:
                bad.append("from subprocess import 其他")
        if isinstance(n, ast.Attribute) and n.attr in ("Thread",) and getattr(n.value, "id", "") == "threading":
            bad.append("threading.Thread (改用 UiBridge)")
    assert bad == []


def test_kiosk_pin_gate_persists_failures():
    """车机 PIN 输错计数落 /run: 重启 kiosk 不清零 (只有车机这一个闸门用这个文件)."""
    calls = [n for n in ast.walk(ast.parse(_src("kiosk.py")))
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "PinGate"]
    assert calls
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert "state_path" in kw and ast.unparse(kw["state_path"]) == "kc.PIN_STATE"


def test_kiosk_has_no_lan1_config_and_no_cross_thread_after():
    src = _src("kiosk.py")
    assert "cfg_lan1" not in src and "pxe_conf_read" not in src and "apply_pxe_conf" not in src
    assert "after(0" not in src


@pytest.mark.parametrize("name", ["kiosk.py", "wifi_panel.py", "ks_panel.py", "netprof_panel.py", "wol_panel.py",
                                  "kiosk_ui_core.py"])
def test_ui_sources_have_no_emoji_or_symbols(name):
    hits = [(i, ln.strip()) for i, ln in enumerate(_src(name).splitlines(), 1) if ui.is_symbol_char(ln)]
    assert hits == []


def test_is_symbol_char_rules():
    assert ui.is_symbol_char("\U0001f512") and ui.is_symbol_char("←") and ui.is_symbol_char("•")
    assert ui.is_symbol_char("▸") and ui.is_symbol_char("▂")
    assert not ui.is_symbol_char("处理中… 串口 · conbroker — [透传中] 警告: ->")


def test_panels_use_bridge_not_after0():
    for name in ("wifi_panel.py", "netprof_panel.py", "ks_panel.py", "wol_panel.py"):
        src = _src(name)
        assert "after(0" not in src and "threading.Thread" not in src, name


def test_net_view_colors_and_unknown_state():
    assert ui.net_view("on", "在线") == {"image": "on", "text": "在线", "fg": "#7ee787"}
    assert ui.net_view("warn", "出不去")["fg"] == "#f0b429"
    assert ui.net_view("off", "无路由")["fg"] == "#ff7b72"
    assert ui.net_view("???", "x")["image"] == "off"         # 认不出的状态按离线画, 不抛


def test_lock_screen_shows_net_indicator():
    """#1: 锁屏右上角也有网络标, 和顶栏用同一份状态刷新."""
    src = open(os.path.join(ROOT, "kiosk.py"), encoding="utf-8").read()
    lock = src[src.index("    def lock(self):"):src.index("    def lock_now(self):")]
    assert "_lock_net" in lock
    render = src[src.index("    def _render_net(self"):src.index("    def _render(self, lines)")]
    assert "_lock_net" in render and "kui.net_view" in render


# ------------------------------------------------------------ 1.07.01 死锁修复: 垃圾回收只在主线程, 灯条采样用常驻线程

class FakeGc:
    def __init__(self):
        self.calls = []

    def disable(self):
        self.calls.append("disable")

    def collect(self):
        self.calls.append("collect")


def test_main_thread_gc_disables_auto_and_collects_on_after():
    """自动回收会在任意线程触发, 析构 Tk 对象时等主线程 -> 主线程又在等这个线程启动 = 死锁 (10/27 冻屏)."""
    root, g = FakeRoot(), FakeGc()
    ui.main_thread_gc(root, every_ms=3000, gc_mod=g)
    assert g.calls == ["disable"] and root.jobs[0][1] == 3000
    root.tick()
    assert g.calls == ["disable", "collect"] and len(root.jobs) == 1     # 收完再排下一轮


def test_repeat_bg_posts_each_result_until_stopped():
    b = ui.UiBridge(FakeRoot(), spawn=_sync_spawn)
    n, got, slept = [0], [], []

    def work():
        n[0] += 1
        if n[0] == 2:
            raise ValueError("boom")
        return n[0]

    def sleep(s):
        slept.append(s)
        if len(slept) == 3:
            b.stopped = True
    b.repeat_bg(work, got.append, every=0.2, sleep=sleep)
    assert got == [] and slept == [0.2, 0.2, 0.2]
    b.drain()
    assert got[0] == 1 and isinstance(got[1], ValueError) and got[2] == 3


def test_kiosk_uses_main_thread_gc_and_no_per_tick_threads():
    src = _src("kiosk.py")
    assert "main_thread_gc(" in src
    assert "repeat_bg(" in src


def test_chworker_with_idle_ch_busy_is_not_gone_and_reap_runs_in_worker():
    """#9: 被占用只丢这次输入, 不标 dead; 'reap' 跟发送排同一个队, 在工作线程里放锁."""
    import kvm_core as kvm
    from ch9329 import Ch9329Busy
    plan, devs, busy, gone = [Ch9329Busy("占用")], [], [], []

    def opener():
        if plan:
            raise plan.pop(0)
        devs.append(FakeCh())
        return devs[-1]
    t = [0.0]
    ich = kvm.IdleCh(opener, on_busy=lambda: busy.append(1), clock=lambda: t[0])
    w = ui.ChWorker(ich, on_gone=gone.append)
    w.send("send_report", bytes(8))
    w.join(1)
    assert busy == [1] and not w.dead and gone == []
    w.send("send_report", bytes(8))
    w.join(1)
    t[0] = 10.0
    w.send("reap", droppable=True)
    w.join(1)
    assert devs[0].closed and not ich.is_open
    w.close(wait=True)


def test_kiosk_opens_ch9329_only_inside_idle_ch_opener():
    """#9: 车机主线程不直接开 CH9329 (开口 = 拿独占锁), 只把开法交给 IdleCh, 由 ChWorker 线程按需开关;
    每秒排一次 reap 放空闲的锁; 退出透传走 IdleCh.release (放过锁就不为松开重开)."""
    tree = ast.parse(_src("kiosk.py"))
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Ch9329"]
    in_opener = {id(lam.body) for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "IdleCh"
                 for lam in n.args[:1] if isinstance(lam, ast.Lambda)}
    assert opens and all(id(c) in in_opener for c in opens)
    assert all(ast.unparse(c.args[0]) == "path" for c in opens)      # by-id 路径, 不是 realpath 后的 ttyACMn
    src = _src("kiosk.py")
    assert '"reap"' in src and "on_busy=" in src and '_ch_send("release"' in src


def test_kiosk_arm_dialog_offers_clone_and_confirms_the_list():
    """#18: 解锁对话框能选抓镜像/恢复镜像; 克隆用途把确认时清单的 digest 交给 pxe_arm;
    一批里先取走菜单的那几台要 retire."""
    tree = ast.parse(_src("kiosk.py"))
    arms = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "pxe_arm"]
    assert arms and all("confirm" in {k.arg for k in c.keywords} for c in arms)
    src = _src("kiosk.py")
    for purpose in ("rocky", "winpe", "clone-save", "clone-restore"):
        assert f'"{purpose}"' in src, purpose
    assert "kc.clone_plan(" in src and "cc.plan_lines(" in src
    assert "kc.pxe_retire(" in src and 'd.get("retire")' in src


# ------------------------------------------------------------ #11 自己发地址限时: 顶上醒目条

def test_serve_bar_counts_down_and_offers_renew():
    v = ui.serve_bar_view({"state": "serving", "until": 1000 + 754, "window": 1800}, 1000)
    assert v["text"].startswith("正在给整个网段发地址 剩 12:34") and "续期" in v["text"]
    assert v["renew"] is True and v["bg"] == ui.SERVE_BAR_BG["serving"]
    assert v["short"] == "正在给整个网段发地址 剩 12:34"         # 锁屏: 锁着不能续, 不提续期


def test_serve_bar_expired_says_stopped_and_renew_minutes():
    v = ui.serve_bar_view({"state": "expired", "until": 900, "window": 1800}, 1000)
    assert "到时" in v["text"] and "续期 30 分钟" in v["text"]
    assert v["renew"] is True and v["bg"] == ui.SERVE_BAR_BG["expired"]


def test_serve_bar_unlimited_has_no_renew():
    v = ui.serve_bar_view({"state": "serving", "until": None, "window": 0}, 0)
    assert v["renew"] is False and "续期" not in v["text"] and v["bg"] == ui.SERVE_BAR_BG["serving"]


@pytest.mark.parametrize("doc", [{}, {"state": "proxy"}, {"state": "no-link"}, None])
def test_serve_bar_hidden_otherwise(doc):
    assert ui.serve_bar_view(doc, 0) is None


def test_serve_bar_texts_have_no_symbols():
    for doc in ({"state": "serving", "until": 99, "window": 1800}, {"state": "expired", "window": 1800}):
        assert not any(ui.is_symbol_char(c) for c in ui.serve_bar_view(doc, 0)["text"])


def test_serve_renew_question_names_minutes_and_whole_segment():
    q = ui.serve_renew_question(1800)
    assert "30 分钟" in q and "整个网段" in q
