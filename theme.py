#!/usr/bin/env python3
"""界面主题 (#27): 车机 (Tk)、副屏 (dash_render)、指示灯 (lamps_core) 的颜色和字体都在这里.

业务代码只引用名字 (th.BG / th.FONT ...), 不写十六进制和字体名 (tests/test_theme.py 扫描兜底).
名字按用途起, 不按颜色起: 换风格只改这里的值.
"""

FAMILY = "Noto Sans CJK SC"
FAMILY_MONO = "Noto Sans Mono CJK SC"


def font(size, bold=False, mono=False):
    """Tk 字体元组. size 负数 = 按像素 (Tk 约定)."""
    f = (FAMILY_MONO if mono else FAMILY, size)
    return f + ("bold",) if bold else f


FONT_SMALL = font(12)        # 串口 / Kickstart 面板
FONT = font(13)
FONT_BIG = font(15)          # 标题、主按钮
FONT_MONO = font(13)
FONT_MONO_BIG = font(14)     # 无线面板的输入框


def contrast(a, b):
    """WCAG 对比度 (1..21), 参数顺序无所谓."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- 车机: 底和面

BG = "#10141c"               # 窗口底、输入框底、副屏底、亮灯上的字
PANEL = "#161b26"            # 状态板、左侧面板
BOX = "#1f2a3d"              # 普通按钮、列表、对话框里的输入框
BTN2 = "#2a3347"             # 次要按钮 (返回 / 取消)、分隔条
ACTIVE = "#3a4560"           # 按下 / 选中
PRIMARY = "#26577c"          # 主操作按钮 (连接 / 应用 / 发送)、输入框焦点框
BLACK = "#000000"            # 视频预览底

# ---------------------------------------------------------------- 车机: 字

FG = "#e8eaf0"               # 标题、按钮字
TEXT = "#c8cede"             # 正文、表单标签
DIM = "#8b93a7"              # 次要说明
FAINT = "#6b7489"            # 很次要 (检查中、昨夜做梦)
FAINTER = "#5d6873"          # 版本号、聊天时间戳
DISABLED = "#4b5368"
MUTED = "#9aa5b0"            # 温度正常
WHITE = "#ffffff"

# ---------------------------------------------------------------- 车机: 状态

OK = "#7ee787"
WARN = "#f0b429"
BAD = "#ff7b72"
NOTICE = "#ffb86c"           # 顶栏横幅、锁屏通知、聊天里对方
MARK = "#7ec8e3"             # 串口控制台里的标记行
STATE = {"OK": OK, "WARN": WARN, "FAIL": BAD}

WARN_BG = "#4a3320"          # 警告类按钮 (重启)
AMBER_BG = "#7c5a26"         # 需要拍板的按钮 (信任认证页)
DANGER_BG = "#47202a"        # 危险按钮 (关机 / 删除 / 解锁)
DANGER_FG = "#ffb3bd"
DANGER_EDGE = "#7a1f2b"      # 危险弹窗的边框、锁屏上的危险提示条
ALERT_BG = "#da3633"         # UPS 倒计时
SERVE_BG = "#b3261e"         # 自己发地址中 (#11)
SERVE_EXPIRED_BG = "#8a5a00"

# ---------------------------------------------------------------- 开机自检页 (白底, 接 Plymouth)

BOOT_BG = WHITE
BOOT_FG = BG
BOOT_SUB = "#6b7280"
BOOT_DIM = MUTED
BOOT_STEP = "#2a2e36"
BOOT_OK, BOOT_WARN, BOOT_FAIL = "#15803d", "#b45309", "#dc2626"
BOOT_STATE = {"OK": BOOT_OK, "WARN": BOOT_WARN, "FAIL": BOOT_FAIL}

# ---------------------------------------------------------------- 指示灯 (#2) / 副屏 (#6)

LAMP = {"OK": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", None: DISABLED}
LAMP_OK, LAMP_FAIL = LAMP["OK"], LAMP["FAIL"]
LAMP_BODY = "#1f2633"
LAMP_EMPTY = PANEL
LAMP_FG = TEXT
LAMP_EMPTY_FG = DISABLED
LAMP_LIT_FG = BG

DASH_BG = BG
DASH_DIM = "#7b8496"
DASH_LINE = LAMP_BODY
