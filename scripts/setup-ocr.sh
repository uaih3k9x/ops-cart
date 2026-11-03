#!/bin/bash
# 推车 RapidOCR (#46): 独立 venv, 不碰系统 Python. root 运行, 幂等. 不装也行, screen_ocr 退回 Tesseract.
# 用法: sudo bash setup-ocr.sh <ocr-requirements.txt> [pip 镜像地址]
# 用到 /opt/pxe/ocr_rapid_worker.py (先部署代码).
set -euo pipefail
REQ=${1:?要 ocr-requirements.txt 的路径}
INDEX=${2:-https://pypi.tuna.tsinghua.edu.cn/simple}
V=/opt/pxe/ocr-venv
[ -x "$V/bin/python" ] || python3 -m venv "$V"
"$V/bin/pip" install --no-input --timeout 20 --retries 2 --disable-pip-version-check -i "$INDEX" -r "$REQ"
"$V/bin/python" -I -c 'import rapidocr_onnxruntime, importlib.metadata as m; print("rapidocr", m.version("rapidocr-onnxruntime"))'
