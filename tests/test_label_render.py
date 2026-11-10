"""中文渲染 (#104): ctypes + libfreetype 放在一次性子进程里. 协议和失败提示用假 run 测, 真 FreeType 有就测."""

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import label_core as lc
import label_render as lr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIFONT = os.path.join(ROOT, "fonts", "unifont-18.0.01.otf")
HAS_FT = bool(ctypes.util.find_library("freetype"))


def _done(payload=None, rc=0, stderr=b""):
    out = json.dumps(payload).encode() if payload is not None else b""
    return subprocess.CompletedProcess([], rc, out, stderr)


def _ok_run(captured=None):
    def run(cmd, **kw):
        if captured is not None:
            captured.update(cmd=cmd, **kw)
        req = json.loads(kw["input"])
        return _done({"px": 20, "lines": req["lines"], "inks": [[8, 2, lr._b64(b"\x01" * 16)] for _ in req["lines"]]})
    return run


def test_render_plan_runs_worker_with_stdin_only():
    seen = {}
    p = lr.render_plan(["SW-A"], 10.0, run=_ok_run(seen))
    assert isinstance(p, lc.Plan) and p.fold == lc.dots(10.0) and p.px == 20
    assert seen["cmd"] == [sys.executable, os.path.abspath(lr.__file__)]
    assert seen["timeout"] == lr.RENDER_TIMEOUT_S
    req = json.loads(seen["input"])
    top, h = lc.face_box(lc.dots(10.0))
    assert req["lines"] == ["SW-A"] and req["box_w"] == lc.CANVAS_W and req["box_h"] == h and top > 0


def test_render_plan_cleans_input_before_spawning():
    def boom(*a, **k):
        raise AssertionError("不该起子进程")
    with pytest.raises(lc.LabelError, match="控制字符"):
        lr.render_plan(["a\r\nPRINT 9"], 10.0, run=boom)


@pytest.mark.parametrize("res, word", [
    (_done(rc=-11), "崩"),
    (_done(rc=2, stderr=b"boom"), "boom"),
    (_done({"error": "字体里没有: X"}), "字体里没有"),
    (subprocess.CompletedProcess([], 0, b"not json", b""), "看不懂"),
    (_done({"px": 20}), "看不懂"),
])
def test_render_plan_failure_is_clear(res, word):
    with pytest.raises(lc.LabelError, match=word):
        lr.render_plan(["SW-A"], 10.0, run=lambda *a, **k: res)


def test_render_timeout_is_clear():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    with pytest.raises(lc.LabelError, match="超时"):
        lr.render_plan(["SW-A"], 10.0, run=run)


def test_render_spawn_oserror_is_clear():
    def run(cmd, **kw):
        raise OSError("no")
    with pytest.raises(lc.LabelError, match="渲染"):
        lr.render_plan(["SW-A"], 10.0, run=run)


def test_decode_rejects_bad_ink_size():
    with pytest.raises(lc.LabelError, match="看不懂"):
        lr.render_plan(["SW-A"], 10.0, run=lambda *a, **k: _done(
            {"px": 20, "lines": ["SW-A"], "inks": [[8, 2, lr._b64(b"\x01")]]}))


def test_no_font_found_is_clear(tmp_path):
    with pytest.raises(lc.LabelError, match="字体"):
        lr.open_first([(str(tmp_path / "nope.ttc"), 0)], loader=lambda p, i: (_ for _ in ()).throw(OSError(p)))


def _fake_face(index_of):
    loaded, face, slot = [], lr._Face(), lr._GlyphSlot()
    face.glyph = ctypes.pointer(slot)
    ft = SimpleNamespace(FT_Get_Char_Index=lambda f, code: index_of.get(code, 0),
                         FT_Load_Glyph=lambda f, idx, flags: loaded.append(idx) or 0)
    return SimpleNamespace(ft=ft, face=ctypes.pointer(face)), loaded


def test_blank_missing_from_font_borrows_space_not_notdef():
    fake, loaded = _fake_face({0x20: 3})
    lr.FreeType._glyph(fake, "\u3000")
    assert loaded == [3]


def test_ink_char_missing_from_font_still_refused():
    fake, loaded = _fake_face({0x20: 3})
    with pytest.raises(lc.LabelError, match="没有"):
        lr.FreeType._glyph(fake, "\u4e2d")
    assert loaded == []


def test_noto_candidates_prefer_simplified_chinese_face():
    assert lr.FONTS[0][1] == 2 and "NotoSansCJK-Bold" in lr.FONTS[0][0]


@pytest.mark.skipif(not HAS_FT, reason="这台机器没有 libfreetype")
def test_real_freetype_renders_chinese_and_ascii():
    ft = lr.FreeType(UNIFONT, 0)
    try:
        ink = ft.render("中A", 32)
        assert ink.w > 0 and ink.h > 0 and len(ink.px) == ink.w * ink.h and any(ink.px)
        assert all(v in (0, 1) for v in ink.px)
        small, big = ft.render("中", 16), ft.render("中", 48)
        assert big.h > small.h
    finally:
        ft.close()


@pytest.mark.skipif(not HAS_FT, reason="这台机器没有 libfreetype")
def test_real_freetype_reports_missing_glyph():
    ft = lr.FreeType(UNIFONT, 0)
    try:
        with pytest.raises(lc.LabelError, match="没有"):
            ft.render("\U0010FFFD", 16)
    finally:
        ft.close()


@pytest.mark.skipif(not HAS_FT, reason="这台机器没有 libfreetype")
def test_worker_end_to_end_with_repo_font():
    p = lr.render_plan(["交换机 SW-A", "to SW-B"], 10.0, fonts=[(UNIFONT, 0)])
    assert p.px >= lc.MIN_PX and lc.problems(p) == []
    job = lc.tspl_job(p, 1)
    assert b"BITMAP" in job and "交换机".encode() not in job
