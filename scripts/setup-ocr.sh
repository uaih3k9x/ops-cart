#!/bin/bash
# 推车 OCR 常驻服务的 venv (#47): 独立环境, 不碰系统 Python. root 运行, 幂等. 不装也行, screen_ocr 退回 Tesseract.
# 用法: sudo bash setup-ocr.sh <ocr-requirements.txt> [pip 镜像地址]
# 装完: sudo systemctl enable --now cart-ocr
set -euo pipefail
REQ=${1:?要 ocr-requirements.txt 的路径}
INDEX=${2:-https://pypi.tuna.tsinghua.edu.cn/simple}
V=/opt/pxe/ocr-venv
if [ -x "$V/bin/python" ] && ! "$V/bin/python" -I -c 'import rapidocr_openvino' 2>/dev/null; then
    echo "==> 旧环境不是 OpenVINO 版, 重建"
    systemctl stop cart-ocr 2>/dev/null || true
    rm -rf "$V"
fi
[ -x "$V/bin/python" ] || python3 -m venv "$V"
"$V/bin/pip" install --no-input --timeout 20 --retries 2 --disable-pip-version-check -i "$INDEX" -r "$REQ"
"$V/bin/python" -I -c 'import rapidocr_openvino, importlib.metadata as m; print("rapidocr-openvino", m.version("rapidocr-openvino"))'
