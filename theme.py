#!/usr/bin/env python3
"""界面主题 (#27): 车机 (Tk)、副屏 (dash_render)、指示灯 (lamps_core) 的颜色和字体都在这里.

业务代码只引用名字 (th.BG / th.FONT / th.RAISED ...), 不写十六进制、字体名和 relief (tests/test_theme.py 扫描兜底).
名字按用途起, 不按颜色起: 换风格只改这里的值.

#28 起是 CDE / Motif 深色: 控件凸 (raised)、输入区和面板凹 (sunken), 亮暗边 Tk 按底色自己算 (= Motif 的算法);
#30 加了同名的 CDE 浅色 (LIGHT), use("light") / use("dark") 切换, 默认深色 (和 #28 一样);
字体 Unifont 16px 点阵 (OTF, 随部署包带, 装到 /usr/local/share/fonts/ops-cart/), 取不到退回 Noto.
"""

import os

# ---------------------------------------------------------------- 字体

UNIFONT = "Unifont"
NOTO, NOTO_MONO = "Noto Sans CJK SC", "Noto Sans Mono CJK SC"
UNIFONT_PX = 16                  # 点阵原始字高; 只按它的整数倍放大
UNIFONT_FILE = "unifont-18.0.01.otf"
# 副屏 (Pillow) 按路径开字体: 先找部署装好的, 再找仓库里带的 (开发机本地预览)
UNIFONT_PATHS = (os.path.join("/usr/local/share/fonts/ops-cart", UNIFONT_FILE),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", UNIFONT_FILE))

FAMILY = NOTO                    # setup() 按机器上有没有 Unifont 定下来


def select_family(families):
    """有 Unifont 用 Unifont, 没有退回 Noto (1.10.00 的样子). 记在 FAMILY, 返回挑中的名字."""
    global FAMILY
    FAMILY = UNIFONT if UNIFONT in families else NOTO
    return FAMILY


def pixel_px(px):
    """点阵字的字高: 取最近的 16 的整数倍, 至少 16."""
    return UNIFONT_PX * max(1, round(px / UNIFONT_PX))


def font(size, bold=False, mono=False):
    """Tk 字体元组. size 负数 = 按像素 (Tk 约定), 正数 = 点.
    Unifont 下一律换成 16 的整数倍像素 (Unifont 本身等宽, mono 不用分); Noto 下原样."""
    if FAMILY == UNIFONT:
        px = -size if size < 0 else size * 4 / 3       # 点 -> 像素只为挑倍数, 按 96 dpi 估
        f = (UNIFONT, -pixel_px(px))
    else:
        f = (NOTO_MONO if mono else NOTO, size)
    return f + ("bold",) if bold else f


# 各档是 Tk 命名字体 (名字 -> (Noto 下的点数, 等宽)): 模块导入时就能引用, setup() 时按挑中的字体建.
# Unifont 下五档全是 16px (10/31 定: 不做 32px 两档)
FONT_TIERS = {"OpsSmall": (12, False),     # 串口 / Kickstart 面板
              "OpsFont": (13, False),
              "OpsBig": (15, False),       # 标题、主按钮
              "OpsMono": (13, True),
              "OpsMonoBig": (14, True)}    # 无线面板的输入框
FONT_SMALL, FONT, FONT_BIG, FONT_MONO, FONT_MONO_BIG = FONT_TIERS


def font_spec(name):
    size, mono = FONT_TIERS[name]
    return font(size, mono=mono)


def contrast(a, b):
    """WCAG 对比度 (1..21), 参数顺序无所谓."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- 两套配色 (#30)
# DARK / LIGHT 名字完全一样; 业务代码照旧只引用 th.名字, 不知道现在是哪套. 用哪套由 use() 定 (默认深色).
# 浅色是 #27 对比图里的 C 版: CDE 经典浅灰蓝, 斜边同样交给 Tk 按底色算; 状态色在浅底上重新取值 (深一档).

DARK = {
    # 车机: 底和面
    "BG": "#262a35",               # 窗口底、输入框 / 列表 / 终端底 (凹)
    "TOP": "#1e212a",              # 顶栏
    "PANEL": "#2f3441",            # 状态板、左侧面板、对话框
    "BOX": "#3a4050",              # 普通按钮、灯块、执行器条 (凸)
    "BTN2": "#4a5165",             # 次要按钮 (返回 / 取消)、分隔条
    "ACTIVE": "#525a70",           # 按下 / 选中
    "PRIMARY": "#3d5a80",          # 主操作按钮 (连接 / 应用 / 发送)、输入框焦点框
    "TITLE": "#4a5f8a",            # 对话框标题条 (白字)
    "INK": "#12151c",              # 最深: 亮灯上的字、副屏底
    "BLACK": "#000000",            # 视频预览底
    # 车机: 字 (比 1.10.00 降一档, 正文仍 >= 4.5:1)
    "FG": "#d0d4de",               # 标题、按钮字
    "TEXT": "#bcc2ce",             # 正文、表单标签
    "DIM": "#959cad",              # 次要说明; 面板 (PANEL) 上最淡只到这个, 按钮底 (BOX) 上不用
    "FAINT": "#8b92a4",            # 很次要 (检查中、昨夜做梦、版本号); 只用在 BG / TOP 上, 面板上不够
    "MUTED": "#a2abb8",            # 温度正常
    "WHITE": "#ffffff",
    # 车机: 状态
    "OK": "#8fd694",
    "WARN": "#e2b04a",
    "BAD": "#f08f86",              # 红字不写在按钮底 (BOX) 上: 不够 4.5
    "NOTICE": "#eab07a",           # 顶栏横幅、锁屏通知、聊天里对方
    "MARK": "#7ec8e3",             # 串口控制台里的标记行
    "WARN_BG": "#5c4424",          # 警告类按钮 (重启)
    "AMBER_BG": "#6b4f22",         # 需要拍板的按钮 (信任认证页)
    "DANGER_BG": "#5a2a2e",        # 危险按钮 (关机 / 删除 / 解锁)、高危提示条
    "DANGER_FG": "#ffd0d4",
    "DANGER_EDGE": "#7a1f2b",      # 危险弹窗的边框、锁屏上的危险提示条、已解锁的装机按钮
    "ALERT_BG": "#c62d2a",         # UPS 倒计时
    "SERVE_BG": "#b3261e",         # 自己发地址中 (#11)
    "SERVE_EXPIRED_BG": "#8a5a00",
    "GO_BG": "#2b6a45",            # 确认发送 (#23 确认框)
    "GO_FG": "#e2f5e6",
    "AGENT": "#a78bfa",            # Agent 在发 / 持写租约 (#23 执行绪方块紫)
    # 指示灯 (#2) / 副屏 (#6)
    "LAMP_OK": "#22c55e",
    "LAMP_WARN": "#f59e0b",
    "LAMP_FAIL": "#ef4444",
    "LAMP_LIT_FG": "#12151c",      # 亮灯 (绿 / 黄 / 红 / 紫) 上一律深字: 红灯上白字只有 3.8
    "DASH_BG": "#12151c",          # 副屏比车机深一档: 红黄绿灯色直接当字色用, 要够 4.5
    "DASH_DIM": "#8088a0",
}

LIGHT = {
    "BG": "#e4e6ec",
    "TOP": "#c4c8d4",
    "PANEL": "#cfd3dd",
    "BOX": "#bcc1cf",
    "BTN2": "#a9afc0",
    "ACTIVE": "#979eb3",
    "PRIMARY": "#a9bedf",          # 浅底上主按钮也是浅蓝 + 深字 (白字在浅蓝上不够)
    "TITLE": "#3d5a80",
    "INK": "#0e1118",
    "BLACK": "#000000",
    "FG": "#14171f",
    "TEXT": "#232733",
    "DIM": "#3c4252",
    "FAINT": "#4a5163",
    "MUTED": "#2f4a3a",
    "WHITE": "#ffffff",
    "OK": "#17602a",
    "WARN": "#6e4a00",
    "BAD": "#9c1f17",
    "NOTICE": "#7c3f0c",
    "MARK": "#145a7a",
    "WARN_BG": "#e3c487",
    "AMBER_BG": "#d9ad5b",
    "DANGER_BG": "#eab6b9",
    "DANGER_FG": "#5e0d16",
    "DANGER_EDGE": "#7a1f2b",
    "ALERT_BG": "#c62d2a",
    "SERVE_BG": "#b3261e",
    "SERVE_EXPIRED_BG": "#8a5a00",
    "GO_BG": "#b5dcbf",
    "GO_FG": "#0f3a1c",
    "AGENT": "#6d28d9",
    "LAMP_OK": "#15803d",          # 浅底上灯色加深: 副屏直接当字色用, 亮灯上改写白字
    "LAMP_WARN": "#9a5b06",
    "LAMP_FAIL": "#b91c1c",
    "LAMP_LIT_FG": "#ffffff",
    "DASH_BG": "#f3f4f7",
    "DASH_DIM": "#555c6e",
}


def _derive(p):
    """两套共用的别名: 开机自检页 / 指示灯 / 副屏引用车机的哪个名字, 两套一样."""
    return dict(p,
                DISABLED=p["BTN2"],            # 不可点的字、空灯的色带
                # 开机自检页 (#28 起跟着车机; 深浅按开机那一刻的模式, #32)
                BOOT_BG=p["BG"], BOOT_FG=p["FG"], BOOT_SUB=p["DIM"], BOOT_DIM=p["FAINT"], BOOT_STEP=p["TEXT"],
                BOOT_OK=p["OK"], BOOT_WARN=p["WARN"], BOOT_FAIL=p["BAD"],
                LAMP_BODY=p["BOX"], LAMP_EMPTY=p["PANEL"], LAMP_FG=p["TEXT"], LAMP_EMPTY_FG=p["DIM"],
                DASH_LINE=p["BOX"])


DARK, LIGHT = _derive(DARK), _derive(LIGHT)
MODES = ("dark", "light")
MODE = "dark"
_followers = []                  # [(模块的 globals(), {别名: theme 名字 或 现算函数})]


def _palette():
    return LIGHT if MODE == "light" else DARK


def _resolve(spec):
    return spec() if callable(spec) else globals()[spec]


def use(mode):
    """切到 mode 那套 ("dark" / "light"; 别的一律深色), 返回实际用上的. 只改 th.名字 和 follow() 登记的别名;
    已经建好的 Tk 控件不会变 -- 车机是锁屏时整个重建 (#32), 重建前再调 setup() 重设 Tk 默认控件."""
    global MODE, STATE, BOOT_STATE, LAMP
    MODE = mode if mode in MODES else "dark"
    c = _palette()
    globals().update(c)
    STATE = {"OK": c["OK"], "WARN": c["WARN"], "FAIL": c["BAD"]}
    BOOT_STATE = {"OK": c["BOOT_OK"], "WARN": c["BOOT_WARN"], "FAIL": c["BOOT_FAIL"]}
    LAMP = {"OK": c["LAMP_OK"], "WARN": c["LAMP_WARN"], "FAIL": c["LAMP_FAIL"], None: c["DISABLED"]}
    for ns, names in _followers:
        ns.update({k: _resolve(v) for k, v in names.items()})
    return MODE


def follow(ns, **names):
    """模块级别名跟着配色走: BG, BTN = th.follow(globals(), BG="PANEL", BTN=lambda: {...}).
    现在设一次 (按参数顺序返回, 左边照写一遍让静态检查看得见), 以后每次 use() 再设.
    名字写错在导入时就 KeyError, 不会等到切浅色才炸."""
    values = {k: _resolve(v) for k, v in names.items()}
    ns.update(values)
    _followers.append((ns, names))
    return tuple(values.values())


def unfollow(ns):
    _followers[:] = [f for f in _followers if f[0] is not ns]


use("dark")


# ---------------------------------------------------------------- 斜边 (Tk 按底色自己算亮暗边)

RAISED = {"relief": "raised", "bd": 2}       # 按钮、灯块、执行器条
SUNKEN = {"relief": "sunken", "bd": 2}       # 状态板、面板、输入框、列表
DIALOG = {"relief": "raised", "bd": 3}       # 对话框外框、执行器条上的操作头
GROOVE = {"relief": "groove", "bd": 2}       # 分组框
FLAT = {"relief": "flat", "bd": 0}           # 凹框里面再套的文字区


def tk_options():
    """Tk 默认控件 (messagebox 的底 / OptionMenu / 滚动条 / 勾选框 / 单选框 / 分组框 ...) 的 option 数据库.
    显式写了的选项照旧优先; 这里只管没写的. 按当前配色出 (#30: use() 之后再 setup() 就重设)."""
    c = _palette()
    return [("*Background", c["PANEL"]), ("*Foreground", c["FG"]), ("*Font", FONT),
            ("*activeBackground", c["ACTIVE"]), ("*activeForeground", c["FG"]),
            ("*selectBackground", c["PRIMARY"]), ("*selectForeground", c["FG"]), ("*selectColor", c["BG"]),
            ("*troughColor", c["BG"]), ("*insertBackground", c["FG"]), ("*disabledForeground", c["DIM"]),
            ("*highlightThickness", 0),
            # 斜边宽只给有斜边的控件; 全局 *borderWidth 会给每个 Frame 都加一圈
            *((f"*{w}.borderWidth", 2) for w in ("Button", "Menubutton", "Menu", "Entry", "Listbox", "Text",
                                                  "Scrollbar", "Labelframe", "Radiobutton", "Checkbutton")),
            ("*Dialog.relief", "raised"), ("*Dialog.borderWidth", 3),     # messagebox: 没有窗口管理器, 自己凸出来
            ("*Button.background", c["BOX"]), ("*Button.relief", "raised"),
            ("*Menubutton.background", c["BOX"]), ("*Menubutton.relief", "raised"),
            ("*Menu.background", c["BOX"]), ("*Menu.relief", "raised"), ("*Menu.activeBorderWidth", 2),
            ("*Scrollbar.background", c["BOX"]), ("*Labelframe.relief", "groove"),
            ("*Entry.background", c["BG"]), ("*Entry.relief", "sunken"),
            ("*Listbox.background", c["BG"]), ("*Listbox.relief", "sunken"),
            ("*Text.background", c["BG"]), ("*Text.relief", "sunken"),
            ("*Dialog.msg.font", FONT), ("*Dialog.dtl.font", FONT)]


_named = {}                      # 自己建的命名字体: 留着引用, 不然 Python 回收时会把 Tk 那边删掉


def setup(root, families=None):
    """启动时调一次 (建好根窗口、建别的控件之前): 挑字体、建 / 改命名字体、设 option 数据库和 ttk 样式
    (X11 上 messagebox 是 ttk 画的). families 给测试注入, 默认问 Tk. 返回用上的字体名 (自检那一行用)."""
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk
    fam = select_family(tkfont.families(root) if families is None else families)
    for name in FONT_TIERS:
        family, size = font_spec(name)
        try:
            tkfont.Font(root, name=name, exists=True).configure(family=family, size=size)
        except tk.TclError:
            _named[name] = tkfont.Font(root, name=name, family=family, size=size)
    for pattern, value in tk_options():
        root.option_add(pattern, value)
    c = _palette()
    s = ttk.Style(root)
    s.theme_use("default")
    s.configure(".", background=c["PANEL"], foreground=c["FG"], font=FONT, troughcolor=c["BG"],
                fieldbackground=c["BG"], selectbackground=c["PRIMARY"], selectforeground=c["FG"], insertcolor=c["FG"])
    s.configure("TButton", background=c["BOX"], relief="raised", borderwidth=2, padding=(14, 4))
    s.map("TButton", background=[("pressed", c["BTN2"]), ("active", c["ACTIVE"])], relief=[("pressed", "sunken")])
    return fam


def unreadable(top, floor=4.5):
    """一棵 Tk 控件树里字和底不够 floor 的: [(控件路径, 字, 哪一对, 对比度)]. 给面板测试和本机检查用.
    查: 字 / 底; 按钮按下 (active); 不带圆点的单选按钮选中 (select); Text 里各 tag 的字色 (记成 "tag 名字").
    没字的 (图标)、灰掉的不查."""
    def hexc(w, color):
        r, g, b = (v // 257 for v in w.winfo_rgb(color))
        return f"#{r:02x}{g:02x}{b:02x}"

    out, stack = [], [top]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        keys = w.keys()
        if "foreground" not in keys or "background" not in keys:
            continue
        bg = hexc(w, w.cget("background"))
        pairs = []
        if w.winfo_class() == "Text":
            pairs.append(("", "fg", w.cget("foreground"), bg))
            pairs += [(f"tag {t}", "fg", w.tag_cget(t, "foreground"), bg) for t in w.tag_names()
                      if w.tag_cget(t, "foreground")]
        else:
            text = str(w.cget("text")) if "text" in keys else ""
            if not text.strip() or ("state" in keys and str(w.cget("state")) == "disabled"):
                continue
            pairs.append((text, "fg", w.cget("foreground"), bg))
            if "activebackground" in keys and w.winfo_class() in ("Button", "Radiobutton", "Checkbutton",
                                                                 "Menubutton"):
                pairs.append((text, "active", w.cget("activeforeground"), hexc(w, w.cget("activebackground"))))
            if "indicatoron" in keys and not int(w.cget("indicatoron")) and w.cget("selectcolor"):
                pairs.append((text, "select", w.cget("foreground"), hexc(w, w.cget("selectcolor"))))
        for text, kind, fg, b in pairs:
            ratio = contrast(hexc(w, fg), b)
            if ratio < floor:
                out.append((str(w), text, kind, round(ratio, 2)))
    return out
