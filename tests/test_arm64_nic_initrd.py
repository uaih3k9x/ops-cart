"""scripts/arm64_nic_initrd.py (#119): 飞腾 D2000 板载网卡 (ACPI FTGM0001) 驱动的附加 initrd.
级联在 Alpine initramfs-lts 后面, 内容只有: 两个新模块 (extra/), depmod 重算后变了的 modules.* 索引, /etc/modules 加载顺序.
depmod 要 Linux (docker 里用 initramfs 自带的 kmod), 这里用假的 depmod 测其余部分; 真跑见 docs/ARM64_PXE.md."""

import gzip
import importlib.util
import os

import pytest

import pxeserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("arm64_nic_initrd", os.path.join(ROOT, "scripts", "arm64_nic_initrd.py"))
ni = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ni)

K = "6.18.60-0-lts"
VM = f"{K} SMP preempt mod_unload modversions aarch64"
MD = f"usr/lib/modules/{K}"
SAME = ("phy_package", "micrel", "realtek", "marvell", "qcom-phy-lib", "at803x", "dp83867",
        "phylink", "pcs_xpcs", "stmmac", "stmmac-platform")
DIR, REG, LNK = 0o040000, 0o100000, 0o120000


def ko(name, vermagic=VM, *aliases):
    info = [f"name={name.replace('-', '_')}", f"vermagic={vermagic}", *(f"alias={a}" for a in aliases)]
    return b"\x7fELF\x02\x01\x01" + b"\0" * 32 + b"\0".join(i.encode() for i in info) + b"\0tail"


def base_entries(extra=()):
    e = [("dev", DIR | 0o755, b""), ("etc", DIR | 0o755, b""), ("init", REG | 0o755, b"#!/bin/sh\n"),
         ("lib", LNK | 0o777, b"usr/lib"), ("usr", DIR | 0o755, b""), ("usr/lib", DIR | 0o755, b""),
         ("usr/lib/modules", DIR | 0o755, b""), (MD, DIR | 0o755, b""),
         (f"{MD}/modules.dep", REG | 0o644, b"old dep\n"), (f"{MD}/modules.softdep", REG | 0o644, b"soft\n")]
    e += [(f"{MD}/kernel/drivers/net/{m}.ko", REG | 0o644, ko(m)) for m in SAME]
    return e + list(extra)


def initramfs(tmp_path, entries=None):
    p = tmp_path / "initramfs-lts"
    p.write_bytes(gzip.compress(ni.write_newc(base_entries() if entries is None else entries)))
    return str(p)


def bundle(tmp_path, ftgm_vermagic=VM, overrides=None, skip=()):
    d = tmp_path / "ftgm-bundle"
    d.mkdir()
    for m in SAME:
        if m in skip:
            continue
        (d / f"{m}.ko").write_bytes((overrides or {}).get(m, ko(m)))
    (d / "dwmac-ftgm.ko").write_bytes(ko("dwmac-ftgm", ftgm_vermagic, "acpi*:FTGM0001:*"))
    (d / "motorcomm.ko").write_bytes(ko("motorcomm", VM, "mdio:0100"))
    (d / "load.sh").write_text("#!/bin/sh\n")
    (d / "memdump").write_bytes(b"\x7fELF static tool")
    return str(d)


def fake_depmod(calls=None, alias=True):
    def run(initramfs_path, kver, extras):
        if calls is not None:
            calls.append((initramfs_path, kver, sorted(extras)))
        dep = b"".join(f"extra/{n}: \n".encode() for n in sorted(extras))
        return {"modules.dep": b"old dep\n" + dep, "modules.dep.bin": b"BIN" + dep,
                "modules.alias": b"alias acpi*:FTGM0001:* dwmac_ftgm\n" if alias else b"",
                "modules.softdep": b"soft\n"}                          # 没变的不进 cpio
    return run


def build(tmp_path, **kw):
    out = str(tmp_path / "out" / ni.OUT_NAME)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    args = dict(bundle_dir=kw.pop("bundle_dir", None) or bundle(tmp_path),
                initramfs=kw.pop("initramfs", None) or initramfs(tmp_path),
                out=out, depmod=kw.pop("depmod", None) or fake_depmod())
    ni.build(**args, **kw)
    return out


def contents(path):
    with open(path, "rb") as f:
        return ni.read_newc(gzip.decompress(f.read()))


# ---- newc

def test_newc_roundtrip_and_alignment():
    ents = [("a", DIR | 0o755, b""), ("a/b", REG | 0o644, b"xyz"), ("l", LNK | 0o777, b"a/b")]
    raw = ni.write_newc(ents)
    assert len(raw) % 4 == 0 and raw.startswith(b"070701")
    assert b"TRAILER!!!" in raw
    assert [(n, m, d) for n, m, d in ni.read_newc(raw)] == ents


def test_read_newc_handles_concatenated_archives():
    raw = ni.write_newc([("a", REG | 0o644, b"1")]) + b"\0" * 512 + ni.write_newc([("b", REG | 0o644, b"2")])
    assert [n for n, _, _ in ni.read_newc(raw)] == ["a", "b"]


def test_newc_header_root_fixed_mtime():
    raw = ni.write_newc([("x", REG | 0o644, b"")])
    f = [int(raw[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
    assert f[2] == 0 and f[3] == 0 and f[5] == ni.MTIME          # uid gid mtime


def test_modinfo():
    info = ni.modinfo(ko("dwmac-ftgm", VM, "acpi*:FTGM0001:*", "acpi*:PHYT0004:*"))
    assert info["vermagic"] == [VM] and info["name"] == ["dwmac_ftgm"]
    assert info["alias"] == ["acpi*:FTGM0001:*", "acpi*:PHYT0004:*"]


# ---- 产物内容

def test_cpio_contents_exact(tmp_path):
    out = build(tmp_path)
    ents = contents(out)
    names = [n for n, _, _ in ents]
    assert names == sorted(names)
    assert names == ["etc", "etc/modules", "usr", "usr/lib", "usr/lib/modules", MD,
                     f"{MD}/extra", f"{MD}/extra/dwmac-ftgm.ko", f"{MD}/extra/motorcomm.ko",
                     f"{MD}/modules.alias", f"{MD}/modules.dep", f"{MD}/modules.dep.bin"]
    by = {n: (m, d) for n, m, d in ents}
    for n, (m, d) in by.items():
        if m & 0o170000 == DIR:
            assert m == DIR | 0o755
        else:
            assert m == REG | 0o644, n
    assert by[f"{MD}/extra/dwmac-ftgm.ko"][1] == ko("dwmac-ftgm", VM, "acpi*:FTGM0001:*")


def test_never_touches_lib_symlink(tmp_path):
    """Alpine 3.24 initramfs 是 usr 合并的: /lib -> usr/lib. cpio 里出现 "lib" 目录项的话内核解包时会删掉符号链接换成空目录."""
    names = [n for n, _, _ in contents(build(tmp_path))]
    assert not any(n == "lib" or n.startswith("lib/") for n in names)


def test_etc_modules_load_order(tmp_path):
    """同 ftgm-glue 的 load.sh: PHY 驱动先于 MAC 胶水 (MDIO 扫描时绑具体的 PHY 驱动而不是 Generic PHY).
    Alpine init 对 /etc/modules 逐行 modprobe -q: 不放注释和空行 (sed 去注释后剩空行, 会变成不带模块名的 modprobe)."""
    by = {n: d for n, _, d in contents(build(tmp_path))}
    lines = by["etc/modules"].decode().splitlines()
    assert lines == list(ni.LOAD_ORDER)
    assert lines[-1] == "dwmac-ftgm" and lines.index("realtek") < lines.index("stmmac")
    assert all(l and not l.startswith("#") and l == l.strip() for l in lines)


def test_skips_tools_and_modules_already_in_initramfs(tmp_path):
    names = [n for n, _, _ in contents(build(tmp_path))]
    assert not any(n.endswith(("stmmac.ko", "realtek.ko", "load.sh", "memdump")) for n in names)


def test_reproducible(tmp_path):
    b = bundle(tmp_path)
    i = initramfs(tmp_path)
    a = build(tmp_path, bundle_dir=b, initramfs=i)
    first = open(a, "rb").read()
    for f in os.listdir(b):
        os.utime(os.path.join(b, f), (1, 1))
        os.chmod(os.path.join(b, f), 0o600)
    assert open(build(tmp_path, bundle_dir=b, initramfs=i), "rb").read() == first
    assert first[4:8] == b"\0\0\0\0"                              # gzip 头 mtime = 0


def test_depmod_gets_new_modules_and_kver(tmp_path):
    calls = []
    i = initramfs(tmp_path)
    build(tmp_path, initramfs=i, depmod=fake_depmod(calls))
    assert calls == [(i, K, ["dwmac-ftgm.ko", "motorcomm.ko"])]


# ---- 拒绝的情况

def test_rejects_vermagic_mismatch(tmp_path):
    with pytest.raises(ValueError, match="vermagic"):
        build(tmp_path, bundle_dir=bundle(tmp_path, ftgm_vermagic="6.18.61-0-lts SMP preempt mod_unload aarch64"))


def test_rejects_bundle_module_differing_from_initramfs(tmp_path):
    with pytest.raises(ValueError, match="stmmac"):
        build(tmp_path, bundle_dir=bundle(tmp_path, overrides={"stmmac": ko("stmmac") + b"x"}))


def test_rejects_existing_etc_modules(tmp_path):
    i = initramfs(tmp_path, base_entries([("etc/modules", REG | 0o644, b"af_packet\n")]))
    with pytest.raises(ValueError, match="etc/modules"):
        build(tmp_path, initramfs=i)


def test_rejects_dir_type_or_mode_mismatch(tmp_path):
    ents = [e if e[0] != "usr/lib" else ("usr/lib", LNK | 0o777, b"lib") for e in base_entries()]
    with pytest.raises(ValueError, match="usr/lib"):
        build(tmp_path, initramfs=initramfs(tmp_path, ents))


def test_rejects_depmod_without_ftgm_alias(tmp_path):
    with pytest.raises(ValueError, match="FTGM0001"):
        build(tmp_path, depmod=fake_depmod(alias=False))


def test_rejects_depmod_writing_unexpected_file(tmp_path):
    def dm(*a):
        return {**fake_depmod()(*a), "evil": b""}
    with pytest.raises(ValueError, match="evil"):
        build(tmp_path, depmod=dm)


def test_depmod_inputs_pass_through_only_if_unchanged(tmp_path):
    """容器里 tar modules.* 会带上 depmod 的输入 (modules.order...): 跟原来一样就不进 cpio, 变了报错."""
    order = b"kernel/drivers/net/phylink.ko\n"
    i = initramfs(tmp_path, base_entries([(f"{MD}/modules.order", REG | 0o644, order)]))
    b = bundle(tmp_path)
    names = [n for n, _, _ in contents(build(tmp_path, initramfs=i, bundle_dir=b,
                                             depmod=lambda *a: {**fake_depmod()(*a), "modules.order": order}))]
    assert f"{MD}/modules.order" not in names
    with pytest.raises(ValueError, match="modules.order"):
        build(tmp_path, initramfs=i, bundle_dir=b, depmod=lambda *a: {**fake_depmod()(*a), "modules.order": b"x"})


def test_rejects_initramfs_without_single_kver(tmp_path):
    ents = base_entries([("usr/lib/modules/6.1.0", DIR | 0o755, b"")])
    with pytest.raises(ValueError, match="内核版本"):
        build(tmp_path, initramfs=initramfs(tmp_path, ents))


def test_load_order_module_missing(tmp_path):
    ents = [e for e in base_entries() if not e[0].endswith("/realtek.ko")]
    with pytest.raises(ValueError, match="realtek"):
        build(tmp_path, initramfs=initramfs(tmp_path, ents),
              bundle_dir=bundle(tmp_path, skip=("realtek",)))


# ---- docker depmod 命令行, SOURCES.tsv

def test_docker_depmod_argv_offline_arm64(tmp_path):
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        raise SystemExit
    with pytest.raises(SystemExit):
        ni.docker_depmod(str(tmp_path / "initramfs-lts"), K, {"dwmac-ftgm.ko": b"x"}, runner=runner)
    a = seen["argv"]
    assert a[:3] == ["docker", "run", "--rm"]
    assert "--network" in a and a[a.index("--network") + 1] == "none"
    assert a[a.index("--platform") + 1] == "linux/arm64"
    assert ni.DEPMOD_IMAGE in a
    assert any(f"chroot /r /sbin/depmod {K}" in x for x in a)     # 用 initramfs 自己的 kmod 生成索引 (格式跟板子上的 modprobe 一致)


def test_upsert_sources(tmp_path):
    st = tmp_path / "staging"
    st.mkdir()
    (st / "SOURCES.tsv").write_text("path\tsize\tsha256\tsource\tverified\n"
                                    "tftp/a\t1\taa\tx\t-\n"
                                    f"tftp/{pxeserver.ARM64_NIC_INITRD}\t2\told\ty\t-\n", encoding="utf-8")
    f = st / "tftp" / pxeserver.ARM64_NIC_INITRD
    f.parent.mkdir(parents=True)
    f.write_bytes(b"abc")
    ni.upsert_sources(str(st), str(f), "src")
    rows = (st / "SOURCES.tsv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("path\t") and rows[1] == "tftp/a\t1\taa\tx\t-"
    assert rows[2] == (f"tftp/{pxeserver.ARM64_NIC_INITRD}\t3\t"
                       "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\tsrc\t-")
    assert len(rows) == 3


def test_paths_match_pxeserver():
    assert f"{ni.TFTP_DIR}/{ni.OUT_NAME}" == pxeserver.ARM64_NIC_INITRD
