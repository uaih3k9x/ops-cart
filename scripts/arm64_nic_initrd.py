#!/usr/bin/env python3
"""飞腾 D2000 板载网卡 (ACPI FTGM0001, Synopsys DWMAC1000) 驱动的附加 initrd (#119): ftgm-initrd.cpio.gz.

GRUB 的 Alpine 诊断项写 `initrd .../initramfs-lts .../ftgm-initrd.cpio.gz`, 内核按顺序解开, 后面的同名文件盖住前面的.
这个 cpio 里只有:
  - usr/lib/modules/<kver>/extra/ 下 initramfs-lts 里没有的模块 (dwmac-ftgm.ko、motorcomm.ko); stmmac 依赖链和其它 PHY
    驱动 initramfs-lts 里本来就有、而且跟 bundle 里的逐字节相同 (不同就报错, 不覆盖);
  - depmod 重算后变了的 modules.* 索引 (modules.dep(.bin)/modules.alias(.bin)...), kmod 的 modprobe 只读 .bin;
  - /etc/modules: Alpine init 在 "Loading boot drivers" (取网之前) 逐行 modprobe, 顺序同 ftgm-glue 的 load.sh (PHY 先于 MAC).
    就算不写它, nlplug-findfs 冷插拔时也会按 MODALIAS=acpi:FTGM0001: 去 modprobe; 写上是为了固定 PHY 先于 MAC 的顺序.
Alpine 3.24 initramfs 是 usr 合并的 (/lib -> usr/lib), 所以路径写 usr/lib/..., 也不带 "lib" 目录项 (会把符号链接换成空目录).

depmod 要 Linux: 在 docker (alpine:3.24, arm64, 断网) 里解开 initramfs-lts, 拷进新模块, chroot 进去用 initramfs 自己的 kmod 跑 depmod,
生成的索引格式跟板子上的 modprobe 一致. 同样输入出同样字节 (排序、属主 root、时间戳固定、gzip mtime=0).

用法 (开发机):
  python3 scripts/arm64_nic_initrd.py --staging ~/code/pxeserver-arm64-staging [--bundle <ftgm-bundle 目录>]
读 <staging>/ftgm-bundle/*.ko 和 <staging>/tftp/aa64/alpine/initramfs-lts, 写 <staging>/tftp/aa64/alpine/ftgm-initrd.cpio.gz,
更新 <staging>/SOURCES.tsv 那一行. .ko 和 cpio 都不进 git (驱动源码在 ftgm-glue 仓库, GPL-2.0)."""

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile

TFTP_DIR = "aa64/alpine"                     # 同 pxeserver.ARM64_DIAG
OUT_NAME = "ftgm-initrd.cpio.gz"             # TFTP_DIR/OUT_NAME = pxeserver.ARM64_NIC_INITRD
BASE_NAME = "initramfs-lts"
MTIME = 1700000000                           # 同 arm64_diag_core 的 apkovl
DEPMOD_IMAGE = "alpine:3.24"
# ftgm-glue load.sh 的 insmod 顺序: PHY 驱动先于 MAC 胶水, MDIO 扫描时绑具体的 PHY 驱动 (实测 RTL8211F) 而不是 Generic PHY
LOAD_ORDER = ("phy_package", "micrel", "realtek", "motorcomm", "marvell", "qcom-phy-lib", "at803x", "dp83867",
              "phylink", "pcs_xpcs", "stmmac", "stmmac-platform", "dwmac-ftgm")
NIC_ALIAS = "acpi*:FTGM0001:*"
DEPMOD_OUTPUTS = {"modules.dep", "modules.dep.bin", "modules.alias", "modules.alias.bin", "modules.softdep",
                  "modules.symbols", "modules.symbols.bin", "modules.builtin.bin", "modules.builtin.alias.bin",
                  "modules.devname", "modules.weakdep"}
DEPMOD_INPUTS = {"modules.builtin", "modules.builtin.modinfo", "modules.order"}    # 打包时一起带出来, 必须跟原来一样
S_IFMT, S_IFDIR, S_IFREG = 0o170000, 0o040000, 0o100000
DIR_MODE, FILE_MODE = S_IFDIR | 0o755, S_IFREG | 0o644


# ---------------------------------------------------------------- newc cpio

def _pad(n):
    return (4 - n % 4) % 4


def write_newc(entries):
    """[(名字, mode, 内容)] -> newc cpio 字节 (按给的顺序; 属主 root, 时间戳 MTIME, inode 从 1 顺序编)."""
    out = bytearray()
    for ino, (name, mode, data) in enumerate(list(entries) + [("TRAILER!!!", 0, b"")], 1):
        nb = name.encode() + b"\0"
        nlink = 2 if mode & S_IFMT == S_IFDIR else 1
        fields = (0 if name == "TRAILER!!!" else ino, mode, 0, 0, nlink if mode else 1,
                  MTIME if mode else 0, len(data), 0, 0, 0, 0, len(nb), 0)
        out += b"070701" + b"".join(b"%08X" % f for f in fields) + nb
        out += b"\0" * _pad(len(out)) + data
        out += b"\0" * _pad(len(out))
    return bytes(out)


def read_newc(raw):
    """newc cpio (可以几个首尾相接) -> [(名字, mode, 内容)]. 设备节点等照样列出 (内容为空)."""
    out, off = [], 0
    while off < len(raw):
        if raw[off] == 0:                                     # 两个归档之间的填充
            off += 1
            continue
        if raw[off:off + 6] not in (b"070701", b"070702"):
            raise ValueError(f"不是 newc cpio (偏移 {off})")
        f = [int(raw[off + 6 + 8 * i:off + 14 + 8 * i], 16) for i in range(13)]
        mode, size, nsize = f[1], f[6], f[11]
        off += 110
        name = raw[off:off + nsize - 1].decode()
        off += nsize
        off += _pad(off)
        data = raw[off:off + size]
        off += size
        off += _pad(off)
        if name != "TRAILER!!!":
            out.append((name, mode, data))
    return out


def gz(data):
    """固定字节的 gzip: 不写文件名, mtime=0."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as g:
        g.write(data)
    return buf.getvalue()


# ---------------------------------------------------------------- 模块

def modinfo(ko):
    """.ko 里 .modinfo 段的 key=value (不解 ELF: 按 NUL 切开找 name/vermagic/alias/depends)."""
    info = {}
    for tok in ko.split(b"\0"):
        m = re.fullmatch(rb"(name|vermagic|alias|depends|license)=([\x20-\x7e]*)", tok)
        if m:
            info.setdefault(m.group(1).decode(), []).append(m.group(2).decode())
    return info


def _stem(path):
    return os.path.basename(path)[:-len(".ko")].replace("-", "_")


def parse_base(raw):
    """initramfs-lts (gzip 后的 cpio) -> (kver, {名字: (mode, 内容)})."""
    try:
        raw = gzip.decompress(raw)
    except OSError as e:
        raise ValueError(f"{BASE_NAME} 不是 gzip (Alpine 换了压缩方式? 要改这里): {e}") from e
    ents = {n: (m, d) for n, m, d in read_newc(raw)}
    kvers = [n.split("/")[3] for n, (m, _) in ents.items()
             if re.fullmatch(r"usr/lib/modules/[^/]+", n) and m & S_IFMT == S_IFDIR]
    if len(kvers) != 1:
        raise ValueError(f"{BASE_NAME} 里 usr/lib/modules/ 下应该正好一个内核版本, 实际 {kvers}")
    return kvers[0], ents


def plan(bundle_dir, kver, base):
    """bundle 的 .ko -> initramfs 里没有的那些 {文件名: 内容}. 检查 vermagic 对得上内核, 已有的必须逐字节相同."""
    base_ko = {_stem(n): d for n, (m, d) in base.items()
               if n.startswith(f"usr/lib/modules/{kver}/") and n.endswith(".ko")}
    extras = {}
    for fn in sorted(os.listdir(bundle_dir)):
        if not fn.endswith(".ko"):
            continue                                          # load.sh / memdump 之类不进 initrd
        with open(os.path.join(bundle_dir, fn), "rb") as f:
            data = f.read()
        vm = modinfo(data).get("vermagic", [""])[0]
        if vm.split(" ")[0] != kver:
            raise ValueError(f"{fn}: vermagic {vm!r} 跟 {BASE_NAME} 的内核 {kver} 对不上, 要按新内核重编驱动")
        if _stem(fn) in base_ko:
            if base_ko[_stem(fn)] != data:
                raise ValueError(f"{fn}: 跟 {BASE_NAME} 里的同名模块内容不同 (不覆盖内核自带模块; 确认来源再说)")
            continue
        extras[fn] = data
    have = set(base_ko) | {_stem(n) for n in extras}
    missing = [m for m in LOAD_ORDER if m.replace("-", "_") not in have]
    if missing:
        raise ValueError(f"加载清单里的模块找不到: {missing}")
    return extras


def docker_depmod(initramfs_path, kver, extras, runner=subprocess.run):
    """docker (arm64, 断网) 里: 解开 initramfs-lts, 拷进新模块到 extra/, chroot 用它自带的 kmod 跑 depmod, 吐出 modules.* (tar)."""
    with tempfile.TemporaryDirectory() as tmp:
        for fn, data in extras.items():
            with open(os.path.join(tmp, fn), "wb") as f:
                f.write(data)
        script = (f"set -eu; K={kver}; mkdir /r; cd /r; gzip -dc /in/{BASE_NAME} | cpio -id 2>/dev/null; "
                  f"mkdir -p usr/lib/modules/$K/extra; cp /extra/*.ko usr/lib/modules/$K/extra/; "
                  f"ln -sf ../bin/kmod /r/sbin/depmod; chroot /r /sbin/depmod {kver}; "
                  f"cd usr/lib/modules/$K; tar -cf - modules.*")
        argv = ["docker", "run", "--rm", "--network", "none", "--platform", "linux/arm64",
                "-v", f"{os.path.abspath(initramfs_path)}:/in/{BASE_NAME}:ro", "-v", f"{tmp}:/extra:ro",
                DEPMOD_IMAGE, "sh", "-c", script]
        r = runner(argv, stdout=subprocess.PIPE, check=True)
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def build(bundle_dir, initramfs, out, depmod=docker_depmod):
    """生成 ftgm-initrd.cpio.gz, 返回 [(名字, mode, 大小, sha256)]."""
    with open(initramfs, "rb") as f:
        kver, base = parse_base(f.read())
    extras = plan(bundle_dir, kver, base)
    md = f"usr/lib/modules/{kver}"
    idx = depmod(initramfs, kver, extras)
    bad = sorted(set(idx) - DEPMOD_OUTPUTS - DEPMOD_INPUTS)
    if bad:
        raise ValueError(f"depmod 生成了没预料的文件: {bad}")
    changed = sorted(fn for fn in DEPMOD_INPUTS & set(idx) if base.get(f"{md}/{fn}", (0, None))[1] != idx[fn])
    if changed:
        raise ValueError(f"depmod 的输入文件变了: {changed}")
    if not any(l.startswith("extra/dwmac-ftgm.ko:") for l in idx.get("modules.dep", b"").decode().splitlines()):
        raise ValueError("depmod 的 modules.dep 里没有 extra/dwmac-ftgm.ko")
    if f"alias {NIC_ALIAS} dwmac_ftgm" not in idx.get("modules.alias", b"").decode():
        raise ValueError(f"depmod 的 modules.alias 里没有 {NIC_ALIAS} (FTGM0001 认不到)")

    files = {"etc/modules": "".join(f"{m}\n" for m in LOAD_ORDER).encode()}
    files.update({f"{md}/extra/{fn}": d for fn, d in extras.items()})
    files.update({f"{md}/{fn}": d for fn, d in idx.items() if base.get(f"{md}/{fn}", (0, None))[1] != d})
    dirs = {"etc", f"{md}/extra"} | {"/".join(md.split("/")[:i]) for i in range(1, 5)}
    ents = sorted([(d, DIR_MODE, b"") for d in dirs] + [(n, FILE_MODE, d) for n, d in files.items()])

    for name, mode, _ in ents:                                # 不改 initramfs-lts 里已有的东西 (除了 depmod 索引)
        if name not in base:
            continue
        bmode = base[name][0]
        if mode & S_IFMT == S_IFDIR and bmode != mode:
            raise ValueError(f"{name}: {BASE_NAME} 里是 {oct(bmode)}, 附加 cpio 要写 {oct(mode)} (会改掉它)")
        if mode & S_IFMT == S_IFREG and not (name.startswith(f"{md}/") and os.path.basename(name) in DEPMOD_OUTPUTS):
            raise ValueError(f"{name}: {BASE_NAME} 里已经有了, 不覆盖")
    data = gz(write_newc(ents))
    tmp = f"{out}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out)
    return [(n, m, len(d), hashlib.sha256(d).hexdigest()) for n, m, d in ents]


# ---------------------------------------------------------------- SOURCES.tsv

def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def upsert_sources(staging, path, source):
    """<staging>/SOURCES.tsv 里替换 (没有就追加) 这个文件那一行. 注意 arm64_diag_core.py fetch 会整个重写这个表."""
    tsv = os.path.join(staging, "SOURCES.tsv")
    rel = os.path.relpath(path, staging)
    rows = []
    if os.path.exists(tsv):
        with open(tsv, encoding="utf-8") as f:
            rows = [l.rstrip("\n") for l in f if l.strip()]
    if not rows:
        rows = ["path\tsize\tsha256\tsource\tverified"]
    rows = [r for r in rows if r.split("\t")[0] != rel]
    rows.append("\t".join((rel, str(os.path.getsize(path)), _sha256(path), source, "-")))
    with open(f"{tsv}.tmp", "w", encoding="utf-8") as f:
        f.writelines(r + "\n" for r in rows)
    os.replace(f"{tsv}.tmp", tsv)


def main(argv=None):  # pragma: no cover - 要 docker
    ap = argparse.ArgumentParser(prog="arm64_nic_initrd.py", description=__doc__.split("\n")[0])
    ap.add_argument("--staging", required=True)
    ap.add_argument("--bundle", help="默认 <staging>/ftgm-bundle")
    a = ap.parse_args(argv)
    st = os.path.expanduser(a.staging)
    bundle_dir = os.path.expanduser(a.bundle or os.path.join(st, "ftgm-bundle"))
    initramfs = os.path.join(st, "tftp", TFTP_DIR, BASE_NAME)
    out = os.path.join(st, "tftp", TFTP_DIR, OUT_NAME)
    ents = build(bundle_dir, initramfs, out)
    for n, m, size, sha in ents:
        print(f"{oct(m)[2:]:>6} {size:>8} {sha[:16]} {n}")
    ins = " ".join(f"{os.path.basename(n)}={sha[:16]}" for n, _, _, sha in ents if "/extra/" in n)
    upsert_sources(st, out, f"scripts/arm64_nic_initrd.py (本仓库生成; {BASE_NAME}={_sha256(initramfs)[:16]} {ins})")
    print(f"{out}  sha256 {_sha256(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
