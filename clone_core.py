#!/usr/bin/env python3
"""clone_core: 整盘克隆 (Clonezilla Live 经 PXE) 的纯逻辑 (#18). 只用标准库, 不碰文件、不起进程.

- 启动参数生成: ocs-sr 命令、镜像仓库 (ocs_repository)、整串内核参数
- 目标盘显式匹配: 按型号/序列号/容量挑出唯一一块盘, 给出 SERIALNO=<序列号>; 绝不默认 sda
- 批量清单: 一批 MAC + 镜像名 + 每台登记的磁盘 -> 每台放不放行、为什么

GRUB 菜单模板不在这里 (全仓库只在 pxeserver 一处); 这里只产出参数 token, kiosk_core 拼起来交给
pxeserver.grub_menu_clone. 参数出处 (官方文档):
- 启动参数  https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/99_Misc/00_live-boot-parameters.doc
- ocs-sr    https://clonezilla.org/fine-print-live-doc.php?path=clonezilla-live/doc/98_ocs_related_command_manpages/01-ocs-sr.doc
- 保留字    https://clonezilla.org/advanced/reserved-word-ocs-sr.php  (SERIALNO=, ask_user, autoname...)
- PXE 启动  https://clonezilla.org/livepxe.php
"""

import re

CLONE_PURPOSES = ("clone-save", "clone-restore")

# ---------------------------------------------------------------- ocs-sr 选项
# 每个选项为什么加 (引号里是 ocs-sr 手册原话). 只有 -batch 跳过确认; 跳过检查的一律不加:
#   -scr  "By default Clonezilla will check the image if restorable before restoring" -> 不跳, 坏镜像不往盘上写
#   -icds "Skip checking destination disk size before creating the partition table"  -> 不跳, 盘小了就停
#   -k / -k1 / -k2 不建分区表 / 按比例改分区表                                       -> 不加, 用镜像里的 (-k0)
#   -c    "Wait for confirmation before saving or restoring"                           -> 不加: 确认在推车上做
#                                                                                         (批量清单 + 验证码)
#   -b    手册: 启动参数里必须写 -batch 不能写 -b, 否则 init 也会吃 -b                  -> 只用 -batch
#   -p reboot (默认)                                                                   -> 不用: 重启后若 PXE 在前,
#                                                                                         会再取一次菜单再写一次盘
SAVE_OPTS = (
    "-q2",          # "Use partclone to save partition(s)": 只存用到的块, 比 dd 小且快 (官方 beginner 默认)
    "-j2",          # "Use dd to clone the image of the data between MBR and 1st partition": 有的引导程序藏在这
    "-z9p",         # "Compress using zstdmt when saving": 多线程 zstd (beginner 默认)
    "-sfsck",       # "Skip running fsck on the source file system": fsck 修复会写源盘, 抓镜像不许动源盘
    "-senc",        # "Skip encrypting the image with passphrase": 不跳会停下来问口令, 无人值守卡死
    "-p", "poweroff",   # 做完关机 (见上: 不用默认的 reboot)
    "-batch",       # "(DANGEROUS!) Run program in batch mode, i.e. without any prompt": 无人值守;
                    # 抓镜像只读目标盘, 写的是推车仓库, 且推车核过镜像名不存在 (不覆盖旧镜像)
)
RESTORE_OPTS = (
    "-g", "auto",   # "Install grub in the MBR of the disk containing partition GRUB_PARTITION", auto=照镜像里的
    "-e1", "auto",  # "Force to change the CHS value of NTFS boot partition after image is restored" (beginner 默认)
    "-e2",          # "Force to use the CHS from EDD when creating partition table by sfdisk" (beginner 默认)
    "-r",           # "Resize the partition when restoration finishes": 文件系统撑满分区 (beginner 默认)
    "-j2",          # 恢复 MBR 与第一个分区之间的数据; 手册: "only works for restoredisk mode"
    "-k0",          # "Create partition table based on the partition table from the image": 默认值, 写明以免误改
    "-p", "poweroff",
    "-batch",       # DANGEROUS: 不再问 "Are you sure"; 只在 (1) 推车上人看过整批清单并输了验证码,
                    # (2) 目标盘按序列号唯一匹配 (SERIALNO=) 时才生成, 不会落到 sda / 第一块盘
)
# 目标盘只认 SERIALNO=: 手册 "When in restoring mode, the DEVICE name can also be assigned by (for disk only):
# SERIALNO (short format), e.g., SERIALNO=2YK8XSYD. If there are spaces in SERIALNO, replace every space by _"
# 设备名 sda/nvme0n1 每次开机顺序可能变, 不用; all / ask_user 是保留字, 更不许.
_SERIAL_RE = re.compile(r"^SERIALNO=[A-Za-z0-9][A-Za-z0-9._:-]*$")

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# 镜像名保留字 (reserved-word-ocs-sr): 会让 ocs-sr 自己起名或问人, 不是我们要的那个目录
_IMAGE_RESERVED = ("ask_user", "autoname", "autohostname", "autoproductname")


def image_name_error(name):
    """镜像名 (仓库里的目录名) 不合格的原因; 合格返回 None. 只许字母数字 . _ -, 不许保留字."""
    if not isinstance(name, str) or not _IMAGE_RE.match(name):
        return f"镜像名不合格: {name!r} (只许字母数字和 . _ -, 不以 . 开头, 最长 64)"
    if name in _IMAGE_RESERVED or name.startswith("autoname-"):
        return f"镜像名 {name!r} 是 Clonezilla 保留字"
    return None


def _purpose(purpose):
    if purpose not in CLONE_PURPOSES:
        raise ValueError(f"克隆用途不对: {purpose!r}")


def ocs_sr_cmd(purpose, image, device, multicast=None):
    """-> ocs_live_run 里跑的 ocs-sr 命令. device 必须是 SERIALNO=<序列号> (来自 match_disk).

    multicast: 组播恢复的参数位置, 第一版不实现 (传了就 NotImplementedError, 不退成单播).
    TODO(#18 组播): 客户端改成 `ocs-sr ... --mcast-port <port> multicast_restoredisk <image> <device>`,
    推车上另起 udpcast 发送端 (ocs-live-feed-img / drbl-ocs), 下面三个键在发送端用:
      clients_to_wait   等够几台再开始 (= 这一批 MAC 数)
      max_wait_s        等不齐时最多等几秒就开始 (之后才来的那台单独单播)
      max_bitrate_mbps  限速, 别把交换机/上联打满
    以及组播不通时退到 BitTorrent 模式. 交换机 IGMP snooping 的实机情况见 docs/CLONE.md."""
    _purpose(purpose)
    err = image_name_error(image)
    if err:
        raise ValueError(err)
    if not isinstance(device, str) or not _SERIAL_RE.match(device):
        raise ValueError(f"目标盘只认 SERIALNO=<序列号>, 不是 {device!r}")
    if multicast is not None:
        if purpose != "clone-restore":
            raise ValueError("只有恢复镜像才有组播")
        raise NotImplementedError("组播 (udpcast) 第一版没做, 只有单播; 见 clone_core.ocs_sr_cmd 的 TODO")
    if purpose == "clone-save":
        opts, verb = SAVE_OPTS, "savedisk"
    else:
        opts, verb = RESTORE_OPTS, "restoredisk"
    return " ".join(("ocs-sr",) + opts + (verb, image, device))


# ---------------------------------------------------------------- 镜像仓库 (ocs_repository)
# 启动参数文档: ocs_repository="smb://[domain;user:password@]host/path" / "(smb1|...|smb3|...)://..." /
# "nfs|nfs4://host/path". 仓库挂到 /home/partimag, 镜像就是下面的 <镜像名>/ 目录.
# 抓镜像和恢复用两个共享 (save_share 可写, restore_share 只读), 恢复不拿能写的账号去挂.

_SMB_VERSIONS = ("smb", "smb1", "smb1.0", "smb2", "smb2.0", "smb2.1", "smb3", "smb3.0", "smb3.11", "smb3.1.1")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_CRED_RE = re.compile(r"^[A-Za-z0-9._-]+$")          # 进 URL 和内核命令行: 不许 @ : / 空格 引号 $
_SHARE_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def _share(value, nfs):
    if not isinstance(value, str):
        raise ValueError("镜像仓库没配共享")
    if nfs and not value.startswith("/"):
        raise ValueError(f"NFS 导出路径要写绝对路径: {value!r}")
    path = value.strip("/")
    if not _SHARE_RE.match(path) or any(p in (".", "..") for p in path.split("/")):
        raise ValueError(f"共享名/路径不合格: {value!r}")
    return path


def repo_url(conf, purpose, default_server):
    """克隆配置 -> ocs_repository 的值. conf 键:
    proto: smb | nfs | nfs4;  server: 不写就用推车装机口地址;  local_dir: 推车上镜像目录 (这里不用)
    save_share / restore_share: SMB 共享名 (可带子目录) 或 NFS 导出绝对路径
    smb: user + password 是抓镜像的可写账号; restore_user + restore_password 是恢复用的只读账号,
         不写就当访客挂 (不回退到可写账号); smb_version: smb3 之类, 不写用 smb.
    不合格抛 ValueError (这些都会进内核命令行, 所以字符集收得很紧)."""
    _purpose(purpose)
    if not isinstance(conf, dict):
        raise ValueError("克隆配置不是对象")
    proto = conf.get("proto")
    server = conf.get("server") or default_server
    if not isinstance(server, str) or not _HOST_RE.match(server):
        raise ValueError(f"镜像仓库地址不合格: {server!r}")
    key = "save_share" if purpose == "clone-save" else "restore_share"
    if proto in ("nfs", "nfs4"):
        return f"{proto}://{server}/{_share(conf.get(key), True)}/"
    if proto != "smb":
        raise ValueError(f"镜像仓库只支持 smb / nfs / nfs4, 不是 {proto!r}")
    scheme = conf.get("smb_version") or "smb"
    if scheme not in _SMB_VERSIONS:
        raise ValueError(f"smb_version 不认识: {scheme!r}")
    ukey, pkey = ("user", "password") if purpose == "clone-save" else ("restore_user", "restore_password")
    user, password = conf.get(ukey) or "", conf.get(pkey) or ""
    cred = ""
    if user:
        if not _CRED_RE.match(user) or not _CRED_RE.match(password):
            raise ValueError(f"{ukey}/{pkey} 不合格 (要有密码, 只许字母数字和 . _ -)")
        cred = f"{user}:{password}@"
    elif password:
        raise ValueError(f"有 {pkey} 没有 {ukey}")
    return f"{scheme}://{cred}{server}/{_share(conf.get(key), False)}/"


# ---------------------------------------------------------------- 目标盘显式匹配
# 输入: 目标机登记的磁盘 = `lsblk -dbJ -o NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,RM` 的输出 (整个 dict 或其
# blockdevices 列表). 规则键:
#   model     型号, 忽略大小写和多余空格后整串相等 (不做子串: "512GB" 会同时套住好几种盘)
#   serial    序列号, 整串相等 (空格按 Clonezilla 的规矩换成 _ 后比)
#   size_min / size_max  字节, 闭区间; 写了就要求盘报了容量
#   tran      接口 (nvme / sata / usb ...)
# model / serial 至少一个: 只按容量或接口太容易套到别的盘. 不认识的键整条规则作废 (拼错一个键会让规则变宽).
# 只看整盘 (type 缺省或 disk); 可移动 / USB 盘除非规则写明 tran=usb, 否则不算候选.
# 恰好一块才给 SERIALNO=<序列号>; 零块、多块、没序列号、序列号有怪字符、序列号和别的盘重复都不给.

_RULE_KEYS = ("model", "serial", "size_min", "size_max", "tran")


def _norm_text(s):
    return " ".join(s.split()).casefold() if isinstance(s, str) else ""


def _norm_serial(s):
    return "_".join(s.split()) if isinstance(s, str) else ""


def _size(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _truthy(v):
    return v is True or v == 1 or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"))


def rule_error(rule):
    """规则不合格的原因; 合格返回 None."""
    if not isinstance(rule, dict) or not rule:
        return "盘匹配规则为空"
    bad = [k for k in rule if k not in _RULE_KEYS]
    if bad:
        return f"盘匹配规则里有不认识的键 {', '.join(map(str, bad))} (只认 {', '.join(_RULE_KEYS)})"
    for k in ("model", "serial", "tran"):
        if k in rule and not isinstance(rule[k], str):
            return f"盘匹配规则 {k} 要写字符串"
    if not _norm_text(rule.get("model")) and not _norm_serial(rule.get("serial")):
        return "盘匹配规则至少要有 model 或 serial (只按容量/接口太容易套到别的盘)"
    for k in ("size_min", "size_max"):
        if k in rule and (isinstance(rule[k], bool) or not isinstance(rule[k], int) or rule[k] < 0):
            return f"盘匹配规则 {k} 要写非负整数 (字节)"
    if rule.get("size_min", 0) > rule.get("size_max", rule.get("size_min", 0)):
        return "盘匹配规则 size_min 比 size_max 大"
    return None


def _disk_list(disks):
    if isinstance(disks, dict):
        disks = disks.get("blockdevices")
    if not isinstance(disks, list):
        return []
    return [d for d in disks if isinstance(d, dict) and d.get("type", "disk") == "disk"]


def _fits(d, rule):
    if "model" in rule and _norm_text(rule["model"]) and _norm_text(d.get("model")) != _norm_text(rule["model"]):
        return False
    if "serial" in rule and _norm_serial(rule["serial"]) and \
            _norm_serial(d.get("serial")) != _norm_serial(rule["serial"]):
        return False
    tran = _norm_text(d.get("tran"))
    want = _norm_text(rule.get("tran"))
    if want and tran != want:
        return False
    if want != "usb" and (tran == "usb" or _truthy(d.get("rm"))):
        return False
    size = _size(d.get("size"))
    if ("size_min" in rule or "size_max" in rule) and size is None:
        return False
    if "size_min" in rule and size < rule["size_min"]:
        return False
    if "size_max" in rule and size > rule["size_max"]:
        return False
    return True


def _describe(d):
    return f"{d.get('name', '?')} {d.get('model') or '?'} {d.get('serial') or '无序列号'} {d.get('size', '?')}"


def match_disk(disks, rule):
    """-> (device, None) 或 (None, 原因). device 形如 SERIALNO=SN-EXAMPLE-1, 直接给 ocs-sr 用.
    绝不默认 sda / 第一块盘: 任何拿不准的情况都是 (None, 原因)."""
    err = rule_error(rule)
    if err:
        return None, err
    whole = _disk_list(disks)
    if not whole:
        return None, "没有登记这台机器的磁盘信息"
    hits = [d for d in whole if _fits(d, rule)]
    if not hits:
        return None, "没有盘符合规则; 登记的盘: " + "; ".join(_describe(d) for d in whole)
    if len(hits) > 1:
        return None, f"有 {len(hits)} 块盘都符合规则, 不猜: " + "; ".join(_describe(d) for d in hits)
    serial = _norm_serial(hits[0].get("serial"))
    if not serial:
        return None, f"匹配到的盘没有序列号, 没法用 SERIALNO= 指定: {_describe(hits[0])}"
    device = "SERIALNO=" + serial
    if not _SERIAL_RE.match(device):
        return None, f"匹配到的盘序列号有不能进启动参数的字符: {hits[0].get('serial')!r}"
    if sum(1 for d in whole if _norm_serial(d.get("serial")) == serial) > 1:
        return None, f"序列号 {serial} 和别的盘重复, SERIALNO= 指不准"
    return device, None


# ---------------------------------------------------------------- 整串内核参数
# 照 livepxe.php 的 APPEND 例子, 去掉交互相关的; fetch= (squashfs 从哪取) 是模板的事, pxeserver 加.
_LIVE_BASE = ("boot=live", "union=overlay", "username=user", "config", "components", "quiet", "noswap",
              "edd=on", "nomodeset", "nodmraid",
              "locales=en_US.UTF-8", "keyboard-layouts=us",   # 文档: 建议在启动参数里给, 免得开机问语言/键盘
              "net.ifnames=0", "nosplash", "noprompt")


def boot_params(purpose, image, device, conf, ip, multicast=None):
    """-> 内核参数 token 列表 (不含 kernel/initrd/fetch). 值里的双引号是 Clonezilla 自己 grub.cfg 的写法,
    pxeserver 写进 GRUB 时转义成 \\".
    ocs_live_run: 文档 "Available program: ocs-live-general, ocs-live-restore or any command you write" -> 直接跑 ocs-sr
    ocs_live_extra_param: 文档 "will be used only when ocs_live_run=ocs-live-restore" -> 留空
    ocs_live_batch: 文档 "Batch mode or not (yes/no), if no, will run interactively" -> yes, 不问语言/键盘;
                    写盘前的确认由 ocs-sr 的 -batch 决定 (见 RESTORE_OPTS)
    ocs_repository: repo_url(); 不用 ocs_prerun 手写 mount (少一处能塞命令的地方)"""
    cmd = ocs_sr_cmd(purpose, image, device, multicast=multicast)
    repo = repo_url(conf, purpose, ip)
    return list(_LIVE_BASE) + [f'ocs_live_run="{cmd}"', 'ocs_live_extra_param=""', 'ocs_live_batch="yes"',
                               f'ocs_repository="{repo}"']
