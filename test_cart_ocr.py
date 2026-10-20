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
    subprocess.run(["convert", "-size", "640x120", "xc:white", "-fill", "black",
                    "-pointsize", "28", "-annotate", "+20+60", "Boot Manager",
                    str(img)], check=True)
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
