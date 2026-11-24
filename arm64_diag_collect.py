#!/usr/bin/env python3
"""ARM64 板子诊断收集 (跑在推车 PXE 起的 Alpine 内存系统里, 只用标准库, Python 3.9+).

收什么: uname、/proc/cpuinfo 等、dmidecode 全量、lspci -nnvvv、lsusb -v、lsblk / nvme / smartctl (只读查询)、dmesg、
ACPI 全表 (acpidump 文本 + acpidump -b 二进制 + iasl -d 反编译)、efibootmgr -v、/sys/firmware/efi 下的文件 (含 efivars)、
网卡 (ip / ethtool)、lshw. 打成 ops-diag-<MAC>[-sn-<SMBIOS 序列号>]-<UTC 时间>.tar.gz, PUT 到推车 diag_upload.py.

只读: 只往内存里的目录写 (--out-dir 默认 /tmp, 所在文件系统必须是 tmpfs/ramfs, 否则退出码 3, 一个文件都不建);
不挂载、不分区、不写任何块设备; 跑的命令都是查询类 (见 plan).
开机自启: apkovl 里的 /etc/local.d/ops-diag.start 调 --auto: 内核参数没有 ops_diag=<URL> 就什么都不做.
手动: ops-diag-collect [--url http://<推车>:8081/diag/] [--no-upload];  重传: ops-diag-collect --resend <包>
退出码: 0 好了; 3 输出目录不在内存里; 4 上传失败 (包留在 --out-dir, 屏幕上有手动重传的命令)."""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

RAM_FS = ("tmpfs", "ramfs")
CMD_TIMEOUT = 180
MAX_FILE = 1 << 20
_PLACEHOLDER = {"", "to be filled by o.e.m.", "default string", "none", "not specified", "not applicable",
                "system serial number", "serial number", "123456789", "n/a", "na", "unknown"}
_DISK = re.compile(r"(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+)")


class NotInRam(Exception):
    pass


class UploadFailed(Exception):
    pass


# ---------------------------------------------------------------- 识别板子

def cmdline_url(text):
    for tok in text.split():
        if tok.startswith("ops_diag=") and len(tok) > len("ops_diag="):
            return tok[len("ops_diag="):]
    return None


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def board_mac(sys_net="/sys/class/net"):
    """起网的那块网卡的 MAC (12 位十六进制, 不带冒号): 优先 operstate=up 的, 没有就第一块真网卡; 都没有 None."""
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return None
    nics = []
    for n in names:
        mac = (_read(os.path.join(sys_net, n, "address")) or "").lower()
        if n == "lo" or not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or mac == "00:00:00:00:00:00":
            continue
        nics.append((_read(os.path.join(sys_net, n, "operstate")) != "up", n, mac.replace(":", "")))
    return min(nics)[2] if nics else None


def board_serial(dmi="/sys/class/dmi/id"):
    """SMBIOS 序列号 (product_serial, 再 board_serial), 换成文件名安全的字符, 占位值 (O.E.M. 之类) 当没有."""
    for f in ("product_serial", "board_serial"):
        raw = _read(os.path.join(dmi, f))
        if raw is None or raw.lower() in _PLACEHOLDER or not raw.strip("0 "):
            continue
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:40].strip("._-") or None
    return None


def archive_name(mac, serial, now):
    parts = ([mac] if mac else []) + ([f"sn-{serial}"] if serial else [])
    return f"ops-diag-{'-'.join(parts) or 'unknown'}-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"


def require_ram(path, mounts="/proc/mounts"):
    """path 所在的文件系统 (最长挂载点前缀) 必须是 tmpfs/ramfs, 不然抛 NotInRam."""
    path = os.path.abspath(path)
    best, fstype = "", None
    with open(mounts, encoding="utf-8") as f:
        for line in f:
            fields = line.split()
            if len(fields) < 3:
                continue
            mnt = fields[1].replace("\\040", " ")
            inside = path == mnt or path.startswith(mnt.rstrip("/") + "/")
            if inside and len(mnt) >= len(best):
                best, fstype = mnt, fields[2]
    if fstype not in RAM_FS:
        raise NotInRam(f"{path} 在 {best or '?'} ({fstype}) 上, 不是内存文件系统")


# ---------------------------------------------------------------- 收集

def disks(sys_block="/sys/block"):
    try:
        return sorted(n for n in os.listdir(sys_block) if _DISK.fullmatch(n))
    except OSError:
        return []


def nvme_ctrls(sys_nvme="/sys/class/nvme"):
    try:
        return [f"/dev/{n}" for n in sorted(os.listdir(sys_nvme)) if re.fullmatch(r"nvme\d+", n)]
    except OSError:
        return []


def plan(sys_net, disk_names, ctrls):
    """-> [(输出文件, argv, 工作子目录或 None)]. 全是只读查询."""
    p = [("uname.txt", ["uname", "-a"], None)]
    for f in ("cpuinfo", "meminfo", "cmdline", "iomem", "interrupts", "version"):
        p.append((f"{f}.txt", ["cat", f"/proc/{f}"], None))
    p += [
        ("firmware-ls.txt", ["ls", "-la", "/sys/firmware"], None),       # 有 acpi/ 还是 devicetree/
        ("dmidecode.txt", ["dmidecode"], None),
        ("lspci-nnvvv.txt", ["lspci", "-nnvvv"], None),
        ("lspci-tree.txt", ["lspci", "-tvnn"], None),
        ("lsusb-v.txt", ["lsusb", "-v"], None),
        ("lsusb-tree.txt", ["lsusb", "-t"], None),
        ("lsblk.txt", ["lsblk", "-a", "-O"], None),
        ("nvme-list.txt", ["nvme", "list"], None),
        ("dmesg.txt", ["dmesg"], None),
        ("lsmod.txt", ["lsmod"], None),
        ("efibootmgr-v.txt", ["efibootmgr", "-v"], None),
        ("ip-addr.txt", ["ip", "-d", "addr"], None),
        ("ip-route.txt", ["ip", "route"], None),
        ("lshw.txt", ["lshw"], None),
        ("acpi/acpidump.txt", ["acpidump"], "acpi"),
        ("acpi/acpidump-b.log", ["acpidump", "-b"], "acpi"),          # 每张表一个 <sig>.dat, 下面 iasl -d
    ]
    for c in ctrls:
        n = os.path.basename(c)
        p += [(f"{n}-id-ctrl.txt", ["nvme", "id-ctrl", c], None), (f"{n}-smart-log.txt", ["nvme", "smart-log", c], None)]
    for d in disk_names:
        p.append((f"smartctl-{d}.txt", ["smartctl", "-a", f"/dev/{d}"], None))
    try:
        nics = sorted(n for n in os.listdir(sys_net) if n != "lo")
    except OSError:
        nics = []
    for n in nics:
        p += [(f"ethtool-{n}.txt", ["ethtool", n], None), (f"ethtool-i-{n}.txt", ["ethtool", "-i", n], None)]
    return p


def run_cmd(argv, cwd, out):
    return subprocess.run(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          timeout=CMD_TIMEOUT).returncode


def _run(run, argv, cwd, path):
    with open(path, "wb") as out:
        try:
            return str(run(argv, cwd, out))
        except FileNotFoundError:
            return "not found"
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError as e:
            return f"error {e}"


def _copy_tree(src, dst, max_file, notes):
    for d, _dirs, files in os.walk(src):
        for name in sorted(files):
            s = os.path.join(d, name)
            t = os.path.join(dst, os.path.relpath(s, src))
            if os.path.islink(s):
                continue
            try:
                with open(s, "rb") as f:
                    data = f.read(max_file + 1)
            except OSError as e:
                notes.append(f"{os.path.relpath(t, os.path.dirname(dst))}\tunreadable\t{e.strerror or e}")
                continue
            if len(data) > max_file:
                notes.append(f"{os.path.relpath(t, os.path.dirname(dst))}\tskipped\t> {max_file} bytes")
                continue
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "wb") as f:
                f.write(data)


def collect(work, run, plan, copies, max_file=MAX_FILE, log=lambda m: None):
    """按 plan 跑命令、把 copies [(源目录, 包里的子目录)] 拷进 work, 再对 acpi/*.dat 跑 iasl -d; 写 MANIFEST.txt.
    -> MANIFEST 行 (输出文件 \\t 退出码/状态 \\t 命令)."""
    os.makedirs(os.path.join(work, "acpi"), exist_ok=True)
    manifest = []
    for name, argv, sub in plan:
        log(f"  {' '.join(argv)}")
        rc = _run(run, argv, os.path.join(work, sub) if sub else work, os.path.join(work, name))
        manifest.append(f"{name}\t{rc}\t{' '.join(argv)}")
    acpi = os.path.join(work, "acpi")
    for dat in sorted(f for f in os.listdir(acpi) if f.endswith(".dat")):
        rc = _run(run, ["iasl", "-d", dat], acpi, os.path.join(acpi, f"iasl-{dat[:-4]}.log"))
        manifest.append(f"acpi/{dat[:-4]}.dsl\t{rc}\tiasl -d {dat}")
    for src, sub in copies:
        log(f"  copy {src}")
        _copy_tree(src, os.path.join(work, sub), max_file, manifest)
    with open(os.path.join(work, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(manifest) + "\n")
    return manifest


# ---------------------------------------------------------------- 打包 / 上传

def pack(work, out):
    with tarfile.open(out, "w:gz") as t:
        t.add(work, arcname=os.path.basename(work.rstrip("/")))
    return out


def upload(path, url, tries=5, sleep=time.sleep, timeout=60):
    """PUT <url><文件名> -> 推车存下的名字 (重名时推车会加后缀). 失败重试, 最后抛 UploadFailed."""
    target = url.rstrip("/") + "/" + os.path.basename(path)
    last = None
    for i in range(tries):
        try:
            with open(path, "rb") as f:
                data = f.read()
            req = urllib.request.Request(target, data=data, method="PUT",
                                         headers={"Content-Type": "application/gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace").strip()
        except OSError as e:            # URLError / HTTPError / 连接被拒都是 OSError
            last = e
            if i < tries - 1:
                sleep(5)
    raise UploadFailed(f"{target}: {last}")


# ---------------------------------------------------------------- 入口

def parse_args(argv):
    p = argparse.ArgumentParser(prog="ops-diag-collect", description=__doc__.splitlines()[0])
    p.add_argument("--auto", action="store_true", help="开机自启用: 内核参数没有 ops_diag= 就什么都不做")
    p.add_argument("--url", help="上传地址 (默认取内核参数 ops_diag=)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--resend", metavar="TAR_GZ", help="不收集, 只把已打好的包再传一次")
    p.add_argument("--out-dir", default="/tmp")
    p.add_argument("--cmdline", default="/proc/cmdline")
    p.add_argument("--mounts", default="/proc/mounts")
    p.add_argument("--sys-net", default="/sys/class/net")
    p.add_argument("--dmi", default="/sys/class/dmi/id")
    p.add_argument("--no-copies", action="store_true", help="不拷 /sys/firmware/efi 和 dmi (测试用)")
    return p.parse_args(argv)


def main(argv=None, now=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    url = a.url or cmdline_url(_read(a.cmdline) or "")
    if a.auto and not url:
        return 0
    if a.resend:
        if not url:
            print("ops-diag: 没有上传地址 (--url 或内核参数 ops_diag=)", flush=True)
            return 4
        try:
            print(f"ops-diag: 已传到推车, 存为 {upload(a.resend, url)}", flush=True)
            return 0
        except UploadFailed as e:
            print(f"ops-diag: 上传失败 {e}", flush=True)
            return 4
    try:
        require_ram(a.out_dir, a.mounts)
    except (NotInRam, OSError) as e:
        print(f"ops-diag: 不收集, 输出目录不在内存里: {e}", flush=True)
        return 3
    now = now or datetime.datetime.now(datetime.timezone.utc)
    name = archive_name(board_mac(a.sys_net), board_serial(a.dmi), now)
    work = os.path.join(a.out_dir, name[:-len(".tar.gz")])
    print(f"ops-diag: 收集到 {work} (只读, 不碰硬盘)", flush=True)
    copies = [] if a.no_copies else [("/sys/firmware/efi", "sys-firmware-efi"), (a.dmi, "dmi-id")]
    collect(work, run_cmd, plan(a.sys_net, disks(), nvme_ctrls()), copies, log=lambda m: print(m, flush=True))
    out = pack(work, os.path.join(a.out_dir, name))
    shutil.rmtree(work, ignore_errors=True)
    print(f"ops-diag: 打包好了 {out} ({os.path.getsize(out)} bytes)", flush=True)
    if a.no_upload or not url:
        return 0
    try:
        stored = upload(out, url)
    except UploadFailed as e:
        print(f"ops-diag: 上传失败 {e}\n  不用重新收集, 手动重传: ops-diag-collect --resend {out} --url {url}",
              flush=True)
        return 4
    print(f"ops-diag: 已传到推车, 存为 {stored}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
