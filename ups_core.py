#!/usr/bin/env python3
"""推车 UPS (issue #4): 读 APC Back-UPS 的电量、市电状态、负载和输入电压.

两条路, 自动挑:
  1. hidpdc 直读 /dev/hidrawN —— 能拿到 upower 不报的 ups.load / input.voltage / ups.status
     (需要 udev 规则 70-apc-ups.rules 给普通用户读权限)
  2. 退回 upower 命令行 —— 只有 state / percentage / warning-level

不装 NUT, 不引第三方库. 纯逻辑 (解析/配色/阈值/合并) 单独可测, 实际读在后台线程每秒一次,
画在 kiosk 顶栏、状态板和锁屏上.

实测 (2026-10-29, BX1200CI-CN): state 会从 charging 翻到 discharging, 但滞后一两分钟;
`time to empty` 一直是 28.5 minutes 不变, 不可信 —— 低电量只能按百分比判断.
"""

import os
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
    """是不是在靠电池跑 (市电断了). hidraw 状态里有 OB/OL 就以它为准, 没有才看 upower:
    upower 断电和来电都要滞后一两分钟才翻, hidraw 是即时的. 按整词比, DISCHRG 不算 OB."""
    if not ups:
        return False
    words = str(ups.get("status") or "").split()
    if "OB" in words:
        return True
    if "OL" in words:
        return False
    return ups.get("state") == "discharging"


def low(ups, threshold=LOW_PCT):
    """放电且电量到了阈值: 该正常关机了."""
    return on_battery(ups) and ups.get("percent", 100) <= threshold


def detail(ups):
    """负载/电压这类附加信息, 一行; 没有就空串."""
    if not ups:
        return ""
    bits = []
    if ups.get("load") is not None:
        bits.append(f"{ups['load']}% 负载")
    if ups.get("voltage"):
        bits.append(f"{int(ups['voltage'])}V")
    return " · ".join(bits)


def label(ups):
    """一行字的电量说明; 没读到就说没读到, 别装作正常."""
    if not ups:
        return "电池 --"
    pct = ups.get("percent", 0)
    if on_battery(ups):
        base = f"电池 {pct}% 放电"
    elif ups.get("state") == "charging":
        base = f"电池 {pct}% 充电"
    else:
        base = f"电池 {pct}%"
    extra = detail(ups)
    return f"{base} · {extra}" if extra else base


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


HIDRAW_GLOB = "/sys/class/hidraw/hidraw*"
APC_VENDOR, APC_PRODUCT = "051d", "0002"


def _hid_id(uevent):
    """uevent 里的 HID_ID=总线:厂商:产品 -> (厂商, 产品). 内核写的是补零 8 位十六进制
    (0003:0000051D:00000002), 按数值比, 别按字符串拼."""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            try:
                _bus, vid, pid = line[7:].split(":")
                return int(vid, 16), int(pid, 16)
            except ValueError:
                return None
    return None


def hidraw_nodes(root=HIDRAW_GLOB):
    """/sys/class/hidraw/hidrawN 里属于 APC UPS 的那些设备节点路径."""
    import glob
    out = []
    for d in sorted(glob.glob(root)):
        uevent = os.path.join(d, "device", "uevent")
        try:
            with open(uevent, encoding="utf-8", errors="replace") as f:
                text = f.read().upper()
        except OSError:
            continue
        if _hid_id(text) == (int(APC_VENDOR, 16), int(APC_PRODUCT, 16)):
            out.append("/dev/" + os.path.basename(d))
    return out


def read_hid(path=None):
    """直读 hidraw 拿负载/电压/状态. 没有权限或没设备时返回 None, 不抛异常."""
    import hidpdc
    for dev in ([path] if path else hidraw_nodes()):
        try:
            fields, fd, sizes = hidpdc.open_device(dev)
        except (OSError, ValueError):
            continue
        try:
            values = hidpdc.read_all(fields, fd, sizes)
        finally:
            os.close(fd)
        return {
            "load": values.get("ups.load"),
            "voltage": values.get("input.voltage"),
            "status": values.get("ups.status", ""),
            "runtime": values.get("battery.runtime"),
            "charge": values.get("battery.charge"),
            "source": "hidraw",
            "path": dev,
        }
    return None


def merge(upops, hid):
    """upower 的结果 + hidraw 的结果 -> 一份状态.

    upower 管 state/percent/warning; hidraw 补 load/voltage/status.
    两边的电量/状态互相印证: 有 hidraw 的 charge 就用它 (刷新更快).
    """
    if not upops and not hid:
        return None
    out = dict(upops or {})
    if hid:
        if hid.get("charge") is not None:
            out["percent"] = int(hid["charge"])
        if hid.get("status"):
            out["status"] = hid["status"]
        for key in ("load", "voltage", "runtime"):
            if hid.get(key) is not None:
                out[key] = hid[key]
        out["source"] = "upower+hidraw" if upops else "hidraw"
    else:
        out.setdefault("source", "upower")
    return out


def sample_all(path=None, runner=_run, hid_reader=read_hid):
    """一次采样: upower 与 hidraw 合并. 两条都失败返回 None."""
    upops = sample(path, runner)
    try:
        hid = hid_reader()
    except Exception:            # noqa: BLE001  直读是加分项, 坏了不能连累 upower
        hid = None
    return merge(upops, hid)


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
