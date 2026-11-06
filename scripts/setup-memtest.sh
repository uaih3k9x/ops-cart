#!/bin/bash
# Memtest86+ 放进 PXE 菜单 (#55). root 运行, 幂等. 要能上外网 (memtest.org); 不联网就先把官方包拷上来用 --zip.
# 用法: sudo bash setup-memtest.sh [--zip mt86plus_<版本>.binaries.zip]
# 版本、下载地址、sha256 都固定在 memtest_core.py 里; 校验不符就中止, 旧文件不动. 说明见 docs/MEMTEST.md
set -euo pipefail
TFTP=/opt/pxe/tftp
python3 /opt/pxe/memtest_core.py --root "$TFTP" "$@"
# 菜单项不用另写: 救援 grub.cfg 由车机 (kiosk 启动 / 上锁) 和 setup-cart.sh 生成, 已带 Memtest86+;
# BIOS 的 pxelinux.cfg/default 由 setup-cart.sh 里的 kiosk_core.py --write-rescue 写.
[ -f "$TFTP/pxelinux.0" ] && [ -f "$TFTP/ldlinux.c32" ] \
    || echo "提示: tftp 根下没有 pxelinux.0 / ldlinux.c32, BIOS 机器起不来 (UEFI 不受影响), 见 docs/MEMTEST.md"
