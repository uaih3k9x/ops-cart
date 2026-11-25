#!/bin/sh
# 编 dsdtguard.efi (AArch64 UEFI 应用, PE32+): docker 里 Alpine 的 clang + lld-link, 不用 EDK2 / gnu-efi.
#   sh efi/dsdtguard/build.sh <输出目录>
# 镜像 local/efi-clang:alpine3.24 第一次会建 (要外网装 clang lld); 之后离线也能编. /Brepro: 同样输入出同样字节.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?用法: build.sh <输出目录>}
mkdir -p "$out"
out=$(cd "$out" && pwd)
img=local/efi-clang:alpine3.24
if ! docker image inspect "$img" >/dev/null 2>&1; then
    printf 'FROM alpine:3.24\nRUN apk add --no-cache clang lld\n' | docker build -q -t "$img" -
fi
docker run --rm --network none -v "$here":/s:ro -v "$out":/o "$img" sh -euc '
    clang --target=aarch64-unknown-windows -ffreestanding -fshort-wchar -fno-stack-protector -fno-builtin \
          -mno-stack-arg-probe -O2 -std=c99 -Wall -Werror -c /s/dsdtguard.c -o /tmp/dsdtguard.o
    lld-link /machine:arm64 /subsystem:efi_application /entry:efi_main /nodefaultlib /Brepro \
             /out:/o/dsdtguard.efi /tmp/dsdtguard.o
    sha256sum /o/dsdtguard.efi'
