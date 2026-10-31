"""界面主题 (#27): 颜色和字体只在 theme.py 定义, 业务代码只引用名字."""

import os
import re
import subprocess

import pytest

import theme as th

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX = re.compile(r"""["']#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?["']""")
RELIEF = re.compile(r"""\brelief\s*=\s*["']""")
FONT_FAMILY = re.compile(r"""["'](?:Noto Sans(?: Mono)? CJK SC|Unifont)["']""")
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
    ("FG", "BG"), ("FG", "TOP"), ("FG", "PANEL"), ("FG", "BOX"), ("FG", "ACTIVE"),
    ("TEXT", "BG"), ("TEXT", "PANEL"), ("TEXT", "BOX"),
    ("DIM", "BG"), ("DIM", "TOP"), ("DIM", "PANEL"), ("FAINT", "BG"), ("FAINT", "TOP"),
    ("OK", "BG"), ("WARN", "BG"), ("BAD", "BG"), ("NOTICE", "BG"), ("MUTED", "BG"), ("MARK", "BG"),
    ("OK", "TOP"), ("WARN", "TOP"), ("BAD", "TOP"), ("NOTICE", "TOP"),
    ("OK", "PANEL"), ("WARN", "PANEL"), ("BAD", "PANEL"), ("NOTICE", "PANEL"), ("MUTED", "PANEL"),
    ("FG", "PRIMARY"), ("FG", "BTN2"), ("DANGER_FG", "BTN2"), ("WHITE", "TITLE"), ("DANGER_FG", "DANGER_BG"),
    ("FG", "WARN_BG"), ("FG", "AMBER_BG"), ("WHITE", "DANGER_EDGE"), ("WHITE", "ALERT_BG"),
    ("WHITE", "SERVE_BG"), ("WHITE", "SERVE_EXPIRED_BG"), ("GO_FG", "GO_BG"),
    ("BOOT_FG", "BOOT_BG"), ("BOOT_SUB", "BOOT_BG"), ("BOOT_DIM", "BOOT_BG"), ("BOOT_STEP", "BOOT_BG"),
    ("BOOT_OK", "BOOT_BG"), ("BOOT_WARN", "BOOT_BG"), ("BOOT_FAIL", "BOOT_BG"),
    ("LAMP_FG", "LAMP_BODY"), ("LAMP_EMPTY_FG", "LAMP_EMPTY"),
    ("LAMP_LIT_FG", "LAMP_OK"), ("LAMP_LIT_FG", "LAMP_WARN"), ("LAMP_LIT_FG", "LAMP_FAIL"), ("LAMP_LIT_FG", "AGENT"),
    ("LAMP_FG", "DASH_BG"), ("DASH_DIM", "DASH_BG"),
    ("LAMP_OK", "DASH_BG"), ("LAMP_WARN", "DASH_BG"), ("LAMP_FAIL", "DASH_BG"),
])
def test_text_pairs_readable(fg, bg):
    """正文配色 >= 4.5:1 (WCAG AA). 推车常在亮堂的教室里用, 只会往高里调.
    亮着的灯 (绿 / 黄 / 红 / 紫) 上一律写深字; 副屏的红黄绿直接当字色用."""
    assert th.contrast(getattr(th, fg), getattr(th, bg)) >= 4.5


@pytest.fixture
def family():
    """测完把挑中的字体还原, 别影响别的测试."""
    old = th.FAMILY
    yield
    th.FAMILY = old


def test_pick_family_prefers_unifont_and_falls_back_to_noto(family):
    assert th.select_family(["DejaVu Sans", "Unifont", "Noto Sans CJK SC"]) == "Unifont" == th.FAMILY
    assert th.select_family(["DejaVu Sans", "Noto Sans CJK SC"]) == "Noto Sans CJK SC" == th.FAMILY
    assert th.select_family([]) == "Noto Sans CJK SC"           # 一个字体都列不出来也不抛


def test_unifont_is_whole_multiples_of_16px(family):
    """点阵字只按整数倍放大; 正文各档一律 16px, 大字取最近的 16 的倍数. 粗体照给 (Xft 加粗, 实测清楚)."""
    th.select_family(["Unifont"])
    assert th.font(13) == ("Unifont", -16)
    assert th.font(12) == th.font(15) == th.font(14, mono=True) == ("Unifont", -16)
    assert th.font(22, mono=True) == ("Unifont", -32)
    assert th.font(40, bold=True) == ("Unifont", -48, "bold")
    assert th.font(64) == ("Unifont", -80)
    assert th.font(-100, bold=True) == ("Unifont", -96, "bold")      # 负数 = 像素
    assert th.font(-5) == ("Unifont", -16)                            # 再小也是 16
    assert {th.font_spec(n) for n in th.FONT_TIERS} == {("Unifont", -16)}


def test_noto_fallback_keeps_old_sizes(family):
    """取不到 Unifont: 回到 1.10.00 的 Noto 和字号."""
    th.select_family([])
    assert th.font(13) == ("Noto Sans CJK SC", 13)
    assert th.font(22, mono=True) == ("Noto Sans Mono CJK SC", 22)
    assert th.font(40, bold=True) == ("Noto Sans CJK SC", 40, "bold")
    assert th.font(-60) == ("Noto Sans CJK SC", -60)
    assert th.font_spec(th.FONT_BIG) == th.font(15)
    assert th.font_spec(th.FONT_MONO) == th.font(13, mono=True)
    assert th.font_spec(th.FONT_MONO_BIG) == th.font(14, mono=True)


def test_font_tiers_are_named_fonts():
    """各档是 Tk 命名字体: 模块导入时就能拿名字, setup() 时才按机器上的字体定下来 (以后换也只改一处)."""
    assert set(th.FONT_TIERS) == {th.FONT_SMALL, th.FONT, th.FONT_BIG, th.FONT_MONO, th.FONT_MONO_BIG}
    assert all(isinstance(n, str) and " " not in n for n in th.FONT_TIERS)


def test_pixel_px_snaps_to_16():
    assert [th.pixel_px(x) for x in (1, 16, 23, 24, 26, 38, 56, 72, 150)] == [16, 16, 16, 32, 32, 32, 64, 64, 144]


def test_tk_options_only_use_theme_values():
    """option 数据库 (messagebox / OptionMenu / 滚动条 / 勾选框 ...): 颜色都是 theme 里的值, 斜边 2 像素."""
    opts = dict(th.tk_options())
    colors = set(_colors().values())
    for k in ("*Background", "*Foreground", "*activeBackground", "*selectColor", "*troughColor",
              "*selectBackground", "*insertBackground", "*disabledForeground"):
        assert opts[k] in colors, k
    assert opts["*Font"] == th.FONT
    assert opts["*highlightThickness"] == 0
    assert "*borderWidth" not in opts                 # 不给所有 Frame 加 2 像素边, 只管有斜边的控件
    assert all(opts[f"*{c}.borderWidth"] == 2 for c in ("Button", "Menubutton", "Entry", "Listbox", "Text",
                                                         "Scrollbar", "Labelframe", "Radiobutton", "Checkbutton"))
    assert (opts["*Dialog.relief"], opts["*Dialog.borderWidth"]) == ("raised", 3)   # messagebox 没有窗口管理器的边框


def test_setup_applies_fonts_and_options(family):
    """真起 Tk (没显示就跳过): 命名字体按挑中的字体建好, 新建的勾选框吃 option 数据库的底色."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    try:
        root.withdraw()
        assert th.setup(root, families=[]) == "Noto Sans CJK SC"
        import tkinter.font as tkfont
        assert tuple(tkfont.Font(root, name=th.FONT_BIG, exists=True).cget(k) for k in ("family", "size")) \
            == ("Noto Sans CJK SC", 15)
        cb = tk.Checkbutton(root)
        assert cb.cget("background") == th.PANEL and cb.cget("selectcolor") == dict(th.tk_options())["*selectColor"]
        assert th.setup(root, families=["Unifont"]) == "Unifont"           # 再调一次: 原地改, 不重复建
        assert tkfont.Font(root, name=th.FONT, exists=True).cget("size") == -16
    finally:
        root.destroy()


def test_relief_constants():
    """CDE 斜边: 控件凸 / 输入区凹, 2 像素; 对话框 3 像素. 亮暗边 Tk 按底色自己算."""
    assert th.RAISED == {"relief": "raised", "bd": 2}
    assert th.SUNKEN == {"relief": "sunken", "bd": 2}
    assert th.DIALOG == {"relief": "raised", "bd": 3}
    assert th.FLAT == {"relief": "flat", "bd": 0}


def test_ui_code_has_no_color_literals():
    assert not _hits(HEX), "颜色写进 theme.py, 业务代码用 th.名字"


def test_ui_code_has_no_font_family_literals():
    assert not _hits(FONT_FAMILY), "字体用 th.FONT* 或 th.font(), 别再写字体名"



def test_ui_code_has_no_relief_literals():
    """斜边用 th.RAISED / th.SUNKEN / th.DIALOG / th.FLAT / th.GROOVE, 别再各写各的 relief="flat"."""
    assert not _hits(RELIEF), "relief 用 **th.RAISED 之类"


def test_ui_code_only_uses_names_theme_has():
    """改名 / 删名 (如 #28 并掉的 FAINTER) 时, 引用了旧名字的地方要跟着改, 不然开到那个面板才炸."""
    missing = [h for h in _hits(re.compile(r"\bth\.[A-Z]")) for n in re.findall(r"\bth\.([A-Z][A-Z0-9_]*)", h)
               if not hasattr(th, n)]
    assert not missing


def test_unreadable_walks_widgets_and_text_tags():
    """th.unreadable(w): 把一棵控件树里字和底不够 4.5 的都找出来 (字 / 按下 / 选中 / Text 的 tag), 给面板测试用."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("没有显示")
    try:
        root.withdraw()
        f = tk.Frame(root, bg=th.PANEL)
        tk.Label(f, text="够", bg=th.PANEL, fg=th.DIM).pack()
        tk.Label(f, text="面板上的很次要", bg=th.PANEL, fg=th.FAINT).pack()
        tk.Label(f, image="", text="", bg=th.PANEL, fg=th.PANEL).pack()               # 没字不算
        tk.Button(f, text="按钮底红字", bg=th.BOX, fg=th.BAD, activebackground=th.ACTIVE, activeforeground=th.FG).pack()
        tk.Button(f, text="按下看不清", bg=th.BOX, fg=th.FG, activebackground=th.FG, activeforeground=th.FG).pack()
        tk.Button(f, text="灰掉的", bg=th.BOX, fg=th.FG, state="disabled", disabledforeground=th.DISABLED).pack()
        tk.Radiobutton(f, text="选中", indicatoron=False, bg=th.BTN2, fg=th.FG, selectcolor=th.FG).pack()
        t = tk.Text(f, bg=th.PANEL, fg=th.TEXT)
        t.tag_config("dim", foreground=th.FAINT)
        t.tag_config("ok", foreground=th.OK)
        t.pack()
        bad = {(x[1], x[2]) for x in th.unreadable(f)}
        assert bad == {("面板上的很次要", "fg"), ("按钮底红字", "fg"), ("按下看不清", "active"), ("选中", "select"),
                       ("tag dim", "fg")}
    finally:
        root.destroy()
