#!/usr/bin/env python3
"""飞腾 D2000 运行时 DSDT 的修正版 (给 dsdtguard.efi 在 UEFI Shell 里整表换上, docs/ARM64_WINPE.md 第 8 节).

从板子上读出的原表 (/sys/firmware/acpi/tables/DSDT) 出发: iasl 反编译 -> 文本上做几处精确改动 -> iasl -oa 编译
-> 再反编译跟原表的反编译逐行比, 只允许预期的那几行不同 (多一行少一行都报错).

变体 (每个都含前一个; 名字也是 dsdtguard.efi swap 的参数):
  adr        Field GNVS Lock -> NoLock (0x7E 的修法) + GPI0.GP00 / GPI1.GP00 补 Name (_ADR, Zero) (0xA5 参数 0xD "_ADR")
  adr-noprw  再删 PWRB 的 _PRW {GPE 0, S4}: Hardware-Reduced 平台没有 GPE 块. Windows 会不会因此停机没把握, 单独一档

_ADR 取 0 的依据: 两个 GPIO 控制器 (PHYT0001/FTGP0001, DesignWare APB GPIO) 各只有一个端口子设备 GP00, 它的 _DSD
"reg" = 0 (端口 A); Linux gpio-dwapb 在 ACPI 下就用 "reg" 当端口号. _ADR 跟 "reg" 取同一个值.

表本身 (固件厂商的 AML) 不进 git: 生成到 efi/dsdtguard/dsdt_tables.h (gitignore) 和输出目录. 用法:
  python3 arm64_dsdt_fix.py build --dsdt <原表> --out <目录>      # 要 docker (local/iasl:alpine3.24, 第一次会建)
测试: tests/test_arm64_dsdt_fix.py."""

import argparse
import hashlib
import os
import re
import subprocess
import sys

import arm64_winpe_local as wl

VARIANTS = ("adr", "adr-noprw")
IASL_IMAGE = "local/iasl:alpine3.24"

LOCK_OLD = "    Field (GNVS, AnyAcc, Lock, Preserve)"
LOCK_NEW = "    Field (GNVS, AnyAcc, NoLock, Preserve)"
REGION_OLD = "    OperationRegion (GNVS, SystemMemory, 0xF1A80000, 0x000A)"
REGION_NEW = "    OperationRegion (GNVS, SystemMemory, 0xF1A80000, 0x0A)"
GP00_RE = re.compile(r"^( +)Device \(GP00\)\n\1\{\n(?=\1    Name \(_UID, Zero\))", re.M)   # 原表: 紧跟 _UID, 没有 _ADR
PRW_RE = re.compile(r"^( +)Name \(_PRW, Package \(0x02\)[^\n]*\n\1\{\n\1    Zero, ?\n\1    0x04\n\1\}\)\n", re.M)
PWRB_RE = re.compile(r"Device \(PWRB\)\n +\{\n +Name \(_HID, EisaId \(\"PNP0C0C\"\)[^\n]*\n")


class Mismatch(Exception):
    pass


def _once(n, what):
    if n != 1:
        raise Mismatch(f"{what}: 找到 {n} 处, 应为 1")


def edit_dsl(text, variant):
    """原表反编译文本 -> 变体的 ASL. 每个锚点必须恰好匹配预期次数, 不然 Mismatch (固件换了就停, 不瞎改)."""
    if variant not in VARIANTS:
        raise ValueError(f"变体只能是 {'/'.join(VARIANTS)}: {variant!r}")
    _once(text.count(LOCK_OLD + "\n"), "Field (GNVS, ..., Lock, ...)")
    text = text.replace(LOCK_OLD + "\n", LOCK_NEW + "\n")
    hits = GP00_RE.findall(text)
    if len(hits) != 2:
        raise Mismatch(f"Device (GP00) (后面紧跟 _UID): 找到 {len(hits)} 处, 应为 2")
    text = GP00_RE.sub(lambda m: f"{m.group(0)}{m.group(1)}    Name (_ADR, Zero)  // _ADR: Address\n", text)
    if variant == "adr-noprw":
        m = PWRB_RE.search(text)
        _once(len(PWRB_RE.findall(text)), "Device (PWRB) PNP0C0C")
        _once(len(PRW_RE.findall(text)), "PWRB 的 _PRW")
        p = PRW_RE.search(text)
        if p.start() != m.end():
            raise Mismatch("_PRW 不在 PWRB 的 _HID 后面")
        text = text[:p.start()] + text[p.end():]
    return text


def body(dsl):
    """去掉反编译文本开头的注释块 (表头: 长度/校验和/编译器版本会变), 从 DefinitionBlock 起算."""
    i = dsl.index("DefinitionBlock (")
    return dsl[i:].splitlines()


def expected_changes(variant):
    """(删掉的行, 加上的行), 各自是排序后的列表 (按反编译格式, 去掉行尾空白比)."""
    # OperationRegion 长度 10: 原表编成 WordConst (0B 0A 00, 固件 AcpiPlatform 认的 15 字节特征串), iasl 一律用最短的
    # ByteConst (0A 0A). 值不变; 换表发生在 AcpiPlatform 改写之后, 不再需要那个特征串.
    minus = [LOCK_OLD, REGION_OLD]
    plus = [LOCK_NEW, REGION_NEW] + ["                Name (_ADR, Zero)  // _ADR: Address"] * 2
    if variant == "adr-noprw":
        minus += ["            Name (_PRW, Package (0x02)  // _PRW: Power Resources for Wake", "            {",
                  "                Zero,", "                0x04", "            })"]
    return sorted(x.rstrip() for x in minus), sorted(x.rstrip() for x in plus)


def line_diff(a, b):
    """两份反编译文本 (body) 的差异: (只在 a 里的行, 只在 b 里的行), 按 difflib 对齐, 各自排序."""
    import difflib
    minus, plus = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            minus += a[i1:i2]
        if tag in ("replace", "insert"):
            plus += b[j1:j2]
    return sorted(x.rstrip() for x in minus), sorted(x.rstrip() for x in plus)


def check_table(aml, oem_from):
    """编出来的表: 签名、长度字段、整表字节和、OEM 字段跟原表相同."""
    n = int.from_bytes(aml[4:8], "little")
    if aml[:4] != b"DSDT" or n != len(aml) or sum(aml) & 0xFF or aml[10:24] != oem_from[10:24]:
        raise Mismatch("编出来的表头/长度/校验和不对")


def c_header(tables):
    """tables: [(变体名, aml)] -> dsdt_tables.h. sha256 写死, dsdtguard.efi 用之前先对数组本身算一遍."""
    out = ["/* 生成的, 别手改, 不进 git: python3 arm64_dsdt_fix.py build (含固件厂商的 AML). */",
           f"#define DG_NTABLES {len(tables)}"]
    for i, (name, aml) in enumerate(tables):
        rows = [", ".join(f"0x{b:02X}" for b in aml[k:k + 16]) for k in range(0, len(aml), 16)]
        out.append(f"static const unsigned char dg_table_{i}[{len(aml)}] = {{\n    " + ",\n    ".join(rows) + "\n};")
    out.append("static const struct dg_table { const char *name; const unsigned char *aml; unsigned long long len; "
               "unsigned char sha256[32]; } dg_tables[DG_NTABLES] = {")
    for i, (name, aml) in enumerate(tables):
        sha = ", ".join(f"0x{b:02X}" for b in hashlib.sha256(aml).digest())
        out.append(f'    {{"{name}", dg_table_{i}, {len(aml)}ULL, {{{sha}}}}},')
    out.append("};")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 构建 (docker 里 iasl)

def _iasl(workdir, script):  # pragma: no cover - docker
    if subprocess.run(["docker", "image", "inspect", IASL_IMAGE], capture_output=True).returncode:
        subprocess.run(["docker", "build", "-q", "-t", IASL_IMAGE, "-"], check=True,
                       input=b"FROM alpine:3.24\nRUN apk add --no-cache iasl\n")
    p = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{workdir}:/w", IASL_IMAGE,
                        "sh", "-euc", f"cd /w && {script}"], capture_output=True, text=True)
    if p.returncode:
        raise Mismatch(f"iasl 失败:\n{p.stdout}\n{p.stderr}")
    return p.stdout + p.stderr


def build(dsdt, out, header=None, log=print):  # pragma: no cover - docker; 纯函数部分有测试
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    orig = open(dsdt, "rb").read()
    if wl.classify(orig) != wl.ORIG:
        raise Mismatch(f"{dsdt} 不是认识的原表: {wl.NAMES.get(wl.classify(orig))}")
    open(os.path.join(out, "orig.aml"), "wb").write(orig)
    _iasl(out, "iasl -d orig.aml >/dev/null")
    orig_dsl = open(os.path.join(out, "orig.dsl")).read()
    tables = []
    for v in VARIANTS:
        open(os.path.join(out, f"{v}.asl"), "w").write(edit_dsl(orig_dsl, v))
        msg = _iasl(out, f"iasl -oa -p {v} {v}.asl && mkdir -p re && cp {v}.aml re/ && iasl -d re/{v}.aml >/dev/null")
        if not re.search(r"0 Errors, 0 Warnings", msg):
            raise Mismatch(f"{v}: iasl 有错误或警告:\n{msg}")
        aml = open(os.path.join(out, f"{v}.aml"), "rb").read()
        check_table(aml, orig)
        got = line_diff(body(orig_dsl), body(open(os.path.join(out, "re", f"{v}.dsl")).read()))
        if got != expected_changes(v):
            raise Mismatch(f"{v}: 反编译跟原表的差异不是预期的:\n- {got[0]}\n+ {got[1]}")
        import difflib
        with open(os.path.join(out, f"{v}.diff"), "w") as f:
            f.writelines(difflib.unified_diff(
                [x + "\n" for x in body(orig_dsl)], [x + "\n" for x in body(open(os.path.join(out, "re", f"{v}.dsl")).read())],
                "orig.dsl", f"{v}.dsl (iasl -d)", n=3))
        tables.append((v, aml))
        log(f"{v}: {len(aml)} 字节 sha256 {hashlib.sha256(aml).hexdigest()} (差异只有预期的那几行)")
    h = c_header(tables)
    with open(header or os.path.join(out, "dsdt_tables.h"), "w") as f:
        f.write(h)
    return tables


def main(argv=None):  # pragma: no cover
    ap = argparse.ArgumentParser(prog="arm64_dsdt_fix.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dsdt", required=True, help="板子上读出的原表")
    b.add_argument("--out", required=True)
    b.add_argument("--header", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "efi", "dsdtguard", "dsdt_tables.h"))
    a = ap.parse_args(argv)
    try:
        build(a.dsdt, a.out, a.header)
    except Mismatch as ex:
        print(f"停: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
