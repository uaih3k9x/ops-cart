"""副屏上的大肥鱼一行 (#53): 快照只带锁屏一行 (不含原文), 锁屏页画出来."""

import json
import os

import pytest

import dash_core as dc


def test_collect_carries_bigfish_line():
    snap = dc.collect({"bigfish": lambda: "大肥鱼 · 空闲"})
    assert snap["bigfish"] == "大肥鱼 · 空闲"
    assert dc.collect({})["bigfish"] == ""


def test_layout_view_shows_bigfish_only_when_locked():
    snap = {"ts": 100, "bigfish": "大肥鱼 · 在看 con-1"}
    assert dc.layout_view(snap, locked=True, now=101)["bigfish"] == "大肥鱼 · 在看 con-1"
    assert dc.layout_view(snap, locked=False, now=101)["bigfish"] == ""


def test_layout_view_bigfish_not_a_string_is_dropped():
    assert dc.layout_view({"ts": 100, "bigfish": {"x": 1}}, locked=True, now=101)["bigfish"] == ""


def test_bigfish_sample_reads_status_file(tmp_path, monkeypatch):
    import bigfish_core as bf
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"schema": 1, "written_at": 1000.0, "current": None}))
    monkeypatch.setattr(bf, "STATUS_PATH", str(p))
    monkeypatch.setattr(bf, "bigfish_uid", lambda user=bf.USER: os.getuid())
    assert dc.bigfish_sample(clock=lambda: 1001.0) == "大肥鱼 · 空闲"


def test_render_locked_draws_bigfish_line():
    pytest.importorskip("PIL")
    import dash_render as r
    base = {"ts": 0, "rows": [], "notice": "欢迎"}
    a = r.render(dc.layout_view(base, locked=True))
    b = r.render(dc.layout_view(dict(base, bigfish="大肥鱼 · 空闲"), locked=True))
    W, H = a.size
    band = (0, H - 175, W, H - 125)
    assert a.crop(band).tobytes() != b.crop(band).tobytes()
