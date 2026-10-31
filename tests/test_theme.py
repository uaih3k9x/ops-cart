"""界面主题 (#27): 颜色和字体只在 theme.py 定义, 业务代码只引用名字."""

import os
import re
import subprocess

import pytest

import theme as th

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX = re.compile(r"""["']#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?["']""")
FONT_FAMILY = re.compile(r"""["']Noto Sans(?: Mono)? CJK SC["']""")
# tspl_preview 产出的是给浏览器看的 HTML/SVG 预览, 有自己的浅色/深色 CSS, 不归车机主题管
EXEMPT = {"theme.py", "tspl_preview.py"}


def _tracked_py():
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        out = [f for f in os.listdir(ROOT) if f.endswith(".py")]
    return sorted(f for f in out if not f.startswith("tests/") and os.path.basename(f) not in EXEMPT)


def _hits(pattern):
    hits = []
    for rel in _tracked_py():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            for no, line in enumerate(f, 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{no}: {line.strip()}")
    return hits


def _colors():
    return {k: v for k, v in vars(th).items() if k.isupper() and isinstance(v, str) and v.startswith("#")}


def test_contrast_ratio_matches_wcag():
    assert th.contrast("#ffffff", "#000000") == pytest.approx(21.0)
    assert th.contrast("#777777", "#777777") == pytest.approx(1.0)
    assert th.contrast("#000000", "#ffffff") == th.contrast("#ffffff", "#000000")


def test_every_color_is_six_digit_lowercase_hex():
    colors = _colors()
    assert colors, "theme 里一个颜色都没有"
    bad = {k: v for k, v in colors.items() if not re.fullmatch(r"#[0-9a-f]{6}", v)}
    assert not bad


def test_no_two_names_for_almost_the_same_color():
    """每个通道都差不到 7 的两个值肉眼分不出, 算同一个颜色起了两个名; 要同色就直接引用同一个名字."""
    items = sorted(_colors().items())
    dup = []
    for i, (a, x) in enumerate(items):
        for b, y in items[i + 1:]:
            if x != y and max(abs(int(x[j:j + 2], 16) - int(y[j:j + 2], 16)) for j in (1, 3, 5)) <= 6:
                dup.append((a, x, b, y))
    assert not dup


def test_health_colors_cover_every_state():
    for table in (th.STATE, th.LAMP, th.BOOT_STATE):
        assert set(table) >= {"OK", "WARN", "FAIL"}
    assert None in th.LAMP


@pytest.mark.parametrize("fg,bg", [
    ("FG", "BG"), ("FG", "PANEL"), ("FG", "BOX"), ("TEXT", "BG"), ("TEXT", "PANEL"),
    ("DIM", "BG"), ("DIM", "PANEL"),
    ("OK", "BG"), ("WARN", "BG"), ("BAD", "BG"), ("NOTICE", "BG"), ("MUTED", "BG"),
    ("FG", "PRIMARY"), ("FG", "BTN2"), ("DANGER_FG", "DANGER_BG"), ("WHITE", "ALERT_BG"),
    ("WHITE", "SERVE_BG"), ("WHITE", "SERVE_EXPIRED_BG"), ("GO_FG", "GO_BG"),
    ("BOOT_FG", "BOOT_BG"), ("BOOT_SUB", "BOOT_BG"),
    ("BOOT_OK", "BOOT_BG"), ("BOOT_WARN", "BOOT_BG"), ("BOOT_FAIL", "BOOT_BG"),
    ("LAMP_FG", "LAMP_BODY"), ("LAMP_LIT_FG", "LAMP_OK"), ("LAMP_LIT_FG", "LAMP_FAIL"),
])
def test_text_pairs_readable(fg, bg):
    """正文配色 >= 4.5:1 (WCAG AA). 推车常在亮堂的教室里用, 只会往高里调."""
    assert th.contrast(getattr(th, fg), getattr(th, bg)) >= 4.5


def test_font_helper():
    assert th.font(13) == ("Noto Sans CJK SC", 13)
    assert th.font(22, mono=True) == ("Noto Sans Mono CJK SC", 22)
    assert th.font(40, bold=True) == ("Noto Sans CJK SC", 40, "bold")
    assert th.FONT_BIG + ("bold",) == th.font(15, bold=True)


def test_mono_font_tiers_are_monospace():
    """状态板、串口终端、输入框靠等宽对齐; 第 1 步外观不变, 字号也照原样."""
    assert th.FONT_MONO == th.font(13, mono=True)
    assert th.FONT_MONO_BIG == th.font(14, mono=True)


def test_ui_code_has_no_color_literals():
    assert not _hits(HEX), "颜色写进 theme.py, 业务代码用 th.名字"


def test_ui_code_has_no_font_family_literals():
    assert not _hits(FONT_FAMILY), "字体用 th.FONT* 或 th.font(), 别再写字体名"
