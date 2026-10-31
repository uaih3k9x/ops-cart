#!/usr/bin/env python3
"""dash_render: 把 dash_core.layout_view() 的版面模型画成副屏 JPEG.

副屏是竖装的 800x1280：这里按竖屏(面板朝向)画，出口处 rot90() 转成
横屏 1280x800 再编码交给 udisp 发；帧头坐标永远按配置串报告的横屏尺寸。

依赖 Pillow（推车上走 /opt/pxe/venv，见 cart-dash.service）。本模块不做 USB、不开设备。
"""

import io
import re

import lamps_core as lc

PANEL_W = 800          # 竖屏宽 (= 横屏配置串里的高)
PANEL_H = 1280         # 竖屏高 (= 横屏配置串里的宽)
JPEG_QUALITY = 72
JPEG_BUDGET = 100 * 1024      # 固件示例每帧缓冲 100KB, 超了会被丢

# 字体: 推车上是 Noto CJK; Mac 本地预览退到苹方/黑体. 都没有才用 Pillow 自带 (不认中文).
FONTS = [("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc", 2),   # 0=JP 1=KR 2=SC
         ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
         ("/System/Library/Fonts/PingFang.ttc", 0)]

M = 40                          # 左右边距
BG = (12, 15, 20)
FG = lc.FG
DIM = "#7b8496"
LINE = "#232a36"
ACCENT = lc.HEALTH_COLOR["OK"]
WARN = lc.HEALTH_COLOR["WARN"]
BAD = lc.HEALTH_COLOR["FAIL"]

NET_TAG = {"on": ("网络 通", ACCENT), "warn": ("无外网", WARN), "off": ("网络断开", BAD),
           "down": ("网络断开", BAD)}
MARK = {"FAIL": ("×", BAD), "WARN": ("▲", WARN)}   # ✕ 不少中文字体没有


_font_cache = {}


def _font(size):
    if size not in _font_cache:
        from PIL import ImageFont
        for path, index in FONTS:
            try:
                _font_cache[size] = ImageFont.truetype(path, size, index=index)
                break
            except OSError:
                continue
        else:
            _font_cache[size] = ImageFont.load_default(size)
    return _font_cache[size]


_TOKEN = re.compile(r"[A-Za-z0-9.:%/_+-]+|.", re.S)
NO_LINE_START = set("，。、；：！？）」』》〉”’,.;:!?)")   # 避头: 这些标点不许当行首


def wrap(text, font, width, max_lines):
    """按像素宽度折行. 中文逐字断; 英文/数字串 (6-10, B101) 整块换行, 比整行还宽才拆.
    超出 max_lines 的末行收成省略号."""
    lines, cur = [], ""
    for tok in _TOKEN.findall(text or ""):
        if font.getlength(cur + tok) <= width or (cur and tok in NO_LINE_START):
            cur += tok                      # 放得下; 或是避头标点, 挂在行尾
            continue
        if cur and font.getlength(tok) <= width:
            lines.append(cur)
            cur = tok
            continue
        for ch in tok:                      # 串本身放不下一行: 退回逐字
            if font.getlength(cur + ch) > width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines = lines[:max_lines - 1] + [last + "…"]
    return lines


def paragraphs(text):
    """公告分段: notice.txt 里的 " / " 和换行都是作者想要的断点."""
    out = []
    for line in (text or "").splitlines():
        out += [p.strip() for p in line.split(" / ")]
    return [p for p in out if p]


def _tile(d, box, label, health, active, label_font, sub=None, sub_font=None):
    """一个 MA3 执行器方块: 顶部色条 = 健康, 在动整块点亮; health=None 是空槽 (暗)."""
    look = lc.lamp_view(health, active, True)
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, 14, fill=look["bg"])
    d.rounded_rectangle([x0, y0, x1, y0 + 12], 6, fill=look["bar"])
    cx = (x0 + x1) // 2
    if sub is None:
        d.text((cx, (y0 + 12 + y1) // 2), label, font=label_font, fill=look["fg"], anchor="mm")
    else:
        d.text((cx, y0 + 12 + (y1 - y0 - 12) * 0.40), label, font=label_font, fill=look["fg"],
               anchor="mm")
        d.text((cx, y1 - 26), sub, font=sub_font, fill=look["fg"], anchor="mm")


def _temp_color(c):
    return ACCENT if c < 70 else WARN if c < 85 else BAD


UPS_PX, UPS_MIN_PX, UPS_GAP = 28, 22, 16    # 头部 UPS 那行: 字号 / 最小字号 / 离大时钟至少留这么宽
CLOCK_X, CLOCK_PX = M - 6, 150


def clock_right(clock):
    return CLOCK_X + _font(CLOCK_PX).getlength(clock or "--:--")


def ups_fit(ups, clock, width=PANEL_W):
    """UPS 那行右对齐在大时钟右边, 不许压到时钟: 全文放得下用全文, 否则换短写法, 还不行再缩字号."""
    room = width - M - clock_right(clock) - UPS_GAP
    texts = [t for t in (ups.get("text"), ups.get("short")) if t]
    for size in range(UPS_PX, UPS_MIN_PX - 1, -2):
        for t in texts:
            if _font(size).getlength(t) <= room:
                return t, size
    return texts[-1], UPS_MIN_PX


def render(view, size=(PANEL_W, PANEL_H)):
    """版面模型 → PIL Image (竖屏朝向). 纯函数, 方便测试和本地预览."""
    from PIL import Image, ImageDraw
    W, H = size
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if view.get("boot"):
        _boot_body(d, view, W, H)
        _footer(d, view, W, H)
        return img

    # ---- 头部: 大时钟 / 日期 / 网络
    d.text((CLOCK_X, 22), view.get("clock") or "--:--", font=_font(CLOCK_PX), fill=FG)
    d.text((W - M, 52), view.get("date") or "", font=_font(38), fill=DIM, anchor="ra")
    tag, color = NET_TAG.get(view.get("net") or "down", NET_TAG["down"])
    tw = _font(38).getlength(tag)
    d.ellipse([W - M - tw - 34, 124, W - M - tw - 14, 144], fill=color)
    d.text((W - M, 112), tag, font=_font(38), fill=color, anchor="ra")
    ups = view.get("ups") or {}
    if ups.get("text"):
        text, px = ups_fit(ups, view.get("clock"), W)
        d.text((W - M, 168), text, font=_font(px), anchor="ra",
               fill={"ok": DIM, "warn": WARN, "bad": BAD}.get(ups.get("level"), DIM))
    info = " · ".join(x for x in ("LAN2 " + view["lan2"] if view.get("lan2") else "",
                                  "开机 " + view["uptime"] if view.get("uptime") else "") if x)
    serve = view.get("serve")
    if serve:       # 推车自己给整个网段发地址 (#11): 盖住 LAN2/开机那行, 整条红 (到时停发琥珀)
        d.rectangle([0, 202, W, 258], fill=BAD if serve.get("level") == "bad" else WARN)
        line = wrap(serve.get("text") or "", _font(32), W - 2 * M, 1)
        d.text((W // 2, 230), line[0] if line else "", font=_font(32), fill=lc.LIT_FG, anchor="mm")
    else:
        d.text((M, 210), info, font=_font(30), fill=DIM)
    d.line([M, 262, W - M, 262], fill=LINE, width=2)

    # ---- 执行器: 上排操作头, 下排键鼠/PXE/网口, 再下面两行 LAN1 / LAN2 的 LLDP 对端
    d.text((M, 280), "操作头", font=_font(26), fill=DIM)
    heads = view.get("heads") or []
    gap, y0 = 20, 320
    tw = (W - 2 * M - gap * 3) // 4
    for i, h in enumerate(heads[:4]):
        x0 = M + i * (tw + gap)
        _tile(d, [x0, y0, x0 + tw, y0 + 170], str(h["n"]), h["health"], h["active"],
              _font(96), h["text"], _font(30))
    lower = view.get("lower") or []
    gap, y0 = 14, 506
    tw = (W - 2 * M - gap * 4) // 5
    for i, x in enumerate(lower[:5]):
        x0 = M + i * (tw + gap)
        _tile(d, [x0, y0, x0 + tw, y0 + 100], x["name"], x["health"], x["active"], _font(32))
    _peers(d, view.get("peers") or [], 614, W)
    d.line([M, 680, W - M, 680], fill=LINE, width=2)

    if view.get("locked"):
        _locked_body(d, view, W, H)
    else:
        _work_body(d, view, W, H)
    _footer(d, view, W, H)
    return img


def _footer(d, view, W, H):
    """底部: 数据源停了就整条红, 否则一道绿线."""
    if view.get("stale"):
        d.rectangle([0, H - 70, W, H], fill=BAD)
        d.text((W // 2, H - 35), "数据源停了 · 以上是旧数据", font=_font(32), fill=lc.LIT_FG,
               anchor="mm")
    else:
        d.rectangle([0, H - 8, W, H], fill=ACCENT)


# ---- 开机自检 (#26): 一行一项, "名称 ······ 结果", 下面一行灰字细节

BOOT_Y0, BOOT_ROW = 236, 108            # 第一行顶 / 行距
BOOT_STATUS_W = 240                     # 右边结果栏宽 (测试按这个框找颜色)
BOOT_COLOR = {"ok": ACCENT, "warn": WARN, "late": WARN, "fail": BAD}   # wait / off 都是灰


def boot_status_box(i, W=PANEL_W):
    """第 i 行结果栏的框 (x0, y0, x1, y1)."""
    y = BOOT_Y0 + i * BOOT_ROW
    return (W - M - BOOT_STATUS_W, y, W - M, y + 56)


def _dashes(d, y, W):
    for x in range(M, W - M, 24):
        d.line([x, y, min(x + 12, W - M), y], fill=DIM, width=2)


def _boot_body(d, view, W, H):
    """开机自检: 抬头 (名称/版本/时钟) + 逐行结果 + 底下一句说明. 没出结果的行灰字"等待"."""
    d.text((M, 48), view.get("title") or "OPS-CART", font=_font(72), fill=FG)
    d.text((W - M, 64), view.get("clock") or "--:--", font=_font(56), fill=DIM, anchor="ra")
    d.text((M, 140), view.get("version") or "", font=_font(28), fill=DIM)
    _dashes(d, 206, W)

    lines = view.get("lines") or []
    lf, sf, df = _font(36), _font(36), _font(26)
    for i, ln in enumerate(lines):
        x0, y, x1, _ = boot_status_box(i, W)
        color = BOOT_COLOR.get(ln.get("state"), DIM)
        status = ln.get("status") or ""
        label = ln.get("label") or ""
        d.text((M, y + 8), label, font=lf, fill=BAD if ln.get("state") == "fail" else FG)
        d.text((x1, y + 8), status, font=sf, fill=color, anchor="ra")
        dot_l = M + lf.getlength(label) + 14            # 名称和结果之间一串点, 开机自检的样子
        dot_r = x1 - sf.getlength(status) - 14
        n = int((dot_r - dot_l) // df.getlength("."))
        if n > 2:
            d.text((dot_l, y + 14), "." * n, font=df, fill=LINE if ln.get("state") == "wait" else DIM)
        detail = wrap(ln.get("detail") or "", df, W - 2 * M - 20, 1)
        if detail:
            d.text((M + 20, y + 58), detail[0], font=df, fill=color if ln.get("state") == "late" else DIM)

    y = BOOT_Y0 + len(lines) * BOOT_ROW + 10
    _dashes(d, y, W)
    states = [ln.get("state") for ln in lines]
    summary = "  ".join(s for s in ("正常 %d" % states.count("ok"),
                                    "等待 %d" % (states.count("wait") + states.count("late")),
                                    "故障 %d" % states.count("fail") if "fail" in states else "") if s)
    d.text((M, y + 24), summary, font=_font(30), fill=BAD if "fail" in states else FG)
    d.text((M, y + 70), "车机就绪后自动切到状态板", font=_font(26), fill=DIM)


def _peers(d, peers, y, W):
    """LAN1 / LAN2 对端: 一口一行, 收到的亮字, 过期/没收到/不可用都灰."""
    x = M + 80
    for p in peers[:2]:
        d.text((M, y), p.get("name") or "", font=_font(24), fill=DIM)
        line = wrap(p.get("text") or "", _font(24), W - M - x, 1)
        if line:
            d.text((x, y), line[0], font=_font(24), fill=FG if p.get("level") == "ok" else DIM)
        y += 32


def _work_body(d, view, W, H):
    """解锁时: 自检问题 / 温度风扇 / 待办 / 昨夜整理. 纵向按块固定起点, 互不挤占."""
    chk = view.get("checks") or {}
    y = 700
    d.text((M, y), "自检", font=_font(34), fill=FG)
    x = M + 90
    for text, n, color in (("正常", chk.get("ok", 0), ACCENT), ("注意", chk.get("warn", 0), WARN),
                           ("故障", chk.get("fail", 0), BAD)):
        s = "%s %d" % (text, n)
        d.text((x, y + 4), s, font=_font(30), fill=color if n else DIM)
        x += _font(30).getlength(s) + 34
    problems = chk.get("problems") or []
    y += 58
    for p in problems[:2]:
        mark, color = MARK[p["state"]]
        d.text((M, y), mark, font=_font(28), fill=color)
        d.text((M + 40, y), p["label"], font=_font(28), fill=color)
        lx = M + 60 + _font(28).getlength(p["label"])
        detail = wrap(p.get("detail") or "", _font(24), W - M - lx, 1)
        if detail:
            d.text((lx, y + 4), detail[0], font=_font(24), fill=DIM)
        y += 44
    if len(problems) > 2:
        d.text((M + 40, y), "还有 %d 项" % (len(problems) - 2), font=_font(24), fill=DIM)

    # 温度: 三列, 每列一条 0–100°C 的条
    y = 892
    temps = [t for t in (view.get("temps") or []) if len(t) > 1][:3]
    if temps:
        cw = (W - 2 * M) // 3
        for i, (name, c) in enumerate(temps):
            x0 = M + i * cw
            d.text((x0, y), str(name), font=_font(26), fill=DIM)
            d.text((x0 + cw - 30, y), "%d°" % c, font=_font(30), fill=_temp_color(c), anchor="ra")
            bw = cw - 30
            d.rounded_rectangle([x0, y + 46, x0 + bw, y + 56], 5, fill=LINE)
            d.rounded_rectangle([x0, y + 46, x0 + max(10, int(bw * min(c, 100) / 100)), y + 56], 5,
                                fill=_temp_color(c))
    rpms = [str(f[1]) for f in (view.get("fans") or [])
            if isinstance(f, (list, tuple)) and len(f) > 1 and f[1]]
    if rpms:
        d.text((M, y + 72), "风扇 " + " · ".join(rpms[:3]) + " rpm", font=_font(26), fill=DIM)
    d.line([M, 1000, W - M, 1000], fill=LINE, width=2)

    # 待办: 最多两条
    y = 1016
    todo = view.get("todo") or []
    d.text((M, y), "待办", font=_font(30), fill=ACCENT if todo else DIM)
    if not todo:
        d.text((M + 80, y + 2), "没有", font=_font(28), fill=DIM)
    for item in todo[:2]:
        y += 44
        d.text((M + 10, y), "· " + wrap(item, _font(28), W - 2 * M - 40, 1)[0], font=_font(28),
               fill=FG)

    # 昨夜整理: 固定贴底, 最多两行
    lines = wrap(view.get("dream") or "", _font(26), W - 2 * M, 2)
    for i, line in enumerate(lines):
        d.text((M, H - 60 - 38 * (len(lines) - i)), line, font=_font(26), fill=DIM)


def _locked_body(d, view, W, H):
    """锁屏: 教室引导牌. 公告大字居中, 别的都不显示."""
    paras = paragraphs(view.get("notice")) or ["推车锁定中"]
    room = H - 160 - 700                    # 分隔线以下、底部提示以上
    for size in (96, 80, 64, 52, 44):       # 字号从大往下试, 整段放得进为止
        lh = int(size * 1.3)
        lines = [ln for p in paras for ln in wrap(p, _font(size), W - 2 * M, 5)]
        if len(lines) * lh <= room:
            break
    lines = lines[:room // lh]
    top = 700 + max(0, (room - lh * len(lines)) // 2)
    for i, line in enumerate(lines):
        d.text((W // 2, top + i * lh + lh // 2), line, font=_font(size), fill=FG, anchor="mm")
    d.text((W // 2, H - 110), "推车已锁定 · 在车上解锁后显示状态", font=_font(26), fill=DIM,
           anchor="mm")


def encode(img, quality=JPEG_QUALITY):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def rot90(img):
    """竖屏(面板朝向) → 横屏(帧头坐标). 实测这块板要顺时针 90°, 即 Pillow 的 ROTATE_270."""
    from PIL import Image
    return img.transpose(Image.ROTATE_270)


def make_frame(view, rotate=True, quality=JPEG_QUALITY, budget=JPEG_BUDGET):
    """版面模型 → 可发送的 JPEG 载荷; 超预算就逐档降质量. 返回 bytes.

    先转图再编码, 每档只压一次: 转的是像素, 不是 JPEG (旧写法转完再压一遍, 降质不生效).
    """
    img = render(view)
    if rotate:
        img = rot90(img)
    q = quality
    jpeg = encode(img, q)
    while len(jpeg) > budget and q > 30:
        q -= 10
        jpeg = encode(img, q)
    return jpeg
