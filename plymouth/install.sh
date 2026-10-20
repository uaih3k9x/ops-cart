#!/bin/bash
# 安装 ops-cart 开机画面. 品牌素材取 /opt/pxe/branding/ (branding.json 的 title/board, logo.png), 没有就用默认.
# 用法: sudo ./install.sh [--no-rebuild]    (不带参数会重建当前内核 initramfs)
set -euo pipefail
cd "$(dirname "$0")"
B=/opt/pxe/branding
T=/usr/share/plymouth/themes/ops-cart
mapfile -t V < <(python3 - "$B/branding.json" "$T" <<'PY'
import json, struct, sys, zlib, os
b = {"title": "OPS-CART", "board": "OPS-CART MAINTENANCE UNIT"}
try:
    b.update(json.load(open(sys.argv[1])))
except (OSError, ValueError):
    pass
asc = lambda s: s.encode("ascii", "ignore").decode().replace('"', "'").replace("|", "/")
print(asc(b["title"])); print(asc(b.get("board", b["title"])))
# 进度条用的 1x1 纯色 PNG (Plymouth 的 Image.Text 是透明底, 画不了色块)
def png(path, rgb):
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0" + bytes(rgb))) + chunk(b"IEND", b"")
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + raw)
os.makedirs(sys.argv[2], exist_ok=True)
png(f"{sys.argv[2]}/bar-bg.png", (224, 224, 230))
png(f"{sys.argv[2]}/bar-fg.png", (51, 115, 217))
PY
)
TITLE=${V[0]}; BOARD=${V[1]}
mkdir -p "$T"
install -m 644 ops-cart/ops-cart.plymouth "$T/"
sed -e "s|@TITLE@|$TITLE|" -e "s|@BOARD@|$BOARD|" ops-cart/ops-cart.script.in > "$T/ops-cart.script"
if [ -f "$B/logo.png" ]; then install -m 644 "$B/logo.png" "$T/logo.png"; else rm -f "$T/logo.png"; fi
if [ "${1:-}" = "--no-rebuild" ]; then plymouth-set-default-theme ops-cart; else plymouth-set-default-theme -R ops-cart; fi
echo "theme installed: title='$TITLE' board='$BOARD' logo=$([ -f "$T/logo.png" ] && echo yes || echo no)"
