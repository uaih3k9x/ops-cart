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


def misc_status():
    _, ts_ip = run(["tailscale", "ip", "-4"])
    ch = subprocess.run(["ls", "/dev/ttyACM0"], capture_output=True).returncode == 0
    _, up = run(["uptime", "-p"])
    return {"tailscale_ip": ts_ip or "-", "ch9329": ch, "uptime": up or "-"}


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
