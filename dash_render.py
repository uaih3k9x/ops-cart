#!/usr/bin/env python3
"""dash_render: 把 dash_core.layout_view() 的版面模型画成副屏 JPEG.

副屏是竖装的 800x1280：这里按竖屏(面板朝向)画，出口处 rot90_frame() 转成
横屏 1280x800 再交给 udisp 发；帧头坐标永远按配置串报告的横屏尺寸。

依赖 Pillow（推车上走 ~/cartdash-venv）。本模块不做 USB、不开设备。
"""

import io

PANEL_W = 800          # 竖屏宽 (= 横屏配置串里的高)
PANEL_H = 1280         # 竖屏高 (= 横屏配置串里的宽)
JPEG_QUALITY = 72
JPEG_BUDGET = 100 * 1024      # 固件示例每帧缓冲 100KB, 超了会被丢

FONT_CJK = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc"
FONT_INDEX_SC = 2     # NotoSansCJK-Bold.ttc: 0=JP 1=KR 2=SC 3=TC 4=HK

BG = (12, 17, 14)
FG = (230, 240, 235)
DIM = (120, 140, 130)
ACCENT = (60, 220, 140)
WARN = (240, 170, 60)
BAD = (235, 80, 80)

NET_TAG = {"on": ("网络 通", ACCENT), "warn": ("网络 无外网", WARN), "off": ("网络 断开", BAD)}


def _load_jpeg():
    from PIL import Image
    return Image


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT_CJK, size, index=FONT_INDEX_SC)
    except OSError:
        return ImageFont.load_default()


def render(view, size=(PANEL_W, PANEL_H)):
    """版面模型 → PIL Image (竖屏朝向). 纯函数, 方便测试和本地预览."""
    Image = _load_jpeg()
    from PIL import ImageDraw
    W, H = size
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    big, mid, small = _font(72), _font(44), _font(34)

    y = 60
    if view.get("locked"):
        d.text((60, y), "推车锁定中", font=big, fill=FG)
        y += 100
        d.text((60, y), "只显示状态 · 待办已隐藏", font=small, fill=DIM)
        y += 70
    else:
        d.text((60, y), "ops-cart", font=big, fill=ACCENT)
        if view.get("clock"):
            d.text((W - 60, y + 12), view["clock"], font=mid, fill=DIM, anchor="ra")
        y += 110

    # 状态行: 网络 / 自检
    tag, color = NET_TAG.get(view.get("net") or "off", NET_TAG["off"])
    d.text((60, y), tag, font=mid, fill=color)
    chk = view.get("checks") or {}
    ok = chk.get("total", 0) - chk.get("bad", 0)
    d.text((330, y), "自检 %d/%d" % (ok, chk.get("total", 0)), font=mid,
           fill=ACCENT if not chk.get("bad") else BAD)
    y += 80
    bad_labels = chk.get("bad_labels") or ([chk["first_bad"]] if chk.get("first_bad") else [])
    if bad_labels:
        d.text((60, y), "异常: " + " · ".join(bad_labels[:2]), font=small, fill=BAD)
        y += 60

    # 操作头计数器
    lamps = view.get("lamps") or {}
    keys = [k for k in lamps if k.startswith("con-")]
    x = 60
    for k in sorted(keys)[:4]:
        d.rounded_rectangle([x, y + 20, x + 150, y + 150], 18,
                            outline=DIM, width=3)
        d.text((x + 57, y + 50), k.replace("con-", ""), font=big, fill=FG)
        val = lamps[k]
        d.text((x + 55, y + 160), "—" if val is None else str(val), font=small,
               fill=DIM if val is None else ACCENT, anchor="ma")
        x += 175
    y += 240

    # 温度 / 风扇
    temps = view.get("temps") or []
    fans = view.get("fans") or []
    line = "  ".join("%s %d°" % (t[0], t[1]) for t in temps[:3] if len(t) > 1)
    if line:
        d.text((60, y), line, font=small, fill=FG)
        y += 56
    rpms = [str(f[1]) for f in fans if isinstance(f, (list, tuple)) and len(f) > 1 and f[1]]
    if rpms:
        d.text((60, y), "风扇 " + " / ".join(rpms[:3]) + " rpm", font=small, fill=FG)
        y += 56

    # 待办
    todo = view.get("todo") or []
    if todo:
        y += 30
        d.text((60, y), "待办", font=mid, fill=ACCENT)
        y += 70
        for item in todo[:6]:
            s = item if len(item) <= 18 else item[:17] + "…"
            d.text((80, y), "· " + s, font=small, fill=FG)
            y += 56

    # 底部: 昨夜整理 / 数据源状态
    bottom = H - 150
    if view.get("dream"):
        d.text((60, bottom), view["dream"][:26], font=small, fill=DIM)
    if view.get("stale"):
        d.text((60, H - 80), "数据源停了", font=mid, fill=BAD)
    d.rectangle([0, H - 8, W, H], fill=ACCENT)
    return img


def encode(img, quality=JPEG_QUALITY):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def rot90_frame(jpeg_bytes):
    """横屏发送前转 90°: 竖屏(面板朝向) → 横屏(帧头坐标).

    实测这块板要顺时针 90°, 也就是 Pillow 的 ROTATE_270.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg_bytes)).transpose(Image.ROTATE_270)
    return encode(img)


def make_frame(view, rotate=True, quality=JPEG_QUALITY, budget=JPEG_BUDGET):
    """版面模型 → 可发送的 JPEG 载荷; 超预算就逐档降质量. 返回 bytes."""
    jpeg = encode(render(view), quality)
    if rotate:
        jpeg = rot90_frame(jpeg)
    q = quality
    while len(jpeg) > budget and q > 30:
        q -= 10
        jpeg = encode(render(view), q)
        if rotate:
            jpeg = rot90_frame(jpeg)
    return jpeg
