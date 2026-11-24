#!/usr/bin/env python3
"""ARM64 WinPE 网络启动用的 BCD: 拿 ADK 自带的 arm64\\Media\\EFI\\Microsoft\\Boot\\BCD (本来就是 ramdisk 写法,
device = ramdisk=[boot]\\sources\\boot.wim,{7619dcc8-...}) 改几处:
  - boot.sdi / boot.wim 的路径指到 TFTP 根下的 \\aa64\\winpe\\<build>\\ (两个版本并排放, 互不干扰);
  - 开 EMS: {emssettings} bootems (启动管理器和 winload 继承) + osloader ems (内核 / SAC);
    默认不写口号和波特率 = 用固件 ACPI SPCR 描述的口 (D2000: PL011 115200); --ems-port/--ems-baud 可强行指定.
  - 不开 testsigning / nointegritychecks (微软签名的 PE 用不着).
[boot] = 启动管理器自己来的设备: 网络启动时就是 TFTP 服务器, 路径从 TFTP 根算.

改 hive 用 hivex 的 Python 绑定 (Debian: python3-hivex), Mac 上在 docker 里跑:
  docker run --rm -v <这个目录>:/s:ro -v <staging>/winpe/22621/boot:/d local/hivex:bookworm \\
      python3 -I /s/arm64_winpe_bcd.py /d/bcd.adk /d/bcd 22621
纯函数 (ramdisk_device / edits) 不依赖 hivex, tests/test_arm64_winpe_bcd.py 测."""

import struct
import sys
import uuid

BUILDS = ("22621", "26100")
TFTP_DIR = "\\aa64\\winpe"                       # 跟 pxeserver.ARM64_WINPE_DIR 一致 (反斜杠写法)
# ADK 模板自定义的 ramdisk 选项对象; 保留名 {ramdiskoptions} 是 {ae5534e0-...}, 不是它
RAMDISK_OPTIONS_ID = "{7619dcc8-fafe-11d9-b411-000476eba25f}"
OSLOADER_ID = "{7619dcc9-fafe-11d9-b411-000476eba25f}"          # ADK 模板里 WinPE 那一项 (Windows Setup)
EMS_ID = "{0ce4991b-e6b3-4b16-b23c-5e0d9250e5d9}"               # {emssettings}

REG_SZ, REG_BINARY = 1, 3
EL_DEVICE, EL_OSDEVICE = "11000001", "21000001"                 # ApplicationDevice / OSLoaderDevice
EL_DESCRIPTION = "12000004"
EL_SDIPATH = "32000004"                                          # ramdisksdipath
EL_EMS_ENABLED = "16000020"                                      # BcdLibraryBoolean_EmsEnabled (bootems)
EL_EMS_PORT, EL_EMS_BAUD = "15000022", "15000023"                # BcdLibraryInteger_EmsPort / EmsBaudRate
EL_OSLOADER_EMS = "260000b0"                                     # BcdOSLoaderBoolean_EmsEnabled (ems)


def _sz(text):
    return REG_SZ, (text + "\0").encode("utf-16le")


def ramdisk_device(path, options_id):
    """ramdisk=[boot]<path>,<options_id> 的设备描述 (BCD 设备元素的二进制格式, 照 ADK 模板逐字段对过).
    两个长度字段 (偏移 24、60) 都是"从所在块起到末尾"的字节数, 换路径要跟着改."""
    if not path.startswith("\\") or "/" in path or len(path) < 2:
        raise ValueError(f"路径要以反斜杠开头、不含正斜杠: {path!r}")
    tail = (path + "\0").encode("utf-16le")
    parent = struct.pack("<IIII", 5, 5, 0, 72) + bytes(56)       # 父设备 = boot (类型 5), 72 字节块
    inner = struct.pack("<II", 1, 0) + parent + struct.pack("<I", 0) + tail
    inner = inner[:4] + struct.pack("<I", len(inner)) + inner[8:]
    outer = struct.pack("<IIII", 0, 1, 0, 0) + struct.pack("<I", 3) + bytes(20) + inner
    outer = outer[:8] + struct.pack("<I", len(outer)) + outer[12:]
    return uuid.UUID(options_id).bytes_le + outer


def check_template(blob):
    """模板原有的 device 必须跟自己拼的 ramdisk=[boot]\\sources\\boot.wim,{7619dcc8-...} 逐字节相同:
    运行时证明 ramdisk_device 的拼装格式对 (ADK 换了格式就停下, 不写出坏 BCD)."""
    if blob != ramdisk_device("\\sources\\boot.wim", RAMDISK_OPTIONS_ID):
        raise SystemExit("ADK 模板的 device 跟 ramdisk_device 拼的不一致, 格式变了? 不改写")


def edits(build, ems_port=None, ems_baud=None):
    """{对象 GUID: {元素号: (注册表类型, 数据)}}: 在 ADK 模板上要写的元素 (没有的新建, 有的覆盖)."""
    if build not in BUILDS:
        raise ValueError(f"build 只能是 {'/'.join(BUILDS)}: {build!r}")
    base = f"{TFTP_DIR}\\{build}"
    dev = (REG_BINARY, ramdisk_device(f"{base}\\sources\\boot.wim", RAMDISK_OPTIONS_ID))
    ems = {EL_EMS_ENABLED: (REG_BINARY, b"\x01")}
    if ems_port is not None:
        ems[EL_EMS_PORT] = (REG_BINARY, struct.pack("<Q", ems_port))
    if ems_baud is not None:
        ems[EL_EMS_BAUD] = (REG_BINARY, struct.pack("<Q", ems_baud))
    return {
        RAMDISK_OPTIONS_ID: {EL_SDIPATH: _sz(f"{base}\\boot\\boot.sdi")},
        OSLOADER_ID: {EL_DEVICE: dev, EL_OSDEVICE: dev, EL_OSLOADER_EMS: (REG_BINARY, b"\x01"),
                      EL_DESCRIPTION: _sz(f"Windows PE ARM64 {build} (cart smoke test)")},
        EMS_ID: ems,
    }


def apply(src, dst, build, ems_port=None, ems_baud=None):  # pragma: no cover - 要 hivex, docker 里跑
    import hivex
    h = hivex.Hivex(src, write=True)
    objects = h.node_get_child(h.root(), "Objects")
    loader = h.node_get_child(objects, OSLOADER_ID)
    dev = loader and h.node_get_child(h.node_get_child(loader, "Elements"), EL_DEVICE)
    if not dev:
        raise SystemExit(f"模板里没有 {OSLOADER_ID} 的 device, 不是 ADK 的 WinPE BCD?")
    check_template(h.value_value(h.node_get_value(dev, "Element"))[1])
    for obj_id, elements in edits(build, ems_port, ems_baud).items():
        obj = h.node_get_child(objects, obj_id)
        if not obj:
            raise SystemExit(f"模板里没有 {obj_id}, 不是 ADK 的 WinPE BCD?")
        els = h.node_get_child(obj, "Elements")
        for name, (typ, data) in elements.items():
            node = h.node_get_child(els, name) or h.node_add_child(els, name)
            h.node_set_value(node, {"key": "Element", "t": typ, "value": data})
    h.commit(dst)


def main(argv):  # pragma: no cover
    if len(argv) not in (3, 5):
        raise SystemExit("用法: arm64_winpe_bcd.py <ADK 的 BCD> <输出> <22621|26100> [EMS 口号 波特率]")
    port, baud = (int(argv[3]), int(argv[4])) if len(argv) == 5 else (None, None)
    apply(argv[0], argv[1], argv[2], port, baud)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
