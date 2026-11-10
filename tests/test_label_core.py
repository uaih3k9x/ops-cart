"""车机打标签的纯逻辑 (#104): 版面、压折痕、位图字节、180° 旋转、注入、校准读写. 不碰打印机, 不碰 Tk."""

import json

import pytest

import cart_label as cl
import label_core as lc
import tspl_preview as tp

FOLD_DEFAULT = lc.dots(cl.CY + cl.Y0)


def solid(text, px):
    """假渲染: 每字 px 宽 px 高的实心块."""
    return lc.Ink(len(text) * px, px, b"\x01" * (len(text) * px * px))


def plan_of(lines, fold_mm=cl.CY + cl.Y0, render=solid):
    top, h = lc.face_box(lc.dots(fold_mm))
    return lc.build_plan(lc.fit_lines(lc.clean_lines(lines), render, lc.CANVAS_W, h), fold_mm)


# ---------------------------------------------------------------- 输入

def test_clean_lines_strips_and_drops_empty():
    assert lc.clean_lines(["  SW-A ", ""]) == ["SW-A"]
    assert lc.clean_lines(["SW-A", "to SW-B"]) == ["SW-A", "to SW-B"]


@pytest.mark.parametrize("raw", [[], ["", "  "]])
def test_clean_lines_empty_is_clear_error(raw):
    with pytest.raises(lc.LabelError, match="输入"):
        lc.clean_lines(raw)


def test_clean_lines_too_many_or_too_long():
    with pytest.raises(lc.LabelError, match="两行"):
        lc.clean_lines(["a", "b", "c"])
    with pytest.raises(lc.LabelError, match="太长"):
        lc.clean_lines(["x" * (lc.MAX_LINE_CHARS + 1)])


@pytest.mark.parametrize("bad", ["a\r\nPRINT 99", "a\x1bb", "a\x00", "a‮b"])
def test_clean_lines_rejects_control_chars(bad):
    with pytest.raises(lc.LabelError, match="控制字符"):
        lc.clean_lines([bad])


# ---------------------------------------------------------------- 换行和字号

def test_fit_picks_largest_size_that_fits():
    f = lc.fit_lines(["AB"], solid, lc.CANVAS_W, 64)
    assert f.px == 64 and f.lines == ["AB"]
    f = lc.fit_lines(["SW-A", "SW-B"], solid, lc.CANVAS_W, 64)
    assert f.px == (64 - lc.LINE_GAP) // 2 // 2 * 2 and len(f.inks) == 2


def test_single_long_line_wraps_at_space_near_middle():
    f = lc.fit_lines(["SWITCH-A to SWITCH-B"], solid, lc.CANVAS_W, 64)
    assert f.lines == ["SWITCH-A to", "SWITCH-B"]


def test_single_line_without_space_splits_in_the_middle():
    assert lc.split_line("交换机到防火墙") == ["交换机", "到防火墙"]
    assert lc.split_line("A") is None


def test_two_lines_are_not_rewrapped_and_too_long_is_clear():
    with pytest.raises(lc.LabelError, match="放不下"):
        lc.fit_lines(["x" * 30, "y"], solid, lc.CANVAS_W, 64)


def test_nothing_printable_is_clear_error():
    with pytest.raises(lc.LabelError, match="没有可打印"):
        lc.fit_lines(["  x"], lambda s, px: lc.Ink(0, 0, b""), lc.CANVAS_W, 64)


# ---------------------------------------------------------------- 版面 (用校准后的折痕)

def test_guides_default_matches_cart_label_guides():
    assert lc.guides(cl.CY + cl.Y0) == tp.cable_guides()


def test_face_box_keeps_clear_of_fold_and_flag_top():
    top, h = lc.face_box(FOLD_DEFAULT)
    assert top == lc.dots(cl.Y0) + lc.MARGIN and top + h == FOLD_DEFAULT - lc.FOLD_GAP


def test_face_box_never_above_paper_or_below_paper():
    top, _ = lc.face_box(lc.dots(6.0))
    assert top == lc.MARGIN
    fold = lc.dots(20.0)
    top, h = lc.face_box(fold)
    assert 2 * fold - top == lc.PAGE_H - lc.MARGIN and h > 0


def test_face_box_too_small_refuses():
    with pytest.raises(lc.LabelError, match="校准"):
        lc.face_box(lc.dots(2.0))


def test_plan_follows_calibrated_fold():
    a, b = plan_of(["SW-A"], 10.0), plan_of(["SW-A"], 13.5)
    assert b.fold - a.fold == lc.dots(3.5)
    assert b.items[0].y - a.items[0].y == lc.dots(3.5)
    assert b.items[1].y - a.items[1].y == lc.dots(3.5)


def test_plan_stays_inside_flag_and_paper():
    p = plan_of(["SW-A", "to SW-B"])
    x0, _, x1, _ = lc.guides(p.fold_mm)["flag"]
    for it in p.items:
        assert x0 <= it.x and it.x + it.ink.w <= x1 and 0 <= it.y and it.y + it.ink.h <= lc.PAGE_H
    assert lc.problems(p) == []


def test_canvas_is_byte_aligned():
    p = plan_of(["SW-A"])
    assert p.items[0].ink.w % 8 == 0 == p.items[1].ink.w % 8


def _ink_points(placed):
    return {(placed.x + i % placed.ink.w, placed.y + i // placed.ink.w)
            for i, v in enumerate(placed.ink.px) if v}


def test_lower_half_is_upper_rotated_180_about_flag_centre():
    def corner(text, px):                     # 不对称: 只点亮左上一格加右边一条
        w, h = px, px
        out = bytearray(w * h)
        out[0] = 1
        for y in range(h):
            out[y * w + w - 1] = 1
        return lc.Ink(w, h, bytes(out))
    p = plan_of(["A"], render=corner)
    cx = round(cl.CX * lc.D)
    mapped = {(2 * cx - 1 - x, 2 * p.fold - 1 - y) for x, y in _ink_points(p.items[0])}
    assert mapped == _ink_points(p.items[1]) and mapped


def test_rotate180_pixels():
    assert lc.rotate180(lc.Ink(2, 2, b"\x01\x00\x00\x00")) == lc.Ink(2, 2, b"\x00\x00\x00\x01")


# ---------------------------------------------------------------- 压折痕 (兜底: 真实版面自动避开, 下面多数用例是手搓的版面)

@pytest.mark.parametrize("fold_mm", [lc.FOLD_DEFAULT_MM, 14.0])
def test_real_layout_full_of_ink_stays_off_fold(fold_mm):
    _, h = lc.face_box(lc.dots(fold_mm))
    ink = lc.Ink(lc.CANVAS_W, h, b"\x01" * (lc.CANVAS_W * h))
    assert lc.FOLD_WARNING not in lc.problems(lc.build_plan(lc.Fit(20, ["x"], [ink], h), fold_mm))


def _plan_with(ink, x, y, fold_mm=10.0):
    return lc.Plan(items=[lc.Placed(x, y, ink)], fold_mm=fold_mm, fold=lc.dots(fold_mm), px=20, lines=["x"], )


def test_ink_on_fold_warns():
    ink = lc.Ink(8, 4, b"\x01" * 32)
    fold = lc.dots(10.0)
    assert any("折痕" in m for m in lc.problems(_plan_with(ink, 480, fold - 2)))


def test_blank_canvas_over_fold_is_fine():
    ink = lc.Ink(8, 4, b"\x00" * 32)
    assert lc.problems(_plan_with(ink, 480, lc.dots(10.0) - 2)) == []


def test_ink_far_from_fold_is_fine():
    ink = lc.Ink(8, 4, b"\x01" * 32)
    assert lc.problems(_plan_with(ink, 480, 20)) == []


def test_check_uses_calibrated_fold_not_default():
    ink = lc.Ink(8, 4, b"\x01" * 32)
    y = lc.dots(14.0) - 2                     # 压在 14mm 上, 默认折痕 10mm 离得远
    assert lc.problems(_plan_with(ink, 480, y, fold_mm=10.0)) == []
    assert lc.problems(_plan_with(ink, 480, y, fold_mm=14.0))


def test_ink_outside_flag_is_reported():
    ink = lc.Ink(8, 4, b"\x01" * 32)
    assert any("旗子" in m for m in lc.problems(_plan_with(ink, 8, 20)))


def test_ink_off_paper_is_reported():
    ink = lc.Ink(8, 4, b"\x01" * 32)
    assert any("纸" in m for m in lc.problems(_plan_with(ink, 480, lc.PAGE_H - 2)))


# ---------------------------------------------------------------- 位图字节 / TSPL

def test_bitmap_bytes_zero_means_print():
    ink = lc.Ink(8, 2, bytes([1, 0, 0, 0, 0, 0, 0, 1,
                              0, 0, 0, 0, 0, 0, 0, 0]))
    assert lc.bitmap_bytes(ink) == bytes([0b01111110, 0xFF])


def test_bitmap_bytes_requires_byte_alignment():
    with pytest.raises(ValueError):
        lc.bitmap_bytes(lc.Ink(7, 1, b"\x00" * 7))


def test_job_structure_mode_0_and_head_from_cart_label():
    p = plan_of(["SW-A"])
    job = lc.tspl_job(p, 3)
    assert job.startswith(cl.to_tspl(cl.HEAD).encode() + b"CLS\r\n")
    assert job.endswith(b"PRINT 3,1\r\n")
    assert job.count(b"BITMAP ") == 2
    for it in p.items:
        assert f"BITMAP {it.x},{it.y},{it.ink.w // 8},{it.ink.h},0,".encode() in job


def test_job_round_trips_through_tspl_preview():
    p = plan_of(["SW-A", "to SW-B"])
    parsed = tp.parse(lc.tspl_job(p, 2))
    assert len(parsed) == 1 and parsed[0]["copies"] == 2
    bm = [it for it in parsed[0]["items"] if it["kind"] == "bitmap"]
    assert len(bm) == 2 and all(len(it["data"]) == it["wb"] * it["h"] for it in bm)
    assert tp.outside(parsed[0], (lc.guides(p.fold_mm)["flag"][0], 0, lc.guides(p.fold_mm)["flag"][2], lc.PAGE_H)) == []


@pytest.mark.parametrize("text", ['";PRINT 99;"', 'A",0,1,1,"B', "PRINT 99", 'TEXT 1,1,"4",0,1,1,"X"'])
def test_user_text_never_reaches_tspl(text):
    job = lc.tspl_job(plan_of([text]), 1)
    assert text.encode() not in job
    assert b"TEXT " not in job and job.count(b"PRINT ") == 1 and job.count(b"CLS") == 1


@pytest.mark.parametrize("n", [0, -1, lc.MAX_COPIES + 1, "2", True, 1.5])
def test_job_rejects_bad_copies(n):
    with pytest.raises(lc.LabelError):
        lc.tspl_job(plan_of(["A"]), n)


def test_feed_job_is_one_blank_label_with_same_head():
    assert lc.feed_job() == cl.to_tspl(cl.HEAD + ["CLS", "PRINT 1,1"]).encode()


def test_ruler_job_is_cart_label_ruler():
    assert lc.ruler_job() == cl.to_tspl(cl.ruler_labels(1)).encode()


# ---------------------------------------------------------------- 校准读写

def test_fold_default_is_cart_label_behavior():
    assert lc.fold_mm(lc.default_conf()) == cl.CY + cl.Y0


def test_save_then_load_per_paper(tmp_path):
    path = tmp_path / "sub" / "label.json"
    lc.save_conf(lc.set_fold(lc.default_conf(), "cable", 11.5), str(path))
    conf = lc.load_conf(str(path))
    assert lc.fold_mm(conf, "cable") == 11.5
    assert json.loads(path.read_text(encoding="utf-8"))["papers"]["cable"]["fold_mm"] == 11.5


def test_save_uses_atomicfile(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(lc.atomicfile, "write_json", lambda p, o, **kw: seen.update(path=p, obj=o, kw=kw))
    lc.save_conf(lc.default_conf(), str(tmp_path / "x.json"))
    assert seen["path"].endswith("x.json") and seen["kw"].get("mode") == 0o644


def test_load_missing_is_default(tmp_path):
    assert lc.load_conf(str(tmp_path / "nope.json")) == lc.default_conf()


@pytest.mark.parametrize("body", ["{broken", "[]", '{"papers": 3}', '{"papers": {"cable": {"fold_mm": "x"}}}',
                                  '{"papers": {"cable": {"fold_mm": 99}}}', '{"papers": {"cable": {"fold_mm": true}}}'])
def test_load_bad_file_falls_back_to_default_fold(tmp_path, body):
    path = tmp_path / "c.json"
    path.write_text(body, encoding="utf-8")
    assert lc.fold_mm(lc.load_conf(str(path))) == cl.CY + cl.Y0


def test_unknown_paper_refused():
    with pytest.raises(lc.LabelError):
        lc.set_fold(lc.default_conf(), "asset", 10.0)


@pytest.mark.parametrize("text, want", [("11.5", 11.5), (" 10 ", 10.0), ("9,5", 9.5)])
def test_parse_fold_ok(text, want):
    assert lc.parse_fold(text) == want


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "4.9", "20.1", "-3"])
def test_parse_fold_refuses(text):
    with pytest.raises(lc.LabelError, match="读数"):
        lc.parse_fold(text)


def test_remember_to_keeps_recent_unique_and_capped():
    conf = lc.default_conf()
    for name in ["to A", "SW-1", "to B", "to A"]:
        conf = lc.remember_to(conf, [name])
    assert conf["recent_to"] == ["to A", "to B"]
    for i in range(lc.RECENT_MAX + 3):
        conf = lc.remember_to(conf, [f"to X{i}"])
    assert len(conf["recent_to"]) == lc.RECENT_MAX and conf["recent_to"][0] == f"to X{lc.RECENT_MAX + 2}"


def test_load_conf_rechecks_recent_to(tmp_path):
    p = tmp_path / "label.json"
    bad = ["x" * (lc.MAX_LINE_CHARS + 1), "to A\r\nPRINT 9", "to OK"]
    p.write_text(json.dumps({"papers": {}, "recent_to": bad}), encoding="utf-8")
    assert lc.load_conf(str(p))["recent_to"] == ["to OK"]


def test_with_to_prefix():
    assert lc.with_to("SW-B") == "to SW-B"
    assert lc.with_to("to SW-B") == "to SW-B"
    assert lc.with_to("") == "to "


# ---------------------------------------------------------------- 状态 / 预览

@pytest.mark.parametrize("st, ok, word", [
    ({"ok": True, "flags": []}, True, "就绪"),
    ({"ok": True, "flags": ["打印中"]}, True, "打印中"),
    ({"ok": False, "flags": ["缺纸", "缺碳带"]}, False, "缺纸"),
    ({"ok": False, "flags": ["开盖"]}, False, "打印头"),
])
def test_status_view(st, ok, word):
    text, good = lc.status_view(st)
    assert good is ok and word in text


def test_preview_pgm_shape_and_ink():
    p = plan_of(["A"])
    data, w, h = lc.preview_pgm(p)
    head = f"P5 {w} {h} 255\n".encode()
    assert data.startswith(head) and len(data) == len(head) + w * h
    assert w == lc.PREVIEW_W and h == lc.PAGE_H and data[len(head):].count(0) > 0
