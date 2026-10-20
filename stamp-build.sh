#!/bin/bash
# 部署前在仓库里跑: 生成 BUILD = "<提交总数> <短哈希>[-dirty]", 跟 VERSION 一起拷到 /opt/pxe/
cd "$(dirname "$0")"
h=$(git rev-parse --short HEAD)
git diff --quiet HEAD -- . ':!BUILD' || h="$h-dirty"
echo "$(git rev-list --count HEAD) $h" > BUILD
cat BUILD
