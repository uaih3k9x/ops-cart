#!/usr/bin/env python3
"""顶栏虚拟指示灯 (issue #2): 颜色看自检, 闪烁看活动. grandMA3 执行器风格的小方块.

每个灯只看一个单调递增的计数器 (日志文件大小 / 网卡字节数 / 键鼠发送次数),
后台约 200 ms 采一次, 数值变了就算"在动", 在动的灯整块闪. 这里只有纯逻辑和便宜的读数,
画在 kiosk.py 里.
"""

import json
import os
import re

SAMPLE_MS = 200
HOLD_S = 1.0            # 计数器变了以后多久内算"在动"
BLINK_S = 0.2

HEALTH_COLOR = {"OK": "#22c55e", "WARN": "#f59e0b", "FAIL": "#ef4444", None: "#4b5563"}
BODY, EMPTY_BODY = "#1f2633", "#141821"
FG, EMPTY_FG, LIT_FG = "#c9d1e0", "#4b5263", "#0b0f17"

# 下排: (显示名, selftest.json 里那一行的 label, 计数器名)
LOWER = [("键鼠", "CH9329", "kvm"), ("PXE", "PXE SERVER", "pxe"),
         ("LAN1", "LAN1 / PXE", "net:eno1"), ("LAN2", "LAN2", "net:enp3s0"),
         ("Wi-Fi", "WIFI", "net:wlp4s0")]
SLOTS = [f"/dev/con-{n}" for n in range(1, 5)]   # /etc/udev/rules.d/99-cart-console.rules
PXE_LOG = "/opt/pxe/pxeserver.log"


class Activity:
    """计数器 -> 哪些灯在动. 第一次见到的值只当基线; None (读不到) 不算变化."""

    def __init__(self, hold=HOLD_S):
        self.hold = hold
        self._last, self._ts = {}, {}

    def update(self, counters, now):
        for name, v in counters.items():
            if v is None:
                continue
            old = self._last.get(name)
            if old is not None and v != old:
                self._ts[name] = now
            self._last[name] = v
        return {n for n, t in self._ts.items() if now - t < self.hold}


def blink_on(now, period=BLINK_S):
    return int(now / period) % 2 == 0


def lamp_view(health, active, on):
    """灯的配色. health=None 是空槽/没在用: 暗着, 不闪."""
    if health is None:
        return {"bg": EMPTY_BODY, "fg": EMPTY_FG, "bar": HEALTH_COLOR[None]}
    color = HEALTH_COLOR.get(health, HEALTH_COLOR[None])
    if active and on:
        return {"bg": color, "fg": LIT_FG, "bar": color}
    return {"bg": BODY, "fg": FG, "bar": color}


def health_of(rows, label):
    """从自检结果里找那一行; "--" (没插/待机) 和找不到都当没在用."""
    hit = next((r for r in rows or [] if r.get("label") == label), None)
    return hit["state"] if hit and hit["state"] in ("OK", "WARN", "FAIL") else None


def net_bytes(path="/proc/net/dev"):
    out = {}
    try:
        lines = open(path).read().splitlines()[2:]
    except OSError:
        return out
    for ln in lines:
        name, _, rest = ln.partition(":")
        f = rest.split()
        if len(f) >= 9:
            out[name.strip()] = int(f[0]) + int(f[8])
    return out


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)   # 同 conbroker 的文件名规则


def slot_status(ddir, devs=SLOTS, exists=os.path.exists, alive=_pid_alive):
    """每个操作头: 线在不在, 有没有活着的 conbroker 服务 (波特率), 它的日志大小 (活动计数)."""
    out = []
    for dev in devs:
        baud = None
        try:
            st = json.load(open(os.path.join(ddir, "run", f"{_safe(dev)}.json")))
            if alive(int(st["pid"])):
                baud = st.get("baud")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        out.append({"dev": dev, "present": bool(exists(dev)), "baud": baud,
                    "count": file_size(os.path.join(ddir, "logs", f"{_safe(dev)}.log"))})
    return out


def slot_view(n, slot):
    """上排方块的文字和健康色. 执行绪名字/占用者等 issue #3, 先显示空闲或波特率."""
    if not slot["present"]:
        return f"{n}\n未插", None
    return f"{n}\n{slot['baud'] or '空闲'}", "OK"


def sample(ddir, kvm_count):
    """一次采样 (后台线程里跑): 操作头状态 + 全部计数器."""
    slots = slot_status(ddir)
    net = net_bytes()
    counters = {f"con-{i + 1}": s["count"] for i, s in enumerate(slots)}
    counters.update({"kvm": kvm_count, "pxe": file_size(PXE_LOG)})
    counters.update({key: net.get(key[4:]) for _n, _l, key in LOWER if key.startswith("net:")})
    return slots, counters
