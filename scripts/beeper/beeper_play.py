#!/usr/bin/env python3
"""主板蜂鸣器放单音旋律: beeper_play.py 曲谱.json [秒数上限]. 曲谱 = [[频率Hz 或 0, 毫秒], ...]
任何退出路径都先关声 (蜂鸣器是硬件方波, 不关会一直响)."""
import json, signal, struct, sys, time
DEV = "/dev/input/by-path/platform-pcspkr-event-spkr"
seq = json.load(open(sys.argv[1]))
limit = float(sys.argv[2]) if len(sys.argv) > 2 else 1e9
f = open(DEV, "wb", buffering=0)
tone = lambda hz: f.write(struct.pack("llHHi", 0, 0, 0x12, 0x02, hz))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
t0 = time.monotonic(); due = t0
try:
    for hz, ms in seq:
        if due - t0 >= limit:
            break
        tone(hz)
        due += ms / 1000
        time.sleep(max(0, due - time.monotonic()))   # 按绝对时间排, 不累积误差
finally:
    tone(0)
    f.close()
