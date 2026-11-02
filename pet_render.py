#!/usr/bin/env python3
"""pet_render: 把桌宠贴到副屏整帧上 (#35). 依赖 Pillow, 不碰 USB.

背景只在版面变了才重画一次, 之后每帧: 拷背景 → 贴猫 → 转 → 压 JPEG (推车实测约 5 ms).
猫只能画在 AREA_TOP..AREA_BOTTOM 之间: 站分隔线时盖一点下排灯, 再往上的状态和底部提示不盖.
"""

import json

import dash_render as r
import pet_core as pc
import pet_sprites as ps
import theme as th

AREA_TOP = pc.CEILING - 20      # 留点余地给起伏和 z
SKIN_PATH = "/etc/ops-cart/pet-skin.json"       # 可选: 换主猫的样子, 现场自备, 不进仓库
AREA_BOTTOM = r.PANEL_H - 140   # 底部 "推车已锁定" 提示和 footer 留出来
SIZE = 24 * pc.S

_sprites = {}
_skin = None


def load_skin(path=SKIN_PATH):
    """读皮肤 JSON: {"palette": {字符: "#rrggbb"}, "poses": {姿势: 24 行 24 列字符串}}. 纯数据, 不执行代码.
    没有文件或内容不对都返回 None, 退回内置的猫: 皮肤坏了不能拖累副屏."""
    try:
        with open(path, encoding="utf-8") as f:
            skin = json.load(f)
        palette, poses = skin["palette"], skin["poses"]
        for color in palette.values():
            int(color[1:], 16)
            if color[0] != "#" or len(color) != 7:
                raise ValueError(color)
        for name, grid in poses.items():
            if name not in ps.POSES or len(grid) != 24 or any(len(row) != 24 for row in grid):
                raise ValueError(name)
            if any(ch != "." and ch not in palette for row in grid for ch in row):
                raise ValueError(name)
        return {"palette": palette, "poses": poses}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None


def use_skin(skin):
    """换主猫皮肤; None 用内置的. 路过的第二只不换."""
    global _skin
    _skin = skin
    _sprites.clear()


def _rgba(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5)) + (255,)


def sprite(name, coat, direction):
    """姿势 → 放大好的 RGBA 图; 朝左的镜像. 缓存."""
    key = (name, coat, direction)
    if key not in _sprites:
        from PIL import Image
        skinned = _skin is not None and coat == pc.COAT and name in _skin["poses"]
        colors = _skin["palette"] if skinned else th.PET_COATS[coat]
        grid = _skin["poses"][name] if skinned else ps.compose(name, coat)
        img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        px = img.load()
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                if ch != ".":
                    px[x, y] = _rgba(colors[ch])
        img = img.resize((SIZE, SIZE), Image.NEAREST)
        _sprites[key] = img.transpose(Image.FLIP_LEFT_RIGHT) if direction < 0 else img
    return _sprites[key]


def _pix(d, rows, x, y, s, color):
    for r_i, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == "#":
                d.rectangle([x + c * s, y + r_i * s, x + (c + 1) * s - 1, y + (r_i + 1) * s - 1], fill=color)


def _fx(d, c):
    """z / 爱心 / 落地灰 / 惊叹号 / 哈! 气泡. 像素对齐, 2 fps 下也看得清."""
    x, y, s, direction = c["x"], c["y"], pc.S, c["dir"]
    head_x = x + direction * s * 7
    if c["state"] == "sleep":
        for i in range(3):
            ph = (c["t"] / 3.6 + i / 3) % 1
            size = (3, 4, 5)[i]
            if ph >= .12:
                zx = head_x + direction * (14 + ph * 42) + (i - 1) * 6
                zy = y - s * 13 - ph * 120
                _pix(d, ps.Z_PIX, int(zx / size) * size, int(zy / size) * size, size, th.PET_FX["z"])
    if c["state"] == "knead":
        lift = int((c["t"] % 2) * 5) * 4
        _pix(d, ps.HEART_PIX, head_x + direction * s * 7, y - s * 19 - lift, 4, th.PET_FX["heart"])
    if c["dust_t"] >= 0:
        k = 0 if c["dust_t"] < .3 else 1
        for side in (-1, 1):
            for i in range(3):
                px = x + side * (s * 9 + i * 12 + k * 12)
                py = y - 6 - i * 4 - k * 4
                d.rectangle([px - 3, py, px + 2, py + 5], fill=th.PET_FX["dust"])
    if c["alarm"]:
        d.rectangle([head_x - 4, y - s * 27, head_x + 3, y - s * 27 + 23], fill=th.PET_FX["alarm"])
        d.rectangle([head_x - 4, y - s * 27 + 28, head_x + 3, y - s * 27 + 35], fill=th.PET_FX["alarm"])
    if c["say"]:
        font = r._font(32)
        bw, bh = int(font.getlength(c["say"])) + 24, 48
        bx = max(8, min(r.PANEL_W - 8 - bw, x + direction * s * 2 - bw // 2))
        by = max(6, y - s * 20 - bh - 14)       # y 已经是相对猫区的; 贴顶也别被分隔线切掉
        tail = x + direction * s * 4
        d.rectangle([bx - 4, by - 4, bx + bw + 3, by + bh + 3], fill=th.PET_FX["ink"])
        d.rectangle([bx, by, bx + bw - 1, by + bh - 1], fill=th.PET_FX["paper"])
        d.rectangle([tail - 6, by + bh + 4, tail + 5, by + bh + 11], fill=th.PET_FX["ink"])
        d.rectangle([tail - 6, by + bh, tail + 5, by + bh + 5], fill=th.PET_FX["paper"])
        d.text((bx + 12, by + bh // 2 + 2), c["say"], font=font, fill=th.PET_FX["say"], anchor="lm")


class Stage:
    """缓存背景的整帧画手. pet=None 或没猫时跟 make_frame 完全一样."""

    def __init__(self):
        self.key = None
        self.base = None
        self.plain = None

    def _base(self, view):
        key = json.dumps(view, sort_keys=True, ensure_ascii=False)
        if key != self.key:
            self.key, self.base, self.plain = key, r.render(view).convert("RGB"), None
        return self.base

    def frame(self, view, pet):
        base = self._base(view)
        draws = pet.draws() if pet is not None else []
        if not draws:
            if self.plain is None:
                self.plain = r.encode_within_budget(r.rot90(base))
            return self.plain
        from PIL import Image, ImageDraw
        area = base.crop((0, AREA_TOP, r.PANEL_W, AREA_BOTTOM))
        layer = area.copy()
        for c in draws:
            img = sprite(c["pose"], c["coat"], c["dir"])
            layer.paste(img, (c["x"] - SIZE // 2, c["y"] - 23 * pc.S - AREA_TOP), img)
        d = ImageDraw.Draw(layer)
        shifted = [dict(c, y=c["y"] - AREA_TOP) for c in draws]
        for c in shifted:
            _fx(d, c)
        out = base.copy()
        out.paste(layer, (0, AREA_TOP))
        return r.encode_within_budget(r.rot90(out))
