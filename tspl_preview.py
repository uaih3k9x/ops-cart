#!/usr/bin/env python3
"""TSPL 预览: 不在打印机边上时把 TSPL 画成 SVG/HTML, 顺便查出界和对折对称. 开发机工具, 只用标准库.

只认 cart_label 用到的那几条: SIZE / CLS / TEXT / DMATRIX / BAR / BITMAP / PRINT, 其余设置类指令忽略.
坐标就是图案坐标 (点, 203 dpi = 8 点/mm), SHIFT 不画: 旗子位置 (cart_label.FL/FR) 是带着 SHIFT 实测的.

画得出: 位置、字框大小、180° 对折后正反面. 画不出: 机内字体的真实字形 (用等宽字近似, 宽高按字体表),
Data Matrix 的真实码点 (只画同尺寸的示意图案, 扫不出来), 走纸漂移和固件怪癖. 定稿仍要实机打一张.

用法:
  tspl_preview.py 标签.tspl -o 预览.html                  # 任意 TSPL
  tspl_preview.py 标签.tspl -o 预览.html --paper cable    # 叠旗子轮廓、折痕, 画对折后正反面, 查出界和对称
退出码: 0 正常 / 1 有出界或对折不对称 (预览照样写出) / 2 读不了
"""

import argparse
import csv
import hashlib
import sys
from xml.sax.saxutils import escape

D = 8                                                    # 点/mm
FONTS = {"1": (8, 12), "2": (12, 20), "3": (16, 24), "4": (24, 32), "5": (32, 48)}   # 宽, 高 (点)
SVG_NS = "http://www.w3.org/2000/svg"


# ---------------------------------------------------------------- 解析

def _args(rest):
    """TSPL 参数: 逗号分隔, 双引号里的逗号不算."""
    return [a.strip() for a in next(csv.reader([rest], skipinitialspace=True))]


def _mm(s):
    return int(float(s.replace("mm", "").strip()) * D)


def parse(data):
    """TSPL 字节 -> [{"size": (宽点, 高点), "copies": n, "items": [...]}], 每个 PRINT 出一张."""
    labels, size, items, i, n = [], (0, 0), [], 0, len(data)
    while i < n:
        j = data.find(b"\n", i)
        j = n if j < 0 else j
        line = data[i:j].rstrip(b"\r")
        if line.startswith(b"BITMAP "):                  # 头 5 个参数之后是 宽字节 x 高 的原始位图
            head = line[7:].split(b",", 5)
            x, y, wb, h, _mode = (int(v) for v in head[:5])
            start = i + 7 + sum(len(v) + 1 for v in head[:5])
            items.append({"kind": "bitmap", "x": x, "y": y, "wb": wb, "h": h,
                          "data": data[start:start + wb * h]})
            j = data.find(b"\n", start + wb * h)
            i = n if j < 0 else j + 1
            continue
        i = j + 1
        text = line.decode("ascii", "replace").strip()
        if not text:
            continue
        cmd, _, rest = text.partition(" ")
        if cmd == "SIZE":
            w, h = rest.split(",")
            size = (_mm(w), _mm(h))
        elif cmd == "CLS":
            items = []
        elif cmd == "TEXT":
            x, y, font, rot, xm, ym, s = _args(rest)
            items.append({"kind": "text", "x": int(x), "y": int(y), "font": font, "rot": int(rot),
                          "xm": int(xm), "ym": int(ym), "s": s})
        elif cmd == "DMATRIX":
            a = _args(rest)
            items.append({"kind": "dmatrix", "x": int(a[0]), "y": int(a[1]), "w": int(a[2]), "h": int(a[3]),
                          "s": a[-1]})
        elif cmd == "BAR":
            x, y, w, h = (int(v) for v in _args(rest))
            items.append({"kind": "bar", "x": x, "y": y, "w": w, "h": h})
        elif cmd == "PRINT":
            labels.append({"size": size, "copies": int(_args(rest)[0]), "items": items})
            items = []
    return labels


# ---------------------------------------------------------------- 几何

def box(it):
    """占位框 (x0, y0, x1, y1), 点. TEXT 旋转绕锚点: 180° 时从锚点往左、往上长."""
    k = it["kind"]
    if k == "text":
        fw, fh = FONTS.get(it["font"], FONTS["1"])
        w, h = fw * it["xm"] * len(it["s"]), fh * it["ym"]
        x, y, r = it["x"], it["y"], it["rot"] % 360
        return {0: (x, y, x + w, y + h), 90: (x - h, y, x, y + w),
                180: (x - w, y - h, x, y), 270: (x, y - w, x + h, y)}[r]
    if k == "bitmap":
        return it["x"], it["y"], it["x"] + it["wb"] * 8, it["y"] + it["h"]
    return it["x"], it["y"], it["x"] + it["w"], it["y"] + it["h"]


def outside(label, area):
    """没完全落在 area (x0, y0, x1, y1) 里的项."""
    ax0, ay0, ax1, ay1 = area
    return [it for it in label["items"]
            if (lambda b: b[0] < ax0 or b[1] < ay0 or b[2] > ax1 or b[3] > ay1)(box(it))]


def _mirror(b, cx, cy):
    x0, y0, x1, y1 = b
    return 2 * cx - x1, 2 * cy - y1, 2 * cx - x0, 2 * cy - y0


def fold_mismatches(label, cx, cy, tol=1):
    """对折检查: 每个 TEXT/DMATRIX 都要有绕 (cx, cy) 转 180° 的同内容搭档. 返回落单的项."""
    pool = [it for it in label["items"] if it["kind"] in ("text", "dmatrix")]
    left = []
    for it in pool:
        want = _mirror(box(it), cx, cy)
        ok = any(o is not it and o["kind"] == it["kind"] and o["s"] == it["s"]
                 and (it["kind"] == "dmatrix" or (o["rot"] - it["rot"]) % 360 == 180)
                 and all(abs(a - b) <= tol for a, b in zip(box(o), want)) for o in pool)
        if not ok:
            left.append(it)
    return left


def cable_guides():
    """旗形线缆标签的参考线 (来自 cart_label 的校准常量), 点."""
    import cart_label as cl
    top, fold = cl.Y0 * D, (cl.CY + cl.Y0) * D
    return {"flag": (cl.FL * D, top, cl.FR * D, 2 * fold - top), "fold": fold, "cx": cl.CX * D}


# ---------------------------------------------------------------- 出图

def _dm_cells(s, n=12):
    """Data Matrix 示意: 左边、下边实线, 上边、右边虚线, 里面按内容哈希填. 不是真码."""
    bits = int(hashlib.sha256(s.encode()).hexdigest(), 16)
    cells = []
    for r in range(n):
        for c in range(n):
            if c == 0 or r == n - 1:
                on = True
            elif r == 0:
                on = c % 2 == 0
            elif c == n - 1:
                on = r % 2 == 1
            else:
                on = (bits >> ((r * n + c) % 256)) & 1
            if on:
                cells.append((r, c))
    return cells


def _item_svg(it):
    k = it["kind"]
    if k == "bar":
        return f'<rect x="{it["x"]}" y="{it["y"]}" width="{it["w"]}" height="{it["h"]}" fill="#000"/>'
    if k == "dmatrix":
        n = 12
        m = it["w"] / n
        d = "".join(f'M{it["x"] + c * m:g} {it["y"] + r * m:g}h{m:g}v{m:g}h-{m:g}z' for r, c in _dm_cells(it["s"], n))
        return f'<path d="{d}" fill="#000"><title>DMATRIX 示意 (不是真码): {escape(it["s"])}</title></path>'
    if k == "bitmap":                                   # 位 0 = 打印; 每行连续的 0 合成一段
        d, wb = [], it["wb"]
        for y in range(it["h"]):
            row, run = it["data"][y * wb:(y + 1) * wb], None
            for x in range(wb * 8 + 1):
                ink = x < wb * 8 and x // 8 < len(row) and not (row[x // 8] >> (7 - x % 8)) & 1
                if ink and run is None:
                    run = x
                elif not ink and run is not None:
                    d.append(f'M{it["x"] + run} {it["y"] + y}h{x - run}v1h-{x - run}z')
                    run = None
        return f'<path d="{"".join(d)}" fill="#000"/>' if d else ""
    fw, fh = FONTS.get(it["font"], FONTS["1"])
    w, h = fw * it["xm"] * len(it["s"]), fh * it["ym"]
    x, y = it["x"], it["y"]
    return (f'<g transform="rotate({it["rot"]} {x} {y})">'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#3b82f6" fill-opacity="0.12"/>'
            f'<text x="{x}" y="{y + h * 0.82:g}" font-family="Menlo, Consolas, monospace" font-size="{h * 0.95:g}" '
            f'textLength="{w}" lengthAdjust="spacingAndGlyphs" fill="#000">{escape(it["s"])}</text></g>')


def _content(label):
    return "".join(_item_svg(it) for it in label["items"])


def to_svg(label, guides=None, bad=()):
    """整张纸: 白底 + 内容; guides 给了就叠旗子轮廓和折痕; bad 里的项描红框."""
    w, h = label["size"]
    out = [f'<svg xmlns="{SVG_NS}" viewBox="0 0 {w} {h}" width="{w / D:g}mm" height="{h / D:g}mm" class="paper">',
           f'<rect width="{w}" height="{h}" fill="#fff" stroke="#999" stroke-width="1"/>']
    if guides:
        x0, y0, x1, y1 = guides["flag"]
        out.append(f'<rect x="{x0:g}" y="{y0:g}" width="{x1 - x0:g}" height="{y1 - y0:g}" fill="#fef3c7" '
                   f'stroke="#d97706" stroke-width="1.5" stroke-dasharray="6 4"/>')
        out.append(f'<line x1="{x0:g}" y1="{guides["fold"]:g}" x2="{x1:g}" y2="{guides["fold"]:g}" '
                   f'stroke="#dc2626" stroke-width="1" stroke-dasharray="3 3"/>')
    out.append(_content(label))
    for it in bad:
        bx0, by0, bx1, by1 = box(it)
        out.append(f'<rect x="{bx0}" y="{by0}" width="{bx1 - bx0}" height="{by1 - by0}" fill="none" '
                   f'stroke="#dc2626" stroke-width="2"/>')
    out.append("</svg>")
    return "".join(out)


def to_svg_folded(label, guides):
    """对折后的两面: 正面 = 折痕以上; 背面 = 折痕以下绕 (cx, 折痕) 转 180° 翻上来. 两面应该一样."""
    x0, y0, x1, _ = guides["flag"]
    fold, cx = guides["fold"], guides["cx"]
    w, h = x1 - x0, fold - y0
    body = _content(label)

    def face(inner, name):
        return (f'<figure><svg xmlns="{SVG_NS}" viewBox="{x0:g} {y0:g} {w:g} {h:g}" width="{w / D:g}mm" '
                f'height="{h / D:g}mm" class="paper"><rect x="{x0:g}" y="{y0:g}" width="{w:g}" height="{h:g}" '
                f'fill="#fff" stroke="#d97706" stroke-width="1.5"/>{inner}</svg><figcaption>{name}</figcaption></figure>')
    return (face(body, "正面 (折痕以上)")
            + face(f'<g transform="rotate(180 {cx:g} {fold:g})">{body}</g>', "背面 (翻过来看)"))


def _describe(it):
    if it["kind"] in ("text", "dmatrix"):
        return f'{it["kind"].upper()} "{it["s"]}" @ {it["x"]},{it["y"]}'
    return f'{it["kind"].upper()} @ {it["x"]},{it["y"]}'


def folded(label):
    """含 180° 文字 = 这张要对折 (刻度尺这类不对折)."""
    return any(it["kind"] == "text" and it["rot"] == 180 for it in label["items"])


def check_area(label, guides):
    """对折的张查整个设计区; 不对折的 (刻度尺, 读原始坐标) 只查左右没出旗子."""
    x0, y0, x1, y1 = guides["flag"]
    return (x0, y0, x1, y1) if folded(label) else (x0, 0, x1, label["size"][1])


def check(labels, guides):
    """[(第几张, 问题说明)]: 出界, 以及要对折的张查对称."""
    probs = []
    for k, lb in enumerate(labels, 1):
        for it in outside(lb, check_area(lb, guides)):
            probs.append((k, "出界: " + _describe(it)))
        if folded(lb):
            for it in fold_mismatches(lb, guides["cx"], guides["fold"]):
                probs.append((k, "对折不对称: " + _describe(it)))
    return probs


CSS = """
:root{--bg:#f6f7f9;--fg:#1f2430;--dim:#5b6475;--card:#fff;--line:#e3e6ec;--bad:#b91c1c;--ok:#15803d}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#11151c;--fg:#e6e9ef;--dim:#9aa3b5;
--card:#1a1f29;--line:#2a3140;--bad:#f87171;--ok:#4ade80}}
:root[data-theme="dark"]{--bg:#11151c;--fg:#e6e9ef;--dim:#9aa3b5;--card:#1a1f29;--line:#2a3140;--bad:#f87171;--ok:#4ade80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;padding:24px 16px}
main{max-width:960px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}p.note{color:var(--dim);margin:0 0 20px;font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
h2{font-size:15px;margin:0 0 10px}svg.paper{display:block;max-width:100%;height:auto}
.faces{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px}figure{margin:0;flex:1 1 220px;max-width:420px}
figure svg.paper{width:100%}figcaption{color:var(--dim);font-size:12px;margin-top:4px}
ul.probs{margin:8px 0 0;padding-left:18px;color:var(--bad);font-size:13px}.ok{color:var(--ok);font-size:13px;margin-top:8px}
"""


def to_html(labels, guides=None, title="TSPL 预览", note=""):
    probs = check(labels, guides) if guides else []
    parts = [f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>'
             f'<style>{CSS}</style></head><body><main><h1>{escape(title)}</h1>'
             f'<p class="note">{escape(note or "字形是等宽近似, Data Matrix 是示意图案; 位置和字框大小按 TSPL 坐标. 定稿仍要实机打一张.")}</p>']
    for k, lb in enumerate(labels, 1):
        mine = [m for n, m in probs if n == k]
        bad = outside(lb, check_area(lb, guides)) if guides else []
        parts.append(f'<section><h2>第 {k} 张</h2>{to_svg(lb, guides, bad)}')
        if guides and folded(lb):
            parts.append(f'<div class="faces">{to_svg_folded(lb, guides)}</div>')
        if mine:
            parts.append('<ul class="probs">' + "".join(f"<li>{escape(m)}</li>" for m in mine) + "</ul>")
        elif guides:
            parts.append('<div class="ok">没出界, 对折对称</div>')
        parts.append("</section>")
    parts.append("</main></body></html>")
    return "".join(parts), probs


def main(argv=None):
    ap = argparse.ArgumentParser(description="TSPL 预览 (SVG/HTML)")
    ap.add_argument("tspl")
    ap.add_argument("-o", "--out", required=True, help="输出 HTML")
    ap.add_argument("--paper", choices=["cable"], help="叠纸型参考线并检查")
    ap.add_argument("--title", default="TSPL 预览")
    a = ap.parse_args(argv)
    try:
        with open(a.tspl, "rb") as f:
            labels = parse(f.read())
    except (OSError, ValueError) as e:
        print(f"读不了: {e}", file=sys.stderr)
        return 2
    html, probs = to_html(labels, cable_guides() if a.paper == "cable" else None, a.title)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    for k, m in probs:
        print(f"第 {k} 张 {m}", file=sys.stderr)
    print(f"{len(labels)} 张 -> {a.out}", file=sys.stderr)
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
