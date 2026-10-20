#!/usr/bin/env python3
"""kiosk_core: 推车 Kiosk 的可测核心层. UI(curses)之外的逻辑都在这.

- 状态采集: systemd 服务 / 网卡 / tailscale / CH9329
- 命令生成: nmcli 改地址, PXE 服务重渲染 (纯函数, 测试不打真机)
- 配置: /opt/pxe/pxe.conf (LAN1 的 PXE 网段与地址池)
"""

import json
import os
import subprocess
import time

PXE_CONF = "/opt/pxe/pxe.conf"
LAN1_CON = "Lan1-pxe"   # eno1, 装机口
LAN2_CON = "enp3s0"     # 管理口

DEFAULT_PXE_CONF = {"ip": "192.168.77.1", "prefix": 24,
                    "pool_start": "192.168.77.10", "pool_end": "192.168.77.20"}


def run(cmd, timeout=5):
    """跑命令, 返回 (rc, stdout). 失败不抛异常, UI 层自己消化."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return 1, str(e)


# ---------------------------------------------------------------- 状态

def services_status(names=("pxe-server", "pxe-http-repo", "pxe-http-boot")):
    """{服务名: (enabled, active)}"""
    out = {}
    for n in names:
        _, en = run(["systemctl", "is-enabled", n])
        _, ac = run(["systemctl", "is-active", n])
        out[n] = (en.strip() == "enabled", ac.strip() == "active")
    return out


def nics_status():
    """[{name, con, ip, method, link}] 只看两个业务口."""
    result = []
    for con, name, role in ((LAN1_CON, "eno1", "PXE 装机口"),
                            (LAN2_CON, "enp3s0", "管理口")):
        _, ip = run(["ip", "-4", "-br", "addr", "show", name])
        ip = ip.split()[-1] if ip else "-"
        _, method = run(["nmcli", "-g", "ipv4.method", "con", "show", con])
        _, link = run(["cat", f"/sys/class/net/{name}/carrier"])
        result.append({"name": name, "con": con, "role": role, "ip": ip,
                       "method": method or "-", "link": link.strip() == "1"})
    return result


CH9329_BY_ID = "/dev/serial/by-id/usb-1a86_*-if00"   # CH343 串口; 重插后 ttyACMn 会变, by-id 不变


def ch9329_port(pattern=CH9329_BY_ID, fallback="/dev/ttyACM0"):
    import glob
    hits = sorted(glob.glob(pattern))
    return hits[0] if hits else fallback


def misc_status():
    _, ts_ip = run(["tailscale", "ip", "-4"])
    ch = os.path.exists(ch9329_port())
    _, up = run(["uptime", "-p"])
    return {"tailscale_ip": ts_ip or "-", "ch9329": ch, "uptime": up or "-"}


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
    return ("OK", os.path.basename(os.path.realpath(ch9329_port()))) if ctx["misc"].get("ch9329") \
        else ("--", "NOT CONNECTED")


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


CHECKS = {"lan": check_lan, "tailscale": check_tailscale, "serial": check_serial,
          "ch9329": check_ch9329, "pxe": check_pxe, "cpu": check_cpu, "fan": check_fan,
          "ping": check_ping, "pci": check_pci}

DEFAULT_PLAN = [{"check": "lan", "label": "LAN2", "iface": "enp3s0"},
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


def selftest_run(plan, ctx, checks=CHECKS):
    """按计划逐项跑; 单项抛异常只把那一行标 FAIL, 不拖垮整页."""
    rows = []
    for item in plan:
        params = {k: v for k, v in item.items() if k not in ("check", "label")}
        label = item.get("label", item.get("check", "?").upper())
        fn = checks.get(item.get("check"))
        if fn is None:
            state, detail = "FAIL", "UNKNOWN CHECK"
        else:
            try:
                state, detail = fn(ctx, **params)
            except Exception:
                state, detail = "FAIL", "ERROR"
        rows.append({"label": label, "state": state, "detail": detail})
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


def selftest_context():
    """真机快照. SMART 要 sudo 且慢, 自检不读."""
    return {"nics": nics_status(), "misc": misc_status(), "svcs": services_status(),
            "temps": temps_status(smart=lambda: None), "fans": fans_status(),
            "exists": os.path.exists, "pci": pci_devices,
            "ping": lambda h: run(["ping", "-c", "1", "-W", "1", h], timeout=3)[0] == 0}


def selftest_rows(nics, misc, svcs, temps, fans, serial_ok):
    """旧接口: 默认计划 + 给定快照."""
    ctx = {"nics": nics, "misc": misc, "svcs": svcs, "temps": temps, "fans": fans,
           "exists": lambda p: serial_ok, "ping": lambda h: False, "pci": lambda v, d: []}
    return selftest_run(DEFAULT_PLAN, ctx)


def selftest_line(row, width=24):
    """"LAN2 ............ OK   192.168.9.228\""""
    return f"{row['label'] + ' ':.<{width}} {row['state']:<4} {row['detail']}".rstrip()


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


def next_video_device(devs, current):
    if not devs:
        return current
    return devs[(devs.index(current) + 1) % len(devs)] if current in devs else devs[0]


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


def pxe_conf_read():
    try:
        with open(PXE_CONF) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_PXE_CONF.items():
            cfg.setdefault(k, v)
        return cfg
    except (OSError, ValueError):
        return dict(DEFAULT_PXE_CONF)


def pxe_conf_write(cfg):
    with open(PXE_CONF, "w") as f:
        json.dump(cfg, f, indent=2)


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


def render_pxe_service(cfg):
    """由配置渲染 pxe-server.service 的 ExecStart."""
    return (f"/usr/bin/python3 /opt/pxe/pxeserver.py serve "
            f"--ip {cfg['ip']} --pool {cfg['pool_start']},{cfg['pool_end']} "
            f"--tftp-root /opt/pxe/tftp --log /opt/pxe/pxeserver.log")


def apply_pxe_conf(cfg, unit_path="/etc/systemd/system/pxe-server.service"):
    """写配置 + 重渲染服务并重启. 返回 (ok, message). unit_path 参数便于测试."""
    if not cfg["ip"] or "/" not in cfg["ip"]:
        return False, "PXE 网卡地址需为 CIDR 形式 (如 192.168.77.1/24)"
    for k in ("pool_start", "pool_end"):
        if not cfg[k]:
            return False, f"缺 {k}"
    pxe_conf_write(cfg)
    unit = ("[Unit]\nDescription=PXE server (DHCP+proxyDHCP+TFTP) on Lan1 install port\n"
            "After=network.target\n\n[Service]\n"
            f"ExecStart={render_pxe_service(cfg)}\n"
            "Restart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n")
    with open(unit_path, "w") as f:
        f.write(unit)
    rc, _ = run(["systemctl", "daemon-reload"])
    rc2, out = run(["systemctl", "restart", "pxe-server"])
    return rc == 0 and rc2 == 0, out or ("ok" if rc2 == 0 else "restart 失败")


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
    with open(path, "w") as f:
        f.write(text)


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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    os.chmod(path, 0o600)


class PinGate:
    """PIN 校验 + 连续输错冷却. attempt() 返回 'ok' / 'bad' / 'locked'."""

    def __init__(self, stored, max_fails=5, lock_s=60):
        self.stored, self.max_fails, self.lock_s = stored, max_fails, lock_s
        self.fails, self.locked_until = 0, 0.0

    @property
    def required(self):
        return bool(self.stored)

    def attempt(self, pin, now=None):
        now = time.time() if now is None else now
        if not self.required:
            return "ok"
        if now < self.locked_until:
            return "locked"
        if pin_verify(pin, self.stored):
            self.fails = 0
            return "ok"
        self.fails += 1
        if self.fails >= self.max_fails:
            self.fails, self.locked_until = 0, now + self.lock_s
            return "locked"
        return "bad"
