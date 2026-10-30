"""tspl_preview: 不在打印机边上时看 TSPL 版面. 拿 cart_label 实机确认过的黄金输出当输入."""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import cart_label as cl
import tspl_preview as tp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "cart_label")


def _golden(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


# ---- 解析 ----

def test_parse_cable_golden():
    labels = tp.parse(_golden("cable.tspl"))
    assert len(labels) == 3
    lb = labels[0]
    assert lb["size"] == (832, 200)                       # SIZE 104 mm, 25 mm
    kinds = [it["kind"] for it in lb["items"]]
    assert kinds.count("dmatrix") == 2 and kinds.count("text") == 6
    t = lb["items"][2]
    assert (t["x"], t["y"], t["font"], t["rot"], t["s"]) == (520, 44, "2", 0, "E-00001")


def test_parse_quoted_comma():
    lb = tp.parse(b'SIZE 10 mm, 10 mm\r\nCLS\r\nTEXT 1,2,"1",0,1,1,"a,b"\r\nPRINT 1\r\n')[0]
    assert lb["items"][0]["s"] == "a,b"


def test_parse_bitmap_asset_golden():
    labels = tp.parse(cl.ASSET_HEADER.encode() + _golden("asset_move.tspl"))   # 黄金输出只有页, 头另拼
    assert len(labels) == 1
    bm = labels[0]["items"][0]
    assert bm["kind"] == "bitmap" and (bm["x"], bm["y"], bm["wb"], bm["h"]) == (0, 4, 58, 76)
    assert len(bm["data"]) == 58 * 76
    assert labels[0]["size"] == (480, 299)                # SIZE 60 mm, 37.4 mm


def test_parse_page_without_print_is_dropped():
    assert tp.parse(b"SIZE 10 mm, 10 mm\r\nCLS\r\nBAR 0,0,1,1\r\n") == []


# ---- 占位框 ----

def test_box_text_rot0_and_rot180():
    t0 = {"kind": "text", "x": 520, "y": 44, "font": "2", "rot": 0, "xm": 1, "ym": 1, "s": "E-00001"}
    assert tp.box(t0) == (520, 44, 604, 64)               # 7 字 x 12 点, 高 20
    t180 = dict(t0, x=644, y=180, rot=180)
    assert tp.box(t180) == (560, 160, 644, 180)           # 180°: 从锚点往左、往上长


def test_box_text_multiplier():
    t = {"kind": "text", "x": 0, "y": 0, "font": "1", "rot": 0, "xm": 2, "ym": 3, "s": "ab"}
    assert tp.box(t) == (0, 0, 32, 36)


# ---- 校验: 出界 / 对折对称 ----

def _flag_area():
    return (int(cl.FL * cl.D), 0, int(cl.FR * cl.D), 25 * cl.D)


def test_golden_cable_inside_flag_and_folds_clean():
    cx, cy = cl.CX * cl.D, (cl.CY + cl.Y0) * cl.D
    for lb in tp.parse(_golden("cable.tspl")):
        assert tp.outside(lb, _flag_area()) == []
        assert tp.fold_mismatches(lb, cx, cy) == []


def test_outside_and_fold_catch_bad_layout():
    data = (b'SIZE 104 mm, 25 mm\r\nCLS\r\n'
            b'TEXT 700,40,"3",0,1,1,"TOO-LONG-FOR-FLAG"\r\n'      # 右边出界
            b'TEXT 520,44,"2",0,1,1,"ABC"\r\n'                    # 没有 180° 搭档
            b'PRINT 1\r\n')
    lb = tp.parse(data)[0]
    assert len(tp.outside(lb, _flag_area())) == 1
    cx, cy = cl.CX * cl.D, (cl.CY + cl.Y0) * cl.D
    assert len(tp.fold_mismatches(lb, cx, cy)) == 2


def test_check_ruler_only_horizontal():
    """刻度尺不对折、不加 Y0, 竖向超出对折设计区是故意的; 只查左右."""
    assert tp.check(tp.parse(_golden("ruler3.tspl")), tp.cable_guides()) == []
    assert tp.check(tp.parse(_golden("text.tspl")), tp.cable_guides()) == []
    assert tp.check(tp.parse(_golden("cable.tspl")), tp.cable_guides()) == []


# ---- 出图 ----

def test_svg_is_wellformed_and_has_text():
    lb = tp.parse(_golden("cable.tspl"))[0]
    svg = tp.to_svg(lb, guides=tp.cable_guides())
    root = ET.fromstring(svg)
    texts = [e.text for e in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "E-00001" in texts


def test_svg_bitmap_draws_ink():
    lb = tp.parse(_golden("asset_move.tspl"))[0]
    svg = tp.to_svg(lb)
    ET.fromstring(svg)
    assert "<path" in svg


def test_cli_writes_html(tmp_path):
    out = tmp_path / "p.html"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tspl_preview.py"),
                        os.path.join(FIX, "cable.tspl"), "-o", str(out), "--paper", "cable"],
                       capture_output=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    html = out.read_text(encoding="utf-8")
    assert html.count("<svg") >= 3 and "E-00042" in html


def test_cli_bad_layout_exit_1(tmp_path):
    bad = tmp_path / "bad.tspl"
    bad.write_bytes(b'SIZE 104 mm, 25 mm\r\nCLS\r\nTEXT 700,40,"3",0,1,1,"TOO-LONG-FOR-FLAG"\r\nPRINT 1\r\n')
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tspl_preview.py"), str(bad),
                        "-o", str(tmp_path / "b.html"), "--paper", "cable"], capture_output=True, cwd=ROOT)
    assert r.returncode == 1
    assert "出界" in r.stderr.decode()
