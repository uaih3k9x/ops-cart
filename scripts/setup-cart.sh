#!/bin/bash
# PXE 服务端部署(推车 rocky9): 挂 ISO 提取引导文件 + 铺 HTTP 仓库. root 运行, 幂等.
set -euo pipefail

ISO=/opt/Rocky-9.8-x86_64-minimal.iso
BASE=/opt/pxe
TFTP=$BASE/tftp
HTTP=$BASE/http
EXPECT_SHA256=d338032cd1cdd41c67139f2f71b4c832c8e4a21943106519db9c7137df7a63d4

[ -f "$ISO" ] || { echo "ISO 不存在: $ISO"; exit 1; }
echo "==> 校验 ISO"
actual=$(sha256sum "$ISO" | awk '{print $1}')
[ "$actual" = "$EXPECT_SHA256" ] || { echo "SHA256 不符: $actual"; exit 1; }

mkdir -p "$TFTP/EFI/BOOT" "$HTTP"

echo "==> 挂载 ISO"
MNT=$(mktemp -d)
mount -o loop,ro "$ISO" "$MNT"
trap 'umount "$MNT"; rmdir "$MNT"' EXIT

echo "==> 提取 TFTP 引导文件"
install -m 644 "$MNT/EFI/BOOT/BOOTX64.EFI"        "$TFTP/shimx64.efi"
install -m 644 "$MNT/EFI/BOOT/grubx64.efi"        "$TFTP/grubx64.efi"
install -m 644 "$MNT/images/pxeboot/vmlinuz"      "$TFTP/vmlinuz"
install -m 644 "$MNT/images/pxeboot/initrd.img"   "$TFTP/initrd.img"

echo "==> grub.cfg (HTTP 快速通道优先, TFTP 兜底)"
cat > "$TFTP/grub.cfg" <<'EOF'
set default=0
set timeout=5

menuentry 'Rocky 9.8 auto install (HTTP fast)' {
    linux (http,192.168.77.1)/vmlinuz ip=dhcp inst.repo=http://192.168.77.1:8000/rocky inst.ks=http://192.168.77.1:8000/ks.cfg
    initrd (http,192.168.77.1)/initrd.img
}

menuentry 'Rocky 9.8 auto install (TFTP slow)' {
    linux /vmlinuz ip=dhcp inst.repo=http://192.168.77.1:8000/rocky inst.ks=http://192.168.77.1:8000/ks.cfg
    initrd /initrd.img
}
EOF
install -m 644 "$TFTP/grub.cfg" "$TFTP/EFI/BOOT/grub.cfg"

echo "==> 铺 HTTP 仓库 (约 2.5GB)"
[ -f "$HTTP/rocky/.treeinfo" ] || cp -a "$MNT" "$HTTP/rocky"

echo "==> ks.cfg 占位(若无)"
[ -f "$HTTP/ks.cfg" ] || echo "MISSING: 请放置 $HTTP/ks.cfg"

echo "完成: $TFTP + $HTTP"
