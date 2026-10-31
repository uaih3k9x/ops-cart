#!/usr/bin/env python3
"""samba_core: 推车 Samba 配置 (#11). 只用标准库, 生成文本不碰系统.

共享分两类, 互不共用账号:
- 装机: win (ISO 只读挂载)、drivers (驱动总裁, 可写)、tools (只读). 账号 INSTALL_USER 登录, 不收访客
  (Win11 内核的 PE 默认拒绝来宾访问).
- 克隆 (#18): 共享名、目录、账号全照 /opt/pxe/clone.json, 跟 Clonezilla 内核命令行里用的是同一份.
  clone.json 没写 restore_user 时 #18 当访客挂只读共享 -> 只有那一个共享收访客 (Clonezilla 是 Linux,
  不受 Win11 拒来宾影响); 写了就按账号.
网络: 只听 lo + LAN1 (eno1), hosts allow 只放装机网段, 只开 SMB_PORT (不要 NetBIOS).
密码不进 smb.conf, 也不进仓库: 装机账号密码由部署脚本交互输入或读 /etc/ops-cart, 克隆账号密码在 clone.json.

CLI (setup-samba.sh 用):
  samba_core.py render --env pxe.env --clone clone.json     打印 smb.conf (clone.json 不存在 = 不出克隆共享)
  samba_core.py rich-rule --env pxe.env                      打印 firewalld rich rule
  samba_core.py users --clone clone.json                     每行 "<账号> rw|ro"
  samba_core.py clone-password --clone clone.json <账号>     打印两遍密码 (喂 smbpasswd -s)
"""

import argparse
import ipaddress
import json
import os
import re
import sys

import srv_data_core as sd

LAN1 = "eno1"
SMB_PORT = 445                  # SMB over TCP; public-safe-ignore
INSTALL_USER = "winpe"
INSTALL_SHARES = ("win", "drivers", "tools")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _net(ip, prefix):
    try:
        p = int(prefix)
        a = ipaddress.IPv4Address(ip)
    except (ValueError, TypeError):
        raise ValueError(f"装机网段不合格: {ip}/{prefix}")
    if not 8 <= p <= 30 or a.is_unspecified:
        raise ValueError(f"装机网段不合格: {ip}/{prefix}")
    return ipaddress.IPv4Interface(f"{a}/{p}").network


def lan_net(ip, prefix):
    """-> smb.conf hosts allow 用的 "网络/掩码" (Samba 手册写的形式)."""
    n = _net(ip, prefix)
    return f"{n.network_address}/{n.netmask}"


def firewall_rich_rule(ip, prefix):
    """只放装机网段来的 445."""
    return f'rule family="ipv4" source address="{_net(ip, prefix)}" port port="{SMB_PORT}" protocol="tcp" accept'


def _clone(conf):
    """clone.json -> None (不是 smb) 或 {rw, ro, path, user, ro_user}; 不合格抛 ValueError."""
    if conf is None:
        return None
    if not isinstance(conf, dict):
        raise ValueError("clone.json 不是对象")
    if conf.get("proto") != "smb":
        return None
    rw, ro = conf.get("save_share") or "", conf.get("restore_share") or ""
    for n in (rw, ro):
        if not _NAME_RE.match(n) or n in INSTALL_SHARES or n in ("global", "homes", "printers"):
            raise ValueError(f"clone.json 共享名不合格或撞名 (这里只生成整个共享, 不带子目录): {n!r}")
    if rw == ro:
        raise ValueError("clone.json 的 save_share 和 restore_share 要分开 (一个可写一个只读)")
    path = conf.get("local_dir") or ""
    if not path.startswith("/") or not re.match(r"^[A-Za-z0-9/._-]+$", path):
        raise ValueError(f"clone.json 的 local_dir 要写推车上的绝对路径: {path!r}")
    user, ro_user = conf.get("user") or "", conf.get("restore_user") or ""
    if not _NAME_RE.match(user):
        raise ValueError("clone.json 没写可写账号 user (抓镜像共享不收访客)")
    if ro_user and not _NAME_RE.match(ro_user):
        raise ValueError(f"clone.json 的 restore_user 不合格: {ro_user!r}")
    if INSTALL_USER in (user, ro_user):
        raise ValueError(f"克隆账号别用装机账号 {INSTALL_USER}")
    if ro_user == user:
        raise ValueError("restore_user 和 user 要分开 (只读共享别拿能写的账号)")
    return {"rw": rw, "ro": ro, "path": path.rstrip("/") or "/", "user": user, "ro_user": ro_user}


def accounts(conf):
    """-> [(账号, 要不要写权限)]: 装机账号 + clone.json 里的账号 (proto 是 smb 时)."""
    c = _clone(conf)
    out = [(INSTALL_USER, True)]
    if c:
        out.append((c["user"], True))
        if c["ro_user"]:
            out.append((c["ro_user"], False))
    return out


def clone_password(conf, user):
    c = _clone(conf)
    if c and user == c["user"]:
        return conf.get("password") or ""
    if c and c["ro_user"] and user == c["ro_user"]:
        return conf.get("restore_password") or ""
    raise ValueError(f"{user} 不是 clone.json 里的账号")


def _share(name, comment, path, read_only, users=None, guest=False):
    lines = [f"[{name}]", f"   comment = {comment}", f"   path = {path}",
             f"   read only = {'yes' if read_only else 'no'}", "   browseable = yes"]
    if guest:
        lines.append("   guest ok = yes")
    else:
        lines += ["   guest ok = no", f"   valid users = {users}"]
    if not read_only:
        lines += [f"   force group = {sd.SMB_GROUP}", "   create mask = 0664", "   directory mask = 2775"]
    return "\n".join(lines)


def smb_conf(ip, prefix, clone_conf):
    allow = lan_net(ip, prefix)
    c = _clone(clone_conf)
    guest = bool(c and not c["ro_user"])
    head = f"""# /etc/samba/smb.conf —— setup-samba.sh 按 /etc/ops-cart/pxe.env 和 /opt/pxe/clone.json 生成 (#11), 别手改.
# 只给 LAN1 装机网段; 装机共享账号登录. 密码不在这里 (smbpasswd 管).
[global]
   workgroup = WORKGROUP
   server string = ops-cart
   server role = standalone server
   security = user
   passdb backend = tdbsam
   interfaces = lo {LAN1}
   bind interfaces only = yes
   hosts allow = 127.0.0.1 {allow}
   hosts deny = ALL
   smb ports = {SMB_PORT}
   disable netbios = yes
   map to guest = {'Bad User' if guest else 'Never'}
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes
   log file = /var/log/samba/log.%m
   max log size = 1000
"""
    shares = [
        _share("win", "Windows ISO (只读挂载)", sd.LAYOUT["win"], True, INSTALL_USER),
        _share("drivers", "驱动总裁 (可写)", sd.LAYOUT["drivers"], False, INSTALL_USER),
        _share("tools", "常用工具", sd.LAYOUT["tools"], True, INSTALL_USER),
    ]
    if c:
        shares.append(_share(c["rw"], "克隆 抓镜像 (#18)", c["path"], False, c["user"]))
        shares.append(_share(c["ro"], "克隆 恢复只读 (#18)", c["path"], True, c["ro_user"], guest=guest))
    return head + "\n" + "\n\n".join(shares) + "\n"


def _env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, eq, v = line.strip().partition("=")
                if eq and not k.startswith("#"):
                    env[k] = v.strip().strip("\"'")
    except OSError:
        raise ValueError(f"读不到 {path} (照 deploy/pxe.env.example 写 /etc/ops-cart/pxe.env)")
    if "PXE_IP" not in env or "PXE_PREFIX" not in env:
        raise ValueError(f"{path} 里没有 PXE_IP / PXE_PREFIX")
    return env["PXE_IP"], env["PXE_PREFIX"]


def _clone_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} 读不了: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("render", "rich-rule", "users", "clone-password"):
        a = sub.add_parser(name)
        if name in ("render", "rich-rule"):
            a.add_argument("--env", default="/etc/ops-cart/pxe.env")
        if name != "rich-rule":
            a.add_argument("--clone", default="/opt/pxe/clone.json")
        if name == "clone-password":
            a.add_argument("user")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "render":
            ip, prefix = _env(args.env)
            sys.stdout.write(smb_conf(ip, prefix, _clone_file(args.clone)))
        elif args.cmd == "rich-rule":
            print(firewall_rich_rule(*_env(args.env)))
        elif args.cmd == "users":
            for u, rw in accounts(_clone_file(args.clone)):
                print(f"{u} {'rw' if rw else 'ro'}")
        else:
            pw = clone_password(_clone_file(args.clone), args.user)
            if not pw:
                raise ValueError(f"clone.json 里 {args.user} 没有密码")
            sys.stdout.write(f"{pw}\n{pw}\n")
        return 0
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
