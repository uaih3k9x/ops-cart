"""#85 观察模式的副屏区块 / 车机前面板: 用快照和假画布测, 不起 Tk、不碰串口."""

import pytest

import dash_core as dc
import observe_core as oc
import observe_panel as op
import theme as th


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


def test_layout_view_shows_logging_in_without_ports():
    doc = dict(_snap(100, ports=[]), state="idle", polled_at=None)
    v = dc.layout_view({"ts": 100, "observe": [doc]}, now=101)
    assert v["front"]["note"] == "登录中" and v["front"]["ports"] == []
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


def test_password_box_emptied_when_taken(monkeypatch):
    monkeypatch.setattr(oc, "record", lambda *a, **k: None)
    box = ["s3cret"]
    seen = {}

    class Obs:
        device = error = None
        state, note = "failed", None

        def start(self, user, pw, cancelled=None):
            seen["pw"], seen["box"] = pw, list(box)
            return False

        def stop(self, now=None):
            pass
    op.ObserveWindow._work(_FakeWin(), Obs(), "admin", box)
    assert seen == {"pw": "s3cret", "box": []} and box == []


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
    h = op.ConsoleHold(p, "/dev/con-1")
    assert h.pause() is True and p.calls == ["disconnect"]
    assert h.resume() is None and p.calls[-1] == ("connect", False)


def test_console_hold_ignores_other_port_or_unconnected():
    p = _Panel(dev="/dev/con-2")
    h = op.ConsoleHold(p, "/dev/con-1")
    assert h.pause() is False and h.resume() is None and p.calls == []
    p = _Panel(sock=False)
    h = op.ConsoleHold(p, "/dev/con-1")
    assert h.pause() is False and p.calls == []


def test_console_hold_no_resume_if_panel_gone_or_switched():
    p = _Panel()
    h = op.ConsoleHold(p, "/dev/con-1")
    h.pause()
    p.alive = False
    assert h.resume() is None and p.calls == ["disconnect"]
    p = _Panel()
    h = op.ConsoleHold(p, "/dev/con-1")
    h.pause()
    p.port_var.v = "/dev/con-2"                  # 人切到别的口了: 不抢回来
    assert h.resume() is None and p.calls == ["disconnect"]


def test_console_hold_reports_failed_resume():
    p = _Panel(reconnect_ok=False)
    h = op.ConsoleHold(p, "/dev/con-1")
    h.pause()
    msg = h.resume()
    assert msg and "手动" in msg


def test_only_one_window_logging_in_at_a_time():
    op._LOGIN_SLOT.clear()
    assert op.login_slot("a") is True
    assert op.login_slot("b") is False
    op.login_slot_done("b")
    assert op.login_slot("b") is False
    op.login_slot_done("a")
    assert op.login_slot("b") is True
    op._LOGIN_SLOT.clear()


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
    p._held = None
    h = op.ConsoleHold(p, "/dev/con-1")
    h.pause()
    assert p._held
    h.resume()
    assert not p._held


def test_serial_panel_no_autoconnect_or_wake_while_held():
    import serial_panel as sp

    class P:
        _held = "/dev/con-1"
    assert sp.SerialPanel._auto_connect_ok(P()) is False
    assert sp.SerialPanel._wake_ok(P(), True) is False
    P._held = None
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
    assert got and got[0][0] == "interrupted" and "Password:" in got[0][1]["note"]


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
