#!/usr/bin/env python3
"""推车 UPS (issue #4): 通过系统自带的 upower 读 APC Back-UPS 的电量与市电状态.

只走 upower 命令行, 不装 NUT, 不引第三方库. 这里的纯逻辑 (解析/配色/阈值) 单独可测,
实际读在后台线程里每秒一次, 画在 kiosk 顶栏、状态板和锁屏上.

实测 (2026-10-29, BX1200CI-CN): state 会从 charging 翻到 discharging, 但滞后一两分钟;
`time to empty` 一直是 28.5 minutes 不变, 不可信 —— 低电量只能按百分比判断.
"""

import re
import subprocess

LOW_PCT = 15                    # 放电到这个百分比就正常关机
UPS_KEY = "ups"                 # 顶栏电池灯在 _lamps 里的键

_DEV = re.compile(r"ups_hiddev|hiddev\d+")
_FIELDS = {
    "state": re.compile(r"^\s*state:\s*(\S+)", re.M),
    "percentage": re.compile(r"^\s*percentage:\s*(\d+)%", re.M),
    "warning": re.compile(r"^\s*warning-level:\s*(\S+)", re.M),
    "model": re.compile(r"^\s*model:\s*(.+?)\s*$", re.M),
}


def parse(text):
    """upower -i 的输出 -> {state, percent, warning, model}; 读不到百分比返回 None."""
    out = {}
    for key, rx in _FIELDS.items():
        m = rx.search(text or "")
        if m:
            out[key] = m.group(1)
    if "percentage" not in out:
        return None
    out["percent"] = int(out.pop("percentage"))
    out.setdefault("state", "")
    out.setdefault("warning", "")
    return out


def on_battery(ups):
    """是不是在靠电池跑 (市电断了)."""
    return bool(ups) and ups.get("state") == "discharging"


def low(ups, threshold=LOW_PCT):
    """放电且电量到了阈值: 该正常关机了."""
    return on_battery(ups) and ups.get("percent", 100) <= threshold


def label(ups):
    """一行字的电量说明; 没读到就说没读到, 别装作正常."""
    if not ups:
        return "电池 --"
    pct = ups.get("percent", 0)
    if on_battery(ups):
        return f"电池 {pct}% 放电"
    if ups.get("state") == "charging":
        return f"电池 {pct}% 充电"
    return f"电池 {pct}%"


def health(ups):
    """灯的健康色: 市电正常=OK, 放电=WARN, 到阈值=FAIL; 读不到=None (暗着)."""
    if not ups:
        return None
    if low(ups):
        return "FAIL"
    if on_battery(ups):
        return "WARN"
    if ups.get("warning") not in ("", "none"):
        return "WARN"
    return "OK"


_CACHE = {"path": None}


def _run(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def device_path(runner=_run):
    """upower -e 里挑那个 UPS 设备路径; 没有就 None.

    找到一次就记住 (设备路径在运行期间不变), 免得每秒都多跑一条命令。
    caller 传了自定义 runner 时不走缓存, 方便测试。
    """
    if runner is _run and _CACHE["path"]:
        return _CACHE["path"]
    try:
        r = runner(["upower", "-e"])
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or b"").decode(errors="replace").splitlines():
        line = line.strip()
        if _DEV.search(line):
            if runner is _run:
                _CACHE["path"] = line
            return line
    return None


def sample(path=None, runner=_run):
    """读一次 UPS 状态. 拿不到就返回 None (界面显示 --), 不抛异常给采样线程."""
    path = path or device_path(runner)
    if not path:
        return None
    try:
        r = runner(["upower", "-i", path])
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    ups = parse((r.stdout or b"").decode(errors="replace"))
    if ups:
        ups["path"] = path
    return ups
