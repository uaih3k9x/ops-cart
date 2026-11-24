#!/usr/bin/env python3
"""ARM64 诊断盘备料: Rocky 的 grubaa64.efi + Alpine aarch64 netboot (lts 内核) + 本地子集 apk 仓库 + apkovl.

装机口那边不保证能上外网, 所以诊断系统要的包 (pxeserver.ARM64_DIAG_PKGS + alpine-base 的依赖闭包) 都预先下好,
推车 80 口 HTTP 从 tftp/aa64/alpine/repo/ 出; apk 在板子上用官方签名的 APKINDEX 校验每个包.
二进制都不进 git. 用法 (在有外网的机器上, 比如 Mac, 落到暂存目录; 上车照 docs/ARM64_PXE.md 拷):
  python3 arm64_diag_core.py fetch --staging ~/code/pxeserver-arm64-staging
  python3 arm64_diag_core.py apkovl --out <tftp>/aa64/alpine/ops-diag.apkovl.tar.gz      # 只重做 apkovl
fetch 落盘布局 = 推车 tftp 根下的相对路径 (<staging>/tftp/aa64/...), 另写 <staging>/SOURCES.tsv (路径 大小 sha256 来源 校验方式)."""

import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import urllib.request

import pxeserver

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(HERE, "arm64_diag_collect.py")

ALPINE_BRANCH, ALPINE_VER = "v3.24", "3.24.4"
ALPINE_MIRROR = "https://dl-cdn.alpinelinux.org/alpine"
ALPINE_REPOS = ("main", "community")
ROCKY_TREE = "https://download.rockylinux.org/pub/rocky/9/BaseOS/aarch64/os"
BASE_PKGS = ("alpine-base", "openssl")      # initramfs 总会装 alpine-base; modloop 验签要 openssl

START = """#!/bin/sh
# ops-cart: 开机后在后台只读收集一遍 ARM64 诊断信息并传回推车 (内核参数没有 ops_diag= 就什么都不做).
# 输出同时到 /dev/console 和 /tmp/ops-diag.log; 手动再跑: ops-diag-collect
( sleep 5; /usr/bin/python3 /usr/local/bin/ops-diag-collect --auto 2>&1 | tee /tmp/ops-diag.log ) \\
    > /dev/console 2>&1 < /dev/null &
"""


# ---------------------------------------------------------------- APKINDEX

def parse_apkindex(text):
    pkgs = []
    for block in text.strip().split("\n\n"):
        d = {}
        for line in block.splitlines():
            k, _, v = line.partition(":")
            d[k] = v
        if "P" in d:
            pkgs.append(d)
    return pkgs


def load_index(path):
    with tarfile.open(path) as t:
        return parse_apkindex(t.extractfile("APKINDEX").read().decode("utf-8"))


def _name(token):
    return re.split(r"[<>=~]", token, maxsplit=1)[0]


def resolve(indexes, wanted):
    """indexes [(仓库名, 包列表)], wanted 包名 -> 依赖闭包 [(仓库名, "<P>-<V>.apk")], 按包名排序.
    认 D: 里的 so:/cmd:/pc: (按 p: 提供者找), 去掉版本约束, 跳过 !冲突; install_if (i:) 条件全满足的也带上.
    同名先到先得 (indexes 的顺序); 虚拟名按 k: (provider_priority) 取最高的, 同 apk. 找不到抛 KeyError."""
    by_name, provides, every = {}, {}, []
    for repo, pkgs in indexes:
        for p in pkgs:
            p = dict(p, _repo=repo)
            every.append(p)
            by_name.setdefault(p["P"], p)
            for tok in p.get("p", "").split():
                n, prio = _name(tok), int(p.get("k") or 0)
                if n not in provides or prio > int(provides[n].get("k") or 0):
                    provides[n] = p

    def find(tok):
        n = _name(tok)
        return by_name.get(n) or provides.get(n)

    sel = {}

    def add(tokens):
        queue = list(tokens)
        while queue:
            tok = queue.pop()
            if tok.startswith("!"):
                continue
            p = find(tok)
            if p is None:
                raise KeyError(tok)
            if p["P"] not in sel:
                sel[p["P"]] = p
                queue.extend(p.get("D", "").split())

    add(wanted)
    grew = True
    while grew:
        grew = False
        for p in every:
            cond = p.get("i", "").split()
            if cond and p["P"] not in sel and all(find(c) is not None and find(c)["P"] in sel for c in cond):
                add([p["P"]])
                grew = True
    return [(p["_repo"], f"{p['P']}-{p['V']}.apk") for p in sorted(sel.values(), key=lambda p: p["P"])]


def wanted():
    return list(BASE_PKGS) + list(pxeserver.ARM64_DIAG_PKGS)


# ---------------------------------------------------------------- apkovl

def _member(name, kind=tarfile.REGTYPE, mode=0o644, data=b"", link=""):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.mtime = kind, mode, 1700000000
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.linkname = link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, (io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def build_apkovl(out, collector=COLLECTOR):
    """Alpine apkovl (initramfs 解到内存根上): 收集脚本 + local 服务开机跑它. 同样输入出同样字节 (可复现)."""
    with open(collector, "rb") as f:
        script = f.read()
    d = tarfile.DIRTYPE
    members = [
        _member("etc/", d, 0o755),
        _member("etc/.default_boot_services"),          # 带 apkovl 时 initramfs 仍加 devfs/mdev/modloop... 默认服务
        _member("etc/local.d/", d, 0o755),
        _member("etc/local.d/ops-diag.start", mode=0o755, data=START.encode()),
        _member("etc/runlevels/", d, 0o755),
        _member("etc/runlevels/default/", d, 0o755),
        _member("etc/runlevels/default/local", tarfile.SYMTYPE, 0o777, link="/etc/init.d/local"),
        _member("usr/", d, 0o755),
        _member("usr/local/", d, 0o755),
        _member("usr/local/bin/", d, 0o755),
        _member("usr/local/bin/ops-diag-collect", mode=0o755, data=script),
    ]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for info, data in members:
            t.addfile(info, data)
    tmp = f"{out}.tmp"
    with open(tmp, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(raw.getvalue())
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- 校验

def treeinfo_sha256(text, path):
    """Rocky .treeinfo [checksums] 里某个文件的 sha256; 没有抛 KeyError."""
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[checksums]" and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == path and v.strip().startswith("sha256:"):
                return v.strip()[len("sha256:"):]
    raise KeyError(path)


def sha256_line(text):
    """官方 .sha256 文件 ("<hex>  <文件名>") -> hex."""
    return text.split()[0]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, want):
    got = sha256_file(path)
    if got != want.lower():
        raise ValueError(f"{path}: sha256 {got} != 官方 {want}")


# ---------------------------------------------------------------- fetch (要外网)

def _get(url, dest=None):
    with urllib.request.urlopen(url, timeout=120) as r:
        if dest is None:
            return r.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(f"{dest}.part", "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    os.replace(f"{dest}.part", dest)
    return dest


def fetch(staging, log=print):
    tftp = os.path.join(staging, "tftp")
    dl = os.path.join(staging, "download")
    rows = []

    def row(path, url, how):
        rows.append((os.path.relpath(path, staging), os.path.getsize(path), sha256_file(path), url, how))

    # Rocky grubaa64.efi: .treeinfo 里的 sha256
    tree = _get(f"{ROCKY_TREE}/.treeinfo").decode()
    ver = re.search(r"^version = (\S+)", tree, re.M).group(1)
    grub = os.path.join(tftp, pxeserver.ARM64_EFI)
    _get(f"{ROCKY_TREE}/EFI/BOOT/grubaa64.efi", grub)
    verify_sha256(grub, treeinfo_sha256(tree, "EFI/BOOT/grubaa64.efi"))
    row(grub, f"{ROCKY_TREE}/EFI/BOOT/grubaa64.efi", f"Rocky {ver} .treeinfo sha256")
    log(f"grubaa64.efi (Rocky {ver}) ok")

    # Alpine netboot: 官方 .sha256; 只取 lts 三件
    rel = f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/releases/aarch64"
    tgz_name = f"alpine-netboot-{ALPINE_VER}-aarch64.tar.gz"
    tgz = os.path.join(dl, "alpine", tgz_name)
    if not os.path.exists(tgz):
        _get(f"{rel}/{tgz_name}", tgz)
    verify_sha256(tgz, sha256_line(_get(f"{rel}/{tgz_name}.sha256").decode()))
    diag = os.path.join(tftp, pxeserver.ARM64_DIAG)
    os.makedirs(diag, exist_ok=True)
    with tarfile.open(tgz) as t:
        for n in pxeserver.ARM64_DIAG_FILES:
            data = t.extractfile(f"boot/{n}").read()
            with open(os.path.join(diag, n), "wb") as f:
                f.write(data)
            row(os.path.join(diag, n), f"{rel}/{tgz_name}:boot/{n}", f"tarball sha256 ({tgz_name}.sha256)")
    log(f"Alpine {ALPINE_VER} netboot lts ok")

    # 子集仓库: 官方 APKINDEX (签名在里面, 板子上 apk 校验) + 闭包里的包
    indexes = []
    for repo in ALPINE_REPOS:
        url = f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/{repo}/aarch64/APKINDEX.tar.gz"
        idx = _get(url, os.path.join(diag, "repo", repo, "aarch64", "APKINDEX.tar.gz"))
        row(idx, url, "apk 板子上验签")
        indexes.append((repo, load_index(idx)))
    closure = resolve(indexes, wanted())
    for repo, apk in closure:
        url = f"{ALPINE_MIRROR}/{ALPINE_BRANCH}/{repo}/aarch64/{apk}"
        dest = os.path.join(diag, "repo", repo, "aarch64", apk)
        if not os.path.exists(dest):
            _get(url, dest)
        row(dest, url, "apk 板子上验签")
    log(f"子集仓库 {len(closure)} 个包 ok")

    ovl = build_apkovl(os.path.join(tftp, pxeserver.ARM64_APKOVL))
    row(ovl, "arm64_diag_core.build_apkovl (本仓库生成)", "-")
    with open(os.path.join(staging, "SOURCES.tsv"), "w", encoding="utf-8") as f:
        f.write("path\tsize\tsha256\tsource\tverified\n")
        f.writelines("\t".join(map(str, r)) + "\n" for r in rows)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arm64_diag_core.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--staging", required=True)
    o = sub.add_parser("apkovl")
    o.add_argument("--out", required=True)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.cmd == "fetch":
        fetch(os.path.expanduser(a.staging))
    else:
        print(build_apkovl(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
