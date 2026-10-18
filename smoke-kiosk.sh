#!/bin/bash
# Kiosk Xvfb 冒烟测试
cd /opt/pxe
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
XPID=$!
sleep 1
DISPLAY=:99 timeout 3 python3 kiosk.py > /tmp/kiosk-smoke.log 2>&1
echo "RC=$?"
kill $XPID 2>/dev/null
echo "--- kiosk 输出 ---"
cat /tmp/kiosk-smoke.log
