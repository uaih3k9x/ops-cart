"""arm64_dsdt_fix: 原表反编译文本上的精确改动、差异核对、生成的 C 头. 真编译要 docker iasl, 不在这里跑."""

import hashlib

import pytest

import arm64_dsdt_fix as fx

# 结构照板子上 DSDT 的反编译 (iasl 20250807), 只留改动涉及的部分
DSL = """/*
 * header comment, Checksum 0x6F
 */
DefinitionBlock ("", "DSDT", 2, "PHYLTD", "PHYTIUM.", 0x20180509)
{
    OperationRegion (GNVS, SystemMemory, 0xF1A80000, 0x000A)
    Field (GNVS, AnyAcc, Lock, Preserve)
    {
        Offset (0x08), 
        MAC0,   8
    }

    Scope (_SB)
    {
        Device (GPI0)
        {
            Name (_UID, Zero)  // _UID: Unique ID
            Device (GP00)
            {
                Name (_UID, Zero)  // _UID: Unique ID
            }
        }

        Device (GPI1)
        {
            Device (GP00)
            {
                Name (_UID, Zero)  // _UID: Unique ID
            }
        }

        Device (CLK0)
        {
            Device (CK10)
            {
                Name (_ADR, Zero)  // _ADR: Address
            }
        }
    }

    Scope (_SB)
    {
        Device (PWRB)
        {
            Name (_HID, EisaId ("PNP0C0C") /* Power Button Device */)  // _HID: Hardware ID
            Name (_PRW, Package (0x02)  // _PRW: Power Resources for Wake
            {
                Zero, 
                0x04
            })
        }
    }
}
"""


def test_adr_variant():
    out = fx.edit_dsl(DSL, "adr")
    assert "AnyAcc, NoLock, Preserve" in out and "AnyAcc, Lock," not in out
    assert out.count("Name (_ADR, Zero)") == 3                     # CK10 原有的 + 两个 GP00
    for blk in out.split("Device (GP00)")[1:]:
        assert blk.splitlines()[2].strip().startswith("Name (_ADR, Zero)")
    assert "_PRW" in out


def test_noprw_variant_removes_only_prw():
    out = fx.edit_dsl(DSL, "adr-noprw")
    assert "_PRW" not in out and "PNP0C0C" in out
    assert out.replace("\n", "").count("0x04") == 0
    assert fx.edit_dsl(DSL, "adr") .replace(
        """            Name (_PRW, Package (0x02)  // _PRW: Power Resources for Wake
            {
                Zero, 
                0x04
            })
""", "") == out


def test_diff_matches_expected_for_both_variants():
    """模拟 iasl -d 的输出: 改动后的文本 + OperationRegion 长度改编成 ByteConst."""
    for v in fx.VARIANTS:
        re_dsl = fx.edit_dsl(DSL, v).replace("0xF1A80000, 0x000A)", "0xF1A80000, 0x0A)")
        assert fx.line_diff(fx.body(DSL), fx.body(re_dsl)) == fx.expected_changes(v)


def test_diff_catches_extra_change():
    re_dsl = fx.edit_dsl(DSL, "adr").replace("0x000A", "0x0A").replace("MAC0,   8", "MAC0,   16")
    assert fx.line_diff(fx.body(DSL), fx.body(re_dsl)) != fx.expected_changes("adr")


@pytest.mark.parametrize("bad", [
    DSL.replace("AnyAcc, Lock,", "AnyAcc, NoLock,"),               # 已经改过 / 别的表
    DSL.replace("Device (GP00)", "Device (GP01)", 1),               # 只剩一个 GP00
    DSL.replace("            Device (GP00)\n            {\n                Name (_UID",
                "            Device (GP00)\n            {\n                Name (_ADR, One)\n                Name (_UID", 1),
])
def test_edit_refuses_unexpected_text(bad):
    with pytest.raises(fx.Mismatch):
        fx.edit_dsl(bad, "adr")


def test_noprw_refuses_when_prw_moved():
    bad = DSL.replace("                0x04\n", "                0x03\n")
    with pytest.raises(fx.Mismatch):
        fx.edit_dsl(bad, "adr-noprw")
    fx.edit_dsl(bad, "adr")                                        # adr 不碰 _PRW


def test_unknown_variant():
    with pytest.raises(ValueError):
        fx.edit_dsl(DSL, "everything")


def test_check_table():
    orig = bytearray(b"DSDT" + (46).to_bytes(4, "little") + b"\x02\x00PHYLTDPHYTIUM." + bytes(22))
    orig[9] = (-sum(orig)) & 0xFF
    fx.check_table(bytes(orig), bytes(orig))
    bad = bytearray(orig)
    bad[30] ^= 1
    with pytest.raises(fx.Mismatch):
        fx.check_table(bytes(bad), bytes(orig))
    other = bytearray(orig)
    other[10:16] = b"OTHER!"
    other[9] = 0
    other[9] = (-sum(other)) & 0xFF
    with pytest.raises(fx.Mismatch):
        fx.check_table(bytes(other), bytes(orig))


def test_c_header():
    h = fx.c_header([("adr", b"\x01\x02"), ("adr-noprw", b"\x03")])
    assert "#define DG_NTABLES 2" in h
    assert '{"adr", dg_table_0, 2ULL, {' in h and '{"adr-noprw", dg_table_1, 1ULL, {' in h
    assert ", ".join(f"0x{b:02X}" for b in hashlib.sha256(b"\x01\x02").digest()) in h
