#!/usr/bin/env python3
"""ARM64 WinPE 从板子本地盘起 + UEFI Shell 里在内存补 DSDT (docs/ARM64_WINPE.md 第 7 节).

为什么: 飞腾 D2000 (百敖固件) 的 DSDT 有 `Field (GNVS, AnyAcc, Lock, Preserve)`, 板子又是 Hardware-Reduced、没有 FACS
(没有全局锁). 22621 的 ACPI.sys 释放全局锁时先解引用 NULL -> 0x7E / 0xC0000005. 修法 = Lock -> NoLock:
运行时 DSDT 偏移 0x3A 0x10 -> 0x00, 校验和 (偏移 9) +0x10. 只改内存, 断电就没, 不碰 SPI 固件、不改 BIOS 设置.
补丁要在同一次开机里起 WinPE 才有用, 所以 WinPE 放本地盘, 由固件自带的 UEFI Shell 跑 startup.nsh:
守卫 (dsdtguard.efi) 核对 -> mm 写两个字节 -> 守卫再核 -> 起 \\EFI\\cart\\bootaa64.efi (Windows 启动管理器).

一个文件三处用 (只用标准库, Python 3.9+):
  开发机:  header (生成 efi/dsdtguard/dsdt_expect.h)、nsh (startup.nsh)、manifest (推车上的文件清单)
  板子上 (推车 PXE 起的内存 Alpine):  check (只读核 DSDT)、prepare (备份 -> 清盘 -> 建 ESP -> 拷 WinPE)
测试: tests/test_arm64_winpe_local.py (含在主机上编 C 判定跟这里对拍)."""

import argparse
import collections
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

# ---------------------------------------------------------------- DSDT 补丁

CSUM_OFF = 9
LOCK_OFF, LOCK_OLD, LOCK_NEW = 0x3A, 0x10, 0x00
CTX_OFF, CTX = 0x33, b"\x5b\x81\x13GNVS"          # FieldOp (5B 81) + PkgLength 0x13 + 名字 GNVS, 后面紧跟标志字节

ORIG, PATCHED = 1, 2
E_SIG, E_LEN, E_SUM, E_CTX, E_FNV, E_STATE = -1, -2, -3, -4, -5, -6      # 同 dsdtguard_core.h
NAMES = {ORIG: "原表 (Lock)", PATCHED: "已补 (NoLock)", E_SIG: "不是 DSDT", E_LEN: "长度不对",
         E_SUM: "字节和不为 0", E_CTX: "0x3A 前不是 Field GNVS", E_FNV: "内容跟预期的表不同",
         E_STATE: "两个字节既不是原值也不是补丁值"}


class Expect(collections.namedtuple("Expect", "addr length csum_old fnv")):
    @property
    def csum_new(self):
        return (self.csum_old + LOCK_OLD - LOCK_NEW) & 0xFF


# 首块飞腾 D2000 板子 (百敖 ByoCore 1.4.0, 20240904): 11-24 从 Linux /sys/firmware/acpi/tables 读出的运行时 DSDT
BOARD = Expect(addr=0xF19F0000, length=4176, csum_old=0x6F, fnv=0x2F12068A03D7022E)


def fnv1a64(data):
    h = 0xcbf29ce484222325
    for b in data:
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def fingerprint(t):
    """FNV-1a 64, 要改的两个字节 (校验和、标志) 当 0: 原表和补过的表指纹相同, 其余任何字节不同都不同."""
    m = bytearray(t)
    if len(m) > LOCK_OFF:
        m[CSUM_OFF] = m[LOCK_OFF] = 0
    return fnv1a64(m)


def patch(t):
    t = bytearray(t)
    t[LOCK_OFF] = LOCK_NEW
    t[CSUM_OFF] = (t[CSUM_OFF] + LOCK_OLD - LOCK_NEW) & 0xFF
    return bytes(t)


def classify(t, e=BOARD):
    """跟 dsdtguard_core.h 的 dg_classify 一步一步对应 (同样的检查顺序和返回码)."""
    if len(t) < 36:
        return E_LEN
    if t[:4] != b"DSDT":
        return E_SIG
    n = int.from_bytes(t[4:8], "little")
    if n != e.length or len(t) < n:
        return E_LEN
    t = t[:n]
    if sum(t) & 0xFF:
        return E_SUM
    if t[CTX_OFF:CTX_OFF + len(CTX)] != CTX:
        return E_CTX
    if fingerprint(t) != e.fnv:
        return E_FNV
    if t[LOCK_OFF] == LOCK_OLD and t[CSUM_OFF] == e.csum_old:
        return ORIG
    if t[LOCK_OFF] == LOCK_NEW and t[CSUM_OFF] == e.csum_new:
        return PATCHED
    return E_STATE


def fadt_dsdt(f):
    """FADT 里 DSDT 的物理地址: X_DSDT (偏移 140) 优先, 没有用 32 位 DSDT (偏移 40); 两个都有又不同 -> None."""
    if len(f) < 148 or f[:4] != b"FACP":
        return None
    d32, x = int.from_bytes(f[40:44], "little"), int.from_bytes(f[140:148], "little")
    if x and d32 and x != d32:
        return None
    return x or d32 or None


def c_header(e):
    ctx = ", ".join(f"0x{b:02X}" for b in CTX)
    return (
        "/* 生成的, 别手改: python3 arm64_winpe_local.py header > efi/dsdtguard/dsdt_expect.h\n"
        " * 预期的运行时 DSDT (arm64_winpe_local.BOARD) 和要改的两个字节. */\n"
        f"#define DG_ADDR     0x{e.addr:08X}ULL\n"
        f"#define DG_LEN      {e.length}ULL\n"
        f"#define DG_FNV      0x{e.fnv:016X}ULL\n"
        f"#define DG_CSUM_OFF {CSUM_OFF}\n"
        f"#define DG_CSUM_OLD 0x{e.csum_old:02X}\n"
        f"#define DG_CSUM_NEW 0x{e.csum_new:02X}\n"
        f"#define DG_LOCK_OFF 0x{LOCK_OFF:02X}\n"
        f"#define DG_LOCK_OLD 0x{LOCK_OLD:02X}\n"
        f"#define DG_LOCK_NEW 0x{LOCK_NEW:02X}\n"
        f"#define DG_CTX_OFF  0x{CTX_OFF:02X}\n"
        f"#define DG_CTX      {{{ctx}}}\n")


# ---------------------------------------------------------------- ESP 布局 + startup.nsh

ESP_GUARD = "EFI/cart/dsdtguard.efi"
ESP_BOOTMGR = "EFI/cart/bootaa64.efi"
ESP_GUARD_SHELL = "\\" + ESP_GUARD.replace("/", "\\")
ESP_BOOTMGR_SHELL = "\\" + ESP_BOOTMGR.replace("/", "\\")
# 推车 aa64/winpe-local/ 下, 本仓库生成的 (bcd 用 scripts/arm64_winpe_bcd.py --local)
LOCAL_FILES = {"bcd": "EFI/Microsoft/Boot/BCD", "dsdtguard.efi": ESP_GUARD, "startup.nsh": "startup.nsh"}


def esp_layout(rel_files):
    """aa64/winpe/<build>/ 下的相对路径 -> ESP 上的路径. 启动管理器**不放** \\EFI\\Boot\\bootaa64.efi:
    固件对没有启动项的盘会直接跑那个路径, 等于绕过 startup.nsh 的守卫、起一个没打补丁的 WinPE.
    BCD 在 \\EFI\\Microsoft\\Boot\\BCD (本地盘启动时启动管理器只认这里); PXE 用的 boot/bcd* 不拷."""
    out = []
    for rel in rel_files:
        if rel == "bootaa64.efi":
            out.append((rel, ESP_BOOTMGR))
        elif rel in ("boot/boot.sdi", "sources/boot.wim"):
            out.append((rel, rel))
        elif rel.startswith("boot/fonts/"):
            out.append((rel, "EFI/Microsoft/Boot/Fonts/" + rel[len("boot/fonts/"):]))
        elif rel == "boot/en-us/bootmgr.efi.mui":       # 启动管理器按 <自己的目录>\en-US\<自己的文件名>.MUI 找
            out.append((rel, "EFI/cart/en-US/bootaa64.efi.mui"))
    return out


def startup_nsh(e):
    """固件 UEFI Shell 开机自动跑 (fs0:\\startup.nsh). 先拿两个必成/必败的空跑确认 %lasterror% 真能分辨成败,
    再核原表 -> mm 写 -> 核补丁; 任何一步不对就 exit /b, 不写 (或不再写)、不起 WinPE, 停在 Shell."""
    g, a_lock, a_sum = ESP_GUARD_SHELL, e.addr + LOCK_OFF, e.addr + CSUM_OFF
    lines = [
        "@echo -off",
        "# ops-cart (arm64_winpe_local.py nsh): DSDT GNVS Lock->NoLock RAM-only patch, then WinPE from this disk.",
        "# Nothing is written to flash or BIOS settings; a power cycle restores the original table.",
        "echo \"ops-cart: DSDT guard + RAM patch + WinPE (ESC at the countdown skips this script)\"",
        "fs0:",
        f"if not exist {g} then",
        "  echo \"ops-cart: STOP: fs0: is not the cart WinPE disk, nothing written\"",
        "  exit /b 1",
        "endif",
        f"{g} selftest-fail",
        "if %lasterror% eq 0 then",
        "  echo \"ops-cart: STOP: lasterror check does not work in this shell, nothing written\"",
        "  exit /b 1",
        "endif",
        f"{g} selftest-ok",
        "if %lasterror% ne 0 then",
        "  echo \"ops-cart: STOP: lasterror check does not work in this shell, nothing written\"",
        "  exit /b 1",
        "endif",
        f"{g} patched",
        "if %lasterror% ne 0 then",
        f"  {g} orig",
        "  if %lasterror% ne 0 then",
        "    echo \"ops-cart: STOP: DSDT is not the known table, nothing written, WinPE not started\"",
        "    exit /b 1",
        "  endif",
        f"  mm {a_lock:X} {LOCK_NEW:02X} -w 1 -n",
        f"  mm {a_sum:X} {e.csum_new:02X} -w 1 -n",
        f"  {g} patched",
        "  if %lasterror% ne 0 then",
        "    echo \"ops-cart: STOP: DSDT check after mm failed, WinPE not started (power cycle restores it)\"",
        "    exit /b 1",
        "  endif",
        "endif",
        "echo \"ops-cart: DSDT is NoLock in RAM, starting WinPE\"",
        ESP_BOOTMGR_SHELL,
        "echo \"ops-cart: boot manager returned %lasterror%\"",
    ]
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- 文件清单

Row = collections.namedtuple("Row", "sha256 size url esp")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(winpe_dir, local_dir, url_winpe="aa64/winpe/22621", url_local="aa64/winpe-local"):
    rels = []
    for base, _, files in os.walk(winpe_dir):
        for n in files:
            rels.append(os.path.relpath(os.path.join(base, n), winpe_dir).replace(os.sep, "/"))
    rows = []
    for rel, esp in esp_layout(sorted(rels)):
        p = os.path.join(winpe_dir, rel)
        rows.append(Row(_sha256_file(p), os.path.getsize(p), f"{url_winpe}/{rel}", esp))
    for n, esp in LOCAL_FILES.items():
        p = os.path.join(local_dir, n)
        rows.append(Row(_sha256_file(p), os.path.getsize(p), f"{url_local}/{n}", esp))
    return sorted(rows, key=lambda r: r.esp)


def format_manifest(rows):
    return "".join(f"{r.sha256}\t{r.size}\t{r.url}\t{r.esp}\n" for r in rows)


def _safe_rel(p):
    return bool(p) and not p.startswith("/") and "\\" not in p and ".." not in p.split("/")


def parse_manifest(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        sha, size, url, esp = line.split("\t")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not _safe_rel(url) or not _safe_rel(esp):
            raise ValueError(f"清单行不对: {line!r}")
        rows.append(Row(sha, int(size), url, esp))
    return rows


# ---------------------------------------------------------------- 板子上: 准备本地盘

RAM_FS = ("tmpfs", "ramfs", "rootfs")
ESP_SIZE = "+1024M"
BACKUP_BYTES = 1 << 20


class Stop(Exception):
    pass


def _read(root, path, mode="r"):
    with open(os.path.join(root, path.lstrip("/")), mode) as f:
        return f.read()


def check_dsdt(root, e=BOARD):
    """这次开机的 DSDT 是不是认识的那张 (原表或已补): 地址看 FACP, 内容看 classify. 返回 (地址, 判定)."""
    addr = fadt_dsdt(_read(root, "/sys/firmware/acpi/tables/FACP", "rb"))
    r = classify(_read(root, "/sys/firmware/acpi/tables/DSDT", "rb"), e)
    return addr, r


def _root_fs(root):
    fs = None
    for line in _read(root, "/proc/mounts").splitlines():
        f = line.split()
        if len(f) >= 3 and f[1] == "/":
            fs = f[2]
    return fs


def _mounted(root, disk):
    return [ln.split()[0] for ln in _read(root, "/proc/mounts").splitlines()
            if ln.split() and ln.split()[0].startswith(f"/dev/{disk}")]


def _backup(root, disk, dev, mac, serial, run, log):
    tmp = os.path.join(root, "tmp")
    size = int(_read(root, f"/sys/block/{disk}/size").strip()) * 512
    with open(dev, "rb") as f:
        head = f.read(BACKUP_BYTES)
        f.seek(size - BACKUP_BYTES)
        tail = f.read(BACKUP_BYTES)
    sg = os.path.join(tmp, f"{disk}.sgdisk.backup")
    rc, out = run(["sgdisk", f"--backup={sg}", dev])
    if rc != 0:
        raise Stop(f"sgdisk --backup 失败 ({rc}): {out.strip()}")
    _, table = run(["sgdisk", "-p", dev])
    info = (f"disk={disk}\nserial={serial}\nmodel={_read(root, f'/sys/block/{disk}/device/model').strip()}\n"
            f"bytes={size}\nhead=0..{BACKUP_BYTES} tail={size - BACKUP_BYTES}..{size}\n")
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
    name = f"nvme-backup-{mac}-{safe}-{ts}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n, data in ((f"{name}/head-1MiB.bin", head), (f"{name}/tail-1MiB.bin", tail),
                        (f"{name}/sgdisk.backup", open(sg, "rb").read()),
                        (f"{name}/sgdisk-p.txt", table.encode()), (f"{name}/info.txt", info.encode())):
            ti = tarfile.TarInfo(n)
            ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
            t.addfile(ti, io.BytesIO(data))
    data = buf.getvalue()
    with open(os.path.join(tmp, name + ".tar.gz"), "wb") as f:
        f.write(data)
    log(f"备份 {name}.tar.gz ({len(data)} 字节), /tmp 留一份")
    return name + ".tar.gz", data


def _wait(path, dev, run, log):
    """sgdisk 写完会让内核重读分区表; 节点由 mdev 建. 慢了先让内核重读一次, 再让 mdev 补建一次."""
    for i in range(40):
        if os.path.exists(path):
            return
        if i == 10:
            log(f"{path} 还没出现, blockdev --rereadpt")
            run(["blockdev", "--rereadpt", dev])
        if i == 20:
            log(f"{path} 还没出现, mdev -s")
            run(["mdev", "-s"])
        time.sleep(0.25)
    raise Stop(f"分区节点 {path} 没出现")


def prepare(disk, serial, server, upload, yes, root="/", expect=BOARD, mac="unknown",
            run=None, get=None, put=None, log=print):
    """核对 -> 下载并校验全部文件到内存 -> 备份 GPT/前后 1 MiB 并传回推车 -> 清盘 -> 建 1 GiB ESP -> 拷文件 -> 只读重挂再核.
    核对没过、备份没传上去, 都在动盘之前停 (Stop)."""
    run = run or _run
    get = get or _get
    put = put or _put
    dev = os.path.join(root, "dev", disk)
    part = dev + ("p1" if disk[-1].isdigit() else "1")
    mnt = os.path.join(root, "mnt", "winpe-esp")

    fs = _root_fs(root)
    if fs not in RAM_FS:
        raise Stop(f"根文件系统是 {fs}, 不是内存里的 (要在推车 PXE 起的 Alpine 里跑)")
    addr, r = check_dsdt(root, expect)
    if addr != expect.addr or r not in (ORIG, PATCHED):
        raise Stop(f"这次开机的 DSDT 不是认识的那张: 地址 {addr!r} (预期 0x{expect.addr:X}), {NAMES.get(r, r)}; 不动盘")
    log(f"DSDT @ 0x{addr:X}: {NAMES[r]}")
    got_serial = _read(root, f"/sys/block/{disk}/device/serial").strip()
    if got_serial != serial:
        raise Stop(f"{disk} 序列号是 {got_serial!r}, 不是 --serial {serial!r}; 不动盘")
    busy = _mounted(root, disk)
    if busy:
        raise Stop(f"{disk} 有分区挂载着: {busy}; 先卸载")
    if not yes:
        raise Stop(f"核对都过了 ({disk} {got_serial}); 真要清盘加 --yes")

    rows = parse_manifest(get(f"{server}/aa64/winpe-local/files.tsv").decode())
    blobs = {}
    for row in rows:
        data = get(f"{server}/{row.url}")
        if len(data) != row.size or hashlib.sha256(data).hexdigest() != row.sha256:
            raise Stop(f"{row.url} sha256/大小不对; 不动盘")
        blobs[row.esp] = data
    log(f"{len(rows)} 个文件已下到内存并核过 sha256")

    name, data = _backup(root, disk, dev, mac, serial, run, log)
    try:
        put(upload.rstrip("/") + "/" + name, data)
    except Exception as ex:      # noqa: BLE001 - 任何上传失败都不能往下清盘
        raise Stop(f"备份传回推车失败 ({ex}); 不动盘, 备份在 /tmp/{name}")
    log(f"备份已传到 {upload}{name}")

    for argv in (["sgdisk", "--zap-all", dev],
                 ["sgdisk", "-n", f"1:2048:{ESP_SIZE}", "-t", "1:ef00", "-c", "1:WINPE-ESP", dev]):
        rc, out = run(argv)
        if rc != 0:
            raise Stop(f"{' '.join(argv)} 失败 ({rc}): {out.strip()}")
    _wait(part, dev, run, log)
    for argv in (["mkfs.vfat", "-F", "32", "-n", "WINPE", part], ["mkdir", "-p", mnt],
                 ["mount", "-t", "vfat", part, mnt]):
        rc, out = run(argv)
        if rc != 0:
            raise Stop(f"{' '.join(argv)} 失败 ({rc}): {out.strip()}")
    os.makedirs(mnt, exist_ok=True)
    for esp, blob in blobs.items():
        p = os.path.join(mnt, esp)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
    for argv in (["sync"], ["umount", mnt], ["mount", "-t", "vfat", "-o", "ro", part, mnt]):
        rc, out = run(argv)
        if rc != 0:
            raise Stop(f"{' '.join(argv)} 失败 ({rc}): {out.strip()}")
    bad = [r.esp for r in rows if _sha256_file(os.path.join(mnt, r.esp)) != r.sha256]
    run(["umount", mnt])
    if bad:
        raise Stop(f"只读重挂后这些文件 sha256 不对: {bad}")
    log(f"完成: {part} (FAT32 WINPE) 上 {len(rows)} 个文件, 只读重挂核对一致. 重启后 GRUB 默认本地盘 -> Shell 跑 startup.nsh")


def _run(argv):  # pragma: no cover - 板子上
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def _get(url):  # pragma: no cover
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def _put(url, data):  # pragma: no cover
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": "application/gzip", "Content-Length": str(len(data))})
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 201):
            raise OSError(f"HTTP {r.status}")


def _mac(root="/"):  # pragma: no cover
    for n in sorted(os.listdir(os.path.join(root, "sys/class/net"))):
        if n != "lo":
            try:
                return _read(root, f"/sys/class/net/{n}/address").strip().replace(":", "")
            except OSError:
                continue
    return "unknown"


def main(argv=None):  # pragma: no cover - 薄壳
    ap = argparse.ArgumentParser(prog="arm64_winpe_local.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("header", help="打印 efi/dsdtguard/dsdt_expect.h")
    n = sub.add_parser("nsh", help="写 startup.nsh")
    n.add_argument("--out", required=True)
    m = sub.add_parser("manifest", help="写推车上的 files.tsv")
    m.add_argument("--winpe", required=True, help="暂存的 winpe/<build> 目录")
    m.add_argument("--local", required=True, help="放 bcd、dsdtguard.efi、startup.nsh 的目录")
    m.add_argument("--url-winpe", default="aa64/winpe/22621")
    m.add_argument("--url-local", default="aa64/winpe-local")
    m.add_argument("--out", required=True)
    sub.add_parser("check", help="板子上: 只读核这次开机的 DSDT")
    p = sub.add_parser("prepare", help="板子上: 备份 -> 清盘 -> 建 ESP -> 拷 WinPE")
    p.add_argument("--disk", default="nvme0n1")
    p.add_argument("--serial", required=True, help="要清的盘的序列号 (/sys/block/<盘>/device/serial), 防清错盘")
    p.add_argument("--server", required=True, help="推车 HTTP, 如 http://192.0.2.1")
    p.add_argument("--upload", help="备份传到哪 (默认 <server>:8081/diag/)")
    p.add_argument("--yes", action="store_true", help="真清盘")
    a = ap.parse_args(argv)
    if a.cmd == "header":
        sys.stdout.write(c_header(BOARD))
    elif a.cmd == "nsh":
        with open(a.out, "w", newline="", encoding="ascii") as f:
            f.write(startup_nsh(BOARD))
    elif a.cmd == "manifest":
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(format_manifest(build_manifest(a.winpe, a.local, a.url_winpe, a.url_local)))
    elif a.cmd == "check":
        addr, r = check_dsdt("/")
        print(f"DSDT @ {addr and hex(addr)}: {NAMES.get(r, r)}")
        return 0 if addr == BOARD.addr and r in (ORIG, PATCHED) else 1
    else:
        try:
            prepare(a.disk, a.serial, a.server.rstrip("/"), a.upload or f"{a.server.rstrip('/')}:8081/diag/",
                    a.yes, mac=_mac())
        except Stop as ex:
            print(f"停: {ex}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
