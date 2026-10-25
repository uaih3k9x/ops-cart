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


def main(run=_run, now=time.time, cooldown_path=COOLDOWN, log=None):
    """跑一轮; 切了 (不管成没成) 返回目标那条已保存连接, 不用切返回 None. 参数都可注入, 方便测试."""
    log = log or cartlog.event
    saved = wc.parse_saved(run(wc.CMD_SAVED)[1])
    for s in saved:                     # 连接名和 SSID 可能不同 (大小写、改过名)
        s["ssid"] = run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "uuid", s["uuid"]])[1]
    scan = wc.parse_scan(run(wc.cmd_scan(rescan="auto"))[1])
    t = now()
    cool = _load(cooldown_path)
    pick = wc.pick_switch(saved, scan, t, cooldown=cool)
    if not pick:
        return None
    cur = next(s for s in saved if s["active"])
    rc, out = run(wc.cmd_up(pick["uuid"]))
    if rc == 0:
        log("wifi", f"按优先级切换 {cur['ssid']} -> {pick['ssid']}")
        return pick
    cool[pick["ssid"]] = t + wc.COOLDOWN_S
    atomicfile.write_json(cooldown_path, cool)
    run(wc.cmd_up(cur["uuid"]))
    log("wifi", f"切到 {pick['ssid']} 失败, 冷却 30 分钟并回到 {cur['ssid']}: {out[-120:]}")
    return pick


if __name__ == "__main__":
    main()
