"""scripts/arm64_winpe_bcd.py: 从 ADK 自带的 ramdisk BCD 改出网络启动用的 BCD (路径指到 aa64/winpe/<build>/, 开 EMS).
只测纯函数部分 (设备描述的二进制格式、要改的元素); 真改 hive 要 hivex, 在 docker 里跑, 见 docs/ARM64_WINPE.md."""

import importlib.util
import os
import struct
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("arm64_winpe_bcd", os.path.join(ROOT, "scripts", "arm64_winpe_bcd.py"))
bcd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcd)

RAMDISK_OPTIONS = "{7619dcc8-fafe-11d9-b411-000476eba25f}"


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


@pytest.mark.parametrize("path", ["\\sources\\boot.wim", "\\aa64\\winpe\\22621\\sources\\boot.wim"])
def test_ramdisk_device_layout(path):
    blob = bcd.ramdisk_device(path, RAMDISK_OPTIONS)
    assert blob[:16] == uuid.UUID(RAMDISK_OPTIONS).bytes_le           # 附加选项 = {ramdiskoptions}
    assert u32(blob, 24) == len(blob) - 16                            # 两层长度字段都是"从这里到末尾"
    assert u32(blob, 32) == 3                                          # 设备类型 ramdisk
    assert u32(blob, 60) == len(blob) - 56
    assert u32(blob, 64) == 5 and u32(blob, 76) == 72                 # 父设备 = boot (启动管理器自己来的设备)
    assert blob[140:] == (path + "\0").encode("utf-16le")


def test_ramdisk_device_length_grows_with_path():
    a = bcd.ramdisk_device("\\a.wim", RAMDISK_OPTIONS)
    b = bcd.ramdisk_device("\\abc.wim", RAMDISK_OPTIONS)
    assert len(b) - len(a) == 4 and u32(b, 24) - u32(a, 24) == 4


@pytest.mark.parametrize("bad", ["sources\\boot.wim", "\\a/b.wim", ""])
def test_ramdisk_device_rejects_bad_path(bad):
    with pytest.raises(ValueError):
        bcd.ramdisk_device(bad, RAMDISK_OPTIONS)


def test_edits_point_into_build_dir_and_enable_ems():
    e = bcd.edits("22621")
    sdi = e[bcd.RAMDISK_OPTIONS_ID]["32000004"]
    assert sdi == (1, "\\aa64\\winpe\\22621\\boot\\boot.sdi".encode("utf-16le") + b"\0\0")
    dev = e[bcd.OSLOADER_ID]["11000001"]
    assert dev == e[bcd.OSLOADER_ID]["21000001"]                      # device 和 osdevice 一样
    assert dev[0] == 3 and dev[1].endswith("\\aa64\\winpe\\22621\\sources\\boot.wim\0".encode("utf-16le"))
    assert e[bcd.OSLOADER_ID]["260000b0"] == (3, b"\x01")             # 内核 EMS (ems on)
    assert e[bcd.EMS_ID]["16000020"] == (3, b"\x01")                  # bootems: 启动管理器 / winload 继承
    assert "15000022" not in e[bcd.EMS_ID]                             # 默认不写口号: 用固件 SPCR 描述的口


def test_edits_explicit_ems_port():
    e = bcd.edits("26100", ems_port=1, ems_baud=115200)
    assert e[bcd.EMS_ID]["15000022"] == (3, struct.pack("<Q", 1))
    assert e[bcd.EMS_ID]["15000023"] == (3, struct.pack("<Q", 115200))


def test_edits_never_weaken_signing():
    """不开 testsigning / nointegritychecks: 微软签名的 PE 用不着."""
    flat = {k for obj in bcd.edits("22621").values() for k in obj}
    assert not flat & {"16000049", "16000048"}


def test_unknown_build_rejected():
    with pytest.raises(ValueError):
        bcd.edits("19041")
