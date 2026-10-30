"""dash_service 测试: 采集轮次 / 版面变化才推 / 拔屏重连 / once 模式. 全部注入假件."""

import io
import json
import time

import dash_core as dc
import dash_service as ds


def _ctx(**over):
    ctx = {"now": time.time, "net": lambda: "on",   # 快照 ts 用真时间, 不然 layout 判成过期
           "rows": lambda: [{"label": "LAN2", "state": "OK"}],
           "temps": lambda: [("CPU", 45)], "fans": lambda: [],
           "uptime": lambda: "1d", "lamps": lambda: {"con-1": 1},
           "kiosk": lambda: {}, "todo": lambda: ["换线"], "dream": lambda: ""}
    ctx.update(over)
    return ctx


class FakeDisp:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.cfg_text = "esp32p4udisp0_R1280x800\x00"

    def send(self, payload):
        if self.fail:
            raise OSError("unplugged")
        self.sent.append(payload)
        return len(payload)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, disp=None, raises=None):
        self.disp, self.raises, self.calls = disp, raises, 0

    def open_display(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.disp


def test_once_writes_snapshot_and_pushes(tmp_path):
    st = tmp_path / "state.json"
    disp = FakeDisp()
    out = io.StringIO()
    n = ds.run(once=True, state_path=str(st), ctx=_ctx(), out=out,
               display_factory=lambda: FakeFactory(disp),
               render=lambda v: b"JPG" + v["net"].encode())
    assert n == 1 and disp.sent == [b"JPGon"]
    assert json.loads(st.read_text())["net"] == "on"


def test_once_without_display_still_writes_state(tmp_path):
    st = tmp_path / "state.json"
    out = io.StringIO()
    n = ds.run(once=True, state_path=str(st), ctx=_ctx(), out=out,
               display_factory=lambda: FakeFactory(raises=RuntimeError("no dev")),
               render=lambda v: b"x")
    assert n == 0 and st.exists()
    assert "副屏没插" in out.getvalue()


def test_push_failure_drops_display_for_reconnect(tmp_path):
    st = tmp_path / "state.json"
    disp = FakeDisp(fail=True)
    out = io.StringIO()
    n = ds.run(once=True, state_path=str(st), ctx=_ctx(), out=out,
               display_factory=lambda: FakeFactory(disp), render=lambda v: b"x")
    assert n == 0 and getattr(disp, "closed", False)
    assert "推送失败" in out.getvalue()


def test_preview_writes_file_not_usb(tmp_path):
    st, pv = tmp_path / "s.json", tmp_path / "p.bmp"
    factory = FakeFactory(FakeDisp())
    n = ds.run(once=True, preview=str(pv), state_path=str(st), ctx=_ctx(),
               out=io.StringIO(), display_factory=lambda: factory, render=lambda v: b"IMG")
    assert pv.read_bytes() == b"IMG"
    assert n == 0 and factory.calls == 0


def test_view_key_ignores_clock_and_stale():
    a = {"net": "on", "clock": "10:01", "stale": False}
    b = {"net": "on", "clock": "10:02", "stale": False}
    c = {"net": "off", "clock": "10:01", "stale": False}
    assert ds._view_key(a) == ds._view_key(b)
    assert ds._view_key(a) != ds._view_key(c)


def test_loop_pushes_only_on_change():
    """同一版面连续几轮只推一次; 数据变了再推."""
    disp = FakeDisp()
    state = {"net": "on"}

    class Stop(Exception):
        pass

    rounds = {"n": 0}

    def ctx():
        return _ctx(**{"net": lambda: state["net"]})

    sleeps = {"n": 0}
    t = {"v": 1000.0}

    real_sleep = ds.time.sleep

    def fake_time():
        t["v"] += 1.0
        return t["v"]

    def fake_sleep(s):
        sleeps["n"] += 1
        if sleeps["n"] >= 6:
            raise Stop()
        if sleeps["n"] == 3:
            state["net"] = "warn"

    ds.time.sleep = fake_sleep
    real_time = ds.time.time
    ds.time.time = fake_time
    try:
        ds.run(state_path="/tmp/_ds_test.json", ctx=ctx(), out=io.StringIO(),
               display_factory=lambda: FakeFactory(disp), render=lambda v: v["net"].encode())
    except Stop:
        pass
    finally:
        ds.time.sleep = real_sleep
        ds.time.time = real_time
        import os
        try:
            os.unlink("/tmp/_ds_test.json")
        except OSError:
            pass
    assert disp.sent == [b"on", b"warn"]


def test_loop_marks_moving_counters_active(tmp_path, monkeypatch):
    """计数器变了 → 版面里对应的灯标成 active (执行器整块点亮); 第一轮只当基线."""
    views, n = [], {"pxe": 0, "round": 0}

    class Stop(Exception):
        pass

    def lamps():
        n["pxe"] += 100                      # PXE 日志在长 = 有机器在取文件
        return {"pxe": n["pxe"]}

    t = {"v": 1000.0}

    def fake_sleep(_s):
        n["round"] += 1
        if n["round"] >= 3:
            raise Stop()

    monkeypatch.setattr(ds.time, "sleep", fake_sleep)
    monkeypatch.setattr(ds.time, "time", lambda: t.__setitem__("v", t["v"] + 1) or t["v"])
    try:
        ds.run(state_path=str(tmp_path / "s.json"), ctx=_ctx(lamps=lamps, now=lambda: t["v"]),
               out=io.StringIO(), preview=str(tmp_path / "p.jpg"),
               render=lambda v: views.append(v) or b"x")
    except Stop:
        pass
    pxe = [next(x for x in v["lower"] if x["name"] == "PXE")["active"] for v in views]
    assert pxe[0] is False and pxe[-1] is True
