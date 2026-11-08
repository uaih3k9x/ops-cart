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
        self._logged_in = self._ended = None


def test_password_box_emptied_when_taken(monkeypatch):
    monkeypatch.setattr(oc, "record", lambda *a, **k: None)
    box = ["s3cret"]
    seen = {}

    class Obs:
        device = error = None
        state, left = "failed", "none"

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
