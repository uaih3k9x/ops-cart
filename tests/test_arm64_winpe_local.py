"""arm64_winpe_local: 本地盘 WinPE + UEFI Shell 内存 DSDT 补丁 (docs/ARM64_WINPE.md 第 7 节)."""

import hashlib
import io
import os
import shutil
import struct
import subprocess
import tarfile

import pytest

import arm64_winpe_local as wl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EFI_DIR = os.path.join(ROOT, "efi", "dsdtguard")


# ---------------------------------------------------------------- 假 DSDT

def fake_dsdt(length=200, lock=0x10, oem=b"OEMID "):
    """结构同板子上的: 0x24 起 OperationRegion GNVS, 0x33 起 Field (GNVS, ..., Lock), 标志字节在 0x3A."""
    body = (b"\x5b\x80GNVS\x00\x0c\x00\x00\xa8\xf1\x0b\x0a\x00"      # 0x24..0x32 OperationRegion
            + b"\x5b\x81\x13GNVS" + bytes([lock]) + b"\x00\x40\x04MAC0\x08MAC1\x08")
    t = bytearray(b"DSDT" + struct.pack("<I", length) + b"\x02\x00" + oem + b"TABLEID " + bytes(12))
    assert len(t) == 36
    t += body + bytes(length - 36 - len(body))
    t[9] = (-sum(t)) & 0xFF
    return bytes(t)


def expect_for(t):
    return wl.Expect(addr=0xF19F0000, length=len(t), csum_old=t[wl.CSUM_OFF], fnv=wl.fingerprint(t))


def test_fake_dsdt_layout_matches_board():
    t = fake_dsdt()
    assert t[wl.LOCK_OFF] == wl.LOCK_OLD
    assert t[wl.CTX_OFF:wl.CTX_OFF + len(wl.CTX)] == wl.CTX
    assert sum(t) % 256 == 0


# ---------------------------------------------------------------- 判定

def test_fnv1a64_known_vector():
    assert wl.fnv1a64(b"") == 0xcbf29ce484222325
    assert wl.fnv1a64(b"a") == 0xaf63dc4c8601ec8c


def test_fingerprint_ignores_only_the_two_patched_bytes():
    t = fake_dsdt()
    p = wl.patch(t)
    assert wl.fingerprint(t) == wl.fingerprint(p)
    other = bytearray(t)
    other[0x50] ^= 1
    assert wl.fingerprint(bytes(other)) != wl.fingerprint(t)


def test_patch_flips_lock_and_fixes_checksum():
    t = fake_dsdt()
    p = wl.patch(t)
    assert p[wl.LOCK_OFF] == wl.LOCK_NEW
    assert p[wl.CSUM_OFF] == (t[wl.CSUM_OFF] + 0x10) & 0xFF
    assert sum(p) % 256 == 0
    assert [i for i in range(len(t)) if t[i] != p[i]] == [wl.CSUM_OFF, wl.LOCK_OFF]


def test_classify_orig_and_patched():
    t = fake_dsdt()
    e = expect_for(t)
    assert wl.classify(t, e) == wl.ORIG
    assert wl.classify(wl.patch(t), e) == wl.PATCHED


@pytest.mark.parametrize("mutate,code", [
    (lambda t: b"SSDT" + t[4:], wl.E_SIG),
    (lambda t: t[:100], wl.E_LEN),
    (lambda t: t[:9] + bytes([(t[9] + 1) & 0xFF]) + t[10:], wl.E_SUM),
])
def test_classify_rejects(mutate, code):
    t = fake_dsdt()
    assert wl.classify(mutate(t), expect_for(t)) == code


def test_classify_rejects_other_table_same_length():
    t, other = fake_dsdt(), fake_dsdt(oem=b"OTHER ")
    assert wl.classify(other, expect_for(t)) == wl.E_FNV


def test_classify_rejects_wrong_declared_length():
    t = fake_dsdt()
    assert wl.classify(t, expect_for(t)._replace(length=len(t) + 4)) == wl.E_LEN


def test_classify_rejects_context_moved():
    t = bytearray(fake_dsdt())
    t[0x36:0x3A] = b"XNVS"                       # Field 不是 GNVS 了: 0x3A 那个字节不是我们以为的那个
    t[9] = (t[9] - (sum(t) % 256)) & 0xFF
    assert wl.classify(bytes(t), expect_for(fake_dsdt())) == wl.E_CTX


def test_classify_rejects_half_patched():
    t = bytearray(fake_dsdt())
    e = expect_for(bytes(t))
    t[wl.LOCK_OFF] = 0x00
    t[0x60] = 0x10                               # 和不变, 指纹不看 0x3A, 但 0x60 变了 -> 指纹不对
    assert wl.classify(bytes(t), e) == wl.E_FNV
    t = bytearray(fake_dsdt())
    t[wl.LOCK_OFF] = 0x00                        # 只写了标志字节, 校验和没跟上
    assert wl.classify(bytes(t), e) == wl.E_SUM
    t = bytearray(fake_dsdt())
    t[wl.LOCK_OFF], t[wl.CSUM_OFF] = 0x08, (t[wl.CSUM_OFF] + 0x08) & 0xFF     # 和对, 但不是我们的补丁
    assert wl.classify(bytes(t), e) == wl.E_STATE


def test_board_expect_is_consistent():
    e = wl.BOARD
    assert e.addr == 0xF19F0000 and e.length == 4176
    assert e.csum_old == 0x6F and e.csum_new == 0x7F
    assert (wl.LOCK_OFF, wl.LOCK_OLD, wl.LOCK_NEW, wl.CSUM_OFF) == (0x3A, 0x10, 0x00, 9)


def fake_fadt(d32, x, length=276):
    f = bytearray(length)
    f[0:4] = b"FACP"
    f[4:8] = length.to_bytes(4, "little")
    struct.pack_into("<I", f, 40, d32)
    struct.pack_into("<Q", f, 140, x)
    f[9] = (-sum(f)) & 0xFF
    return bytes(f)


def test_fadt_retarget():
    f = fake_fadt(0xF19F0000, 0xF19F0000)
    g = wl.fadt_retarget(f, 0xE0001000)
    assert wl.fadt_dsdt(g) == 0xE0001000 and sum(g) % 256 == 0
    assert struct.unpack_from("<I", g, 40)[0] == 0xE0001000
    assert [i for i in range(len(f)) if f[i] != g[i] and not 40 <= i < 44 and not 140 <= i < 148] == [9]
    assert wl.fadt_retarget(f, 0x100000000) is None                # 32 位指针放不下
    only_x = wl.fadt_retarget(fake_fadt(0, 0xF19F0000), 0x100000000)
    assert struct.unpack_from("<I", only_x, 40)[0] == 0 and wl.fadt_dsdt(only_x) == 0x100000000
    assert wl.fadt_retarget(fake_fadt(0, 0), 0x1000) is None
    bad = bytearray(f)
    bad[100] ^= 1
    assert wl.fadt_retarget(bytes(bad), 0x1000) is None           # 原来的校验和就不对: 不碰


def test_startup_nsh_swap_mode():
    s = wl.startup_nsh(wl.BOARD, "adr")
    lines = [ln.strip() for ln in s.splitlines()]
    assert not any(ln.startswith("mm ") for ln in lines)
    g = wl.ESP_GUARD_SHELL
    i = lines.index(f"{g} swap adr")
    assert lines.index(f"{g} selftest-ok") < i < lines.index(wl.ESP_BOOTMGR_SHELL)
    assert lines[i + 1:i + 5] == ["if %lasterror% ne 0 then", lines[i + 2], "exit /b 1", "endif"]
    assert "STOP" in lines[i + 2]
    with pytest.raises(ValueError):
        wl.startup_nsh(wl.BOARD, "adr; reset")


def test_fadt_dsdt_addr():
    f = bytearray(276)
    f[0:4] = b"FACP"
    struct.pack_into("<I", f, 40, 0xF19F0000)
    struct.pack_into("<Q", f, 140, 0xF19F0000)
    assert wl.fadt_dsdt(bytes(f)) == 0xF19F0000
    struct.pack_into("<I", f, 40, 0)
    assert wl.fadt_dsdt(bytes(f)) == 0xF19F0000
    struct.pack_into("<I", f, 40, 0x1000)                        # 两个指针不一致: 不认
    assert wl.fadt_dsdt(bytes(f)) is None
    struct.pack_into("<Q", f, 140, 0)
    assert wl.fadt_dsdt(bytes(f)) == 0x1000                      # 只有 32 位指针
    assert wl.fadt_dsdt(b"FACP" + bytes(100)) is None


# ---------------------------------------------------------------- startup.nsh

def test_startup_nsh_guards_before_writes():
    s = wl.startup_nsh(wl.BOARD)
    lines = [ln.strip() for ln in s.splitlines()]
    mm = [i for i, ln in enumerate(lines) if ln.startswith("mm ")]
    assert [lines[i] for i in mm] == ["mm F19F003A 00 -w 1 -n", "mm F19F0009 7F -w 1 -n"]
    g = wl.ESP_GUARD_SHELL
    order = [lines.index(f"{g} selftest-fail"), lines.index(f"{g} selftest-ok"),
             lines.index(f"{g} patched"), lines.index(f"{g} orig")]
    assert order == sorted(order) and order[-1] < mm[0]
    # orig 检查和写之间只有一个 if/exit: 没过就停
    between = lines[order[-1] + 1:mm[0]]
    assert between == ["if %lasterror% ne 0 then", between[1], "exit /b 1", "endif"]
    assert between[1].startswith("echo ") and "STOP" in between[1]
    # 写完再核一遍, 不过不启动
    after = lines[mm[1] + 1:]
    assert after[:2] == [f"{g} patched", "if %lasterror% ne 0 then"] and after[3] == "exit /b 1"
    assert lines.index(wl.ESP_BOOTMGR_SHELL) > mm[1]
    assert s.isascii() and "\r\n" in s              # 固件 Shell 的脚本: 纯 ASCII + CRLF


def test_startup_nsh_ifs_balanced():
    lines = [ln.strip() for ln in wl.startup_nsh(wl.BOARD).splitlines()]
    depth = 0
    for ln in lines:
        if ln.startswith("if "):
            assert ln.endswith(" then")
            depth += 1
        elif ln == "endif":
            depth -= 1
            assert depth >= 0
    assert depth == 0


def test_esp_has_no_removable_default_loader():
    """固件对没有启动项的盘会去跑 \\EFI\\Boot\\bootaa64.efi: 绕过 startup.nsh 的守卫直接起 WinPE (没打补丁).
    所以 ESP 上绝不能有这个路径."""
    dests = {d.lower() for _, d in wl.esp_layout(["bootaa64.efi", "boot/boot.sdi", "sources/boot.wim",
                                                   "boot/fonts/wgl4_boot.ttf", "boot/en-us/bootmgr.efi.mui"])}
    assert "efi/boot/bootaa64.efi" not in dests
    assert not any(d.startswith("efi/boot/") for d in dests)
    assert {"efi/cart/bootaa64.efi", "efi/microsoft/boot/fonts/wgl4_boot.ttf", "boot/boot.sdi",
            "sources/boot.wim", "efi/cart/en-us/bootaa64.efi.mui"} <= dests


def test_esp_layout_skips_pxe_only_files():
    got = dict(wl.esp_layout(["bootaa64.efi", "boot/bcd", "boot/bcd.adk", "boot/bcd.com1", "boot/boot.sdi"]))
    assert set(got) == {"bootaa64.efi", "boot/boot.sdi"}


# ---------------------------------------------------------------- C 头 + 主机上编 C 判定, 跟 Python 对拍

def test_header_matches_committed():
    with open(os.path.join(EFI_DIR, "dsdt_expect.h"), encoding="utf-8") as f:
        assert f.read() == wl.c_header(wl.BOARD)


CC = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


@pytest.mark.skipif(CC is None, reason="没有 C 编译器")
def test_c_core_agrees_with_python(tmp_path):
    t = fake_dsdt()
    e = expect_for(t)
    (tmp_path / "dsdt_expect.h").write_text(wl.c_header(e))
    exe = tmp_path / "host"
    subprocess.run([CC, "-std=c99", "-Wall", "-Werror", "-O1", f"-I{tmp_path}", f"-I{EFI_DIR}",
                    os.path.join(HERE, "dsdtguard_host.c"), "-o", str(exe)], check=True)
    cases = {
        "orig": t, "patched": wl.patch(t), "sig": b"SSDT" + t[4:], "short": t[:100],
        "sum": t[:9] + bytes([(t[9] + 1) & 0xFF]) + t[10:], "other": fake_dsdt(oem=b"OTHER "),
        "lock0": fake_dsdt(lock=0x00),
    }
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        out = subprocess.run([str(exe), "classify", str(p)], capture_output=True, text=True, check=True).stdout.strip()
        assert int(out) == wl.classify(data, e), name
        out = subprocess.run([str(exe), "sha256", str(p)], capture_output=True, text=True, check=True).stdout.strip()
        assert out == hashlib.sha256(data).hexdigest(), name
    for n in (0, 1, 55, 56, 63, 64, 65, 119, 120, 4187):              # SHA-256 填充的边界
        p = tmp_path / f"len{n}"
        p.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(n)))
        out = subprocess.run([str(exe), "sha256", str(p)], capture_output=True, text=True, check=True).stdout.strip()
        assert out == hashlib.sha256(p.read_bytes()).hexdigest(), n
    for f, addr in ((fake_fadt(0xF19F0000, 0xF19F0000), 0xE0001000), (fake_fadt(0, 0xF19F0000), 0x123456789000),
                    (fake_fadt(0xF19F0000, 0), 0xE0001000), (fake_fadt(0xF19F0000, 0xF19F0000), 0x100000000),
                    (fake_fadt(0, 0), 0x1000), (b"XXXX" + fake_fadt(1, 1)[4:], 0x1000)):
        p = tmp_path / "fadt"
        p.write_bytes(f)
        out = subprocess.run([str(exe), "retarget", str(p), hex(addr)], capture_output=True, text=True,
                             check=True).stdout.strip()
        want = wl.fadt_retarget(f, addr)
        assert out == ("-1" if want is None else want.hex()), (f[:4], addr)


def test_efi_source_uses_expect_and_core():
    src = open(os.path.join(EFI_DIR, "dsdtguard.c"), encoding="utf-8").read()
    assert '#include "dsdt_expect.h"' in src and '#include "dsdtguard_core.h"' in src
    # 守卫本身只读: 写交给 startup.nsh 里的 mm, 写前写后都由它核
    assert "= DG_LOCK_NEW" not in src and "= DG_CSUM_NEW" not in src
    for word in ("selftest-ok", "selftest-fail", "orig", "patched", "status", "swap"):
        assert f'"{word}"' in src
    assert '#include "dsdt_tables.h"' in src and "EFI_ACPI_RECLAIM" in src and "dg_fadt_retarget" in src


# ---------------------------------------------------------------- 清单

def test_manifest_rows(tmp_path):
    w = tmp_path / "winpe"
    for rel, data in {"bootaa64.efi": b"bm", "boot/boot.sdi": b"sdi", "boot/bcd": b"pxe",
                      "sources/boot.wim": b"wim", "boot/fonts/wgl4_boot.ttf": b"f"}.items():
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_bytes(data)
    loc = tmp_path / "local"
    loc.mkdir()
    for n in wl.LOCAL_FILES:
        (loc / n).write_bytes(n.encode())
    rows = wl.build_manifest(str(w), str(loc), url_winpe="aa64/winpe/22621", url_local="aa64/winpe-local")
    by_esp = {r.esp: r for r in rows}
    assert by_esp["sources/boot.wim"].url == "aa64/winpe/22621/sources/boot.wim"
    assert by_esp["sources/boot.wim"].sha256 == hashlib.sha256(b"wim").hexdigest()
    assert by_esp["EFI/Microsoft/Boot/BCD"].url == "aa64/winpe-local/bcd"
    assert by_esp["startup.nsh"].url == "aa64/winpe-local/startup.nsh"
    assert by_esp[wl.ESP_GUARD].url == "aa64/winpe-local/dsdtguard.efi"
    assert not any(r.url.endswith("boot/bcd") for r in rows)
    text = wl.format_manifest(rows)
    assert wl.parse_manifest(text) == rows


def test_parse_manifest_rejects_escape():
    bad = "0" * 64 + "\t1\taa64/x\t../etc/passwd\n"
    with pytest.raises(ValueError):
        wl.parse_manifest(bad)
    with pytest.raises(ValueError):
        wl.parse_manifest("0" * 64 + "\t1\taa64/x\t/abs\n")


# ---------------------------------------------------------------- 板子上: 准备本地盘

class Box:
    """假板子: 假 sysfs / proc / dev, 记命令, 假 HTTP."""

    def __init__(self, tmp, serial="S123", mounted=False, rootfs="tmpfs", dsdt=None, facp_addr=0xF19F0000):
        self.root = tmp
        self.cmds, self.puts = [], []
        sysb = tmp / "sys/block/nvme0n1"
        (sysb / "device").mkdir(parents=True)
        (sysb / "device/serial").write_text(serial + "   \n")
        (sysb / "device/model").write_text("Fake NVMe\n")
        (sysb / "size").write_text(str(4 * 2048) + "\n")          # 4 MiB
        acpi = tmp / "sys/firmware/acpi/tables"
        acpi.mkdir(parents=True)
        t = fake_dsdt() if dsdt is None else dsdt
        (acpi / "DSDT").write_bytes(t)
        f = bytearray(276)
        f[0:4] = b"FACP"
        struct.pack_into("<Q", f, 140, facp_addr)
        (acpi / "FACP").write_bytes(bytes(f))
        (tmp / "proc").mkdir()
        lines = [f"none / {rootfs} rw 0 0"]
        if mounted:
            lines.append("/dev/nvme0n1p3 /media/x ext4 ro 0 0")
        (tmp / "proc/mounts").write_text("\n".join(lines) + "\n")
        (tmp / "dev").mkdir()
        disk = bytearray(4 << 20)
        disk[:8] = b"OLDMBR!!"
        disk[-8:] = b"OLDGPT!!"
        (tmp / "dev/nvme0n1").write_bytes(bytes(disk))
        (tmp / "tmp").mkdir()
        (tmp / "mnt").mkdir()
        self.files = {}

    def run(self, argv):
        self.cmds.append(list(argv))
        if argv[0] == "sgdisk" and argv[1].startswith("--backup="):
            open(argv[1].split("=", 1)[1], "wb").write(b"sgdisk-backup")
        if argv[0] == "sgdisk" and argv[1] == "-n":
            (self.root / "dev/nvme0n1p1").write_bytes(b"")
        return 0, "ok\n"

    def get(self, url):
        return self.files[url]

    def put(self, url, data):
        self.puts.append((url, data))


def make_opts(box, **kw):
    t = fake_dsdt()
    o = dict(disk="nvme0n1", serial="S123", server="http://192.0.2.1", upload="http://192.0.2.1:8081/diag/",
             yes=True, root=str(box.root), expect=expect_for(t), mac="980e24793132")
    o.update(kw)
    return o


def manifest_for(box):
    rows = []
    for esp, data in {"startup.nsh": b"nsh", "EFI/cart/dsdtguard.efi": b"g", "sources/boot.wim": b"w" * 10}.items():
        url = f"aa64/winpe-local/{esp.replace('/', '_')}"
        box.files[f"http://192.0.2.1/{url}"] = data
        rows.append(wl.Row(hashlib.sha256(data).hexdigest(), len(data), url, esp))
    box.files["http://192.0.2.1/aa64/winpe-local/files.tsv"] = wl.format_manifest(rows).encode()
    return rows


def test_prepare_happy_path_order(tmp_path):
    box = Box(tmp_path)
    manifest_for(box)
    wl.prepare(run=box.run, get=box.get, put=box.put, log=lambda *_: None, **make_opts(box))
    names = [c[0] if c[0] != "sgdisk" else f"sgdisk {c[1]}" for c in box.cmds]
    assert box.puts, "备份没上传"
    assert names.index("sgdisk --zap-all") > names.index(next(n for n in names if n.startswith("sgdisk --backup")))
    assert ["mkfs.vfat", "-F", "32", "-n", "WINPE", str(tmp_path / "dev/nvme0n1p1")] in box.cmds
    assert ["sgdisk", "-n", "1:2048:+1024M", "-t", "1:ef00", "-c", "1:WINPE-ESP", str(tmp_path / "dev/nvme0n1")] in box.cmds
    url, data = box.puts[0]
    assert url.startswith("http://192.0.2.1:8081/diag/nvme-backup-980e24793132-S123-") and url.endswith(".tar.gz")
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        names_in = t.getnames()
        head = t.extractfile([n for n in names_in if n.endswith("head-1MiB.bin")][0]).read()
        tail = t.extractfile([n for n in names_in if n.endswith("tail-1MiB.bin")][0]).read()
    assert head[:8] == b"OLDMBR!!" and len(head) == 1 << 20
    assert tail[-8:] == b"OLDGPT!!" and len(tail) == 1 << 20
    assert any(n.endswith("sgdisk.backup") for n in names_in)


def test_prepare_upload_before_zap(tmp_path):
    box = Box(tmp_path)
    manifest_for(box)
    events = []
    orig_run, orig_put = box.run, box.put
    box_run = lambda a: (events.append(" ".join(a[:2])), orig_run(a))[1]
    box_put = lambda u, d: (events.append("PUT"), orig_put(u, d))[1]
    wl.prepare(run=box_run, get=box.get, put=box_put, log=lambda *_: None, **make_opts(box))
    zap = next(i for i, e in enumerate(events) if e == "sgdisk --zap-all")
    assert events.index("PUT") < zap


def test_prepare_upload_failure_stops_before_zap(tmp_path):
    box = Box(tmp_path)
    manifest_for(box)

    def bad_put(url, data):
        raise OSError("refused")
    with pytest.raises(wl.Stop):
        wl.prepare(run=box.run, get=box.get, put=bad_put, log=lambda *_: None, **make_opts(box))
    assert not any(c[:2] == ["sgdisk", "--zap-all"] for c in box.cmds)


@pytest.mark.parametrize("box_kw,opt_kw,why", [
    ({"serial": "OTHER"}, {}, "序列号"),
    ({"mounted": True}, {}, "挂载"),
    ({"rootfs": "ext4"}, {}, "内存"),
    ({"dsdt": fake_dsdt(oem=b"OTHER ")}, {}, "DSDT"),
    ({"facp_addr": 0x1000}, {}, "DSDT"),
    ({}, {"yes": False}, "--yes"),
])
def test_prepare_refuses_before_touching_disk(tmp_path, box_kw, opt_kw, why):
    box = Box(tmp_path, **box_kw)
    manifest_for(box)
    with pytest.raises(wl.Stop) as ei:
        wl.prepare(run=box.run, get=box.get, put=box.put, log=lambda *_: None, **make_opts(box, **opt_kw))
    assert why in str(ei.value)
    assert box.cmds == [] and box.puts == []
    assert (tmp_path / "dev/nvme0n1").read_bytes()[:8] == b"OLDMBR!!"


def test_prepare_refuses_already_patched_dsdt_is_fine(tmp_path):
    """这次开机 DSDT 已经是补过的 (比如以后固件固化了): 也算认识的表, 照样能准备盘."""
    box = Box(tmp_path, dsdt=wl.patch(fake_dsdt()))
    manifest_for(box)
    wl.prepare(run=box.run, get=box.get, put=box.put, log=lambda *_: None, **make_opts(box))


def test_prepare_bad_download_hash_stops(tmp_path):
    box = Box(tmp_path)
    manifest_for(box)
    box.files["http://192.0.2.1/aa64/winpe-local/sources_boot.wim"] = b"corrupt"
    with pytest.raises(wl.Stop) as ei:
        wl.prepare(run=box.run, get=box.get, put=box.put, log=lambda *_: None, **make_opts(box))
    assert "sha256" in str(ei.value)


def test_prepare_writes_files_into_mount(tmp_path):
    box = Box(tmp_path)
    rows = manifest_for(box)
    wl.prepare(run=box.run, get=box.get, put=box.put, log=lambda *_: None, **make_opts(box))
    mnt = tmp_path / "mnt/winpe-esp"
    for r in rows:
        assert (mnt / r.esp).read_bytes() == box.files[f"http://192.0.2.1/{r.url}"]
    mounts = [c for c in box.cmds if c[0] == "mount"]
    assert mounts[0][-2:] == [str(tmp_path / "dev/nvme0n1p1"), str(mnt)]
    assert any("ro" in c for c in mounts[1:])            # 写完以只读重挂再核一遍
    assert box.cmds[-1][0] == "umount"


# ---------------------------------------------------------------- 板子上: 只更新 ESP 上变了的文件 (迭代 DSDT 变体用)

def esp_box(tmp_path, label="WINPE"):
    box = Box(tmp_path)
    (tmp_path / "dev/nvme0n1p1").write_bytes(b"")
    blkid = {"label": label}

    def run(argv):
        box.cmds.append(list(argv))
        if argv[0] == "blkid":
            return 0, blkid["label"] + "\n"
        return 0, ""
    box.run = run
    return box


def test_update_esp_rewrites_only_changed(tmp_path):
    box = esp_box(tmp_path)
    rows = manifest_for(box)
    mnt = tmp_path / "mnt/winpe-esp"
    for r in rows:                                            # 盘上已有旧版
        (mnt / r.esp).parent.mkdir(parents=True, exist_ok=True)
        (mnt / r.esp).write_bytes(box.files[f"http://192.0.2.1/{r.url}"])
    (mnt / "startup.nsh").write_bytes(b"old nsh")
    fetched = []
    get = lambda u: (fetched.append(u), box.get(u))[1]
    changed = wl.update_esp("nvme0n1", "S123", "http://192.0.2.1", root=str(tmp_path), run=box.run, get=get,
                            log=lambda *_: None)
    assert changed == ["startup.nsh"]
    assert (mnt / "startup.nsh").read_bytes() == b"nsh"
    assert not any(u.endswith("sources_boot.wim") for u in fetched)       # 没变的大文件不下
    mounts = [c for c in box.cmds if c[0] == "mount"]
    assert mounts[0][-2:] == [str(tmp_path / "dev/nvme0n1p1"), str(mnt)] and "ro" in mounts[-1]
    assert not any(c[0] in ("sgdisk", "mkfs.vfat") for c in box.cmds)


@pytest.mark.parametrize("kw,why", [({"label": "OTHER"}, "卷标"), ({}, "序列号")])
def test_update_esp_refuses(tmp_path, kw, why):
    box = esp_box(tmp_path, **kw)
    manifest_for(box)
    serial = "S123" if kw else "WRONG"
    with pytest.raises(wl.Stop) as ei:
        wl.update_esp("nvme0n1", serial, "http://192.0.2.1", root=str(tmp_path), run=box.run, get=box.get,
                      log=lambda *_: None)
    assert why in str(ei.value)
    assert not any(c[0] == "mount" for c in box.cmds)
