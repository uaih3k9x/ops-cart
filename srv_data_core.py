#!/usr/bin/env python3
"""srv_data_core: 数据盘 /srv/data 和装机库 ISO 只读挂载 (#11). 只用标准库.

- 数据盘按 UUID 挂 /srv/data (XFS, nofail): 没装盘时不挡开机, PXE 照旧. 换盘只改 UUID, 这里不写容量
- /srv/data/iso/<名>.iso 每个生成一个 systemd .mount 单元, 只读挂到 /srv/data/win/<名>/
  (单元名按 systemd-escape --path 规则转义, 推车上不依赖 systemd-escape 命令)
- 目录布局见 LAYOUT; 建目录的 tmpfiles 在 deploy/srv-data/srv-data.tmpfiles.conf (测试核对两边一致)

格式化/分区不在这里, 也不在任何脚本里: 只写在 docs/DATA_DISK.md, 让人自己确认执行.

CLI (setup-srv-data.sh 用):
  srv_data_core.py data-unit <UUID>                         打印 srv-data.mount
  srv_data_core.py iso-units --iso-dir D --unit-dir U       按 D 里的 ISO 同步 U 里生成的单元, 打印 write/remove
"""

import argparse
import os
import re
import sys

DATA = "/srv/data"
SMB_GROUP = "ops-smb"           # 可写目录的属组; 装机/抓镜像的 Samba 账号进这个组
LAYOUT = {
    "iso": DATA + "/iso",           # Windows ISO 文件本身 (win10.iso / win11.iso)
    "win": DATA + "/win",           # ISO 只读挂载点: win/<名>/setup.exe
    "drivers": DATA + "/drivers",   # 驱动总裁: 可写, PE 里直接跑
    "tools": DATA + "/tools",       # 常用工具: 只读共享, 推车上 root 往里放
    "org": DATA + "/org",           # 单位的数据: 交接/离开时整块拷走或清掉
    "clone": DATA + "/org/clone",   # #18 克隆镜像 (含单位机器内容, 最占空间)
}
ISO_MARK = "# ops-cart srv-data iso: setup-srv-data.sh 生成, 别手改"
ISO_CONTEXT = "context=system_u:object_r:samba_share_t:s0"     # ISO 只读不能 restorecon, 挂载时就给 smbd 能读的标签

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ISO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}\.iso$")
_VALID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.")


def escape_path(path):
    """systemd-escape --path: 去重/去首尾斜杠, / -> -, 合法字符外 (和整串开头的 .) 转 \\xNN (按 UTF-8 字节)."""
    p = "/".join(s for s in path.split("/") if s)
    if not p:
        return "-"
    out = []
    for i, b in enumerate(p.encode("utf-8")):
        c = chr(b)
        if c == "/":
            out.append("-")
        elif c not in _VALID or (i == 0 and c == "."):
            out.append("\\x%02x" % b)
        else:
            out.append(c)
    return "".join(out)


def mount_unit_name(path):
    return escape_path(path) + ".mount"


def check_uuid(s):
    """文件系统 UUID (blkid 输出的 8-4-4-4-12) -> 小写; 不像就抛 ValueError (别把 /dev/sdX 塞进来)."""
    u = s.strip().lower() if isinstance(s, str) else ""
    if not _UUID_RE.match(u):
        raise ValueError(f"不是文件系统 UUID (要 blkid 里那种 8-4-4-4-12): {s!r}")
    return u


def data_mount_unit(uuid):
    u = check_uuid(uuid)
    return f"""# ops-cart 数据盘 (#11): setup-srv-data.sh 生成. 换盘 = 数据拷过去 + 重跑脚本换 UUID
# nofail: 没装数据盘时只是这个单元失败, 不挡开机, PXE 照旧
[Unit]
Description=推车数据盘 {DATA}

[Mount]
What=/dev/disk/by-uuid/{u}
Where={DATA}
Type=xfs
Options=defaults,noatime,nofail
TimeoutSec=30

[Install]
WantedBy=multi-user.target
"""


def iso_name_error(name):
    """ISO 文件名 -> 不合格的原因; 合格 None. 只许 ASCII 字母数字和 . _ - (PE 里要敲路径, 也要进单元名)."""
    if not isinstance(name, str) or not name.endswith(".iso"):
        return "不是 .iso"
    if not _ISO_RE.match(name):
        return "文件名只许字母数字和 . _ -, 字母数字开头, 不超过 64 个字符; 改个名 (比如 win11.iso)"
    return None


def iso_unit(name):
    """ISO 文件名 -> (单元名, 单元文本)."""
    err = iso_name_error(name)
    if err:
        raise ValueError(f"{name}: {err}")
    stem = name[:-len(".iso")]
    what, where = f"{LAYOUT['iso']}/{name}", f"{LAYOUT['win']}/{stem}"
    return mount_unit_name(where), f"""{ISO_MARK}
[Unit]
Description=装机库 ISO 只读挂载 {name}
RequiresMountsFor={DATA}
ConditionPathExists={what}

[Mount]
What={what}
Where={where}
Type=udf
Options=ro,loop,nofail,{ISO_CONTEXT}

[Install]
WantedBy=multi-user.target
"""


def iso_plan(files, existing):
    """files: ISO 目录里的文件名; existing: {单元名: 文本} (单元目录里现有的 .mount).
    -> {"write": {单元名: 文本}, "remove": [带记号但不再需要的单元名], "skipped": [(文件名, 原因)]}.
    没记号的单元 (人手写的、数据盘本身) 不碰."""
    write, skipped = {}, []
    for f in sorted(files):
        if not f.endswith(".iso"):
            continue
        err = iso_name_error(f)
        if err:
            skipped.append((f, err))
            continue
        n, text = iso_unit(f)
        write[n] = text
    remove = sorted(n for n, t in existing.items() if t.startswith(ISO_MARK) and n not in write)
    return {"write": write, "remove": remove, "skipped": skipped}


def _sync_iso_units(iso_dir, unit_dir):
    try:
        files = os.listdir(iso_dir)
    except OSError:
        files = []
    existing = {}
    for n in os.listdir(unit_dir):
        if n.endswith(".mount"):
            try:
                with open(os.path.join(unit_dir, n), encoding="utf-8") as f:
                    existing[n] = f.read()
            except OSError:
                pass
    p = iso_plan(files, existing)
    for f, why in p["skipped"]:
        print(f"跳过 {f}: {why}", file=sys.stderr)
    for n, text in p["write"].items():
        with open(os.path.join(unit_dir, n), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"write {n}")
    for n in p["remove"]:
        os.unlink(os.path.join(unit_dir, n))
        print(f"remove {n}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("data-unit")
    a.add_argument("uuid")
    a = sub.add_parser("iso-units")
    a.add_argument("--iso-dir", default=LAYOUT["iso"])
    a.add_argument("--unit-dir", default="/etc/systemd/system")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "data-unit":
            sys.stdout.write(data_mount_unit(args.uuid))
            return 0
        return _sync_iso_units(args.iso_dir, args.unit_dir)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
