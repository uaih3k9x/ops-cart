#!/bin/bash
# 开发机上打 1.16.00 部署包: scripts/pack-1.16.00.sh <输出目录>  -> <输出目录>/deploy-1.16.00/
# 只打 git ls-files 里的文件 (不 cp *.py, 免得混进没提交的); 工作区不干净就不打.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?用法: scripts/pack-1.16.00.sh <输出目录>}
if ! git diff --quiet HEAD -- || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "工作区有没提交的改动, 先提交" >&2; exit 1
fi
scripts/stamp-build.sh >/dev/null
grep -q -- '-dirty' BUILD && { echo "BUILD 带 -dirty" >&2; exit 1; }
python3 scripts/cart_accept.py --make-manifest scripts/accept-manifest.json
P=$OUT/deploy-1.16.00
rm -rf "$P" && mkdir -p "$P/opt" "$P/systemd" "$P/accept" "$P/tk-test"
# 顶层 *.py + VERSION + web/ icons/: 都从 git 的文件清单来
git ls-files -z -- ':(glob)*.py' VERSION web icons | COPYFILE_DISABLE=1 tar --null -T - -cf - | tar -C "$P/opt" -xf -
cp BUILD "$P/opt/"
for u in cart-mcp.service cart-mcp-ro.service cart-ble-input.service; do cp "deploy/systemd/$u" "$P/systemd/"; done
cp scripts/cart_accept.py scripts/accept_core.py scripts/accept-manifest.json "$P/accept/"
cp scripts/install-1.16.00.sh "$P/install.sh"
cp tests/conftest.py tests/test_confirm_panel.py "$P/tk-test/"
echo "打好: $P ($(cat BUILD))"
