#!/bin/bash
# 开发机上打 1.16.01 部署包: scripts/pack-1.16.01.sh <输出目录>  -> <输出目录>/deploy-1.16.01/
# 照 pack-1.16.00.sh: 只打 git ls-files 里的文件 (不 cp *.py, 免得混进没提交的); 工作区不干净就不打.
# 这版多带 #95 的 pxe-server.service、logrotate 配置, Tk 测试多一个大肥鱼面板 (smb 这版直接停用, 不带 drop-in / 钩子).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?用法: scripts/pack-1.16.01.sh <输出目录>}
if ! git diff --quiet HEAD -- || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "工作区有没提交的改动, 先提交" >&2; exit 1
fi
scripts/stamp-build.sh >/dev/null
grep -q -- '-dirty' BUILD && { echo "BUILD 带 -dirty" >&2; exit 1; }
python3 scripts/cart_accept.py --make-manifest scripts/accept-manifest.json
P=$OUT/deploy-1.16.01
rm -rf "$P" && mkdir -p "$P/opt" "$P/systemd" "$P/etc/logrotate.d" "$P/accept" "$P/tk-test"
# 顶层 *.py + VERSION + web/ icons/: 都从 git 的文件清单来
git ls-files -z -- ':(glob)*.py' VERSION web icons | COPYFILE_DISABLE=1 tar --null -T - -cf - | tar -C "$P/opt" -xf -
cp BUILD "$P/opt/"
cp deploy/systemd/pxe-server.service "$P/systemd/"
cp deploy/logrotate.d/ops-cart "$P/etc/logrotate.d/"
cp scripts/cart_accept.py scripts/accept_core.py scripts/accept-manifest.json "$P/accept/"
cp scripts/install-1.16.01.sh "$P/install.sh"
cp tests/conftest.py tests/test_confirm_panel.py tests/test_bigfish_panel.py "$P/tk-test/"
echo "打好: $P ($(cat BUILD))"
