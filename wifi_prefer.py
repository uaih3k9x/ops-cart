#!/usr/bin/env python3
"""定时让无线优先级真正生效: 连着低优先级网络、而更高优先级的已保存网络看得见且信号够时切过去.
切失败的网络冷却 30 分钟并切回原来的, 免得来回跳. 规则见 wifi_core.pick_switch.
由 wifi-prefer.timer 每 3 分钟跑一次 (root)."""

import json
import subprocess
import time

import atomicfile
import cartlog
import wifi_core as wc

COOLDOWN = "/run/ops-cart/wifi-cooldown.json"


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as ex:
        return 1, str(ex)


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    saved = wc.parse_saved(_run(wc.CMD_SAVED)[1])
    for s in saved:                     # 连接名和 SSID 可能不同 (scnunet / SCNUNET)
        s["ssid"] = _run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "uuid", s["uuid"]])[1]
    scan = wc.parse_scan(_run(wc.cmd_scan(rescan="auto"))[1])
    now = time.time()
    cool = _load(COOLDOWN)
    pick = wc.pick_switch(saved, scan, now, cooldown=cool)
    if not pick:
        return
    cur = next(s for s in saved if s["active"])
    rc, out = _run(wc.cmd_up(pick["uuid"]))
    if rc == 0:
        cartlog.event("wifi", f"按优先级切换 {cur['ssid']} → {pick['ssid']}")
        return
    cool[pick["ssid"]] = now + wc.COOLDOWN_S
    atomicfile.write_json(COOLDOWN, cool)
    _run(wc.cmd_up(cur["uuid"]))
    cartlog.event("wifi", f"切到 {pick['ssid']} 失败, 冷却 30 分钟并回到 {cur['ssid']}: {out[-120:]}")


if __name__ == "__main__":
    main()
