#!/bin/bash
# Kiosk Xvfb 冒烟测试: 在虚拟屏上起 3 秒, 看有没有 traceback.
# 用法: smoke-kiosk.sh [代码目录]   默认 /opt/pxe; 日志写到临时文件, 跑完删掉
set -u
DIR="${1:-/opt/pxe}"
cd "$DIR" || { echo "进不去 $DIR"; exit 2; }
LOG="$(mktemp -t kiosk-smoke.XXXXXX)"
trap 'kill "$XPID" 2>/dev/null; rm -f "$LOG"' EXIT
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
XPID=$!
sleep 1
DISPLAY=:99 timeout 3 python3 kiosk.py >"$LOG" 2>&1
RC=$?
echo "RC=$RC (124 = 跑满 3 秒被 timeout 结束, 算正常)"
echo "--- kiosk 输出 ---"
cat "$LOG"
grep -q Traceback "$LOG" && exit 1
exit 0
