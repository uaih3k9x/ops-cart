#!/usr/bin/env python3
"""RapidOCR 子进程 (#46): /opt/pxe/ocr-venv/bin/python -I /opt/pxe/ocr_rapid_worker.py 图.png
最后一行 stdout 打 JSON: [{text, x, y, w, h, conf(0-100)}], 同 cart_ocr 的格式. 只用 venv 里的包, 不 import 仓库模块.
限制推理线程, 不抢车机和副屏. 模型随包带, 不联网. 由 cart_ocr.ocr_rapid 调, 出错退出码非零, 调用方退回 Tesseract.
"""

import contextlib
import json
import sys

THREADS = 4


def to_row(box, text, conf):
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    x, y = int(min(xs)), int(min(ys))
    return {"text": text, "x": x, "y": y, "w": int(max(xs)) - x, "h": int(max(ys)) - y, "conf": round(float(conf) * 100, 1)}


def main(path):
    with contextlib.redirect_stdout(sys.stderr):        # 库的日志别混进结果
        from rapidocr_onnxruntime import RapidOCR
        result, _ = RapidOCR(intra_op_num_threads=THREADS, inter_op_num_threads=1)(path)
    print(json.dumps([to_row(*r) for r in result or []], ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
