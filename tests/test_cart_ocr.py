import shutil

import pytest

import cart_ocr as co

# tesseract --psm 6 tsv 输出节选(3 倍放大后的坐标)
TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t1920\t1440\t-1\t
4\t1\t1\t1\t1\t0\t69\t414\t1100\t60\t-1\t
5\t1\t1\t1\t1\t1\t69\t420\t180\t48\t96.5\tSelect
5\t1\t1\t1\t1\t2\t270\t420\t240\t48\t95.1\tLanguage
5\t1\t1\t1\t1\t3\t720\t414\t420\t54\t71.0\tStandard
5\t1\t1\t1\t1\t4\t1160\t414\t250\t54\t70.2\tEnglish>
5\t1\t1\t1\t2\t1\t30\t530\t20\t40\t40.0\t>
5\t1\t1\t1\t2\t2\t69\t530\t180\t48\t93.0\tBoot
5\t1\t1\t1\t2\t3\t270\t530\t270\t48\t92.0\tManager
5\t1\t1\t1\t3\t1\t69\t600\t50\t48\t10.0\t
"""


def test_parse_tsv_groups_words_into_lines():
    lines = co.parse_tsv(TSV)
    # "Select Language" 和取值 "Standard English>" 之间空隙大, 分成两栏
    assert [l["text"] for l in lines] == ["Select Language", "Standard English>", "> Boot Manager"]
    value = lines[1]
    assert (value["x"], value["y"], value["w"], value["h"]) == (720, 414, 1410 - 720, 468 - 414)
    assert 70 < value["conf"] < 71


def test_parse_tsv_scales_boxes_back():
    line = co.parse_tsv(TSV, scale=3)[2]
    assert (line["x"], line["y"]) == (10, 176)


def test_find_text_case_and_space_insensitive():
    lines = co.parse_tsv(TSV)
    assert co.find_text(lines, "boot   manager")["text"] == "> Boot Manager"
    assert co.find_text(lines, "Device Manager") is None


def test_find_text_tolerates_ocr_spaces():
    lines = [{"text": "Cont inue", "x": 0, "y": 0, "w": 1, "h": 1, "conf": 90}]
    assert co.find_text(lines, "Continue") is lines[0]


@pytest.mark.skipif(not shutil.which("tesseract") or not shutil.which("convert"),
                    reason="需要 tesseract 和 ImageMagick")
def test_ocr_image_end_to_end(tmp_path):
    import subprocess
    img = tmp_path / "t.png"
    r = subprocess.run(["convert", "-size", "640x120", "xc:white", "-fill", "black",
                        "-pointsize", "28", "-annotate", "+20+60", "Boot Manager", str(img)],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip("ImageMagick 画不了字(缺默认字体), 生成不了测试图")
    hit = co.find_text(co.ocr_image(str(img)), "Boot Manager")
    assert hit is not None and hit["x"] < 100


def test_parse_tsv_splits_columns_on_big_gap():
    tsv = TSV.splitlines()[0] + "\n" + "\n".join([
        "5\t1\t1\t1\t5\t1\t24\t528\t60\t45\t95\t>",
        "5\t1\t1\t1\t5\t2\t100\t528\t200\t45\t95\tDevice",
        "5\t1\t1\t1\t5\t3\t320\t528\t230\t45\t95\tManager",
        "5\t1\t1\t1\t5\t4\t1368\t528\t100\t45\t95\tthe",          # 右栏, 空隙远大于字高
        "5\t1\t1\t1\t5\t5\t1490\t528\t250\t45\t95\tlanguage",
    ])
    texts = [l["text"] for l in co.parse_tsv(tsv)]
    assert texts == ["> Device Manager", "the language"]


# ============================================================ C 线: 超时 / 退出码 / 自适应放大

import struct
import subprocess


def _png_header(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


def test_image_width_reads_png_header(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png_header(1024, 768))
    assert co.image_width(str(p)) == 1024
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8junk")
    assert co.image_width(str(tmp_path / "b.jpg")) is None


@pytest.mark.parametrize("width,scale", [(640, 4), (800, 4), (1024, 3), (1280, 2), (1920, 2),
                                         (3840, 1), (None, co.SCALE)])
def test_auto_scale(width, scale):
    assert co.auto_scale(width) == scale


def test_subprocesses_have_timeouts(monkeypatch, tmp_path):
    seen = []

    class R:
        stdout = b"level\n"
    monkeypatch.setattr(co.subprocess, "run", lambda cmd, **kw: seen.append(kw.get("timeout")) or R())
    co.ocr_image(str(tmp_path / "x.png"), scale=2)
    assert len(seen) == 2 and all(t and t > 0 for t in seen)


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    img = str(tmp_path / "x.png")
    monkeypatch.setattr(co, "ocr_image", lambda *a, **k: [])
    assert co.main([img, "--find", "Boot"]) == 1                 # 没找到 = 1

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, ["tesseract"], stderr=b"bad lang")
    monkeypatch.setattr(co, "ocr_image", boom)
    assert co.main([img]) == 2                                   # 出错 = 2
    for exc in (subprocess.TimeoutExpired(["convert"], 30), FileNotFoundError("convert")):
        monkeypatch.setattr(co, "ocr_image", lambda *a, e=exc, **k: (_ for _ in ()).throw(e))
        assert co.main([img]) == 2
    assert "OCR 失败" in capsys.readouterr().err
