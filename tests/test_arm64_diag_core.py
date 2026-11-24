"""ARM64 诊断盘的备料 (arm64_diag_core.py): APKINDEX 依赖闭包、apkovl (收集脚本 + 开机自启)、.treeinfo / sha256 校验."""

import io
import os
import tarfile

import pytest

import arm64_diag_core as core
import pxeserver as ps

INDEX = """C:Q1x
P:alpine-base
V:3.24.4-r0
D:busybox openrc

P:busybox
V:1.37.0-r31
p:cmd:sh=1.37.0-r31 /bin/sh

P:openrc
V:0.63.2-r0
D:so:libcap.so.2 !openrc-legacy

P:libcap2
V:2.78-r0
p:so:libcap.so.2=2

P:pciutils
V:3.15.0-r0
D:pciutils-libs>=3.15 hwdata-pci

P:pciutils-libs
V:3.15.0-r0

P:hwdata-pci
V:0.408-r0

P:busybox-openrc
V:1.37.0-r31
i:busybox=1.37.0-r31 openrc

P:unrelated
V:1-r0
"""
COMMUNITY = """P:lshw
V:02.20-r1
D:so:libcap.so.2
"""


def tgz(text):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = text.encode()
        info = tarfile.TarInfo("APKINDEX")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_parse_apkindex():
    pk = core.parse_apkindex(INDEX)
    assert pk[0]["P"] == "alpine-base" and pk[0]["D"] == "busybox openrc"
    assert len(pk) == 9


def test_resolve_closure_with_provides_versions_and_install_if():
    got = core.resolve([("main", core.parse_apkindex(INDEX)), ("community", core.parse_apkindex(COMMUNITY))],
                       ["alpine-base", "pciutils", "lshw"])
    assert got == [("main", "alpine-base-3.24.4-r0.apk"), ("main", "busybox-1.37.0-r31.apk"),
                   ("main", "busybox-openrc-1.37.0-r31.apk"), ("main", "hwdata-pci-0.408-r0.apk"),
                   ("main", "libcap2-2.78-r0.apk"), ("community", "lshw-02.20-r1.apk"),
                   ("main", "openrc-0.63.2-r0.apk"), ("main", "pciutils-3.15.0-r0.apk"),
                   ("main", "pciutils-libs-3.15.0-r0.apk")]


def test_resolve_picks_highest_provider_priority():
    """apk 按 k: (provider_priority) 选虚拟包的提供者; 选错了板子上 apk 会去要子集仓库里没有的包."""
    idx = core.parse_apkindex("P:alpine-conf\nV:1-r0\nD:ifupdown-any\n\n"
                              "P:busybox-ifupdown\nV:1-r0\np:ifupdown-any\nk:100\n\n"
                              "P:ifupdown-ng\nV:2-r0\np:ifupdown-any\nk:900\nD:bridge\n\n"
                              "P:bridge\nV:1-r0\n")
    assert core.resolve([("main", idx)], ["alpine-conf"]) == [
        ("main", "alpine-conf-1-r0.apk"), ("main", "bridge-1-r0.apk"), ("main", "ifupdown-ng-2-r0.apk")]


def test_resolve_missing_raises():
    with pytest.raises(KeyError):
        core.resolve([("main", core.parse_apkindex(INDEX))], ["nope"])


def test_load_index_from_tgz(tmp_path):
    p = tmp_path / "APKINDEX.tar.gz"
    p.write_bytes(tgz(INDEX))
    assert core.load_index(str(p))[0]["P"] == "alpine-base"


def test_wanted_packages_follow_menu():
    assert set(ps.ARM64_DIAG_PKGS) <= set(core.wanted())
    assert {"alpine-base", "openssl"} <= set(core.wanted())
    for tool in ("acpica", "iasl", "dmidecode", "pciutils", "usbutils", "nvme-cli", "efibootmgr", "lshw", "python3"):
        assert tool in ps.ARM64_DIAG_PKGS, tool


def test_apkovl_contents(tmp_path):
    out = core.build_apkovl(str(tmp_path / "ops-diag.apkovl.tar.gz"))
    with tarfile.open(out) as t:
        m = {i.name: i for i in t.getmembers()}
        start = t.extractfile("etc/local.d/ops-diag.start").read().decode()
        script = t.extractfile("usr/local/bin/ops-diag-collect").read()
    assert "etc/.default_boot_services" in m                     # 带 apkovl 时 initramfs 照样加默认开机服务
    assert m["etc/runlevels/default/local"].issym() and m["etc/runlevels/default/local"].linkname == "/etc/init.d/local"
    assert m["etc/local.d/ops-diag.start"].mode & 0o111 and m["usr/local/bin/ops-diag-collect"].mode & 0o111
    assert "--auto" in start and "&" in start                    # 后台跑, 不挡登录
    assert script == open(core.COLLECTOR, "rb").read()
    assert all(not n.startswith(("/", "..")) and ".." not in n.split("/") for n in m)
    assert all(i.uid == 0 and i.gid == 0 for i in m.values())


def test_apkovl_is_reproducible(tmp_path):
    a = core.build_apkovl(str(tmp_path / "a.tar.gz"))
    b = core.build_apkovl(str(tmp_path / "b.tar.gz"))
    assert open(a, "rb").read() == open(b, "rb").read()


def test_treeinfo_checksum():
    text = "[checksums]\nEFI/BOOT/grubaa64.efi = sha256:abc\nimages/install.img = sha256:def\n\n[general]\narch = aarch64\n"
    assert core.treeinfo_sha256(text, "EFI/BOOT/grubaa64.efi") == "abc"
    with pytest.raises(KeyError):
        core.treeinfo_sha256(text, "EFI/BOOT/BOOTAA64.EFI")


def test_sha256_line():
    assert core.sha256_line("9d10f9  alpine-netboot-3.24.4-aarch64.tar.gz\n") == "9d10f9"


def test_verify_sha256(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    core.verify_sha256(str(p), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    with pytest.raises(ValueError):
        core.verify_sha256(str(p), "00")
