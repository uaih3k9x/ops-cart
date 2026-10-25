#!/usr/bin/env python3
r"""公开仓库脱敏检查: 查真实地址 / MAC / 本机路径 / 本地词表. 只用标准库.

内置规则:
  - IPv4: 公网地址和 CGNAT 段 (tailnet) 算发现; 文档段 (192.0.2/24, 198.51.100/24,
    203.0.113/24)、本机、广播、掩码放过. 私网段 (10/8, 172.16/12, 192.168/16) 太常见,
    哪些是真实现场网段写进本地词表 (如 re:\b192\.168\.9\.)
  - IPv6: 全局单播地址, 文档段 2001:db8::/32 除外
  - MAC: 全球唯一的单播地址; 本地管理位 02:..、组播、全 0 / 全 f 放过
  - 路径: /Users/<名字>、/home/<名字> (通用账号名放过)
行内写 public-safe-ignore 可豁免整行. 额外的词表放在不进仓库的 .public-safe.local:
每行一个词 (不分大小写子串), 以 re: 开头的按正则.

用法: python3 scripts/check_public_safe.py [仓库目录]   有发现退出码 1
"""

import ipaddress
import os
import re
import subprocess
import sys

IGNORE_MARK = "public-safe-ignore"
SKIP_PREFIXES = ("web/vendor/",             # 第三方原样拷贝, 不归我们管
                 "scripts/check_public_safe.py", "tests/test_public_safe.py")   # 规则和样例本身
_DOC_V4 = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                                              "127.0.0.0/8")]
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DOC_V6 = ipaddress.ip_network("2001:db8::/32")
_OK_USERS = {"ops", "user", "you", "example", "<user>", "username"}

_V4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
_V6 = re.compile(r"(?<![0-9A-Za-z:])((?:[0-9a-fA-F]{1,4}:){1,7}:?(?:[0-9a-fA-F]{1,4})?(?::[0-9a-fA-F]{1,4})*)"
                 r"(?![0-9A-Za-z:])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2})"
                  r"(?![0-9A-Fa-f:-]*[0-9A-Fa-f])")
_GRUB_MAC = re.compile(r"cfg-01-((?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2})")    # grub.cfg-01-<mac>
_PATH = re.compile(r"/(?:Users|home)/([^/\s\"'`]+)")


def _v4_bad(s):
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    if s in ("0.0.0.0", "255.255.255.255") or s.startswith("255.") or any(a in n for n in _DOC_V4):
        return False
    return a in _CGNAT or a.is_global


def _v6_bad(s):
    if "::" not in s and s.count(":") != 7:
        return False                        # 时间 12:00:00 之类
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    return a.version == 6 and a.is_global and a not in _DOC_V6


def _mac_bad(s):
    octets = [int(x, 16) for x in re.split("[:-]", s)]
    if all(o == 0 for o in octets) or all(o == 0xFF for o in octets):
        return False
    return not octets[0] & 0x03            # 本地管理位或组播位置位的都不指向具体网卡


def _word_hits(line, deny_words):
    for w in deny_words:
        if w.startswith("re:"):
            if re.search(w[3:], line, re.IGNORECASE):
                yield w
        elif w.lower() in line.lower():
            yield w


def scan_text(text, deny_words=()):
    """-> [(行号, 类别, 片段)]; 类别 ipv4 / ipv6 / mac / path / word."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARK in line:
            continue
        for m in _V4.finditer(line):
            if _v4_bad(m.group(1)):
                out.append((n, "ipv4", m.group(1)))
        for m in _V6.finditer(line):
            if _v6_bad(m.group(1)):
                out.append((n, "ipv6", m.group(1)))
        for m in _MAC.finditer(line):
            if _mac_bad(m.group(1)):
                out.append((n, "mac", m.group(1)))
        for m in _GRUB_MAC.finditer(line):
            if _mac_bad(m.group(1)):
                out.append((n, "mac", m.group(1)))
        for m in _PATH.finditer(line):
            if m.group(1) not in _OK_USERS and not m.group(1).startswith(("{", "$")):
                out.append((n, "path", m.group(0)))
        for w in _word_hits(line, deny_words):
            out.append((n, "word", w))
    return out


def load_local(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [s.strip() for s in f if s.strip() and not s.lstrip().startswith("#")]
    except OSError:
        return []


def scan_repo(root, files, deny_words=()):
    """-> [(文件, 行号, 类别, 片段)]; 二进制文件跳过, 文件名本身也查."""
    out = []
    for rel in files:
        if rel.startswith(SKIP_PREFIXES):
            continue
        for n, k, s in scan_text(rel, deny_words):
            out.append((rel, 0, k, s))
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        out.extend((rel, n, k, s) for n, k, s in scan_text(text, deny_words))
    return out


def main(argv=None):
    root = (argv or sys.argv[1:] or ["."])[0]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                           check=True).stdout.split()
    found = scan_repo(root, files, load_local(os.path.join(root, ".public-safe.local")))
    for f, n, k, s in found:
        print(f"{f}:{n}: {k} {s}")
    print(f"{len(found)} 处发现" if found else "脱敏检查通过")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
