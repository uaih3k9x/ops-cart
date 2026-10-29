"""dash_core 测试: 采集兜底 / 原子落盘 / 过期判定 / 锁屏版面. 不碰真机."""

import json
import time

import dash_core as dc


def _ctx(**over):
    ctx = {"safe": lambda f, d=None: (f() if f else d), "now": lambda: 1000.0, "seq": 7,
           "net": lambda: "on", "rows": lambda: [{"label": "LAN2", "state": "OK"},
                                                 {"label": "FAN", "state": "FAIL"}],
           "temps": lambda: [("CPU", 45)], "fans": lambda: [], "uptime": lambda: "1d",
           "lamps": lambda: {"con-1": 3, "kvm": 0}, "kiosk": lambda: {"locked": False},
           "todo": lambda: ["换线"], "dream": lambda: "昨夜整理 10-28 · 修了 PXE"}
    ctx.update(over)
    return ctx


def test_collect_shape():
    s = dc.collect(_ctx())
    assert s["ts"] == 1000.0 and s["seq"] == 7 and s["net"] == "on"
    assert s["lamps"]["con-1"] == 3
    assert json.dumps(s, ensure_ascii=False)      # 可序列化


def test_collect_survives_broken_collector():
    def boom():
        raise OSError("device gone")
    s = dc.collect(_ctx(**{"net": boom, "todo": boom}))
    assert s["net"] == "down" and s["todo"] == []


def test_write_is_atomic_and_readable(tmp_path):
    p = tmp_path / "state.json"
    dc.write(str(p), dc.collect(_ctx()))
    assert dc.read(str(p))["seq"] == 7
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]   # 不留临时文件


def test_read_missing_or_broken_is_none(tmp_path):
    assert dc.read(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert dc.read(str(bad)) is None


def test_stale_rules():
    assert dc.stale(None)
    assert dc.stale({})
    assert dc.stale({"ts": "x"})
    assert not dc.stale({"ts": 1000.0}, now=1002.5)
    assert dc.stale({"ts": 1000.0}, now=1004.0)          # > 3 s


def test_read_todo_skips_done_and_headers(tmp_path):
    p = tmp_path / "todo.md"
    p.write_text("# 待办\n\n- [ ] 换 LAN2 的线\n- [x] 打标签\n* [ ] 补 CART-LOG\n普通一行\n")
    assert dc.read_todo(str(p)) == ["换 LAN2 的线", "补 CART-LOG"]
    assert dc.read_todo(str(tmp_path / "nope.md")) == []


def test_read_todo_limit(tmp_path):
    p = tmp_path / "todo.md"
    p.write_text("\n".join(f"- [ ] 第{i}件" for i in range(10)))
    assert len(dc.read_todo(str(p), limit=3)) == 3


def test_layout_locked_hides_todo():
    snap = dc.collect(_ctx())
    v = dc.layout_view(snap, locked=True)
    assert v["locked"] and v["todo"] == []
    assert v["checks"] == {"total": 2, "bad": 1, "first_bad": "FAN", "bad_labels": ["FAN"]}


def test_layout_unlocked_keeps_todo_and_reads_kiosk_flag():
    snap = dc.collect(_ctx(**{"kiosk": lambda: {"locked": True}}))
    v = dc.layout_view(snap)
    assert v["locked"] is True and v["todo"] == []
    v2 = dc.layout_view(dc.collect(_ctx()))
    assert v2["locked"] is False and v2["todo"] == ["换线"]


def test_layout_marks_stale_snapshot():
    snap = dict(dc.collect(_ctx()), ts=time.time())      # ts 用"现在", 不然按 1970 算
    old = dict(snap, ts=time.time() - 10)
    assert dc.layout_view(old)["stale"]
