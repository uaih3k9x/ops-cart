#!/usr/bin/env python3
"""cart_ocr: 推车的"眼睛" -- 截图 OCR, 带每行坐标(给键鼠自动化定位用).

  python3 cart_ocr.py shot.png                  # 打印每行: 坐标 置信度 文字
  python3 cart_ocr.py shot.png --find "Boot Manager"
  python3 cart_ocr.py shot.png --lang eng+chi_sim

依赖: tesseract(+ chi_sim 语言包) 和 ImageMagick convert. 预处理 = 放大 + 灰度,
BIOS 的小点阵字放大后识别明显变好; 放大倍数按图宽自适应 (放大到 3000 像素上下, 1~4 倍).
退出码: 0 正常; 1 --find 没找到; 2 出错 (工具缺失 / 超时 / 识别失败).
"""

import argparse
import re
import struct
import subprocess
import sys

SCALE = 3                   # 读不出图宽时的放大倍数
TARGET_W = 3072             # 放大后的目标宽度
CONVERT_TIMEOUT = 30
TESSERACT_TIMEOUT = 90
GAP = 3          # 同一行里词间空隙 > 3 倍字高就切成两栏(BIOS 左菜单 / 中间取值 / 右说明)


def parse_tsv(tsv, scale=1):
    """tesseract tsv -> [{text, x, y, w, h, conf}], 按行聚合, 坐标除以 scale 还原."""
    groups = {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        key = (f[2], f[3], f[4])
        left, top, w, h, conf = int(f[6]), int(f[7]), int(f[8]), int(f[9]), float(f[10])
        groups.setdefault(key, []).append((left, top, left + w, top + h, conf, f[11]))
    segments = []
    for words in groups.values():
        words.sort()
        seg = [words[0]]
        for w in words[1:]:
            if w[0] - seg[-1][2] > GAP * (seg[-1][3] - seg[-1][1]):
                segments.append(seg)
                seg = []
            seg.append(w)
        segments.append(seg)
    lines = []
    for words in segments:
        x0 = min(w[0] for w in words); y0 = min(w[1] for w in words)
        x1 = max(w[2] for w in words); y1 = max(w[3] for w in words)
        lines.append({"text": " ".join(w[5] for w in words),
                      "x": int(x0 / scale), "y": int(y0 / scale),
                      "w": int((x1 - x0) / scale), "h": int((y1 - y0) / scale),
                      "conf": sum(w[4] for w in words) / len(words)})
    return lines            # tesseract 的行序, 同一行内从左到右


def _norm(s):
    return re.sub(r"\s+", "", s).lower()


def find_text(lines, needle):
    """第一行包含 needle 的(忽略大小写和空格, OCR 常把 Continue 认成 Cont inue)."""
    n = _norm(needle)
    return next((l for l in lines if n in _norm(l["text"])), None)


def image_width(path):
    """PNG 头里的宽度 (cart_snap 出的都是 PNG); 别的格式返回 None."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">I", head[16:20])[0]


def auto_scale(width):
    """小图多放大, 大图少放大: 1024 宽的 BIOS 截图放 3 倍, 1080p 放 2 倍, 4K 不放."""
    if not width:
        return SCALE
    return max(1, min(4, int(round(TARGET_W / width))))


def ocr_image(path, lang="eng", scale=None, psm=6):
    scale = scale or auto_scale(image_width(path))
    pre = subprocess.run(["convert", path, "-resize", f"{scale * 100}%", "-colorspace", "Gray", "png:-"],
                         capture_output=True, check=True, timeout=CONVERT_TIMEOUT).stdout
    tsv = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", str(psm), "tsv"],
                         input=pre, capture_output=True, check=True, timeout=TESSERACT_TIMEOUT).stdout.decode()
    return parse_tsv(tsv, scale=scale)


def main(argv=None):
    ap = argparse.ArgumentParser(description="推车截图 OCR")
    ap.add_argument("image")
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--find", help="只找这行, 打印坐标; 找不到退出码 1")
    args = ap.parse_args(argv)
    try:
        lines = ocr_image(args.image, lang=args.lang)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace").strip()
        print(f"OCR 失败: {e.cmd[0]} 退出码 {e.returncode} {err}".strip(), file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as e:
        print(f"OCR 失败: {e.cmd[0]} 超时 ({e.timeout} 秒)", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"OCR 失败: {e} (装了 tesseract 和 ImageMagick 吗?)", file=sys.stderr)
        return 2
    if args.find:
        hit = find_text(lines, args.find)
        if not hit:
            print(f"没找到: {args.find}", file=sys.stderr)
            return 1
        lines = [hit]
    for l in lines:
        print(f"({l['x']:>4},{l['y']:>4}) {l['w']:>4}x{l['h']:<3} {l['conf']:5.1f}  {l['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
