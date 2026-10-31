"""cart_label: TSC 标签机 TSPL 生成器.

黄金输出 (tests/fixtures/cart_label/*.tspl) 是用 ops-cart 技能目录里实机确认过的原脚本
(tsc_label.py 10/30 版 / tsc_asset_label.py 10/28 版) 对同样输入生成的, 搬过来必须逐字节一致.
资产标签的 PGM 输入由下面的 _pgm() 现造, 生成黄金输出时用的是同一段代码.
例外: cable.tspl 是 10-30 改版 (对端大字 / 本端 "@ ") 后用本模块生成的, 待实机确认; 预览看 tspl_preview.py.
"""

import json
import os
import subprocess
import sys

import pytest

import cart_label as cl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "cart_label")
ABBREV = os.path.join(ROOT, "examples", "label_abbrev.json")

TEXT_ARGS = ["LAN1", "CART END", "UPLINK", "AT SW-A Gi0/1",
             "ABCDEFGHIJK", "AT ABCDEFGHIJ-KLM 0/22", "X", "y"]


def _golden(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def _rows():
    with open(os.path.join(FIX, "cable_rows.json")) as f:
        return json.load(f)


def _pgm(wd, h, rects, comment=False):
    px = bytearray(b"\xff" * (wd * h))
    for x0, y0, x1, y1, v in rects:
        for y in range(y0, y1):
            for x in range(x0, x1):
                px[y * wd + x] = v
    head = b"P5\n" + (b"# made by test\n" if comment else b"") + b"%d %d\n255\n" % (wd, h)
    return head + bytes(px)


ASSET_CASES = {
    # 三块, 末块离得远 -> 上移 2mm; 左 2mm 和右边界外的墨要裁掉
    "asset_move": _pgm(500, 80, [(0, 0, 10, 80, 0), (20, 5, 200, 15, 0), (30, 25, 300, 35, 100),
                                 (40, 60, 120, 70, 0), (482, 2, 499, 78, 0)], comment=True),
    # 末块离上一块太近 -> 不挪; 灰度 127/128 阈值边界
    "asset_close": _pgm(480, 40, [(16, 4, 100, 10, 127), (16, 14, 100, 20, 128), (20, 22, 90, 30, 0)]),
    # 比可用宽窄 (右边补白), 高于 299 行 (截掉)
    "asset_tall_narrow": _pgm(300, 320, [(16, 0, 299, 3, 0), (100, 290, 200, 320, 0)]),
}


# ---- 黄金输出: 线缆旗形标签 ----

def test_text_matches_golden():
    pairs = list(zip(TEXT_ARGS[::2], TEXT_ARGS[1:][::2]))
    assert cl.to_tspl(cl.text_labels(pairs)).encode() == _golden("text.tspl")


def test_cable_matches_golden():
    assert cl.to_tspl(cl.cable_labels(_rows())).encode() == _golden("cable.tspl")


def test_ruler_matches_golden():
    assert cl.to_tspl(cl.ruler_labels(3)).encode() == _golden("ruler3.tspl")
    assert cl.to_tspl(cl.ruler_labels()).encode() == _golden("ruler_default.tspl")


def _cli(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "cart_label.py")] + list(args),
                          capture_output=True, cwd=ROOT)


def test_cli_matches_golden():
    assert _cli("text", *TEXT_ARGS).stdout == _golden("text.tspl")
    assert _cli("cable", os.path.join(FIX, "cable_rows.json")).stdout == _golden("cable.tspl")
    assert _cli("ruler", "3").stdout == _golden("ruler3.tspl")
    assert _cli("ruler").stdout == _golden("ruler_default.tspl")


# ---- 黄金输出: 资产标签 (PGM -> TSPL 纯函数部分) ----

@pytest.mark.parametrize("name", sorted(ASSET_CASES))
def test_asset_page_matches_golden(name):
    w, h, px = cl.parse_pgm(ASSET_CASES[name])
    assert cl.asset_page(w, h, cl.asset_layout(w, h, px)) == _golden(name + ".tspl")


def test_asset_job_is_header_plus_pages():
    job = cl.asset_job(ASSET_CASES[n] for n in ["asset_move", "asset_close"])
    assert job == cl.ASSET_HEADER.encode() + _golden("asset_move.tspl") + _golden("asset_close.tspl")


def test_asset_layout_warns_when_last_block_too_close():
    msgs = []
    w, h, px = cl.parse_pgm(ASSET_CASES["asset_close"])
    cl.asset_layout(w, h, px, warn=msgs.append)
    assert len(msgs) == 1 and "22-29" in msgs[0]
    msgs.clear()
    w, h, px = cl.parse_pgm(ASSET_CASES["asset_move"])
    cl.asset_layout(w, h, px, warn=msgs.append)
    assert msgs == []


def test_asset_blocks_ignore_ink_outside_crop():
    w, h, px = cl.parse_pgm(ASSET_CASES["asset_move"])
    assert cl.asset_blocks(w, h, px) == [(5, 14), (25, 34), (60, 69)]


def test_parse_pgm_rejects_non_p5():
    with pytest.raises(ValueError):
        cl.parse_pgm(b"P6\n2 2\n255\n" + b"\0" * 12)


@pytest.mark.parametrize("data", [b"P5 10", b"P5\n2 2", b"P5\n# only comment", b"P5\n2 2\n255"])
def test_parse_pgm_truncated_header_raises(data):
    """头不完整要报错, 不能卡死 (曾经死循环)."""
    with pytest.raises(ValueError):
        cl.parse_pgm(data)


def test_parse_pgm_short_pixels_raises():
    with pytest.raises(ValueError):
        cl.parse_pgm(b"P5\n2 2\n255\n" + b"\0" * 3)


# ---- 行为 ----

def test_short_id_pads_to_five():
    assert cl.short_id(1) == "E-00001"
    assert cl.short_id(42) == "E-00042"
    assert cl.short_id(123456) == "E-123456"


def test_ascii_only_rejects_chinese_and_control():
    assert cl.ascii_only("Gi0/1 ~") == "Gi0/1 ~"
    for bad in ["交换机", "A\tB", "café", "x\x7f"]:
        with pytest.raises(ValueError):
            cl.ascii_only(bad)
    with pytest.raises(ValueError):
        cl.text_labels([("LAN1", "上联")])


def test_text_wider_than_flag_raises():
    with pytest.raises(ValueError, match="超出旗子宽度"):
        cl.text_pair("A" * 12, "4", 0, 0.8, center=True)
    with pytest.raises(ValueError):
        cl.text_pair("A", "1", cl.FL - 1, 4.0)            # 左边出界


def test_cable_big_there_max_12_here_max_23():
    """大字 (font3) 写对端最多 12 字; 本端加 '@ ' 前缀, 最多 23 字."""
    ok = {"shortId": 1, "here": "H" * 23, "there": "T" * 12}
    cl.cable_labels([ok])
    with pytest.raises(ValueError):
        cl.cable_labels([dict(ok, here="H" * 24)])
    with pytest.raises(ValueError):
        cl.cable_labels([dict(ok, there="T" * 13)])


def test_cable_layout_big_there_small_here():
    """版面: 短号 font2, 对端 font3 大字不加前缀, 本端 font1 带 '@ '; 各自有 180° 搭档."""
    lines = cl.cable_labels([{"shortId": 7, "here": "SW-A Te0/0/1", "there": "SW-B Te0/2"}])
    texts = [ln for ln in lines if ln.startswith("TEXT ")]
    assert any(',"3",0,1,1,"SW-B Te0/2"' in t for t in texts)
    assert any(',"3",180,1,1,"SW-B Te0/2"' in t for t in texts)
    assert any(',"1",0,1,1,"@ SW-A Te0/0/1"' in t for t in texts)
    assert any(',"2",0,1,1,"E-00007"' in t for t in texts)
    assert not any('"> ' in t for t in texts)


def test_cable_layout_passes_preview_check():
    """拿预览器查: 不出旗子设计区, 对折对称, 三行不互相压."""
    import tspl_preview as tp
    rows = [{"shortId": 123456, "here": "H" * 23, "there": "W" * 12}]
    labels = tp.parse(cl.to_tspl(cl.cable_labels(rows)).encode())
    assert tp.check(labels, tp.cable_guides()) == []
    upper = sorted((tp.box(it) for it in labels[0]["items"] if it["kind"] == "text" and it["rot"] == 0),
                   key=lambda b: b[1])
    for a, b in zip(upper, upper[1:]):
        assert a[3] <= b[1], (a, b)                        # 上一行底 <= 下一行顶
    fold = (cl.CY + cl.Y0) * cl.D
    assert upper[-1][3] <= fold - 4                        # 最下一行离折痕至少 0.5mm


def _xy(cmd):
    head, _ = cmd.split(" ", 1)
    parts = cmd.split(" ", 1)[1].split(",")
    return head, int(parts[0]), int(parts[1])


def test_lower_half_is_180_rotation_about_fold_center():
    cx2, cy2 = 2 * cl.CX * cl.D, 2 * (cl.CY + cl.Y0) * cl.D
    for s, font, x, y, center in [("LAN1", "4", 0, 0.8, True), ("AT SW-A", "2", 0, 5.6, True),
                                  ("E-00001", "2", cl.FL + 10, 0.5, False)]:
        up, down = cl.text_pair(s, font, x, y, center)
        _, ux, uy = _xy(up)
        _, dx, dy = _xy(down)
        assert (ux + dx, uy + dy) == (int(cx2), int(cy2))
        assert ',0,1,1,' in up and ',180,1,1,' in down
    size = cl.DM_X * cl.DM_N
    up, down = cl.dmatrix_pair("E-00001", cl.FL + 1.5, 1.0)
    _, ux, uy = _xy(up)
    _, dx, dy = _xy(down)
    assert (ux + dx + size, uy + dy + size) == (int(cx2), int(cy2))   # 码不转, 只放对称位置


def test_text_shifted_by_y0_but_ruler_not():
    up, _ = cl.text_pair("A", "1", cl.FL + 10, 4.0)
    assert _xy(up)[2] == int((4.0 + cl.Y0) * cl.D)
    ruler = cl.ruler_labels(1)
    assert "BAR 464,0,2,176" in ruler                     # 竖线从原始 y=0 开始
    assert 'TEXT 520,0,"2",0,1,1,"0"' in ruler


def test_calibration_constants():
    """实机校准值, 改之前先看 CART-LOG."""
    assert (cl.FL, cl.FR, cl.CY, cl.Y0, cl.X0) == (52.0, 87.5, 9.0, 5.0, -3.0)   # 10-31 实机: 整体左挪 3mm
    assert "SHIFT 18,39" in cl.HEAD and "SIZE 104 mm, 25 mm" in cl.HEAD
    assert cl.ASSET_HEADER == "SIZE 60 mm, 37.4 mm\r\nGAP 2 mm, 0 mm\r\nDENSITY 15\r\nDIRECTION 1\r\nSHIFT 34,-76\r\n"


def test_cli_errors_exit_nonzero_with_chinese_message():
    r = _cli("text", "交换机", "x")
    assert r.returncode != 0 and r.stdout == b""
    assert "ASCII" in r.stderr.decode()
    r = _cli("text", "only-one")
    assert r.returncode != 0 and "成对" in r.stderr.decode()
    r = _cli("bogus")
    assert r.returncode != 0


# ---- 缩写对照表 ----

def _table():
    return cl.load_abbrev(ABBREV)


def test_limits_match_example_table():
    with open(ABBREV, encoding="utf-8") as f:
        assert json.load(f)["_limits"] == cl.LIMITS


def test_limits_match_flag_geometry():
    """限长和旗子宽度一致: 刚好放得下, 多一个字就超."""
    for kind, font, x, center in [("tag_big", "4", 0, True), ("tag_small", "2", 0, True),
                                  ("cable_here", "1", cl.FL + 10, False), ("cable_there", "3", cl.FL + 10, False)]:
        n = cl.LIMITS[kind + "_max"]
        pre = "@ " if kind == "cable_here" else ""
        cl.text_pair(pre + "W" * n, font, x, 1.0, center)
        with pytest.raises(ValueError):
            cl.text_pair(pre + "W" * (n + 1), font, x, 1.0, center)


def test_check_text_length_and_ascii():
    assert cl.check_text("CORE-SW1", "tag_big") == "CORE-SW1"
    with pytest.raises(ValueError, match="超长"):
        cl.check_text("A" * 12, "tag_big")
    with pytest.raises(ValueError, match="ASCII"):
        cl.check_text("核心", "tag_small")
    with pytest.raises(ValueError):
        cl.check_text("x", "no_such_kind")


def test_abbreviate_longest_key_first():
    t = _table()
    assert cl.abbreviate("华为 核心交换机", t) == "HW CORESW"
    assert cl.abbreviate("上联口", t) == "UP"                 # 不是 "U" + "联口"
    assert cl.abbreviate("主配线间", t) == "MDF"
    assert cl.abbreviate("对端 锐捷交换机 管理口", t) == "RGSW MGMT"         # 前缀由版面加, 表里只删词
    assert cl.abbreviate("  Huawei   TenGigabitEthernet1/0/1 ", t) == "HW Te1/0/1"


def test_abbreviate_checked_raises_when_still_too_long():
    t = _table()
    assert cl.abbreviate_checked("华为交换机", "tag_big", t) == "HWSW"
    with pytest.raises(ValueError, match="超长"):
        cl.abbreviate_checked("华为 核心交换机 TenGigabitEthernet1/0/1", "tag_big", t)
    with pytest.raises(ValueError, match="ASCII"):
        cl.abbreviate_checked("未知设备", "cable_here", t)


def test_table_values_are_valid_labels():
    """对照表自身不许写错: 值全是 ASCII 且不超过最短限长, 同一个键在各类里不许映射成不同值."""
    seen = {}
    raw = json.load(open(ABBREV, encoding="utf-8"))
    for cat, m in raw.items():
        if cat.startswith("_"):
            continue
        for k, v in m.items():
            cl.check_text(v, "tag_big")
            assert seen.setdefault(k, v) == v, (cat, k)
    assert cl.validate_abbrev(raw) == []


def test_validate_abbrev_reports_bad_entries():
    bad = {"_limits": cl.LIMITS, "vendor": {"华为": "华为", "X": "TOOLONGVALUE"}, "port": {"X": "Y"}}
    probs = cl.validate_abbrev(bad)
    assert len(probs) == 3
