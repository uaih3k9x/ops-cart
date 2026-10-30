"""recipe_core 测试: OCR 容错匹配 / 步骤执行器 / 配方校验. 全离线, 用假画面序列驱动, 不碰硬件."""

import pytest

import recipe_core as rc


# ------------------------------------------------------------ OCR 归一化匹配

@pytest.mark.parametrize("text", [
    "Press F12 for Boot Menu",
    "Press FI2 for Boot Menu",          # 1 认成 I
    "Press Fl2 for Boot Menu",          # 1 认成 l
    "Press f12 for boot menu",
    "Press F 12 for Boot Menu",         # 中间多了空格
    "<F12> Boot Menu",
    "F12=Boot Menu",
    "Press Ｆ１２ for Boot Menu",        # 全角
    "F|2 Boot",
])
def test_match_f12_tolerates_ocr_noise(text):
    assert rc.match(text, "F12")


@pytest.mark.parametrize("text", [
    "Press F1 to enter setup",          # F1 不是 F12
    "Press F11 for BBS Popup",
    "Press F2 to enter setup",
    "F120",                             # 后面还跟着数字, 不是 F12
    "",
])
def test_match_f12_rejects_other_keys(text):
    assert not rc.match(text, "F12")


def test_match_f1_does_not_hit_f12_or_f11():
    assert not rc.match("Press F12 for Boot Menu", "F1")
    assert not rc.match("F11 BBS Popup", "F1")
    assert rc.match("Press F1 to continue", "F1")


@pytest.mark.parametrize("text", [
    "UEFI: PXE IPv4 Intel(R) Ethernet",
    "UEFI: PX E IPv4",                  # PXE 中间断开
    "UEFI:pxe ipv4",
    "UEFI: PXE IPv4",
    "UEFI: PXE lPv4",                   # I 认成 l
    "UEFI: PXE 1Pv4",
])
def test_match_pxe_ipv4_variants(text):
    assert rc.match(text, "PXE")
    assert rc.match(text, "IPv4")


def test_match_o_and_zero_confusable():
    assert rc.match("B00t Menu", "Boot Menu")
    assert rc.match("BootMenu", "Boot Menu")
    assert rc.match("boot  menu", "Boot Menu")


def test_match_is_whole_word_not_anything():
    # ESC 不能在 Description 里撞上
    assert not rc.match("Description", "ESC")
    assert rc.match("Press ESC for boot options", "ESC")
    assert rc.match("<ESC> Boot Menu", "ESC")
    # PXE 不能在别的词中间
    assert not rc.match("APXEB", "PXE")
    # 完全不相干的不能对上
    assert not rc.match("Windows Boot Manager", "PXE")
    assert not rc.match("SATA: Samsung SSD", "Network")
    assert not rc.match("Network", "Networks")


def test_match_ignores_confusables_only_where_listed():
    # S/5, B/8 不算同一个字 (不做成什么都能对上)
    assert not rc.match("PXE 8OOT", "PXE BOOT")
    assert not rc.match("Boot", "8oot")
    assert not rc.match("Setup", "5etup")


def test_match_chinese():
    assert rc.match("请选择启动设备", "启动设备")
    assert not rc.match("请选择启动设备", "网络启动")


def test_norm_maps_confusables():
    assert rc.norm("FI2") == rc.norm("F12") == rc.norm("fl2") == rc.norm("f|2")
    assert rc.norm("B0OT") == rc.norm("boot")
    assert rc.norm("Ｆ１２") == rc.norm("f12")


def test_find_returns_first_line_by_pattern_priority():
    lines = [{"text": "UEFI: PXE IPv6 Intel", "y": 10},
             {"text": "UEFI: PXE IPv4 Intel", "y": 30}]
    # 模式按优先级: 先找 IPv4, 再退到 PXE
    hit = rc.find(lines, ["IPv4", "PXE"])
    assert hit == (1, "IPv4")
    assert rc.find(lines, ["Network"]) is None
    assert rc.find([], ["PXE"]) is None


def test_find_skips_lines_matching_avoid():
    lines = [{"text": "UEFI: PXE IPv6", "y": 10}, {"text": "UEFI: PXE IPv4", "y": 30}]
    assert rc.find(lines, ["PXE"], avoid=["IPv6"]) == (1, "PXE")
    assert rc.find(lines[:1], ["PXE"], avoid=["IPv6"]) is None


def test_pattern_too_short_rejected():
    assert not rc.pattern_ok("F")
    assert not rc.pattern_ok("  ")
    assert not rc.pattern_ok("--")
    assert rc.pattern_ok("F8")
    assert rc.pattern_ok("启动")


def test_match_ipv4_not_ipv6_and_punctuation_boundaries():
    assert not rc.match("UEFI: PXE IPv6", "IPv4")
    assert rc.match("PXE-E18: Server response timeout", "PXE")
    assert rc.match("(F12)", "F12")
    assert rc.match("F12:Boot", "F12")
