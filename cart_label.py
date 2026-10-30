#!/usr/bin/env python3
"""推车 TSC TTP-244 Pro 标签机的 TSPL 生成器 (纯函数, 只出字节流, 不碰 /dev/usb/lp0).

两种纸:
- P 型旗形线缆标签 (25x38+40, 上下对折): 上半份正印, 下半份绕旗子中心旋转 180° 再印一份,
  对折后正反两面都是正的. 打印机内置字体只有 ASCII.
- 资产标签 (60x37.4 mm): PDF 在 Mac 上用 pdftoppm 渲成 203 dpi 灰度 PGM, 再转 BITMAP.

打印由调用方做: 把输出 `cat > /dev/usb/lp0` (要 sudo). 先打一张看, 没问题再批量.

用法 (输出 TSPL 到标准输出):
  cart_label.py text "LAN1" "CART END" "LAN2" "UPLINK"    # 大字/小字成对, 每对一张
  cart_label.py cable rows.json                           # [{"shortId":1,"here":"..","there":".."}]
  cart_label.py ruler 3                                   # 3 张纵向刻度尺, 调位置用
  cart_label.py asset 标签.pdf                            # 资产标签, 多页 PDF 每页一张 (要 pdftoppm)
"""

import json
import os
import re
import subprocess
import sys
import tempfile

# ---- 线缆旗形标签: 10-26 实机校准 (人工确认"齐了"), 10-30 加 Y0 ----
D = 8                                   # 203 dpi = 8 点/mm
FL, FR = 55.0, 90.5                     # 旗子在打印头上的左右边 (mm)
CX, CY = (FL + FR) / 2, 9.0             # 旗子中心横坐标; 10-26 校准时折痕在图案 y=9mm
Y0 = 5.0                                # 10-30 走纸漂移: 图案整体 (连折痕) 下移 5mm; 再偏改这里, 别动 SHIFT
HEAD = ["DENSITY 11", "SIZE 104 mm, 25 mm", "GAP 2.9 mm, 4.5 mm",
        "SHIFT 18,39", "DIRECTION 1", "REFERENCE 0,0"]
FONTS = {"1": (8, 12), "2": (12, 20), "3": (16, 24), "4": (24, 32)}   # 宽, 高 (点)
DM_X, DM_N = 5, 12                      # Data Matrix: 每码点 5 点, 12x12 = 7.5mm

# ---- 资产标签: 10-28 标准版面 (定为固定资产标签统一版面) ----
ASSET_LEFT, ASSET_WIDTH = 2 * D, 464    # 左裁 2mm, 可用宽 58mm
ASSET_LAST_UP = 2 * D                   # 最下面一块 (资产号) 上移 2mm
ASSET_MIN_GAP = D // 2                  # 上移后跟上一块少于 0.5mm 就不挪
ASSET_DOWN = 4                          # 整体下移 0.5mm (BITMAP y=4), SHIFT 不动
ASSET_MAX_ROWS = 299
ASSET_HEADER = "SIZE 60 mm, 37.4 mm\r\nGAP 2 mm, 0 mm\r\nDENSITY 15\r\nDIRECTION 1\r\nSHIFT 34,-76\r\n"

# ---- 缩写对照表的限长 (同 examples/label_abbrev.json 的 _limits) ----
LIMITS = {"cable_here_max": 23, "cable_there_max": 12, "tag_big_max": 11, "tag_small_max": 23}


# ======== 线缆旗形标签 ========

def short_id(n):
    """线缆短号: E- 前缀, 不足 5 位补零."""
    return "E-" + str(n).zfill(5)


def ascii_only(s):
    if any(not (32 <= ord(c) < 127) for c in s):
        raise ValueError(f"打印机只认 ASCII: {s!r}")
    return s


def text_pair(s, font, x_mm, y_mm, center=False):
    """上半正印 + 下半旋转 180° 的一对 TEXT."""
    w, _ = FONTS[font]
    s = ascii_only(s)
    x = int(CX * D - w * len(s) / 2) if center else int(x_mm * D)
    if x + w * len(s) > FR * D or x < FL * D:
        raise ValueError(f"超出旗子宽度: {s!r}")
    y = int((y_mm + Y0) * D)
    return [f'TEXT {x},{y},"{font}",0,1,1,"{s}"',
            f'TEXT {int(2 * CX * D - x)},{int(2 * (CY + Y0) * D - y)},"{font}",180,1,1,"{s}"']


def dmatrix_pair(data, x_mm, y_mm):
    """Data Matrix 不分方向, 下半份只放到对称位置, 不旋转."""
    size = DM_X * DM_N
    x, y = int(x_mm * D), int((y_mm + Y0) * D)
    xr, yr = int(2 * CX * D - x - size), int(2 * (CY + Y0) * D - y - size)
    return [f'DMATRIX {x},{y},{size},{size},x{DM_X},{DM_N},{DM_N},"{data}"',
            f'DMATRIX {xr},{yr},{size},{size},x{DM_X},{DM_N},{DM_N},"{data}"']


def page(body):
    return ["CLS"] + body + ["PRINT 1,1"]


def text_labels(pairs):
    """[(大字, 小字)] -> TSPL 行; 大字 font4、小字 font2, 都居中."""
    out = list(HEAD)
    for big, small in pairs:
        out += page(text_pair(big, "4", 0, 0.8, center=True) + text_pair(small, "2", 0, 5.6, center=True))
    return out


def cable_labels(rows):
    """[{"shortId", "here", "there"}] -> TSPL 行.
    标签挂在本端, 人要看的是这根线去哪: 对端用 font3 大字 (最多 12 字), 本端小字前加 "@ " (最多 23 字)."""
    out = list(HEAD)
    for r in rows:
        sid = short_id(r["shortId"])
        body = dmatrix_pair(sid, FL + 1.5, 1.0)
        body += text_pair(sid, "2", FL + 10, 0.5)
        body += text_pair(r["there"], "3", FL + 10, 3.2)
        body += text_pair("@ " + r["here"], "1", FL + 10, 6.5)
        out += page(body)
    return out


def ruler_labels(n=2, x_mm=58, h_mm=22):
    """刻度尺不加 Y0: 读的是打印机原始坐标, 调位置时拿它量."""
    out = list(HEAD)
    for i in range(1, n + 1):
        body = [f"BAR {x_mm * D},0,2,{h_mm * D}"]
        for y in range(h_mm + 1):
            long_tick = y % 5 == 0 or y == h_mm
            body.append(f"BAR {x_mm * D},{min(y * D, h_mm * D - 2)},{(6 if long_tick else 3) * D},2")
            if long_tick:
                body.append(f'TEXT {(x_mm + 7) * D},{max(y * D - 6, 0)},"2",0,1,1,"{y}"')
        body.append(f'TEXT {(x_mm + 16) * D},{9 * D},"2",0,1,1,"#{i}"')
        out += page(body)
    return out


def to_tspl(lines):
    """TSPL 行 -> 发给打印机的字符串 (CRLF 结尾)."""
    return "\r\n".join(lines) + "\r\n"


# ======== 资产标签 (PGM 位图 -> BITMAP) ========

def parse_pgm(data):
    """P5 灰度 PGM 字节 -> (宽, 高, 像素 bytearray). 头里允许 # 注释."""
    if data[:2] != b"P5":
        raise ValueError("不是 P5 灰度 PGM")
    fields, i, n = [], 0, len(data)
    while len(fields) < 4:                       # P5 w h maxval
        while i < n and data[i:i + 1].isspace():
            i += 1
        if i >= n:
            raise ValueError("PGM 头不完整")
        if data[i:i + 1] == b"#":
            i = data.find(b"\n", i)
            if i < 0:
                raise ValueError("PGM 头不完整")
            continue
        j = i
        while j < n and not data[j:j + 1].isspace():
            j += 1
        fields.append(data[i:j])
        i = j
    if i >= n:                                   # maxval 后面要有一个空白再接像素
        raise ValueError("PGM 头不完整")
    w, h = int(fields[1]), int(fields[2])
    px = bytearray(data[i + 1:i + 1 + w * h])
    if len(px) < w * h:
        raise ValueError(f"PGM 像素不够: 要 {w * h}, 只有 {len(px)}")
    return w, h, px


def asset_blocks(w, h, px):
    """按空白行切成一块块内容 [(起行, 止行)], 只看裁剪后的可用宽."""
    out, start = [], None
    for y in range(h + 1):
        ink = y < h and any(px[y * w + x] < 128 for x in range(ASSET_LEFT, min(w, ASSET_LEFT + ASSET_WIDTH)))
        if ink and start is None:
            start = y
        elif not ink and start is not None:
            out.append((start, y - 1))
            start = None
    return out


def asset_layout(w, h, px, warn=None):
    """最下面一块上移 2mm (原地改 px 并返回); 离上一块太近就不挪, 调 warn(提示)."""
    segs = asset_blocks(w, h, px)
    if len(segs) >= 2:
        (_, prev_end), (a, b) = segs[-2], segs[-1]
        if a - ASSET_LAST_UP - prev_end - 1 >= ASSET_MIN_GAP:
            rows = [bytes(px[y * w:(y + 1) * w]) for y in range(a, b + 1)]
            for y in range(a, b + 1):
                px[y * w:(y + 1) * w] = b"\xff" * w
            for k, row in enumerate(rows):
                px[(a - ASSET_LAST_UP + k) * w:(a - ASSET_LAST_UP + k + 1) * w] = row
        elif warn:
            warn(f"最后一块离上一块太近, 没上移 (rows {a}-{b})")
    return px


def asset_page(w, h, px):
    """一页 -> CLS + BITMAP + PRINT. 阈值 128, 位 0=打印 1=不打, 超出图宽补白."""
    rows = min(h, ASSET_MAX_ROWS) - ASSET_DOWN
    wb = ASSET_WIDTH // 8
    out = bytearray()
    for y in range(rows):
        for xb in range(wb):
            v = 0
            for k in range(8):
                x = ASSET_LEFT + xb * 8 + k
                v = (v << 1) | (1 if x >= w or px[y * w + x] >= 128 else 0)
            out.append(v)
    return b"CLS\r\n" + f"BITMAP 0,{ASSET_DOWN},{wb},{rows},0,".encode() + bytes(out) + b"\r\nPRINT 1\r\n"


def asset_job(pgms, warn=None):
    """多页 PGM 字节 -> 整个任务 (头部 + 每页一张)."""
    job = ASSET_HEADER.encode()
    for data in pgms:
        w, h, px = parse_pgm(data)
        job += asset_page(w, h, asset_layout(w, h, px, warn))
    return job


def pdf_to_pgms(pdf):
    """pdftoppm 203 dpi 灰度渲染, 返回每页 PGM 字节 (按页序)."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "203", "-gray", pdf, os.path.join(tmp, "p")], check=True)
        names = sorted(f for f in os.listdir(tmp) if f.endswith(".pgm"))
        pages = []
        for f in names:
            with open(os.path.join(tmp, f), "rb") as fh:
                pages.append(fh.read())
        return pages


# ======== 缩写对照表 ========

def check_text(s, kind, limits=None):
    """按 kind (cable_here / cable_there / tag_big / tag_small) 查 ASCII 和长度, 合格原样返回."""
    limits = limits or LIMITS
    key = kind + "_max"
    if key not in limits:
        raise ValueError(f"不认识的标签种类: {kind!r}")
    ascii_only(s)
    if len(s) > limits[key]:
        raise ValueError(f"超长: {s!r} {len(s)} 字符, {kind} 最多 {limits[key]}")
    return s


def _flat(table):
    """对照表各类合成一张 {原文: 缩写}, 跳过 _ 开头的说明项."""
    out = {}
    for cat, m in table.items():
        if not cat.startswith("_"):
            out.update(m)
    return out


def validate_abbrev(table):
    """对照表自查, 返回问题清单 (空=没问题): 值要 ASCII 且不超过最短限长, 同键不许两种写法."""
    limits = table.get("_limits") or LIMITS
    shortest = min(limits.values())
    probs, seen = [], {}
    for cat, m in table.items():
        if cat.startswith("_"):
            continue
        for k, v in m.items():
            if any(not (32 <= ord(c) < 127) for c in v):
                probs.append(f"{cat}.{k}: 值不是 ASCII: {v!r}")
            elif len(v) > shortest:
                probs.append(f"{cat}.{k}: 值超过 {shortest} 字符: {v!r}")
            if seen.setdefault(k, v) != v:
                probs.append(f"{cat}.{k}: 和别处写法不一致 ({seen[k]!r} / {v!r})")
    return probs


def load_abbrev(path):
    """读对照表 JSON; 表本身有错就抛 ValueError."""
    with open(path, encoding="utf-8") as f:
        table = json.load(f)
    probs = validate_abbrev(table)
    if probs:
        raise ValueError("缩写对照表有错: " + "; ".join(probs))
    return table


def abbreviate(s, table):
    """按对照表替换 (长的词优先, 一趟扫完, 替换结果不再被二次替换), 再合并多余空白."""
    m = _flat(table)
    if m:
        pat = re.compile("|".join(re.escape(k) for k in sorted(m, key=len, reverse=True)))
        s = pat.sub(lambda mo: m[mo.group(0)], s)
    return " ".join(s.split())


def abbreviate_checked(s, kind, table):
    """缩写后再按 kind 校验; 还超长或还有中文就抛 ValueError, 交给人改."""
    return check_text(abbreviate(s, table), kind, table.get("_limits"))


# ======== 命令行 ========

def _render(argv):
    if not argv:
        raise ValueError(__doc__)
    mode, args = argv[0], argv[1:]
    if mode == "text":
        if len(args) % 2:
            raise ValueError("text 要成对给: 大字 小字 ...")
        return to_tspl(text_labels(list(zip(args[::2], args[1:][::2])))).encode()
    if mode == "cable":
        if not args:
            raise ValueError("cable 要给 JSON 文件")
        with open(args[0], encoding="utf-8") as f:
            return to_tspl(cable_labels(json.load(f))).encode()
    if mode == "ruler":
        return to_tspl(ruler_labels(int(args[0]) if args else 2)).encode()
    if mode == "asset" or mode.lower().endswith(".pdf"):     # 兼容 tsc_asset_label.py 标签.pdf
        pdf = args[0] if mode == "asset" and args else mode
        if not pdf.lower().endswith(".pdf"):
            raise ValueError("asset 要给 PDF 文件")
        pages = pdf_to_pgms(pdf)
        job = asset_job(pages, warn=lambda m: print(m, file=sys.stderr))
        print(f"{len(pages)} 张", file=sys.stderr)
        return job
    raise ValueError(__doc__)


def main(argv=None):
    try:
        data = _render(sys.argv[1:] if argv is None else argv)
    except KeyError as e:
        print(f"cable 每行要有 shortId/here/there, 缺 {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(e.args[0] if isinstance(e, ValueError) and e.args else f"出错: {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
