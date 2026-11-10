"""车机打线缆标签的纯逻辑 (#104): 版面、换行和字号、压折痕检查、TSPL 字节流、校准读写. 不碰打印机, 不碰 Tk.

字一律渲染成位图 (TSPL BITMAP 模式 0, 位 0 = 打印), 用户输入不进 TSPL 命令文本.
旗子位置和打印头设置来自 cart_label; 这里只多一个校准值: 折痕在打印机原始坐标里的读数 (mm).
上半份贴着折痕往上排, 下半份绕 (旗子中心, 折痕) 转 180°, 对折后正反都是正的.
"""

import collections
import json
import math
import re
import unicodedata

import atomicfile
import cart_label as cl
import tspl_preview as tp

D = cl.D
_, PAGE_H = tp.parse(cl.to_tspl(cl.HEAD + cl.page([])).encode())[0]["size"]

MARGIN = D // 2                 # 旗子边 / 纸边留白 (点)
FOLD_GAP = D // 2               # 版面离折痕的距离 (点)
FOLD_WARN = D // 4              # 墨离折痕比这还近就提示
CANVAS_WB = (int((cl.FR - cl.FL) * D) - 2 * MARGIN) // 8        # 位图宽 (字节), 整字节才能按位翻转
CANVAS_W = CANVAS_WB * 8
MIN_PX, MAX_PX, PX_STEP = 16, 64, 2     # 字号 (点); 16 点 = 2mm, 再小现场看不清
LINE_GAP = 4
MAX_LINES, MAX_LINE_CHARS = 2, 40
MAX_COPIES = 30                 # 同 MCP 单次上限

CONF_PATH = "/etc/ops-cart/label.json"
PAPERS = {"cable": "旗形线缆标签"}
PAPER = "cable"
FOLD_MIN_MM, FOLD_MAX_MM = 5.0, 20.0
FOLD_DEFAULT_MM = cl.CY + cl.Y0         # 没校准时和 cart_label 现有行为一样
RECENT_MAX = 6

PREVIEW_X0 = int(cl.FL * D)     # 预览裁出的区域: 旗子左右边, 整张纸高
PREVIEW_W = int(cl.FR * D) - PREVIEW_X0

Ink = collections.namedtuple("Ink", "w h px")                   # px: w*h 字节, 1 = 有墨
Fit = collections.namedtuple("Fit", "px lines inks box_h")
Placed = collections.namedtuple("Placed", "x y ink")
Plan = collections.namedtuple("Plan", "items fold_mm fold px lines")


class LabelError(ValueError):
    """给人看的错误: 界面原样显示."""


def dots(mm):
    return int(round(mm * D))


# ---------------------------------------------------------------- 输入

def _has_control(s):
    return any(unicodedata.category(c).startswith("C") for c in s)


def clean_lines(raw):
    """界面两个输入框 -> 要打的行 (去空白、去空行). 控制字符一律拒: 不进版面, 也不留注入的念想."""
    lines = []
    for s in raw:
        if _has_control(s):
            raise LabelError("文字里有控制字符, 重新输入")
        s = s.strip()
        if s:
            lines.append(s)
    if not lines:
        raise LabelError("先输入要打的文字")
    if len(lines) > MAX_LINES:
        raise LabelError("最多两行")
    for s in lines:
        if len(s) > MAX_LINE_CHARS:
            raise LabelError(f"一行太长 ({len(s)} 字), 最多 {MAX_LINE_CHARS} 字")
    return lines


def with_to(s):
    """"to XX" 快捷填: 给第一行加前缀, 已经有了不重复."""
    s = s.strip()
    return s if s.lower().startswith("to ") else f"to {s}" if s else "to "


# ---------------------------------------------------------------- 换行和字号

def split_line(s):
    """一行拆两行: 有空格挑最靠中间的空格, 没有就从中间切. 太短拆不了回 None."""
    if len(s) < 2:
        return None
    spaces = [i for i, c in enumerate(s) if c == " "]
    i = min(spaces, key=lambda k: abs(k - len(s) / 2)) if spaces else len(s) // 2
    return [s[:i].strip(), s[i:].strip()]


def _fits(inks, box_w, box_h):
    return all(i.w <= box_w for i in inks) and sum(i.h for i in inks) + LINE_GAP * (len(inks) - 1) <= box_h


def fit_lines(lines, render, box_w, box_h):
    """找放得下的最大字号. render(文字, px) -> 紧贴墨迹的 Ink. 只有一行时, 拆成两行字更大就拆."""
    tries = [list(lines)]
    if len(lines) == 1 and split_line(lines[0]):
        tries.append(split_line(lines[0]))
    for px in range(MAX_PX, MIN_PX - 1, -PX_STEP):
        for cand in tries:
            inks = [render(s, px) for s in cand]
            if not all(i.w and i.h for i in inks):
                raise LabelError("这行没有可打印的字形")
            if _fits(inks, box_w, box_h):
                return Fit(px, cand, inks, box_h)
    raise LabelError(f"字太多, 最小字号 ({MIN_PX} 点) 也放不下; 减几个字或分两行")


# ---------------------------------------------------------------- 版面

def guides(fold_mm):
    """旗子轮廓和折痕 (点), 格式同 tspl_preview.cable_guides(), 折痕用校准值."""
    top, fold = (fold_mm - cl.CY) * D, fold_mm * D
    return {"flag": (cl.FL * D, top, cl.FR * D, 2 * fold - top), "fold": fold, "cx": cl.CX * D}


def face_box(fold):
    """折痕以上的版面 -> (顶, 高), 点. 顶不出旗子和纸, 下半份转下来也不出纸; 太矮说明校准值不对."""
    cy = int(cl.CY * D)
    top = max(max(fold - cy, 0) + MARGIN, 2 * fold - (PAGE_H - MARGIN))
    h = fold - FOLD_GAP - top
    if h < MIN_PX:
        raise LabelError(f"折痕读数 {fold / D:g} mm 放不下字, 请重新校准")
    return top, h


def compose(fit):
    """画到 CANVAS_W x box_h 的整块画布: 每行水平居中, 整块垂直居中."""
    w, h = CANVAS_W, fit.box_h
    out = bytearray(w * h)
    y = (h - sum(i.h for i in fit.inks) - LINE_GAP * (len(fit.inks) - 1)) // 2
    for ink in fit.inks:
        x = (w - ink.w) // 2
        for row in range(ink.h):
            out[(y + row) * w + x:(y + row) * w + x + ink.w] = ink.px[row * ink.w:(row + 1) * ink.w]
        y += ink.h + LINE_GAP
    return Ink(w, h, bytes(out))


def rotate180(ink):
    return Ink(ink.w, ink.h, ink.px[::-1])


def build_plan(fit, fold_mm):
    """上半份 + 绕 (旗子中心, 折痕) 转 180° 的下半份."""
    fold, cx = dots(fold_mm), round(cl.CX * D)
    top, _ = face_box(fold)
    canvas = compose(fit)
    x = cx - canvas.w // 2
    upper = Placed(x, top, canvas)
    lower = Placed(2 * cx - x - canvas.w, 2 * fold - top - canvas.h, rotate180(canvas))
    return Plan([upper, lower], fold_mm, fold, fit.px, fit.lines)


FOLD_WARNING = "字压到折痕了"


def _ink_rows(placed):
    """(有墨的最左列, 最右列, 最上行, 最下行), 绝对坐标; 没墨回 None."""
    ink = placed.ink
    rows = [y for y in range(ink.h) if any(ink.px[y * ink.w:(y + 1) * ink.w])]
    if not rows:
        return None
    cols = [i % ink.w for i, v in enumerate(ink.px) if v]
    return placed.x + min(cols), placed.x + max(cols), placed.y + rows[0], placed.y + rows[-1]


def problems(plan):
    """版面问题 (给人看, 空 = 没问题): 按实际墨迹和校准后的折痕算, 不看画布空白."""
    out = []
    x0, _, x1, _ = guides(plan.fold_mm)["flag"]
    for placed in plan.items:
        box = _ink_rows(placed)
        if box is None:
            continue
        left, right, top, bottom = box
        if left < x0 or right >= x1:
            out.append("字出了旗子左右边")
        if top < 0 or bottom >= PAGE_H:
            out.append("字出了纸")
        if top < plan.fold + FOLD_WARN and bottom >= plan.fold - FOLD_WARN:
            out.append(FOLD_WARNING)
    return list(dict.fromkeys(out))


def blocking(plan):
    """会打坏的问题: 出旗子、出纸. 压折痕只提示, 由人决定."""
    return [m for m in problems(plan) if m != FOLD_WARNING]


# ---------------------------------------------------------------- TSPL

def bitmap_bytes(ink):
    """Ink -> BITMAP 数据: 每行整字节, 高位在左, 位 0 = 打印."""
    if ink.w % 8:
        raise ValueError("位图宽要是 8 的倍数")
    out = bytearray()
    for y in range(ink.h):
        row = ink.px[y * ink.w:(y + 1) * ink.w]
        for b in range(0, ink.w, 8):
            v = 0
            for bit in row[b:b + 8]:
                v = (v << 1) | (0 if bit else 1)
            out.append(v)
    return bytes(out)


def tspl_job(plan, copies=1):
    """整个打印任务. 命令文本只有数字, 文字全在位图里."""
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= MAX_COPIES:
        raise LabelError(f"份数要 1~{MAX_COPIES}")
    job = bytearray(cl.to_tspl(cl.HEAD + ["CLS"]).encode())
    for it in plan.items:
        job += f"BITMAP {it.x},{it.y},{it.ink.w // 8},{it.ink.h},0,".encode() + bitmap_bytes(it.ink) + b"\r\n"
    job += f"PRINT {copies},1\r\n".encode()
    return bytes(job)


def feed_job():
    """换纸后走一张空白, 第一张错位的毛病让空白纸吃掉."""
    return cl.to_tspl(cl.HEAD + cl.page([])).encode()


def ruler_job():
    return cl.to_tspl(cl.ruler_labels(1)).encode()


# ---------------------------------------------------------------- 校准读写

def default_conf():
    return {"schema": 1, "papers": {}, "recent_to": []}


def _fold_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and FOLD_MIN_MM <= v <= FOLD_MAX_MM


def load_conf(path=CONF_PATH):
    """没文件或读不懂都回默认: 校准坏了不能挡打印 (面板显示当前值, 人看得见)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return default_conf()
    if not isinstance(raw, dict) or not isinstance(raw.get("papers"), dict):
        return default_conf()
    conf = default_conf()
    conf["papers"] = {k: {"fold_mm": float(v["fold_mm"])} for k, v in raw["papers"].items()
                      if k in PAPERS and isinstance(v, dict) and _fold_ok(v.get("fold_mm"))}
    recent = raw.get("recent_to")
    if isinstance(recent, list):
        conf["recent_to"] = [s for s in recent if isinstance(s, str) and s][:RECENT_MAX]
    return conf


def save_conf(conf, path=CONF_PATH):
    atomicfile.write_json(path, conf, mode=0o644, indent=2)


def fold_mm(conf, paper=PAPER):
    p = conf["papers"].get(paper)
    return p["fold_mm"] if p else FOLD_DEFAULT_MM


def set_fold(conf, paper, mm):
    if paper not in PAPERS:
        raise LabelError(f"不认识的纸型: {paper}")
    return dict(conf, papers=dict(conf["papers"], **{paper: {"fold_mm": float(mm)}}))


def parse_fold(text):
    """刻度尺上读到的折痕位置 (mm); 中文输入习惯的逗号也当小数点."""
    try:
        v = float(text.strip().replace(",", "."))
    except ValueError:
        v = math.nan
    if not (math.isfinite(v) and FOLD_MIN_MM <= v <= FOLD_MAX_MM):
        raise LabelError(f"读数要在 {FOLD_MIN_MM:g}~{FOLD_MAX_MM:g} mm 之间")
    return v


def remember_to(conf, lines):
    """打成功的 "to XX" 记进常用, 最新的放最前. 没有 to 行就原样返回."""
    fresh = [s for s in lines if re.match(r"(?i)to\s", s)]
    if not fresh:
        return conf
    recent = fresh + [s for s in conf["recent_to"] if s not in fresh]
    return dict(conf, recent_to=recent[:RECENT_MAX])


# ---------------------------------------------------------------- 状态 / 预览

STATUS_NAMES = {"开盖": "打印头或上盖开着"}


def status_view(st):
    """mcp_label.decode_status 的结果 -> (一行字, 能不能打)."""
    if not st["flags"]:
        return "打印机就绪", True
    return "打印机: " + " / ".join(STATUS_NAMES.get(f, f) for f in st["flags"]), bool(st["ok"])


def preview_pgm(plan):
    """裁出旗子那一条的 P5 灰度图 (白纸黑字) -> (字节, 宽, 高). 折痕和轮廓由界面叠加."""
    w, h = PREVIEW_W, PAGE_H
    out = bytearray(b"\xff" * (w * h))
    for it in plan.items:
        for y in range(it.ink.h):
            for x in range(it.ink.w):
                px, py = it.x + x - PREVIEW_X0, it.y + y
                if it.ink.px[y * it.ink.w + x] and 0 <= px < w and 0 <= py < h:
                    out[py * w + px] = 0
    return f"P5 {w} {h} 255\n".encode() + bytes(out), w, h
