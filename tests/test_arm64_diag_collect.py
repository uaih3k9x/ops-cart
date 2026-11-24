"""ARM64 诊断盘上的收集脚本 (arm64_diag_collect.py, 跑在 Alpine 内存系统里): 命名 / 只读检查 / 收集 / 打包 / 上传."""

import datetime
import os
import tarfile
import threading

import pytest

import arm64_diag_collect as dc
import diag_upload as du

NOW = datetime.datetime(2026, 11, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def mk(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def sysfs(tmp_path):
    net = tmp_path / "net"
    mk(net, "lo/address", "00:00:00:00:00:00\n")
    mk(net, "lo/operstate", "unknown\n")
    mk(net, "eth1/address", "02:00:00:00:0b:02\n")
    mk(net, "eth1/operstate", "down\n")
    mk(net, "eth0/address", "02:00:00:00:0b:01\n")
    mk(net, "eth0/operstate", "up\n")
    dmi = tmp_path / "dmi"
    mk(dmi, "product_serial", "SN 123/45\n")
    return net, dmi


# ---- 内核参数和命名

def test_cmdline_url():
    assert dc.cmdline_url("BOOT_IMAGE=/x ip=dhcp ops_diag=http://192.0.2.1:8081/diag/ quiet") == \
        "http://192.0.2.1:8081/diag/"
    assert dc.cmdline_url("ip=dhcp quiet") is None
    assert dc.cmdline_url("ops_diag=") is None


def test_board_mac_prefers_up_interface(sysfs):
    net, _ = sysfs
    assert dc.board_mac(str(net)) == "020000000b01"


def test_board_mac_falls_back_to_first_real_interface(sysfs):
    net, _ = sysfs
    (net / "eth0" / "operstate").write_text("down\n")
    assert dc.board_mac(str(net)) == "020000000b01"
    assert dc.board_mac(str(net / "none")) is None


def test_board_serial_sanitized_and_placeholders_dropped(sysfs):
    _, dmi = sysfs
    assert dc.board_serial(str(dmi)) == "SN_123_45"
    for junk in ("To be filled by O.E.M.", "Default string", "  ", "0", "None", "Not Specified"):
        (dmi / "product_serial").write_text(junk + "\n")
        assert dc.board_serial(str(dmi)) is None, junk
    (dmi / "product_serial").unlink()
    assert dc.board_serial(str(dmi)) is None


def test_archive_name():
    assert dc.archive_name("020000000b01", "SN_1", NOW) == "ops-diag-020000000b01-sn-SN_1-20261124T120000Z.tar.gz"
    assert dc.archive_name("020000000b01", None, NOW) == "ops-diag-020000000b01-20261124T120000Z.tar.gz"
    assert dc.archive_name(None, "SN_1", NOW) == "ops-diag-sn-SN_1-20261124T120000Z.tar.gz"
    assert dc.archive_name(None, None, NOW) == "ops-diag-unknown-20261124T120000Z.tar.gz"
    assert du.safe_name(dc.archive_name("020000000b01", "x" * 40, NOW))             # 接收端认这个名字


# ---- 只写内存

def test_require_ram_ok_and_refuse(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("tmpfs / tmpfs rw 0 0\n/dev/sda1 /media/sda1 ext4 ro 0 0\n")
    dc.require_ram("/tmp/x", str(mounts))
    with pytest.raises(dc.NotInRam):
        dc.require_ram("/media/sda1/x", str(mounts))
    mounts.write_text("/dev/sda2 / ext4 rw 0 0\n")
    with pytest.raises(dc.NotInRam):
        dc.require_ram("/tmp/x", str(mounts))


# ---- 收集

class FakeRun:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, argv, cwd, out):
        self.calls.append((tuple(argv), cwd))
        if argv[0] in self.fail:
            raise FileNotFoundError(argv[0])
        if argv[:2] == ["acpidump", "-b"]:
            for t in ("dsdt", "mcfg", "spcr"):
                open(os.path.join(cwd, f"{t}.dat"), "wb").close()
        out.write(f"out of {' '.join(argv)}\n".encode())
        return 0


def test_plan_has_required_items(sysfs):
    net, _ = sysfs
    names = [s[0] for s in dc.plan(str(net), ["nvme0n1", "sda"], ["/dev/nvme0"])]
    for want in ("uname.txt", "cpuinfo.txt", "dmidecode.txt", "lspci-nnvvv.txt", "lsusb-v.txt", "lsblk.txt",
                 "dmesg.txt", "efibootmgr-v.txt", "nvme-list.txt", "nvme0-id-ctrl.txt", "smartctl-sda.txt",
                 "ethtool-eth0.txt", "ethtool-i-eth0.txt", "acpi/acpidump.txt"):
        assert want in names, want
    assert "ethtool-lo.txt" not in names
    for _, argv, _ in dc.plan(str(net), ["sda"], []):
        assert argv[0] not in ("dd", "mount", "mkfs", "fdisk", "parted", "wipefs")
        assert not any(a in ("-w", "--write", "format", "-s", "--set") for a in argv[1:]), argv


def test_collect_runs_plan_then_decompiles_acpi(tmp_path, sysfs):
    net, _ = sysfs
    work = tmp_path / "w"
    run = FakeRun(fail={"lshw"})
    manifest = dc.collect(str(work), run, plan=dc.plan(str(net), [], []), copies=[])
    assert (work / "uname.txt").read_bytes().startswith(b"out of uname -a")
    assert "lshw" in (work / "MANIFEST.txt").read_text() and "not found" in (work / "MANIFEST.txt").read_text()
    iasl = [c for c in run.calls if c[0][0] == "iasl"]
    assert sorted(c[0] for c in iasl) == [("iasl", "-d", f) for f in ("dsdt.dat", "mcfg.dat", "spcr.dat")]
    assert all(c[1] == str(work / "acpi") for c in iasl)
    assert any(m.startswith("uname.txt\t0") for m in manifest)


def test_collect_copies_sysfs_trees_with_cap(tmp_path):
    efi = tmp_path / "efi"
    mk(efi, "efivars/Boot0000-8be4df61-93ca-11d2-aa0d-00e098032b8c", "x")
    mk(efi, "fw_platform_size", "64\n")
    big = mk(efi, "big", "x" * 100)
    work = tmp_path / "w"
    dc.collect(str(work), FakeRun(), plan=[], copies=[(str(efi), "sys-firmware-efi")], max_file=50)
    assert (work / "sys-firmware-efi" / "fw_platform_size").read_text() == "64\n"
    assert (work / "sys-firmware-efi" / "efivars" / "Boot0000-8be4df61-93ca-11d2-aa0d-00e098032b8c").exists()
    assert not (work / "sys-firmware-efi" / "big").exists()
    assert "big" in (work / "MANIFEST.txt").read_text()
    assert big.read_text() == "x" * 100                           # 源头不动


# ---- 打包和上传

def test_pack(tmp_path):
    work = tmp_path / "ops-diag-x"
    mk(work, "uname.txt", "Linux\n")
    mk(work, "acpi/dsdt.dsl", "DefinitionBlock")
    out = dc.pack(str(work), str(tmp_path / "ops-diag-x.tar.gz"))
    with tarfile.open(out) as t:
        names = sorted(t.getnames())
    assert names == ["ops-diag-x", "ops-diag-x/acpi", "ops-diag-x/acpi/dsdt.dsl", "ops-diag-x/uname.txt"]


def test_upload_end_to_end(tmp_path):
    store = tmp_path / "cart"
    store.mkdir()
    srv = du.make_server("127.0.0.1", 0, str(store), max_bytes=1 << 20)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        work = tmp_path / "ops-diag-x"
        mk(work, "uname.txt", "Linux\n")
        tgz = dc.pack(str(work), str(tmp_path / "ops-diag-x.tar.gz"))
        stored = dc.upload(tgz, f"http://127.0.0.1:{srv.server_address[1]}/diag/")
        assert stored == "ops-diag-x.tar.gz"
        assert (store / stored).read_bytes() == open(tgz, "rb").read()
    finally:
        srv.shutdown()
        srv.server_close()


def test_upload_retries_then_gives_up(tmp_path):
    f = tmp_path / "a.tar.gz"
    f.write_bytes(b"\x1f\x8b")
    sleeps = []
    with pytest.raises(dc.UploadFailed):
        dc.upload(str(f), "http://127.0.0.1:9/diag/", tries=3, sleep=sleeps.append, timeout=1)
    assert len(sleeps) == 2


# ---- 入口

def test_auto_without_url_does_nothing(tmp_path):
    cmd = tmp_path / "cmdline"
    cmd.write_text("ip=dhcp\n")
    assert dc.main(["--auto", "--cmdline", str(cmd), "--out-dir", str(tmp_path / "o")]) == 0
    assert not (tmp_path / "o").exists()


def test_main_collects_packs_and_uploads(tmp_path, sysfs, monkeypatch):
    net, dmi = sysfs
    mounts = tmp_path / "mounts"
    mounts.write_text(f"tmpfs / tmpfs rw 0 0\ntmpfs {tmp_path} tmpfs rw 0 0\n")
    sent = []
    monkeypatch.setattr(dc, "upload", lambda path, url, **k: sent.append((path, url)) or os.path.basename(path))
    monkeypatch.setattr(dc, "run_cmd", FakeRun())
    rc = dc.main(["--url", "http://192.0.2.1:8081/diag/", "--out-dir", str(tmp_path / "o"), "--mounts", str(mounts),
                  "--sys-net", str(net), "--dmi", str(dmi), "--no-copies"], now=NOW)
    assert rc == 0
    name = "ops-diag-020000000b01-sn-SN_123_45-20261124T120000Z.tar.gz"
    assert sent == [(str(tmp_path / "o" / name), "http://192.0.2.1:8081/diag/")]
    assert tarfile.open(sent[0][0]).getnames()[0] == name[:-len(".tar.gz")]


def test_main_refuses_out_dir_on_disk(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda2 / ext4 rw 0 0\n")
    assert dc.main(["--no-upload", "--out-dir", str(tmp_path / "o"), "--mounts", str(mounts)]) == 3
    assert not (tmp_path / "o").exists()


def test_resend_only_uploads(tmp_path, monkeypatch):
    f = tmp_path / "ops-diag-x.tar.gz"
    f.write_bytes(b"\x1f\x8b")
    sent = []
    monkeypatch.setattr(dc, "upload", lambda path, url, **k: sent.append((path, url)) or "ops-diag-x.tar.gz")
    monkeypatch.setattr(dc, "run_cmd", None)                     # 不许收集
    assert dc.main(["--resend", str(f), "--url", "http://192.0.2.1:8081/diag/"]) == 0
    assert sent == [(str(f), "http://192.0.2.1:8081/diag/")]


# ---- 审查 (17d608b) 补的

@pytest.mark.parametrize("text", ["rootfs / rootfs rw 0 0\ntmpfs / tmpfs rw 0 0\n",
                                  "tmpfs / tmpfs rw 0 0\nrootfs / rootfs rw 0 0\n",
                                  "rootfs / rootfs rw 0 0\n"])
def test_require_ram_accepts_rootfs(tmp_path, text):
    m = tmp_path / "mounts"
    m.write_text(text)
    dc.require_ram("/tmp/x", str(m))


def test_require_ram_later_overmount_wins(tmp_path):
    m = tmp_path / "mounts"
    m.write_text("rootfs / rootfs rw 0 0\n/dev/sda2 / ext4 rw 0 0\n")
    with pytest.raises(dc.NotInRam):
        dc.require_ram("/tmp/x", str(m))


# 允许的命令 (整条 argv 用空格连起来全匹配). 加新命令先在这里加, 并确认它只读.
_READ_ONLY = [r"uname -a", r"cat /proc/[a-z]+", r"ls -la /sys/firmware", r"dmidecode", r"lspci -nnvvv", r"lspci -tvnn",
              r"lsusb -v", r"lsusb -t", r"lsblk -a -O", r"nvme list", r"nvme (id-ctrl|smart-log) /dev/nvme\d+",
              r"dmesg", r"lsmod", r"efibootmgr -v", r"ip -d addr", r"ip route", r"lshw", r"acpidump", r"acpidump -b",
              r"smartctl -a /dev/[a-z0-9]+", r"ethtool (-i )?[A-Za-z0-9._-]+"]


def test_plan_only_whitelisted_read_only_commands(sysfs):
    import re
    net, _ = sysfs
    for _, argv, _ in dc.plan(str(net), ["sda", "nvme0n1"], ["/dev/nvme0", "/dev/nvme1"]):
        line = " ".join(argv)
        assert any(re.fullmatch(p, line) for p in _READ_ONLY), line
