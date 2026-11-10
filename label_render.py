#!/usr/bin/env python3
"""文字 -> 1 位图 (#104): 打印机内置字体只有 ASCII, 车机 Python 没有 Pillow, 用 ctypes 调 libfreetype.

FreeType 和手写的结构体放在一次性子进程里 (同 ime_proc 隔离 librime 的做法): 字体解析或结构体对不上最多崩子进程,
kiosk 只会收到一句"渲染失败". 这个文件既是父进程调的 render_plan, 也是子进程入口; 子进程只从 stdin 读 JSON.
"""

import base64
import ctypes
import ctypes.util
import json
import os
import subprocess
import sys

import label_core as lc
import theme as th

RENDER_TIMEOUT_S = 15
_NOTO = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-{}.ttc"
# (路径, ttc 里第几个): Noto 的 2 = 简体; 热转印小字要粗一点, 先 Bold; 没有就退到随部署包带的 Unifont
FONTS = tuple((_NOTO.format(w), 2) for w in ("Bold", "Medium", "Regular")) + tuple((p, 0) for p in th.UNIFONT_PATHS)

FT_LOAD_RENDER, FT_LOAD_NO_BITMAP = 4, 8
FT_PIXEL_MODE_GRAY = 2
GRAY_INK = 110                  # 灰度 >= 这个算有墨; 比 128 略低, 热转印小字笔画不断


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------- FreeType 公开结构体 (只声明用到的前缀)

class _Generic(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("finalizer", ctypes.c_void_p)]


class _BBox(ctypes.Structure):
    _fields_ = [(n, ctypes.c_long) for n in ("xMin", "yMin", "xMax", "yMax")]


class _Vector(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Bitmap(ctypes.Structure):
    _fields_ = [("rows", ctypes.c_uint), ("width", ctypes.c_uint), ("pitch", ctypes.c_int),
                ("buffer", ctypes.POINTER(ctypes.c_ubyte)), ("num_grays", ctypes.c_ushort),
                ("pixel_mode", ctypes.c_ubyte), ("palette_mode", ctypes.c_ubyte), ("palette", ctypes.c_void_p)]


class _GlyphMetrics(ctypes.Structure):
    _fields_ = [(n, ctypes.c_long) for n in ("width", "height", "horiBearingX", "horiBearingY", "horiAdvance",
                                             "vertBearingX", "vertBearingY", "vertAdvance")]


class _GlyphSlot(ctypes.Structure):
    _fields_ = [("library", ctypes.c_void_p), ("face", ctypes.c_void_p), ("next", ctypes.c_void_p),
                ("glyph_index", ctypes.c_uint), ("generic", _Generic), ("metrics", _GlyphMetrics),
                ("linearHoriAdvance", ctypes.c_long), ("linearVertAdvance", ctypes.c_long), ("advance", _Vector),
                ("format", ctypes.c_int), ("bitmap", _Bitmap), ("bitmap_left", ctypes.c_int),
                ("bitmap_top", ctypes.c_int)]


class _Face(ctypes.Structure):
    _fields_ = [("num_faces", ctypes.c_long), ("face_index", ctypes.c_long), ("face_flags", ctypes.c_long),
                ("style_flags", ctypes.c_long), ("num_glyphs", ctypes.c_long),
                ("family_name", ctypes.c_char_p), ("style_name", ctypes.c_char_p),
                ("num_fixed_sizes", ctypes.c_int), ("available_sizes", ctypes.c_void_p),
                ("num_charmaps", ctypes.c_int), ("charmaps", ctypes.c_void_p),
                ("generic", _Generic), ("bbox", _BBox),
                ("units_per_EM", ctypes.c_ushort), ("ascender", ctypes.c_short), ("descender", ctypes.c_short),
                ("height", ctypes.c_short), ("max_advance_width", ctypes.c_short),
                ("max_advance_height", ctypes.c_short), ("underline_position", ctypes.c_short),
                ("underline_thickness", ctypes.c_short), ("glyph", ctypes.POINTER(_GlyphSlot))]


def _load_lib():
    for name in ("libfreetype.so.6", ctypes.util.find_library("freetype")):
        if name:
            try:
                ft = ctypes.CDLL(name)
            except OSError:
                continue
            ft.FT_New_Face.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)]
            ft.FT_Set_Pixel_Sizes.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
            ft.FT_Get_Char_Index.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            ft.FT_Get_Char_Index.restype = ctypes.c_uint
            ft.FT_Load_Glyph.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int32]
            ft.FT_Done_Face.argtypes = [ctypes.c_void_p]
            ft.FT_Done_FreeType.argtypes = [ctypes.c_void_p]
            return ft
    raise lc.LabelError("推车上找不到 libfreetype")


class FreeType:
    """一个字体的渲染器. render(文字, px) -> 紧贴墨迹的 lc.Ink."""

    def __init__(self, path, index):
        self.ft, self.lib, self.face = _load_lib(), ctypes.c_void_p(), ctypes.c_void_p()
        if self.ft.FT_Init_FreeType(ctypes.byref(self.lib)):
            raise lc.LabelError("FreeType 初始化失败")
        if self.ft.FT_New_Face(self.lib, os.fsencode(path), index, ctypes.byref(self.face)):
            self.close()
            raise OSError(f"打不开字体 {path}")

    def _glyph(self, ch):
        idx = self.ft.FT_Get_Char_Index(self.face, ord(ch))
        if idx == 0 and ch.isspace():   # 字体没有这种空白 (如全角空格): 借空格的宽度, 不画 .notdef 的方框
            idx = self.ft.FT_Get_Char_Index(self.face, ord(" "))
        if idx == 0:
            raise lc.LabelError(f"字体里没有这个字: {ch}")
        if self.ft.FT_Load_Glyph(self.face, idx, FT_LOAD_RENDER | FT_LOAD_NO_BITMAP):
            raise lc.LabelError(f"渲染这个字失败: {ch}")
        return ctypes.cast(self.face, ctypes.POINTER(_Face)).contents.glyph.contents

    def render(self, text, px):
        if self.ft.FT_Set_Pixel_Sizes(self.face, 0, px):
            raise lc.LabelError(f"字体不支持 {px} 点")
        pen, parts = 0, []              # [(x, y, 宽, 高, 灰度字节)]; y 向下, 基线在 0
        for ch in text:
            slot = self._glyph(ch)
            bm = slot.bitmap
            if bm.rows:
                if bm.pixel_mode != FT_PIXEL_MODE_GRAY or bm.pitch < 0:
                    raise lc.LabelError("字体给的位图格式不认识")
                raw = ctypes.string_at(bm.buffer, bm.rows * bm.pitch)
                parts.append((pen + slot.bitmap_left, -slot.bitmap_top, bm.width, bm.rows, bm.pitch, raw))
            pen += slot.advance.x >> 6
        if not parts:
            return lc.Ink(0, 0, b"")
        x0, y0 = min(p[0] for p in parts), min(p[1] for p in parts)
        w = max(p[0] + p[2] for p in parts) - x0
        h = max(p[1] + p[3] for p in parts) - y0
        out = bytearray(w * h)
        for gx, gy, gw, gh, pitch, raw in parts:
            for row in range(gh):
                base = (gy - y0 + row) * w + gx - x0
                for col in range(gw):
                    if raw[row * pitch + col] >= GRAY_INK:
                        out[base + col] = 1
        return lc.Ink(w, h, bytes(out))

    def close(self):
        if self.face:
            self.ft.FT_Done_Face(self.face)
            self.face = ctypes.c_void_p()
        if self.lib:
            self.ft.FT_Done_FreeType(self.lib)
            self.lib = ctypes.c_void_p()


def open_first(fonts, loader=FreeType):
    """按顺序开第一个能开的字体."""
    for path, index in fonts:
        try:
            return loader(path, index)
        except OSError:
            continue
    raise lc.LabelError("找不到中文字体 (Noto CJK / Unifont), 请检查部署")


# ---------------------------------------------------------------- 父进程

def _decode(res, box_h):
    try:
        data = json.loads(res.stdout)
    except ValueError:
        raise lc.LabelError("渲染失败: 渲染进程的回复看不懂") from None
    if isinstance(data, dict) and "error" in data:
        raise lc.LabelError(f"渲染失败: {data['error']}")
    try:
        inks = [lc.Ink(int(w), int(h), base64.b64decode(b)) for w, h, b in data["inks"]]
        fit = lc.Fit(int(data["px"]), list(data["lines"]), inks, box_h)
    except (ValueError, KeyError, TypeError) as e:
        raise lc.LabelError(f"渲染失败: 渲染进程的回复看不懂 ({type(e).__name__})") from None
    if len(fit.lines) != len(inks) or any(i.w * i.h != len(i.px) for i in inks):
        raise lc.LabelError("渲染失败: 渲染进程的回复看不懂")
    return fit


def render_plan(lines, fold_mm, run=subprocess.run, timeout=RENDER_TIMEOUT_S, fonts=None):
    """要打的文字 + 校准折痕 -> lc.Plan. 所有失败都是 lc.LabelError, 话是给现场的人看的."""
    lines = lc.clean_lines(lines)
    _top, box_h = lc.face_box(lc.dots(fold_mm))
    req = {"lines": lines, "box_w": lc.CANVAS_W, "box_h": box_h, "fonts": list(fonts or FONTS)}
    try:
        res = run([sys.executable, os.path.abspath(__file__)], input=json.dumps(req, ensure_ascii=False).encode(),
                  capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise lc.LabelError(f"渲染超时 ({timeout:g} 秒), 再试一次") from None
    except OSError as e:
        raise lc.LabelError(f"渲染进程起不来: {e}") from None
    if res.returncode < 0:
        raise lc.LabelError(f"渲染进程崩了 (信号 {-res.returncode}); FreeType 或字体可能有问题") from None
    if res.returncode:
        raise lc.LabelError(f"渲染失败: {res.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return lc.build_plan(_decode(res, box_h), fold_mm)


# ---------------------------------------------------------------- 子进程

def worker(req):
    ft = open_first([tuple(f) for f in req["fonts"]])
    try:
        fit = lc.fit_lines(req["lines"], ft.render, req["box_w"], req["box_h"])
    finally:
        ft.close()
    return {"px": fit.px, "lines": fit.lines, "inks": [[i.w, i.h, _b64(i.px)] for i in fit.inks]}


def main():
    try:
        out = worker(json.loads(sys.stdin.buffer.read().decode("utf-8")))
    except lc.LabelError as e:
        out = {"error": str(e)}
    except Exception as e:              # noqa: BLE001  子进程只报错, 由父进程提示
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
