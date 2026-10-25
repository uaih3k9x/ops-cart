#!/usr/bin/env python3
"""kiosk_core: 推车 Kiosk 的可测核心层. UI(curses)之外的逻辑都在这.

- 状态采集: systemd 服务 / 网卡 / tailscale / CH9329
- 命令生成: nmcli 改地址 (纯函数, 测试不打真机)
- 装机网段: 部署配置 /etc/ops-cart/pxe.env (pxe-* 单元和 grub 菜单共用同一份)
"""

import argparse
import json
import os
import subprocess
import sys
import time

import atomicfile
import cartlog
import pxeserver

LAN1_CON = "Lan1-pxe"   # eno1, 装机口
LAN2_CON = "enp3s0"     # 管理口

# 装机网段只在部署时定 (systemd EnvironmentFile), 车机不改; 仓库里只放示例值
PXE_ENV = "/etc/ops-cart/pxe.env"
DEFAULT_PXE_ENV = {"PXE_IP": "192.0.2.1", "PXE_PREFIX": "24", "PXE_POOL": "192.0.2.10,192.0.2.20"}


def pxe_env_read(path=PXE_ENV):
    """KEY=VALUE 形式 (systemd EnvironmentFile 子集): 认识的键覆盖默认, 引号去掉, 其余忽略."""
    env = dict(DEFAULT_PXE_ENV)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return env
    for line in lines:
        k, eq, v = line.strip().partition("=")
        if eq and k in env and not k.startswith("#"):
            env[k] = v.strip().strip("\"'")
    return env


def run(cmd, timeout=5):
    """跑命令, 返回 (rc, stdout). 失败不抛异常, UI 层自己消化.
    命令不存在 / 超时: stdout 给空串 (127 / 124), 错误文字不能被当成 IP、状态之类的值显示出去."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    except Exception:           # noqa: BLE001  约定就是不抛
        return 1, ""


# ---------------------------------------------------------------- 状态

def parse_systemctl_show(text, names):
    """`systemctl show -p Id,ActiveState,UnitFileState a b c` 的输出 -> {名字: (enabled, active)}.
    每个单元一段, 段间空行; 段内属性顺序不定. 先按 Id 认, 认不出再按段的顺序. 缺的给 (False, False)."""
    blocks = []
    for chunk in text.split("\n\n"):
        kv = dict(line.split("=", 1) for line in chunk.splitlines() if "=" in line)
        if kv:
            blocks.append(kv)
    by_id = {b.get("Id", ""): b for b in blocks}
    out = {}
    for i, n in enumerate(names):
        b = by_id.get(n) or by_id.get(n + ".service")
        if b is None and len(blocks) == len(names) and not blocks[i].get("Id"):
            b = blocks[i]
        b = b or {}
        out[n] = (b.get("UnitFileState") == "enabled", b.get("ActiveState") == "active")
    return out


def services_status(names=("pxe-server", "pxe-http-repo", "pxe-http-boot")):
    """{服务名: (enabled, active)}; 一次 systemctl show 查全部."""
    names = list(names)
    _, out = run(["systemctl", "show", "-p", "Id,ActiveState,UnitFileState"] + names)
    return parse_systemctl_show(out, names)


SYS_NET = "/sys/class/net"


def nics_status(sysfs=SYS_NET):
    """[{name, con, ip, method, link}] 只看两个业务口. carrier 直接读 sysfs, 读不到 = 没插线."""
    result = []
    for con, name, role in ((LAN1_CON, "eno1", "PXE 装机口"),
                            (LAN2_CON, "enp3s0", "管理口")):
        _, ip = run(["ip", "-4", "-br", "addr", "show", name])
        ip = ip.split()[-1] if ip else "-"
        _, method = run(["nmcli", "-g", "ipv4.method", "con", "show", con])
        link = _read(f"{sysfs}/{name}/carrier") == "1"
        result.append({"name": name, "con": con, "role": role, "ip": ip,
                       "method": method or "-", "link": link})
    return result


def kp_enter_as_return(root):
    """小键盘回车在所有输入框里等同主键盘回车 (各处只绑了 <Return>, simpledialog 也是)."""
    root.bind_class("Entry", "<KP_Enter>", lambda e: e.widget.event_generate("<Return>"))


PROC_UPTIME = "/proc/uptime"


def uptime_text(seconds):
    """秒 -> 和 `uptime -p` 一样的说法: "up 1 day, 2 hours, 5 minutes"."""
    m = int(seconds) // 60
    d, h, m = m // 1440, m // 60 % 24, m % 60

    def unit(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")
    parts = ([unit(d, "day")] if d else []) + ([unit(h, "hour")] if d or h else []) + [unit(m, "minute")]
    return "up " + ", ".join(parts)


def kvm_summary(online, conf):
    """键鼠概况: online = 在线的 CH9329 块数 (kvm_core.ports), bound = 绑了键鼠的画面源个数."""
    return {"online": len(online), "bound": len((conf or {}).get("bind") or {})}


def misc_status(kvm_ports=None, kvm_conf=None):
    import kvm_core
    _, ts_ip = run(["tailscale", "ip", "-4"])
    kvm = kvm_summary((kvm_ports or kvm_core.ports)(), (kvm_conf or kvm_core.load)())
    try:
        up = uptime_text(float((_read(PROC_UPTIME) or "").split()[0]))
    except (ValueError, IndexError):
        up = "-"
    return {"tailscale_ip": ts_ip or "-", "kvm": kvm, "uptime": up}


# ---------------------------------------------------------------- 顶栏网络标

NET_PROBE_HOST = "223.5.5.5"        # 公共 DNS: 哪条出口都通, ping 得通就算有外网  public-safe-ignore
ICON_DIR = "/opt/pxe/icons"


def default_route_dev(route_text):
    """`ip route show default` 里第一条默认路由走哪个口(内核按 metric 排好了); 没有 -> None."""
    for line in route_text.splitlines():
        p = line.split()
        if p and p[0] == "default" and "dev" in p[:-1]:
            return p[p.index("dev") + 1]
    return None


def net_state(route_text, internet):
    """-> (状态, 说明). off=没默认路由; warn=连上了但出不去(校园网没认证/上游断); on=通外网."""
    dev = default_route_dev(route_text)
    if not dev:
        return "off", "未连接"
    via = "有线" if dev.startswith(("en", "eth")) else "Wi-Fi" if dev.startswith("wl") else dev
    return ("on", via) if internet else ("warn", f"{via} 无外网")


def net_probe():
    route = run(["ip", "route", "show", "default"])[1]
    up = bool(default_route_dev(route)) and \
        run(["ping", "-c", "1", "-W", "1", NET_PROBE_HOST], timeout=3)[0] == 0
    return net_state(route, up)


def net_icon(state, d=ICON_DIR):
    return f"{d}/net-{state}.png"


HWMON = "/sys/class/hwmon"
# hwmon 驱动名 -> (显示名, 取哪个 label; None = 第一个 temp)
TEMP_SOURCES = (("coretemp", "CPU", "Package id 0"),
                ("nouveau", "显卡", None),
                ("acpitz", "主板", None))


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _hwmon_temp(d, label):
    """d 目录下 label 对应的温度(°C); label=None 取 temp1."""
    for i in range(1, 32):
        raw = _read(f"{d}/temp{i}_input")
        if raw is None:
            continue
        if label is None or _read(f"{d}/temp{i}_label") == label:
            return int(raw) / 1000
    return None


def smart_temp(dev="/dev/sda"):
    """SSD 温度(°C), 读不到返回 None. 需要 root (kiosk.service 以 root 跑)."""
    rc, out = run(["smartctl", "-j", "-A", dev])
    try:
        return json.loads(out)["temperature"]["current"]
    except (ValueError, KeyError, TypeError):
        return None


def temps_status(root=HWMON, smart=smart_temp):
    """[(显示名, °C)] 按 TEMP_SOURCES 顺序, 再加 SSD; 缺的跳过."""
    found = {}
    if os.path.isdir(root):
        for e in sorted(os.listdir(root)):
            name = _read(f"{root}/{e}/name")
            for drv, shown, label in TEMP_SOURCES:
                if name == drv and shown not in found:
                    t = _hwmon_temp(f"{root}/{e}", label)
                    if t is not None:
                        found[shown] = t
    result = [(shown, found[shown]) for _, shown, _ in TEMP_SOURCES if shown in found]
    ssd = smart()
    if ssd is not None:
        result.append(("SSD", float(ssd)))
    return result


def fans_status(root=HWMON):
    """[(fanN, RPM)] 来自 it87 系 SuperIO (IT8625E, 树外 it87 驱动 + DKMS)."""
    result = []
    if os.path.isdir(root):
        for e in sorted(os.listdir(root)):
            if not (_read(f"{root}/{e}/name") or "").startswith("it8"):
                continue
            for i in range(1, 9):
                raw = _read(f"{root}/{e}/fan{i}_input")
                if raw is not None:
                    result.append((f"fan{i}", int(raw)))
    return result


def fans_to_show(fans, seen):
    """只显示转过的风扇; seen 记住它们, 之后掉到 0 也照样显示(好报警)."""
    seen.update(n for n, rpm in fans if rpm > 0)
    return [(n, rpm) for n, rpm in fans if n in seen]


def temp_level(c):
    """<70 ok, 70~85 warn, >=85 hot."""
    return "hot" if c >= 85 else "warn" if c >= 70 else "ok"


# ---------------------------------------------------------------- 版本号

APP_DIR = "/opt/pxe"


def version_string(d=APP_DIR):
    """"Ver 1.00.00   Build 0012 (4265fc8)". VERSION 手动维护(进仓库); BUILD 由 stamp-build.sh 部署时生成."""
    ver = _read(f"{d}/VERSION")
    if not ver:
        return "Ver dev"
    build = (_read(f"{d}/BUILD") or "").split()
    if len(build) != 2 or not build[0].isdigit():
        return f"Ver {ver}"
    return f"Ver {ver}   Build {int(build[0]):04d} ({build[1]})"


# ---------------------------------------------------------------- 开机自检 / 品牌

BRANDING_DIR = "/opt/pxe/branding"     # 不进仓库; 放 branding.json + logo.png 换皮
BRANDING_DEFAULT = {"title": "OPS-CART", "subtitle": "SYSTEM STARTUP"}


def branding_read(d=BRANDING_DIR):
    """{"title", "subtitle", "logo"(路径或 None)}; 缺文件/坏 JSON 用默认."""
    b = dict(BRANDING_DEFAULT)
    try:
        with open(f"{d}/branding.json") as f:
            b.update({k: v for k, v in json.load(f).items() if isinstance(v, str)})
    except (OSError, ValueError):
        pass
    logo = f"{d}/logo.png"
    b["logo"] = logo if os.path.exists(logo) else None
    return b


# 自检 = 一张计划表(/opt/pxe/selftest.json, 没有就用 DEFAULT_PLAN) x 一组检查函数.
# 检查函数: (ctx, **参数) -> (state, detail); state: OK / WARN / FAIL / --(没接, 不算故障).
# ctx 是开机时采一次的快照 + 几个探测函数, 测试里整个换掉.

def check_lan(ctx, iface="enp3s0"):
    nic = {n["name"]: n for n in ctx["nics"]}.get(iface, {})
    if not nic.get("link"):
        return "FAIL", "NO LINK"
    if nic.get("ip", "-") == "-":
        return "WARN", "NO ADDRESS"
    return "OK", nic["ip"].split("/")[0]


def check_tailscale(ctx):
    ts = ctx["misc"].get("tailscale_ip", "-")
    return ("OK", ts) if ts != "-" else ("WARN", "OFFLINE")


def check_serial(ctx, dev="/dev/ttyUSB0"):
    return ("OK", os.path.basename(dev)) if ctx["exists"](dev) else ("--", "NOT CONNECTED")


def check_ch9329(ctx):
    k = ctx["misc"].get("kvm") or {}
    if not k.get("online"):
        return "--", "NOT CONNECTED"
    return "OK", f"{k['online']} ONLINE / {k.get('bound', 0)} BOUND"


def check_pxe(ctx, iface="eno1"):
    if ctx["svcs"].get("pxe-server", (False, False))[1]:
        return "OK", "RUNNING"
    nic = {n["name"]: n for n in ctx["nics"]}.get(iface, {})
    return "--", "LINK UP / STANDBY" if nic.get("link") else "STANDBY"


def check_cpu(ctx):
    cpu = dict(ctx["temps"]).get("CPU")
    if cpu is None:
        return "--", "NO SENSOR"
    return {"ok": "OK", "warn": "WARN", "hot": "FAIL"}[temp_level(cpu)], f"{cpu:.0f}°C"


def check_fan(ctx):
    fans = ctx["fans"]
    if not fans:
        return "--", "NO SENSOR"
    if all(rpm == 0 for _, rpm in fans):
        return "FAIL", "STOPPED"
    return "OK", " / ".join(str(rpm) for _, rpm in fans if rpm) + " RPM"


def check_ping(ctx, host):
    return ("OK", host) if ctx["ping"](host) else ("WARN", "UNREACHABLE")


def check_pci(ctx, id, count=1, driver=None):
    """PCI 卡在不在(vendor:device); 给了 driver 就要求已绑定."""
    vid, did = id.split(":")
    found = ctx["pci"](vid, did)
    if not found:
        return "FAIL", "NOT FOUND"
    if len(found) < count:
        return "FAIL", f"x{len(found)} / {count}"
    bound = {d["driver"] for d in found} - {None}
    if driver and driver not in bound:
        return "WARN", f"x{len(found)} NO {driver.upper()}"
    return "OK", f"x{len(found)} " + (", ".join(sorted(bound)) if bound else "NO DRIVER")


def check_service(ctx, unit, required=False):
    """在跑 OK; 开了自启却没跑 WARN (标 required 才算 FAIL); 没开自启 --."""
    enabled, active = ctx["svcs"].get(unit, (False, False))
    if active:
        return "OK", "RUNNING"
    if enabled:
        return ("FAIL" if required else "WARN"), "NOT RUNNING"
    return "--", "OFF"


def check_wifi(ctx):
    st = ctx["wifi"]()
    if not st.get("ssid"):
        return "--", "NOT CONNECTED"
    auth = st.get("auth") or {}
    if auth and not auth.get("ok") and auth.get("state") != "skip":
        return "WARN", f"{st['ssid']} AUTH {auth.get('state', '?').upper()} {auth.get('message', '')}".strip()
    # 默认路由走无线 = 网线断了它在顶班, 能用但该提醒
    return ("WARN", f"{st['ssid']} {st['signal']}% IN USE") if st["egress"] \
        else ("OK", f"{st['ssid']} {st['signal']}% STANDBY")


def check_signal(ctx, match):
    """采集卡有没有画面信号. match 对友好名/原名/key 做子串匹配, 取第一个."""
    hit = next((x for x in ctx["videos"]() if match in x["friendly"] or match in x["name"]
                or match in x["key"]), None)
    if not hit:
        return "FAIL", "NOT FOUND"
    dev = os.path.basename(hit["dev"])
    return ("OK", f"SIGNAL {dev}") if ctx["signal"](hit["dev"]) else ("--", f"NO SIGNAL {dev}")


CHECKS = {"lan": check_lan, "tailscale": check_tailscale, "serial": check_serial,
          "ch9329": check_ch9329, "pxe": check_pxe, "cpu": check_cpu, "fan": check_fan,
          "ping": check_ping, "pci": check_pci, "service": check_service, "wifi": check_wifi,
          "signal": check_signal}

DEFAULT_PLAN = [{"check": "service", "label": "PXE SERVER", "unit": "pxe-server"},
                {"check": "service", "label": "PXE HTTP REPO", "unit": "pxe-http-repo"},
                {"check": "service", "label": "PXE HTTP BOOT", "unit": "pxe-http-boot"},
                {"check": "lan", "label": "LAN2", "iface": "enp3s0"},
                {"check": "wifi", "label": "WIFI"},
                {"check": "tailscale"},
                {"check": "serial", "label": "CONSOLE"},
                {"check": "ch9329", "label": "CH9329"},
                {"check": "pxe", "label": "LAN1 / PXE", "iface": "eno1"},
                {"check": "cpu"},
                {"check": "fan"}]

SELFTEST_PLAN = "/opt/pxe/selftest.json"


def selftest_plan_read(path=SELFTEST_PLAN):
    try:
        with open(path) as f:
            plan = json.load(f)
        return plan if isinstance(plan, list) else DEFAULT_PLAN
    except (OSError, ValueError):
        return DEFAULT_PLAN


def _selftest_fail(detail, label="SELFTEST", check="selftest"):
    return {"label": label, "state": "FAIL", "detail": detail, "check": check}


def selftest_run(plan, ctx, checks=CHECKS):
    """按计划逐项跑; 单项抛异常只把那一行标 FAIL, 不拖垮整页. 永不抛异常.
    ctx 可以是快照, 也可以是采集函数 (采集出错 -> 一行 SELFTEST / FAIL)."""
    if callable(ctx):
        try:
            ctx = ctx()
        except Exception:       # noqa: BLE001
            return [_selftest_fail("CONTEXT ERROR")]
    if not isinstance(plan, list):
        return [_selftest_fail("BAD PLAN")]
    rows = []
    for item in plan:
        try:
            params = {k: v for k, v in item.items() if k not in ("check", "label")}
            label = str(item.get("label", str(item.get("check", "?")).upper()))
            fn = checks.get(item.get("check"))
        except Exception:       # noqa: BLE001  计划里混进了不是对象的东西
            rows.append(_selftest_fail("BAD ITEM", check=None))
            continue
        if fn is None:
            state, detail = "FAIL", "UNKNOWN CHECK"
        else:
            try:
                state, detail = fn(ctx, **params)
            except Exception:   # noqa: BLE001
                state, detail = "FAIL", "ERROR"
        rows.append({"label": label, "state": state, "detail": detail, "check": item.get("check")})
    return rows


def pci_devices(vid, did, root="/sys/bus/pci/devices"):
    """[{slot, driver}], vid/did 是不带 0x 的十六进制."""
    out = []
    for slot in sorted(os.listdir(root)):
        d = os.path.join(root, slot)
        if (_read(os.path.join(d, "vendor")) or "").lower() == "0x" + vid.lower() and \
           (_read(os.path.join(d, "device")) or "").lower() == "0x" + did.lower():
            drv = os.path.join(d, "driver")
            out.append({"slot": slot, "driver": os.path.basename(os.readlink(drv)) if os.path.islink(drv) else None})
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception:           # noqa: BLE001  一样采不到不拖垮整个自检
        return default


def selftest_context():
    """真机快照. SMART 要 sudo 且慢, 自检不读. 每一样都兜住, 本函数不抛."""
    return {"nics": _safe(nics_status, []), "misc": _safe(misc_status, {}),
            "svcs": _safe(services_status, {}),
            "temps": _safe(lambda: temps_status(smart=lambda: None), []), "fans": _safe(fans_status, []),
            "exists": os.path.exists, "pci": pci_devices,
            "ping": lambda h: run(["ping", "-c", "1", "-W", "1", h], timeout=3)[0] == 0,
            "videos": video_nodes, "signal": lambda dev: video_signal(dev), "wifi": wifi_status}


def wifi_status():
    import ruijie_portal as rp
    import wifi_core as wc
    st = wc.status(run(wc.CMD_STATUS_SCAN)[1], run(["ip", "-4", "-br", "addr", "show", wc.IFACE])[1],
                   run(["ip", "route"])[1])
    st["auth"] = rp._read_json(rp.STATUS)       # 校园网认证最近一次结果 (campus-auth 写)
    return st


def selftest_rows(nics, misc, svcs, temps, fans, serial_ok):
    """旧接口: 默认计划 + 给定快照."""
    ctx = {"nics": nics, "misc": misc, "svcs": svcs, "temps": temps, "fans": fans,
           "exists": lambda p: serial_ok, "ping": lambda h: False, "pci": lambda v, d: [],
           "wifi": lambda: {"ssid": None}}
    return selftest_run(DEFAULT_PLAN, ctx)


def selftest_line(row, width=24):
    """"LAN2 ............ OK   192.0.2.5\""""
    return f"{row['label'] + ' ':.<{width}} {row['state']:<4} {row['detail']}".rstrip()


STATE_TAG = {"OK": "ok", "WARN": "t_warn", "FAIL": "bad"}
ON_TEMPS_BLOCK = {"cpu", "fan"}      # 状态板下面有温度/风扇明细, 自检行就不重复


def status_board_lines(rows, temps, fans, uptime):
    """状态板 = 自检结果 (rows 为 None 表示第一轮还没跑完) + 温度/风扇 + 开机时长. -> [(文本, tag)]"""
    lines = [("检查中…", "dim")] if rows is None else \
        [(selftest_line(r, width=22), STATE_TAG.get(r["state"], "dim")) for r in rows
         if r.get("check") not in ON_TEMPS_BLOCK]
    lines += [("", "dim"), (uptime, "dim")]
    if temps:
        lines.append(("", "dim"))
        lines += [(f"{name:<6}{c:>5.0f}°C", "t_" + temp_level(c)) for name, c in temps]
    lines += [(f"{name:<6}{rpm:>5} RPM", "t_ok" if rpm else "t_hot") for name, rpm in fans]
    return lines


_CTRL_KEYS = {"Control_L", "Control_R"}
_ALT_KEYS = {"Alt_L", "Alt_R", "Meta_L", "Meta_R"}     # 没有 WM 时 Alt 常报成 Meta


def is_release_combo(keysym, state):
    """透传中按下的这个键 + 已按住的修饰位(X state: Ctrl=0x4, Alt=0x8) 是否凑成 Ctrl+Alt."""
    return (keysym in _ALT_KEYS and state & 0x4) or (keysym in _CTRL_KEYS and state & 0x8)


def video_devices(root="/dev"):
    try:
        names = [n for n in os.listdir(root) if n.startswith("video") and n[5:].isdigit()]
    except OSError:
        return []
    return [os.path.join(root, n) for n in sorted(names, key=lambda n: int(n[5:]))]



V4L_SYS = "/sys/class/video4linux"
VIDEO_NAMES = "/opt/pxe/video-names.json"     # 可选改名: {"0000:02:00.0": "X光左屏", "345f:2109": "小采集棒"}


def video_nodes(sysdir=V4L_SYS, dev_root="/dev", names=None):
    """能出画面的采集节点 (index=0; UVC 的元数据节点跳过), 带友好名称.
    key = PCI 插槽 (SC0710) 或 USB vid:pid (从 UVC 名字里取), 用来改名和自检匹配."""
    import re as _re
    if names is None:
        try:
            names = json.load(open(VIDEO_NAMES))
        except (OSError, ValueError):
            names = {}
    try:
        entries = [n for n in os.listdir(sysdir) if n.startswith("video") and n[5:].isdigit()]
    except OSError:
        return []
    nodes = []
    for n in sorted(entries, key=lambda n: int(n[5:])):
        d = os.path.join(sysdir, n)
        if (_read(os.path.join(d, "index")) or "0").strip() != "0":
            continue
        name = (_read(os.path.join(d, "name")) or n).strip()
        parent = os.path.basename(os.path.realpath(os.path.join(d, "device")))
        usb = _re.search(r"\(([0-9a-f]{4}:[0-9a-f]{4})\)", name)
        if usb:
            key = usb.group(1)
        elif _re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d", parent):
            key = parent
        else:                           # USB 设备名里没带 vid:pid: 去接口的上一级读, 别用会变的 videoN
            up = os.path.dirname(os.path.realpath(os.path.join(d, "device")))
            ids = [(_read(os.path.join(up, f)) or "").strip() for f in ("idVendor", "idProduct")]
            key = ":".join(ids) if all(ids) else n
        nodes.append({"dev": os.path.join(dev_root, n), "name": name, "key": key})
    sc = sorted(x["key"] for x in nodes if x["name"].startswith("SC0710"))
    for x in nodes:
        if x["key"] in names:
            x["friendly"] = names[x["key"]]
        elif x["name"].startswith("SC0710"):
            x["friendly"] = f"SC0710 卡{sc.index(x['key']) + 1}"
        elif x["key"] == "345f:2109":
            x["friendly"] = "MS2130 USB"
        else:
            x["friendly"] = x["name"][:24]
    return nodes


def video_key(dev, nodes=None):
    """画面源设备 -> 绑定用的 key (同 video_nodes); 找不到 None."""
    nodes = video_nodes() if nodes is None else nodes
    return next((x["key"] for x in nodes if x["dev"] == dev), None)


def video_friendly(dev, nodes=None):
    nodes = video_nodes() if nodes is None else nodes
    return next((x["friendly"] for x in nodes if x["dev"] == dev), os.path.basename(dev))


def check_video(ctx, match, count=1):
    """采集节点在不在: match 对 key/原名/友好名做子串匹配 (如 "345f:2109"、"SC0710")."""
    hit = [x for x in ctx["videos"]() if match in x["key"] or match in x["name"] or match in x["friendly"]]
    if not hit:
        return "FAIL", "NOT FOUND"
    if len(hit) < count:
        return "FAIL", f"x{len(hit)} / {count}"
    return "OK", " ".join(os.path.basename(x["dev"]) for x in hit)


CHECKS["video"] = check_video       # 定义在 CHECKS 之后, 这里补登记


def next_video_device(devs, current):
    if not devs:
        return current
    return devs[(devs.index(current) + 1) % len(devs)] if current in devs else devs[0]


def parse_input_signal(text):
    """v4l2-ctl --get-input 的输出 -> True 有信号 / False 无信号 / None 读不到."""
    import re as _re
    m = _re.search(r"Video input\s*:\s*\d+\s*\((.*)\)", text)
    if not m:
        return None
    return not _re.search(r"no signal|no hsync|no lock", m.group(1), _re.I)


def video_signal(dev):
    rc, out = run(["v4l2-ctl", "-d", dev, "--get-input"], timeout=3)
    return parse_input_signal(out)


def default_video(nodes, has_signal=video_signal):
    """开机默认画面源: 有信号的 SC0710 (延迟低) > 其他有信号的 > 第一个."""
    devs = [x["dev"] for x in nodes]
    sc = [x["dev"] for x in nodes if x["name"].startswith("SC0710")]
    for group in (sc, devs):
        hit = next((d for d in group if has_signal(d)), None)
        if hit:
            return hit
    return devs[0] if devs else None


NOTICE_PATH = "/opt/pxe/notice.txt"


def notice_read(path=NOTICE_PATH, max_lines=12, max_chars=400):
    """锁屏公告牌. 文件不存在/全空返回 ""; 去首尾空行, 限行数和字数."""
    text = _read(path) or ""
    lines = text.splitlines()[:max_lines]
    return "\n".join(lines).strip()[:max_chars]


def notice_font_px(text, width, height, lo=24, hi=220):
    """公告字号(像素): 塞满 width x height 的最大值. 中文 1 字宽, ASCII 0.6, 行高 1.3."""
    lines = text.splitlines()
    if not lines:
        return lo
    units = max(sum(0.6 if ord(c) < 128 else 1 for c in l) for l in lines) or 1
    px = int(min(width / units, height / (len(lines) * 1.3)))
    return max(lo, min(hi, px))


# ---------------------------------------------------------------- 命令生成 (纯函数)

def nmcli_set_static(con, addr, gateway="", dns=""):
    cmds = [["nmcli", "con", "mod", con,
             "ipv4.method", "manual", "ipv4.addresses", addr]]
    cmds[0] += ["ipv4.gateway", gateway or ""] + (["ipv4.dns", dns] if dns else [])
    cmds.append(["nmcli", "con", "up", con])
    return cmds


def nmcli_set_dhcp(con):
    return [["nmcli", "con", "mod", con, "ipv4.method", "auto",
             "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""],
            ["nmcli", "con", "up", con]]


def power(off=True):
    run(["systemctl", "poweroff" if off else "reboot"], timeout=3)


def screensaver_due(last_activity_ts, now_ts, threshold_s=180, showing=False):
    """空闲超过 threshold 秒该出屏保; 已在屏保里则不重复触发."""
    if showing:
        return False
    return (now_ts - last_activity_ts) >= threshold_s


# ---------------------------------------------------------------- Kickstart 编辑

KS_PATH = "/opt/pxe/http/ks.cfg"
KS_BACKUPS = 5


def ks_read(path=KS_PATH):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def ks_write(text, path=KS_PATH):
    """保存前先轮换备份 (保留最近 KS_BACKUPS 份)."""
    import glob
    import shutil
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = f"{path}.bak-{stamp}"
        n = 1
        while os.path.exists(bak):          # 同秒多次保存防撞车
            n += 1
            bak = f"{path}.bak-{stamp}.{n}"
        shutil.copy2(path, bak)
        backups = sorted(glob.glob(path + ".bak-*"))
        for old in backups[:-KS_BACKUPS]:
            os.unlink(old)
    atomicfile.write_text(path, text)


def ks_set_rootpw(text, hashed):
    """替换 rootpw 行 (hash 必须是 sha512crypt $6$ 开头)."""
    if not hashed.startswith("$6$"):
        raise ValueError("密码 hash 必须是 sha512crypt ($6$ 开头)")
    out = []
    replaced = False
    for ln in text.splitlines():
        if ln.strip().startswith("rootpw"):
            out.append(f"rootpw --iscrypted {hashed}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        raise ValueError("找不到 rootpw 行")
    return "\n".join(out) + "\n"


def ks_set_hostname(text, hostname):
    """替换 network 行的 --hostname 值; 没有则报错."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", hostname or ""):
        raise ValueError("主机名不合法")
    out, replaced = [], False
    for ln in text.splitlines():
        if ln.strip().startswith("network") and "--hostname" in ln:
            out.append(_re.sub(r"--hostname\s+\S+", f"--hostname {hostname}", ln))
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        raise ValueError("找不到 network --hostname 行")
    return "\n".join(out) + "\n"


def ks_hash_password(plain):
    """openssl 生成 sha512crypt hash (subprocess, 可 mock)."""
    import subprocess as _sp
    import secrets as _se
    r = _sp.run(["openssl", "passwd", "-6", "-salt", _se.token_hex(8), plain],
                capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise ValueError(f"openssl 失败: {r.stderr.strip()}")
    return r.stdout.strip()


def ks_validate(text):
    """返回警告列表 (不阻断保存, 但 UI 会亮出来)."""
    import re as _re
    warns = []
    if "rootpw" not in text:
        warns.append("没有 rootpw 行: 装完 root 无密码(或被安装器拦截)")
    if _re.search(r"^clearpart\s+--all", text, _re.M):
        warns.append("clearpart --all: 装机时清空目标机器全部磁盘!")
    if "reboot" not in text:
        warns.append("没有 reboot: 装完停在完成界面, 需手动重启")
    if not text.strip():
        warns.append("内容为空")
    return warns


# ------------------------------------------------------------ PIN 锁屏
# 防"路过的人顺手按两下": 只存加盐 PBKDF2 哈希, 连续输错冷却.

KIOSK_CONF = "/opt/pxe/kiosk.conf"
_PIN_ITERS = 200_000


def pin_hash(pin, salt=None):
    import hashlib as _hl
    import secrets as _se
    salt = salt or _se.token_hex(16)
    dk = _hl.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _PIN_ITERS)
    return f"pbkdf2${_PIN_ITERS}${salt}${dk.hex()}"


def pin_verify(pin, stored):
    import hashlib as _hl
    import hmac as _hm
    try:
        tag, iters, salt, want = stored.split("$")
        if tag != "pbkdf2":
            return False
        dk = _hl.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iters))
    except (ValueError, AttributeError):
        return False
    return _hm.compare_digest(dk.hex(), want)


def pin_conf_read(path=None):
    """返回存储的 PIN 哈希; 没设过返回 None (= 不上锁)."""
    try:
        return json.load(open(path or KIOSK_CONF)).get("pin") or None
    except (OSError, ValueError):
        return None


def pin_set(pin, path=None):
    if not (pin.isdigit() and 4 <= len(pin) <= 12):
        raise ValueError("PIN 须为 4-12 位数字")
    path = path or KIOSK_CONF
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        cfg = {}
    cfg["pin"] = pin_hash(pin)
    atomicfile.write_json(path, cfg, mode=0o600)


PIN_MAX_LOCK_S = 15 * 60                # 车机输错冷却封顶
PIN_STATE = "/run/ops-cart/pin-gate.json"   # 失败计数落在这 (重启 kiosk 不清零, 重启推车清零)
_DEFAULT = object()


class PinGate:
    """PIN 校验 + 连续输错冷却. attempt() 返回 'ok' / 'bad' / 'locked'.
    冷却指数退避: lock_s 起步, 每连续冷却一次翻倍, 封顶 max_lock_s (默认 PIN_MAX_LOCK_S;
    显式传 None 表示固定 lock_s 不翻倍); 输对一次回到起步.
    state_path 给了就把失败计数、冷却到期时间持久化 (kiosk 用 PIN_STATE), 读写失败都不影响校验.
    进入冷却时记 cartlog [pin]. 网页口多线程同时试, 内部加锁, 计数不会被并发冲掉."""

    def __init__(self, stored, max_fails=5, lock_s=60, max_lock_s=_DEFAULT, state_path=None,
                 event=None):
        import threading as _th
        self.stored, self.max_fails, self.lock_s = stored, max_fails, lock_s
        if max_lock_s is _DEFAULT:
            max_lock_s = PIN_MAX_LOCK_S
        self.max_lock_s = max(max_lock_s or lock_s, lock_s)
        self.state_path = state_path
        self.event = event or (lambda msg: cartlog.event("pin", msg))
        self.fails, self.locked_until, self.lockouts = 0, 0.0, 0
        self._lock = _th.Lock()
        self._load()

    @property
    def required(self):
        return bool(self.stored)

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            self.fails, self.locked_until, self.lockouts = \
                int(st["fails"]), float(st["locked_until"]), int(st["lockouts"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save(self):
        if not self.state_path:
            return
        try:
            atomicfile.write_json(self.state_path, {"fails": self.fails, "locked_until": self.locked_until,
                                                    "lockouts": self.lockouts}, mode=0o600)
        except OSError:
            pass

    def attempt(self, pin, now=None):
        now = time.time() if now is None else now
        if not self.required:
            return "ok"
        with self._lock:
            if now < self.locked_until:
                return "locked"
            if pin_verify(pin, self.stored):
                if self.fails or self.lockouts:
                    self.fails, self.lockouts = 0, 0
                    self._save()
                return "ok"
            self.fails += 1
            if self.fails >= self.max_fails:
                span = min(self.lock_s * 2 ** self.lockouts, self.max_lock_s)
                self.fails, self.locked_until, self.lockouts = 0, now + span, self.lockouts + 1
                self._save()
                try:
                    self.event(f"PIN 连续输错 {self.max_fails} 次, 冷却 {span:.0f} 秒 (第 {self.lockouts} 次)")
                except Exception:   # noqa: BLE001  记日志不能拖垮校验
                    pass
                return "locked"
            self._save()
            return "bad"


# ---------------------------------------------------------------- 装机模式
# 默认: tftp/grub.cfg 只有救援项 (inst.rescue, 不带 ks). 推车屏幕上输随机码解锁时,
# 只给选定 MAC 写 grub.cfg-01-<mac> (GRUB 先找按 MAC 命名的菜单); 30 分钟或该文件被取走即上锁.

TFTP_ROOT = "/opt/pxe/tftp"
PXE_MODE = "/opt/pxe/pxe-mode.json"
PXE_LOG = "/opt/pxe/pxeserver.log"
ARM_SECONDS = 30 * 60


def grub_cfg_rescue(env_path=None):
    return ("# 推车生成: 默认只进救援 shell, 不装系统 (装机要在推车上解锁)\n" +
            pxeserver.grub_menu(pxe_env_read(env_path or PXE_ENV)["PXE_IP"]))


def grub_cfg_install(env_path=None):
    return ("# 推车生成: 装机模式, 只下发给解锁时选定的那台机器\n" +
            pxeserver.grub_menu(pxe_env_read(env_path or PXE_ENV)["PXE_IP"], install=True))


_norm_mac = pxeserver.norm_mac


def mac_cfg_name(mac):
    return "grub.cfg-01-" + _norm_mac(mac).replace(":", "-")


def _write_atomic(path, text):
    atomicfile.write_text(path, text)


def _pxe_event(msg):
    cartlog.event("pxe", msg)


def pxe_mode_read(path=PXE_MODE, now=None):
    """{"mode": "rescue"} / {"mode": "install", mac, since, until} / 过期则 mode="expired"."""
    return pxeserver.pxe_mode(path, now)


def pxe_arm(mac, now=None, root=TFTP_ROOT, path=PXE_MODE, seconds=ARM_SECONDS):
    now = time.time() if now is None else now
    mac = _norm_mac(mac)
    _write_atomic(os.path.join(root, "grub.cfg"), grub_cfg_rescue())
    _write_atomic(os.path.join(root, mac_cfg_name(mac)), grub_cfg_install())
    _write_atomic(path, json.dumps({"mac": mac, "since": now, "until": now + seconds}))
    _pxe_event(f"装机模式解锁 mac={mac} {seconds // 60} 分钟")


def pxe_disarm(root=TFTP_ROOT, path=PXE_MODE):
    """删掉所有按 MAC 的装机菜单, 主菜单写回救援. 开机时也调一次, 保证默认安全.
    整棵 tftp 树里 GRUB 会自己去取的菜单 (grub.cfg / grub.cfg-*: EFI/BOOT 下的、按 IP 命名的)
    只要带 inst.ks= 也一并删掉. grub.cfg.bak-* 这类手工备份 GRUB 不会去取, 不动."""
    root = str(root)
    removed = []
    for d, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(d, name)
            fetched = name == "grub.cfg" or name.startswith("grub.cfg-")
            if not fetched or full == os.path.join(root, "grub.cfg"):
                continue
            if not (name.startswith("grub.cfg-01-") and d == root):
                try:
                    with open(full, errors="replace") as f:
                        if "inst.ks=" not in f.read():
                            continue
                except OSError:
                    continue
            os.remove(full)
            removed.append(os.path.relpath(full, root))
    _write_atomic(os.path.join(root, "grub.cfg"), grub_cfg_rescue())
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    _pxe_event("装机模式上锁, 只剩救援菜单" + (f"; 删掉 {', '.join(sorted(removed))}" if removed else ""))


def _log_ts(line):
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def pxe_consumed(log_text, mac, since):
    """解锁之后, 选定机器成功取走了它的装机菜单 (TFTP RRQ 成功那行)."""
    needle = f"TFTP RRQ /{mac_cfg_name(mac)} ("
    for line in log_text.splitlines():
        ts = _log_ts(line)
        if ts is not None and ts >= since and needle in line:
            return True
    return False


def recent_pxe_clients(log_text, now=None, window=3600):
    """最近来要过地址的机器, 最新在前 (给解锁时选 MAC 用)."""
    now = time.time() if now is None else now
    seen = []
    for line in log_text.splitlines():
        ts = _log_ts(line)
        if ts is None or now - ts > window or " DHCP 客户端 " not in line:
            continue
        mac = line.split(" DHCP 客户端 ", 1)[1].split()[0]
        if mac in seen:
            seen.remove(mac)
        seen.insert(0, mac)
    return seen


def new_code():
    import secrets as _se
    return f"{_se.randbelow(10**6):06d}"


def code_matches(typed, code):
    import hmac as _hm
    return _hm.compare_digest(typed.strip(), code)


# ---------------------------------------------------------------- 预览

def mpv_cmd(wid, dev):
    """嵌进 Tk Frame 的低延迟预览. 鼠标和键盘都不让 mpv 收: X11 下它不订阅这些事件,
    点击才会穿透给 Tk 的 Frame (单击进透传)."""
    # --vo=xv: GT740 + nouveau 下 mpv 自己用 GL 画, 会把碎片画到盖在上面的锁屏上 (推车无合成器)
    return ["mpv", f"--wid={wid}", "--vo=xv", "--no-osc", "--no-input-default-bindings",
            "--input-vo-keyboard=no", "--input-cursor=no", "--really-quiet", "--fs=no",
            # 透传要看着画面操作: 不缓冲, 来一帧显示一帧
            "--profile=low-latency", "--untimed", "--cache=no",
            "--demuxer-lavf-o-add=fflags=+nobuffer",
            # 远程截图口: cart_snap.py 经它存当前帧 (锁屏盖住也拿得到)
            "--input-ipc-server=/run/ops-cart/mpv.sock",
            # 裸路径会被当文件 read(), SC0710 驱动的 read() 不出帧; 显式走 v4l2 mmap 采集
            f"av://v4l2:{dev}"]


# ---------------------------------------------------------------- 透传释放组合

class ReleaseCombo:
    """VMware 式: Ctrl+Alt 都按下且期间没按别的键, 松开其中一个时才释放透传.
    这样 Ctrl+Alt+Del 能完整透传过去. key() 收修饰键名 ("ctrl"/"alt"/...) 或普通键名."""

    def __init__(self):
        self.held, self.armed = set(), False

    def key(self, name, down):
        if down:
            self.held.add(name)
            if name in ("ctrl", "alt"):
                if {"ctrl", "alt"} <= self.held and self.held <= {"ctrl", "alt"}:
                    self.armed = True
            else:
                self.armed = False          # 组合被用掉 (Ctrl+Alt+Del 之类)
            return False
        fire = self.armed and name in ("ctrl", "alt")
        self.held.discard(name)
        if fire or not self.held:
            self.armed = False
        return fire


# ---------------------------------------------------------------- 透传键盘: 物理键码 -> HID

# Linux evdev 键码 -> USB HID usage (修饰键给位); X 键码 = evdev + 8
_EVDEV_HID = {
    1: 0x29, 14: 0x2A, 15: 0x2B, 28: 0x28, 57: 0x2C, 58: 0x39,
    12: 0x2D, 13: 0x2E, 26: 0x2F, 27: 0x30, 43: 0x31, 39: 0x33, 40: 0x34, 41: 0x35,
    51: 0x36, 52: 0x37, 53: 0x38, 86: 0x64, 127: 0x65,
    99: 0x46, 70: 0x47, 119: 0x48, 110: 0x49, 102: 0x4A, 104: 0x4B, 111: 0x4C,
    107: 0x4D, 109: 0x4E, 106: 0x4F, 105: 0x50, 108: 0x51, 103: 0x52,
    69: 0x53, 98: 0x54, 55: 0x55, 74: 0x56, 78: 0x57, 96: 0x58, 83: 0x63,
    79: 0x59, 80: 0x5A, 81: 0x5B, 75: 0x5C, 76: 0x5D, 77: 0x5E, 71: 0x5F, 72: 0x60, 73: 0x61, 82: 0x62,
    87: 0x44, 88: 0x45,
}
_EVDEV_HID.update({2 + i: 0x1E + i for i in range(10)})                 # 1..0
_EVDEV_HID.update({59 + i: 0x3A + i for i in range(10)})                # F1..F10
for _ev, _c in zip((30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20,
                    22, 47, 17, 45, 21, 44), "abcdefghijklmnopqrstuvwxyz"):
    _EVDEV_HID[_ev] = 0x04 + ord(_c) - ord("a")
_EVDEV_MOD = {29: 0x01, 42: 0x02, 56: 0x04, 125: 0x08, 97: 0x10, 54: 0x20, 100: 0x40, 126: 0x80}

X_KEYCODE_HID = {ev + 8: ("key", u) for ev, u in _EVDEV_HID.items()}
X_KEYCODE_HID.update({ev + 8: ("mod", b) for ev, b in _EVDEV_MOD.items()})
_MOD_NAMES = {0x01: "ctrl", 0x10: "ctrl", 0x02: "shift", 0x20: "shift",
              0x04: "alt", 0x40: "alt", 0x08: "gui", 0x80: "gui"}


def mod_name(xkeycode):
    kind, val = X_KEYCODE_HID.get(xkeycode, (None, None))
    return _MOD_NAMES[val] if kind == "mod" else None


class HidKeyboard:
    """按住/松开同步成 8 字节报告 (Alt 按着连按 Tab、长按都行); 状态没变返回 None."""

    def __init__(self):
        self.mods, self.keys = 0, []

    def _report(self):
        return bytes([self.mods, 0] + self.keys + [0] * (6 - len(self.keys)))

    def press(self, xkeycode):
        kind, val = X_KEYCODE_HID.get(xkeycode, (None, None))
        if kind == "mod":
            if self.mods & val:
                return None
            self.mods |= val
        elif kind == "key":
            if val in self.keys or len(self.keys) >= 6:
                return None
            self.keys.append(val)
        else:
            return None
        return self._report()

    def release(self, xkeycode):
        kind, val = X_KEYCODE_HID.get(xkeycode, (None, None))
        if kind == "mod" and self.mods & val:
            self.mods &= ~val
        elif kind == "key" and val in self.keys:
            self.keys.remove(val)
        else:
            return None
        return self._report()

    def release_all(self):
        self.mods, self.keys = 0, []
        return self._report()


# ---------------------------------------------------------------- 绝对鼠标

_MOUSE_BITS = {1: 0x01, 2: 0x04, 3: 0x02}   # Tk 左/中/右 -> HID 位


def mouse_bit(num):
    """Tk 鼠标键号 -> HID 按键位; 4/5 是滚轮不是键, 返回 None."""
    return _MOUSE_BITS.get(int(num)) if str(num).isdigit() else None


def wheel_step(ev_type, num, delta):
    """一个 Tk 事件折算成几格滚轮 (正 = 上滚). X11 上滚轮是 ButtonPress-4/5,
    Windows/macOS 是 MouseWheel(delta, 一格 120); 按幅度向外取整, 正负对称."""
    if ev_type == "4":
        return {"4": 1, "5": -1}.get(str(num), 0)
    if ev_type == "38" and delta:
        n = max(1, round(abs(delta) / 120))
        return n if delta > 0 else -n
    return 0


def abs_point(x, y, fw, fh, vw, vh):
    """预览框里的点 -> CH9329 绝对坐标 (0~4095). mpv 等比缩放居中, 点在黑边上返回 None."""
    scale = min(fw / vw, fh / vh)
    dw, dh = vw * scale, vh * scale
    rx, ry = x - (fw - dw) / 2, y - (fh - dh) / 2
    if not (0 <= rx <= dw - 1 and 0 <= ry <= dh - 1):
        return None
    return round(rx * 4095 / (dw - 1)), round(ry * 4095 / (dh - 1))


def parse_v4l2_size(text):
    import re as _re
    m = _re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def video_size(dev):
    """采集设备当前分辨率; 读不到按 1920x1080 (16:9) 算."""
    rc, out = run(["v4l2-ctl", "-d", dev, "--get-fmt-video"], timeout=3)
    return parse_v4l2_size(out) or (1920, 1080)


# ---------------------------------------------------------------- 命令行 (部署脚本用)

def main(argv=None):
    """python3 kiosk_core.py --write-rescue: 扫干净 tftp 树, 只留救援菜单 (setup-cart.sh 调).
    菜单只从这里 (pxeserver.grub_menu) 生成, 部署脚本不再自己写 grub.cfg."""
    ap = argparse.ArgumentParser(prog="kiosk_core.py")
    ap.add_argument("--write-rescue", action="store_true", help="写救援菜单并删掉所有自动装机菜单")
    ap.add_argument("--root", default=TFTP_ROOT)
    ap.add_argument("--state", default=PXE_MODE)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if not a.write_rescue:
        ap.print_usage(sys.stderr)
        return 2
    pxe_disarm(root=a.root, path=a.state)
    print(f"救援菜单已写入 {os.path.join(a.root, 'grub.cfg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
