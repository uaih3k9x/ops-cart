#!/bin/bash
# 奶龙屏保: X 空闲 idle 秒后淡入全屏轮播(单图) -image 60 秒, 任意键/鼠标退出
# 由 kiosk 启动脚本随 X 一起拉起; 独立于 kiosk.py, 挂了不影响任何功能.
if [ -z "$DISPLAY" ]; then echo "需要 X 会话"; exit 1; fi
exec xscreensaver-command -exit 2>/dev/null; true
