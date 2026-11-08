"""#85 观察模式的副屏区块 / 车机前面板: 用快照和假画布测, 不起 Tk、不碰串口."""

import os

import pytest

import dash_core as dc
import observe_core as oc
import observe_panel as op
import theme as th


@pytest.fixture(autouse=True)
def _clean_registries():
    """观察中 / 占用登记都在模块级, 每条用例前后清掉."""
    oc._OBSERVING.clear()
    oc._CLAIMS.clear()
    yield
    oc._OBSERVING.clear()
    oc._CLAIMS.clear()


def _snap(now, ports=None):
    ports = ports if ports is not None else [
        {"name": "GigabitEthernet0/0/1", "label": "1", "x": 0, "y": 0, "shape": "rj45", "state": "up",
         "in_uti": 0, "out_uti": 0},
        {"name": "GigabitEthernet0/0/2", "label": "2", "x": 0, "y": 1, "shape": "rj45", "state": "admin",
         "in_uti": 0, "out_uti": 0},
        {"name": "XGigabitEthernet0/0/1", "label": "1", "x": 2, "y": 0, "shape": "sfp", "state": "down",
         "in_uti": 0, "out_uti": 0}]
    return {"ts": now, "polled_at": now, "owner": "w1", "head": "con-1", "device": "SW-TEST", "model": None, "state": "polling", "error": None,
            "ports": ports}


def test_layout_view_has_front_when_observing():
    v = dc.layout_view({"ts": 100, "observe": _snap(100)}, now=101)
    assert v["front"]["title"] == "con-1 · SW-TEST"
    assert [p["state"] for p in v["front"]["ports"]] == ["up", "admin", "down"]


def test_layout_view_no_front_when_locked_stale_or_missing():
    assert dc.layout_view({"ts": 100, "observe": _snap(100)}, locked=True, now=101)["front"] is None
    assert dc.layout_view({"ts": 100, "observe": _snap(0)}, now=oc.WRITE_STALE_S + 1)["front"] is None
    assert dc.layout_view({"ts": 100}, now=101)["front"] is None


def test_collect_pulls_observe():
    snap = dc.collect({"observe": lambda: _snap(5), "now": lambda: 5})
    assert snap["observe"]["device"] == "SW-TEST"
    assert dc.collect({"now": lambda: 5})["observe"] == []


def test_render_draws_front_lamps():
    PIL = pytest.importorskip("PIL")  # noqa: F841
    import dash_render as r
    v = dc.layout_view({"ts": 100, "observe": _snap(100)}, now=101)
    img = r.render(v)
    boxes = r.front_boxes(v["front"])
    (x0, y0, x1, y1), port = boxes[0]
    assert port["state"] == "up"
    assert img.getpixel(((x0 + x1) // 2, y0 + 3)) == _rgb(th.LAMP_OK)
    (x0, y0, x1, y1), port = boxes[1]
    assert img.getpixel(((x0 + x1) // 2, y0 + 3)) == _rgb(th.LAMP_WARN)
    assert all(r.M <= b[0][0] and b[0][2] <= r.PANEL_W - r.M for b in boxes)


def test_render_front_many_ports_fit_width():
    pytest.importorskip("PIL")
    import dash_render as r
    names = [f"GigabitEthernet0/0/{i}" for i in range(1, 49)] + [f"XGigabitEthernet0/0/{i}" for i in range(1, 5)]
    ports = [dict(p, state="up") for p in oc.auto_layout(names)]
    v = dc.layout_view({"ts": 100, "observe": _snap(100, ports)}, now=101)
    boxes = r.front_boxes(v["front"])
    assert len(boxes) == 52
    assert max(b[0][2] for b in boxes) <= r.PANEL_W - r.M
    r.render(v)


def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ------------------------------------------------------------ 车机前面板: 纯计算 + 假画布

def test_panel_cells_colors_and_shapes():
    ports = _snap(0)["ports"]
    cells = op.panel_cells(ports, {p["name"]: p for p in ports}, now=0)
    by = {c["name"]: c for c in cells}
    assert by["GigabitEthernet0/0/1"]["fill"] == th.LAMP_OK
    assert by["GigabitEthernet0/0/2"]["fill"] == th.LAMP_WARN
    assert by["XGigabitEthernet0/0/1"]["fill"] == op.OFF
    assert by["GigabitEthernet0/0/1"]["box"][1] < by["GigabitEthernet0/0/2"]["box"][1]     # 奇数口在上
    assert by["XGigabitEthernet0/0/1"]["shape"] == "sfp"


def test_port_info_line():
    p = {"name": "GigabitEthernet0/0/6", "phy": "up", "proto": "up", "state": "up", "in_uti": 12.5, "out_uti": 3.2}
    s = op.port_info(p)
    assert "GigabitEthernet0/0/6" in s and "12.5%" in s and "3.2%" in s
    assert "--" in op.port_info({"name": "Vlanif1", "phy": "up", "proto": "up", "in_uti": None, "out_uti": None})


class FakeCanvas:
    def __init__(self):
        self.calls = []

    def delete(self, *a):
        self.calls.append(("delete",) + a)

    def create_rectangle(self, *a, **k):
        self.calls.append(("rect", a, k))
        return len(self.calls)

    def create_polygon(self, *a, **k):
        self.calls.append(("poly", a, k))
        return len(self.calls)

    def create_text(self, *a, **k):
        self.calls.append(("text", a, k))
        return len(self.calls)


def test_draw_on_fake_canvas():
    ports = _snap(0)["ports"]
    c = FakeCanvas()
    hits = op.draw(c, op.panel_cells(ports, {p["name"]: p for p in ports}, now=0))
    texts = [c_[2]["text"] for c_ in c.calls if c_[0] == "text"]
    assert texts.count("1") == 2 and "2" in texts
    assert len(set(hits.values())) == 3 and all(isinstance(i, int) for i in hits)


def test_render_stale_front_is_grey_with_note():
    pytest.importorskip("PIL")
    import dash_render as r
    snap = dict(_snap(1000), polled_at=1000 - oc.FRONT_STALE_S - 1)
    v = dc.layout_view({"ts": 1000, "observe": snap}, now=1000)
    assert v["front"]["stale"] and v["front"]["note"]
    img = r.render(v)
    (x0, y0, x1, y1), _p = r.front_boxes(v["front"])[0]
    assert img.getpixel(((x0 + x1) // 2, y0 + 3)) == _rgb(th.DASH_LINE)


def test_panel_cells_grey_when_stale():
    ports = _snap(0)["ports"]
    cells = op.panel_cells(ports, {p["name"]: p for p in ports}, now=0, stale=True)
    assert {c["fill"] for c in cells} == {op.OFF}


def test_one_window_per_port():
    oc._CLAIMS.clear()
    assert oc.claim("con-1", "w1") is True
    assert oc.claim("con-1", "w2") is False
    assert oc.claim("con-2", "w3") is True
    oc.release("con-1", "w2")                   # 别人的不放
    assert oc.claim("con-1", "w4") is False
    oc.release("con-1", "w1")
    assert oc.claim("con-1", "w4") is True
    oc._CLAIMS.clear()


def test_unknown_port_drawn_distinctly():
    ports = [{"name": "GigabitEthernet0/0/1", "label": "1", "x": 0, "y": 0, "shape": "rj45", "state": "unknown"}]
    cells = op.panel_cells(ports, {p["name"]: p for p in ports}, now=0)
    assert cells[0]["fill"] not in (op.OFF, th.LAMP_OK, th.LAMP_WARN, th.DIM)


def test_layout_view_picks_newest_fresh_of_several():
    a, b = dict(_snap(100), head="con-1"), dict(_snap(105), head="con-2")
    v = dc.layout_view({"ts": 105, "observe": [b, a]}, now=106)
    assert v["front"]["title"].startswith("con-2")
    v = dc.layout_view({"ts": 105, "observe": [dict(b, ts=0), a]}, now=106)
    assert v["front"]["title"].startswith("con-1")


def test_layout_view_shows_not_started_without_ports():
    doc = dict(_snap(100, ports=[]), state="idle", polled_at=None)
    v = dc.layout_view({"ts": 100, "observe": [doc]}, now=101)
    assert v["front"]["note"] == "准备开始" and v["front"]["ports"] == []
    pytest.importorskip("PIL")
    import dash_render as r
    r.render(v)


def test_default_ctx_reads_per_port_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "STATE_DIR", str(tmp_path))
    (tmp_path / "con-3.json").write_text('{"ts": 1, "head": "con-3"}')
    assert [d["head"] for d in oc.read_snapshots()] == ["con-3"]



@pytest.mark.parametrize("names", [
    [f"GigabitEthernet0/0/{i}" for i in range(1, 49)] + [f"XGigabitEthernet0/0/{i}" for i in range(1, 5)],
    [f"GigabitEthernet0/0/{i}" for i in range(1, 25)] + [f"XGigabitEthernet0/0/{i}" for i in range(1, 5)],
    [f"GigabitEthernet0/0/{i}" for i in range(1, 9)],
])
def test_panel_fits_canvas_width(names):
    panel = oc.auto_layout(names)
    cells = op.panel_cells(panel, {}, now=0, width=op.CANVAS_W)
    assert len(cells) == len(names)
    assert max(c["box"][2] for c in cells) <= op.CANVAS_W
    assert min(c["box"][2] - c["box"][0] for c in cells) >= 16


class _FakeWin:
    def __init__(self):
        import queue
        import threading
        self.ui, self.stop_evt, self.head = queue.Queue(), threading.Event(), "con-1"
        self._logged_in = self._ended = self._console_back = None
        self.token = "t"


def test_snapshot_write_failure_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(oc, "STATE_DIR", str(blocker / "observe"))
    w = _FakeWin()
    w.obs, w.token, w._snap_at, w._snap_err = None, "t", 0, None

    class Obs:
        def snapshot(self, head, owner=None):
            return {"ts": 1}
    w.obs = Obs()
    op.ObserveWindow._write_snap(w)
    assert w._snap_err


# ------------------------------------------------------------ 第五轮: 车机控制台暂停 / 接回, 窗口生命周期

class _Panel:
    """串口面板替身: sock / port_var / _serves / _disconnect / _connect / winfo_exists."""

    def __init__(self, dev="/dev/con-1", sock=True, alive=True, reconnect_ok=True):
        class V:
            def __init__(self, v):
                self.v = v

            def get(self):
                return self.v
        self.port_var, self._serves = V(dev), {"/dev/con-1": "9600", "/dev/con-2": "9600"}
        self.sock, self.alive, self.ok, self.calls = (object() if sock else None), alive, reconnect_ok, []

    def winfo_exists(self):
        return self.alive

    def _disconnect(self):
        self.calls.append("disconnect")
        self.sock = None

    def _connect(self, wake=True):
        self.calls.append(("connect", wake))
        self.sock = object() if self.ok else None


def test_console_hold_pauses_only_same_port_and_resumes_it():
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    assert h.pause() is True and p.calls == ["disconnect"]
    assert h.resume() is None and p.calls[-1] == ("connect", False)


def test_console_hold_ignores_other_port_or_unconnected():
    p = _Panel(dev="/dev/con-2")
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    assert h.pause() is False and p.calls == [] and oc.is_observing("/dev/con-1")   # 没断开别的口, 但照样登记观察中
    assert h.resume() is None and p.calls == [] and not oc.is_observing("/dev/con-1")
    p = _Panel(sock=False)
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    assert h.pause() is False and p.calls == []


def test_console_hold_no_resume_if_panel_gone_or_switched():
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    p.alive = False
    assert h.resume() is None and p.calls == ["disconnect"]
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    p.port_var.v = "/dev/con-2"                  # 人切到别的口了: 不抢回来
    assert h.resume() is None and p.calls == ["disconnect"]


def test_console_hold_reports_failed_resume():
    p = _Panel(reconnect_ok=False)
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    msg = h.resume()
    assert msg and "手动" in msg


def test_life_releases_claim_only_when_window_and_worker_both_gone():
    oc._CLAIMS.clear()
    oc.claim("con-1", "t1")
    life = op.Life("con-1", "t1")
    life.worker_started()
    life.window_gone()                           # 面板切走 / 窗口关了, worker 还在跑
    assert oc.holder("con-1") == "t1"
    life.worker_done()
    assert oc.holder("con-1") is None
    oc.claim("con-1", "t2")
    life = op.Life("con-1", "t2")
    life.window_gone()                           # 没起过 worker: 关窗就放
    assert oc.holder("con-1") is None
    oc._CLAIMS.clear()


def test_open_for_hangs_window_on_root_not_panel(monkeypatch):
    oc._CLAIMS.clear()
    made = {}

    class FakeWin:
        def __init__(self, master, dev, name, vendor, ddir, token, panel=None):
            made.update(master=master, panel=panel)
    monkeypatch.setattr(op, "ObserveWindow", FakeWin)
    p = _Panel()
    root = object()
    p.winfo_toplevel = lambda: root
    p.winfo_children = lambda: []
    op.open_for(p, "/dev/con-1", "/dev/con-1", "huawei", "/x")
    assert made["master"] is root and made["panel"] is p
    oc._CLAIMS.clear()


# ---- 第六轮 低7: 借走控制台期间, 串口面板不许自动接回 / 发唤醒回车

def test_console_hold_marks_panel_held():
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    assert oc.is_observing("/dev/con-1")
    h.resume()
    assert not oc.is_observing("/dev/con-1")


def test_two_holds_on_one_panel_do_not_clear_each_other():
    p = _Panel()
    a, b = op.ConsoleHold("/dev/con-1", panels=lambda: [p]), op.ConsoleHold("/dev/con-2", panels=lambda: [p])
    a.pause()
    b.pause()
    b.resume()
    assert oc.is_observing("/dev/con-1") and not oc.is_observing("/dev/con-2")


def test_serial_panel_held_check_uses_selected_port():
    import serial_panel as sp

    class V:
        def __init__(self, v):
            self.v = v

        def get(self):
            return self.v

    oc.observing_add("/dev/con-1")

    class P:
        _serves = {"/dev/con-1": "9600", "/dev/con-2": "9600"}
        port_var = V("/dev/con-1")
    assert sp.SerialPanel._auto_connect_ok(P()) is False
    assert sp.SerialPanel._wake_ok(P(), True) is False           # 观察中途切到这一路: 不发唤醒回车
    P.port_var = V("/dev/con-2")                                  # 别的口不受影响
    assert sp.SerialPanel._auto_connect_ok(P()) is True
    assert sp.SerialPanel._wake_ok(P(), True) is True and sp.SerialPanel._wake_ok(P(), False) is False



# ---- 不自动 quit: kiosk 退出时只给后台线程一点时间收尾, 同步记一笔

def test_shutdown_joins_worker_briefly_and_logs(monkeypatch):
    import threading
    got = []
    monkeypatch.setattr(oc, "record", lambda action, **kw: got.append((action, kw)))
    w = _FakeWin()
    w.worker = threading.Thread(target=lambda: w.stop_evt.wait(5), daemon=True)
    w.worker.start()
    op.ObserveWindow._shutdown(w, timeout=2)
    assert w.stop_evt.is_set() and not w.worker.is_alive()
    assert got and got[0][0] == "interrupted" and "打断" in got[0][1]["note"]


def test_shutdown_gives_up_after_timeout(monkeypatch):
    import threading
    import time as _t
    monkeypatch.setattr(oc, "record", lambda action, **kw: None)
    w = _FakeWin()
    hang = threading.Event()
    w.worker = threading.Thread(target=lambda: hang.wait(5), daemon=True)
    w.worker.start()
    t0 = _t.monotonic()
    op.ObserveWindow._shutdown(w, timeout=0.3)
    assert _t.monotonic() - t0 < 1.5
    hang.set()


def test_shutdown_without_worker_logs_nothing(monkeypatch):
    got = []
    monkeypatch.setattr(oc, "record", lambda action, **kw: got.append(action))
    w = _FakeWin()
    w.worker = None
    op.ObserveWindow._shutdown(w)
    assert got == []



# ---- 第七轮: 观察全程持有控制台; _work 收尾; kiosk 退出提示按阶段写

def test_work_always_finishes_life_and_posts_ended(monkeypatch):
    monkeypatch.setattr(oc, "record", lambda *a, **k: None)
    w = _FakeWin()
    w.life = op.Life("con-1", "t")
    oc.claim("con-1", "t")
    w.life.worker_started()
    w.life.window_gone()

    class Obs:
        device = error = None
        state = "polling"

        def start(self, name, cancelled=None):
            return True

        def tick(self, now):
            raise RuntimeError("炸了")

        def stop(self):
            pass
    obs = Obs()
    op.ObserveWindow._work(w, obs, "SW-TEST")      # 意外异常: 不再往外抛, 观察置成 failed
    assert obs.state == "failed" and "炸了" in obs.error
    assert oc.holder("con-1") is None
    assert not w.ui.empty()


def test_serial_panel_connect_refused_while_observing():
    import serial_panel as sp

    class V:
        def get(self):
            return "/dev/con-1"

    class T:
        def __init__(self):
            self.kw = {}

        def config(self, **kw):
            self.kw.update(kw)

    oc.observing_add("/dev/con-1")

    class P:
        _serves = {"/dev/con-1": "9600"}
        port_var = V()
        sock = None

        def __init__(self):
            self.con_title, self.disconnects = T(), 0

        def _disconnect(self):
            self.disconnects += 1
    p = P()
    p._show_held = lambda: sp.SerialPanel._show_held(p)
    sp.SerialPanel._connect(p)
    assert p.sock is None and "观察中" in p.con_title.kw["text"]


def test_console_hold_for_whole_observation_until_ended():
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    assert p.sock is None and oc.is_observing("/dev/con-1")
    p.port_var.v = "/dev/con-2"                  # 观察中途人切走又切回来
    p.port_var.v = "/dev/con-1"
    assert oc.is_observing("/dev/con-1")         # 仍然登记着, 面板不会自动接 / 发唤醒
    h.resume()
    assert p.calls[-1] == ("connect", False) and not oc.is_observing("/dev/con-1")



# ------------------------------------------------------------ 半自动: 开始观察 = 看现场 + 人确认设备名

class _Lbl:
    def __init__(self):
        self.kw = {}

    def config(self, **kw):
        self.kw.update(kw)


class _BeginWin:
    """ObserveWindow.begin / confirm / cancel_confirm 用到的那些属性的替身. probes: 依次返回的现场."""

    def __init__(self, *probes):
        self.worker, self.obs, self._closing, self._confirming, self.spawned = None, None, True, None, []
        self.status, self.close_btn, self.start_btn = _Lbl(), _Lbl(), _Lbl()
        self.stop_evt = __import__("threading").Event()
        self.hold = op.ConsoleHold("/dev/con-1", panels=lambda: [None])
        self.close, self.shown = None, []
        self._probes = list(probes)

    def _new_observer(self):
        win = self

        class O:
            def probe(self):
                return win._probes.pop(0) if len(win._probes) > 1 else win._probes[0]
        return O()

    def _show_confirm(self, text):
        self.shown.append(text)

    def _hide_confirm(self):
        self.shown.append(None)

    def _spawn(self, obs, name):
        self.spawned.append(name)
        self.worker = object()


def test_begin_refuses_with_reason_without_confirming():
    w = _BeginWin((None, "console 还没登录 (停在 Username:), 请先在 console 上登录"))
    op.ObserveWindow.begin(w)
    assert "还没登录" in w.status.kw["text"] and w.shown == [] and w.spawned == [] and w._confirming is None


def test_begin_shows_inline_confirm_not_modal():
    import inspect
    w = _BeginWin(("SW-TEST", None))
    op.ObserveWindow.begin(w)
    assert w.shown == [op.confirm_text("SW-TEST")] and w.spawned == [] and w._confirming
    assert w.start_btn.kw["state"] == "disabled"
    assert "messagebox" not in inspect.getsource(op)                 # 不用模态框 (嵌套事件循环可重入)


def test_begin_reentry_during_confirm_does_nothing():
    w = _BeginWin(("SW-TEST", None))
    op.ObserveWindow.begin(w)
    op.ObserveWindow.begin(w)                    # 确认期间又点一次
    assert len([x for x in w.shown if x]) == 1 and w.spawned == []
    op.ObserveWindow.confirm(w)
    op.ObserveWindow.confirm(w)                  # 再点确认也只起一个线程
    assert w.spawned == ["SW-TEST"] and w._closing is False and w.close_btn.kw["text"] == "退出观察"


def test_confirm_rechecks_scene():
    w = _BeginWin(("SW-TEST", None), ("OTHER-SW", None))
    op.ObserveWindow.begin(w)
    op.ObserveWindow.confirm(w)
    assert w.spawned == [] and "变" in w.status.kw["text"] and w.start_btn.kw["state"] == "normal"
    w = _BeginWin(("SW-TEST", None), (None, "console 停在系统视图里"))
    op.ObserveWindow.begin(w)
    op.ObserveWindow.confirm(w)
    assert w.spawned == [] and "系统视图" in w.status.kw["text"]


def test_confirm_refused_if_worker_already_running():
    w = _BeginWin(("SW-TEST", None))
    op.ObserveWindow.begin(w)
    w.worker = object()
    op.ObserveWindow.confirm(w)
    assert w.spawned == []


def test_cancel_confirm():
    w = _BeginWin(("SW-TEST", None))
    op.ObserveWindow.begin(w)
    op.ObserveWindow.cancel_confirm(w, "锁屏了")
    assert w._confirming is None and w.shown[-1] is None and "锁屏" in w.status.kw["text"]
    assert w.start_btn.kw["state"] == "normal"
    op.ObserveWindow.confirm(w)
    assert w.spawned == []


def test_lock_cancels_pending_confirm():
    op._WINDOWS.clear()
    calls = []

    class W(_Win):
        _confirming = ("obs", "SW-TEST")

        def cancel_confirm(self, why=None):
            calls.append(why)
            self._confirming = None
    w = W()
    op._WINDOWS.add(w)
    op.hide_all()
    assert calls and "锁屏" in calls[0] and "withdraw" in w.calls
    op._WINDOWS.clear()


def test_begin_holds_panel_console_for_whole_observation():
    p = _Panel()
    w = _BeginWin(("SW-TEST", None))
    w.hold = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    op.ObserveWindow.begin(w)
    assert p.calls == []                         # 等确认的时候还不动面板
    op.ObserveWindow.confirm(w)
    assert p.calls == ["disconnect"] and oc.is_observing("/dev/con-1")   # 人用面板登录的: 观察期间借走



# ============================================================ 半自动版复审: 面板 / kiosk

def test_new_serial_panel_sees_observing_port():
    import serial_panel as sp

    class V:
        def get(self):
            return "/dev/con-1"

    class P:
        _serves = {"/dev/con-1": "9600"}
        port_var = V()
    oc._OBSERVING.clear()
    oc.observing_add("/dev/con-1")
    try:
        assert sp.SerialPanel._auto_connect_ok(P()) is False
        assert sp.SerialPanel._wake_ok(P(), True) is False
    finally:
        oc._OBSERVING.clear()
    assert sp.SerialPanel._auto_connect_ok(P()) is True


def test_console_hold_resume_finds_current_live_panel():
    oc._OBSERVING.clear()
    old = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [old])
    h.pause()
    assert old.calls == ["disconnect"] and oc.is_observing("/dev/con-1")
    old.alive = False                           # kiosk 切页: 旧面板没了, 新建了一个
    new = _Panel(sock=False)
    h.panels = lambda: [old, new]
    assert h.resume() is None
    assert new.calls == [("connect", False)] and old.calls == ["disconnect"]
    assert not oc.is_observing("/dev/con-1")


def test_console_hold_resume_skips_panel_on_other_port():
    oc._OBSERVING.clear()
    p = _Panel(dev="/dev/con-2", sock=False)
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    h.resume()
    assert p.calls == []


def test_console_hold_resume_reports_failure():
    oc._OBSERVING.clear()
    p = _Panel(reconnect_ok=False)
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    assert "手动" in h.resume()


def test_serial_panel_registers_itself_live():
    class P:
        pass
    p = P()
    oc.register_panel(p)
    assert p in list(oc.live_panels())


class _Win:
    def __init__(self):
        self.calls = []

    def withdraw(self):
        self.calls.append("withdraw")

    def deiconify(self):
        self.calls.append("deiconify")

    def lift(self):
        self.calls.append("lift")

    def winfo_exists(self):
        return True


def test_lock_hides_and_unlock_restores_observe_windows():
    op._WINDOWS.clear()
    a, b = _Win(), _Win()
    op._WINDOWS.update({a, b})
    op.hide_all()
    assert a.calls == ["withdraw"] and b.calls == ["withdraw"]
    op.show_all()
    assert a.calls[-2:] == ["deiconify", "lift"]
    op._WINDOWS.clear()


def test_kiosk_lock_and_unlock_call_observe_hide_show():
    import ast
    src = open(os.path.join(os.path.dirname(op.__file__), "kiosk.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "hide_all" in fns["lock"] and "show_all" in fns["_try_unlock"]



# ============================================================ 第九轮

def test_life_counts_workers():
    oc.claim("con-1", "t")
    life = op.Life("con-1", "t")
    life.worker_started()
    life.worker_started()                        # 万一起了两个
    life.window_gone()
    life.worker_done()
    assert oc.holder("con-1") == "t"             # 先结束的那个不能把占用放掉
    life.worker_done()
    assert oc.holder("con-1") is None


def test_console_hold_drop_does_not_reconnect():
    p = _Panel()
    h = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    h.pause()
    h.drop()
    assert p.calls == ["disconnect"] and not oc.is_observing("/dev/con-1")


def test_release_console_on_kiosk_exit_drops_without_reconnect():
    p = _Panel()

    class W:
        hold = op.ConsoleHold("/dev/con-1", panels=lambda: [p])
    w = W()
    w.hold.pause()
    op.ObserveWindow._release_console(w, interrupted=True)
    assert p.calls == ["disconnect"]
    w.hold.pause()
    op.ObserveWindow._release_console(w, interrupted=False)
    assert p.calls[-1] == ("connect", False)


def test_shutdown_reports_whether_it_interrupted(monkeypatch):
    import threading
    monkeypatch.setattr(oc, "record", lambda *a, **k: None)
    w = _FakeWin()
    w.worker = None
    assert op.ObserveWindow._shutdown(w) is False
    w.stop_evt.clear()
    w.worker = threading.Thread(target=lambda: w.stop_evt.wait(5), daemon=True)
    w.worker.start()
    assert op.ObserveWindow._shutdown(w, timeout=2) is True


def test_new_panel_shows_observing_after_refresh():
    import serial_panel as sp

    class V:
        def get(self):
            return "/dev/con-1"

    class P:
        _serves = {"/dev/con-1": "9600"}
        port_var = V()
        sock = None

        def __init__(self):
            self.shown, self.connected = 0, []

        def _show_held(self):
            self.shown += 1

        def _connect(self, wake=True):
            self.connected.append(wake)

        def _auto_connect_ok(self):
            return sp.SerialPanel._auto_connect_ok(self)

        def _held_here(self):
            return sp.SerialPanel._held_here(self)
    oc.observing_add("/dev/con-1")
    p = P()
    sp.SerialPanel._after_refresh(p, True)
    assert p.shown == 1 and p.connected == []
    oc.observing_discard("/dev/con-1")
    p = P()
    sp.SerialPanel._after_refresh(p, True)
    assert p.shown == 0 and p.connected == [True]
