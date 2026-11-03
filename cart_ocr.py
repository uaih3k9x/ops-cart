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
import json
import os
import re
import socket
import struct
import subprocess
import sys

SCALE = 3                   # 读不出图宽时的放大倍数
TARGET_W = 3072             # 放大后的目标宽度
CONVERT_TIMEOUT = 30
TESSERACT_TIMEOUT = 90
RAPID_SOCK = "/run/ops-ocr/ocr.sock"        # 常驻服务 cart-ocr (#47)
RAPID_TIMEOUT = 30
ENGINE = "rapidocr-openvino"
RAPID_MIN_CONF = 60.0       # RapidOCR 的置信度偏高, 低于这个基本是图标被当成字
RAPID_SYMBOL_CONF = 90.0    # 孤零零一个符号要更有把握才留
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


def parse_rapid(line):
    """服务回的一行 -> 行列表. 服务报错或形状不对抛 ValueError."""
    doc = json.loads(line or "")
    if not isinstance(doc, dict):
        raise ValueError("OCR 服务回复格式不对")
    if "error" in doc:
        raise ValueError(str(doc["error"]))
    rows, keys = doc.get("rows"), {"text", "x", "y", "w", "h", "conf"}
    if not isinstance(rows, list) or not all(isinstance(r, dict) and keys <= set(r) and isinstance(r["text"], str)
                                             for r in rows):
        raise ValueError("OCR 服务回复格式不对")
    return rows


def drop_noise(rows):
    """图标常被认成低分的字或孤立符号."""
    def keep(r):
        t = r["text"].strip()
        if not t or r["conf"] < RAPID_MIN_CONF:
            return False
        return len(t) > 1 or t.isalnum() or r["conf"] >= RAPID_SYMBOL_CONF
    return [r for r in rows if keep(r)]


def ocr_rapid(path, sock_path=RAPID_SOCK):
    """把图片字节发给常驻服务 (root 的私有截图服务读不到). 连不上抛 OSError, 回复不对抛 ValueError."""
    with open(path, "rb") as f:
        data = f.read()
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(RAPID_TIMEOUT)
    try:
        s.connect(sock_path)
        s.sendall(struct.pack("!I", len(data)) + data)
        with s.makefile("rb") as r:
            line = r.readline().decode("utf-8", "replace")
    finally:
        s.close()
    return drop_noise(parse_rapid(line))


def ocr_best(path, lang="eng", rapid=ocr_rapid, tess=None):
    """-> (行, 引擎说明). 优先常驻服务; 没开 / 超时 / 出错退回 Tesseract 并写明原因. 配方不走这里, 仍用 ocr_image."""
    try:
        return rapid(path), ENGINE
    except (OSError, ValueError) as e:
        why = "服务没开" if isinstance(e, (FileNotFoundError, ConnectionRefusedError)) else type(e).__name__
        why = "timeout" if isinstance(e, socket.timeout) else why
    return (tess or (lambda p, lg: ocr_image(p, lang=lg)))(path, lang), f"tesseract (RapidOCR 不可用: {why})"


def main(argv=None):
    ap = argparse.ArgumentParser(description="推车截图 OCR")
    ap.add_argument("image")
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--find", help="只找这行, 打印坐标; 找不到退出码 1")
    ap.add_argument("--engine", choices=("auto", "tesseract"), default="auto",
                    help="auto = 优先常驻服务 RapidOCR (彩色桌面也能读), 没开退回 Tesseract")
    args = ap.parse_args(argv)
    try:
        if args.engine == "auto":
            lines, engine = ocr_best(args.image, args.lang)
            print(f"引擎: {engine}", file=sys.stderr)
        else:
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
