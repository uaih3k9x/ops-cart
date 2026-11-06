"""Memtest86+ 放进 PXE 菜单 (#55): 固定官方版本, 下载后 zip 整包和解出的文件各校验一次 sha256,
任何一步不符就中止, 什么都不写 (旧文件原样留着). 二进制不进仓库.

用法 (scripts/setup-memtest.sh 调): sudo python3 /opt/pxe/memtest_core.py --root /opt/pxe/tftp [--zip 本地包]
写到 <root>/memtest/memtest64.efi (pxeserver.MEMTEST_EFI), GRUB 经 HTTP chainloader, pxelinux 用 LINUX 加载.

升级版本: 改 VERSION / MEMBER, 校验和从官网同目录的 sha256sum.txt 抄 ZIP_SHA256,
MEMBER_SHA256 是解出来那个文件的 sha256 (自己下载核对后填). 两个都要填真值, 占位一律当不符.
"""

import argparse
import hashlib
import io
import os
import re
import sys
import urllib.request
import zipfile

import atomicfile
import pxeserver

VERSION = "8.10"
ZIP_URL = f"https://memtest.org/download/v{VERSION}/mt86plus_{VERSION}.binaries.zip"
ZIP_SHA256 = "7e6c5162cb84ab959aeb9d13c9cfd6976b0dec3b34936b73820b20c55eb26c29"
MEMBER = "mt86p_810_x86_64"
MEMBER_SHA256 = "11808288ea1ee332f7d89e683cef10fbcdf2d6585bb972688ed9fe0e3d30fc55"
MAX_BYTES = 16 * 1024 * 1024        # 官方包几百 KB; 远大于这个的不收
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ChecksumError(Exception):
    pass


def check_sha256(data, expect, what):
    want = (expect or "").strip().lower()
    if not _HEX64.match(want):
        raise ChecksumError(f"{what}: 期望的 sha256 不是 64 位十六进制 (没填?): {expect!r}")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise ChecksumError(f"{what}: sha256 不符, 期望 {want}, 实际 {got}")


def extract(zip_bytes):
    """校验 zip 整包 -> 取出 MEMBER -> 再校验它. 返回文件内容."""
    check_sha256(zip_bytes, ZIP_SHA256, "zip")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            data = z.read(MEMBER)
    except (KeyError, zipfile.BadZipFile) as e:
        raise ChecksumError(f"zip 里取不到 {MEMBER}: {e}") from e
    check_sha256(data, MEMBER_SHA256, MEMBER)
    return data


def install(zip_bytes, root):
    """两道校验都过才写 (原子替换); 返回写入的路径."""
    data = extract(zip_bytes)
    path = os.path.join(str(root), pxeserver.MEMTEST_EFI)
    atomicfile.write_bytes(path, data, mode=0o644)
    return path


def fetch(url, timeout=60):
    if not url.startswith("https://"):
        raise ValueError(f"只从 https 下载: {url}")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"下载内容超过 {MAX_BYTES} 字节, 不像官方包")
    return data


def main(argv=None, fetch=fetch):
    ap = argparse.ArgumentParser(prog="memtest_core.py", description=f"安装 Memtest86+ {VERSION} 到 tftp 树")
    ap.add_argument("--root", required=True, help="tftp 根目录")
    ap.add_argument("--zip", help="用已下载的官方包, 不联网")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if a.zip:
            with open(a.zip, "rb") as f:
                blob = f.read(MAX_BYTES + 1)
        else:
            print(f"下载 {ZIP_URL}")
            blob = fetch(ZIP_URL)
        path = install(blob, a.root)
    except (ChecksumError, OSError, ValueError) as e:
        print(f"中止, 没写任何文件: {e}", file=sys.stderr)
        return 1
    print(f"Memtest86+ {VERSION} 已写入 {path} (sha256 {MEMBER_SHA256})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
