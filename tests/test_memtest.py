"""#55 PXE 菜单里的 Memtest86+: UEFI 走 GRUB chainloader (HTTP), BIOS 走 pxelinux; 二进制不进仓库,
部署时按固定版本下载, zip 整包和解出的文件各校验一次 sha256, 不符就中止、什么都不写."""

import hashlib
import io
import pathlib
import re
import zipfile

import pytest

import kiosk_core as kc
import memtest_core as mt
import pxeserver as ps

ROOT = pathlib.Path(__file__).resolve().parent.parent
IP = "192.0.2.1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- 菜单

def _entries(cfg):
    return re.split(r"^\s*menuentry\b", cfg, flags=re.M)[1:]


def test_rescue_grub_menu_has_memtest_chainloader_over_http():
    cfg = ps.grub_menu(IP)
    mem = [e for e in _entries(cfg) if "Memtest86+" in e]
    assert len(mem) == 1
    assert f"chainloader (http,{IP})/{ps.MEMTEST_EFI}\n" in mem[0]
    assert ps.MEMTEST_EFI == "memtest/memtest64.efi"


def test_memtest_entry_is_last_and_not_default():
    cfg = ps.grub_menu(IP)
    assert "set default=0" in cfg
    entries = _entries(cfg)
    assert "inst.rescue" in entries[0] and "Memtest86+" in entries[-1]


def test_memtest_title_warns_secure_boot():
    assert "Secure Boot OFF" in ps.MEMTEST_TITLE
    assert ps.MEMTEST_TITLE in ps.grub_menu(IP)


def test_memtest_is_not_an_unlock_only_menu():
    """Memtest 不动硬盘, 留在人人可取的救援菜单里; 不能因此被当成解锁菜单拦掉或删掉."""
    assert not ps.unlock_only_menu(ps.grub_menu(IP))


def test_install_menu_has_no_memtest():
    """解锁装机菜单倒计时 5 秒, 不多加项."""
    assert "Memtest86+" not in ps.grub_menu(IP, install=True)


def test_pxelinux_menu_default_rescue_and_memtest_label():
    cfg = ps.pxelinux_menu(IP)
    assert re.search(r"^DEFAULT rescue$", cfg, re.M)
    rescue = cfg.split("LABEL rescue", 1)[1].split("LABEL ", 1)[0]
    assert "KERNEL vmlinuz" in rescue and "initrd=initrd.img" in rescue
    assert f"inst.stage2=http://{IP}:8000/rocky" in rescue and "inst.rescue" in rescue
    assert "inst.ks" not in cfg
    memtest = cfg.split("LABEL memtest", 1)[1]
    assert f"LINUX {ps.MEMTEST_EFI}" in memtest          # LINUX: 不按扩展名猜类型


def test_disarm_rewrites_pxelinux_default_with_current_ip(tmp_path, monkeypatch):
    """#55 审查: pxelinux.cfg/default 跟 grub.cfg 一起在上锁时重写, PXE_IP 改了不会留旧地址."""
    env = tmp_path / "pxe.env"
    monkeypatch.setattr(kc, "PXE_ENV", str(env))
    root = tmp_path / "tftp"
    env.write_text("PXE_IP=198.51.100.1\n")
    kc.pxe_disarm(root=root, path=tmp_path / "pxe-mode.json")
    env.write_text("PXE_IP=203.0.113.9\n")
    kc.pxe_disarm(root=root, path=tmp_path / "pxe-mode.json")
    text = (root / "pxelinux.cfg" / "default").read_text()
    assert ps.pxelinux_menu("203.0.113.9") in text and "198.51.100.1" not in text


def test_disarm_scrubs_unlock_menus_under_pxelinux_cfg(tmp_path, monkeypatch):
    """推车不生成按 MAC/IP 命名的 pxelinux 配置; 手工放进来带装机/WinPE/克隆的一律删, 别的不动."""
    env = tmp_path / "pxe.env"
    env.write_text("PXE_IP=198.51.100.1\n")
    monkeypatch.setattr(kc, "PXE_ENV", str(env))
    d = tmp_path / "pxelinux.cfg"
    d.mkdir()
    (d / "01-02-00-00-00-00-01").write_text("DEFAULT x\nLABEL x\n KERNEL vmlinuz\n APPEND inst.ks=http://x/ks.cfg\n")
    (d / "C0000201").write_text("LABEL c\n KERNEL clonezilla/vmlinuz\n")
    (d / "notes").write_text("LABEL r\n KERNEL vmlinuz\n APPEND inst.rescue\n")
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "pxe-mode.json")
    assert sorted(p.name for p in d.iterdir()) == ["default", "notes"]
    assert not ps.unlock_only_menu((d / "default").read_text())


def test_write_rescue_also_writes_pxelinux_default(tmp_path, monkeypatch):
    env = tmp_path / "pxe.env"
    env.write_text("PXE_IP=198.51.100.1\n")
    monkeypatch.setattr(kc, "PXE_ENV", str(env))
    assert kc.main(["--write-rescue", "--root", str(tmp_path / "tftp"),
                    "--state", str(tmp_path / "pxe-mode.json")]) == 0
    text = (tmp_path / "tftp" / "pxelinux.cfg" / "default").read_text()
    assert ps.pxelinux_menu("198.51.100.1") in text
    assert "Memtest86+" in (tmp_path / "tftp" / "grub.cfg").read_text()


# ---------------------------------------------------------------- 固定版本和校验和

def test_pinned_release_is_official_and_checksums_look_real():
    assert re.fullmatch(r"\d+\.\d+", mt.VERSION)
    assert mt.ZIP_URL == f"https://memtest.org/download/v{mt.VERSION}/mt86plus_{mt.VERSION}.binaries.zip"
    assert HEX64.match(mt.ZIP_SHA256) and HEX64.match(mt.MEMBER_SHA256)
    assert mt.MEMBER == "mt86p_" + mt.VERSION.replace(".", "") + "_x86_64"


def _zip(member, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, data)
    return buf.getvalue()


def _sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def fake_release(monkeypatch):
    payload = b"MZ fake memtest"
    blob = _zip("mt86p_x", payload)
    monkeypatch.setattr(mt, "MEMBER", "mt86p_x")
    monkeypatch.setattr(mt, "ZIP_SHA256", _sha(blob))
    monkeypatch.setattr(mt, "MEMBER_SHA256", _sha(payload))
    return blob, payload


def test_check_sha256_accepts_match_and_rejects_mismatch():
    mt.check_sha256(b"abc", _sha(b"abc"), "x")
    mt.check_sha256(b"abc", _sha(b"abc").upper(), "x")
    with pytest.raises(mt.ChecksumError):
        mt.check_sha256(b"abd", _sha(b"abc"), "x")


def test_check_sha256_rejects_placeholder_expectation():
    """没填真校验和 (占位) 时不许当成通过."""
    for bad in ("", "TODO", "0" * 63):
        with pytest.raises(mt.ChecksumError):
            mt.check_sha256(b"abc", bad, "x")


def test_extract_returns_member_when_both_checks_pass(fake_release):
    blob, payload = fake_release
    assert mt.extract(blob) == payload


def test_extract_rejects_tampered_zip(fake_release):
    blob, _ = fake_release
    with pytest.raises(mt.ChecksumError):
        mt.extract(blob + b"x")


def test_extract_rejects_tampered_member(fake_release, monkeypatch):
    monkeypatch.setattr(mt, "MEMBER_SHA256", _sha(b"other"))
    with pytest.raises(mt.ChecksumError):
        mt.extract(fake_release[0])


def test_extract_missing_member_is_checksum_error(fake_release, monkeypatch):
    monkeypatch.setattr(mt, "MEMBER", "nope")
    with pytest.raises(mt.ChecksumError):
        mt.extract(fake_release[0])


def test_install_writes_efi_under_tftp_root(fake_release, tmp_path):
    blob, payload = fake_release
    path = mt.install(blob, tmp_path)
    assert pathlib.Path(path) == tmp_path / ps.MEMTEST_EFI
    assert (tmp_path / ps.MEMTEST_EFI).read_bytes() == payload


def test_install_bad_checksum_writes_nothing_and_keeps_old(fake_release, tmp_path):
    old = tmp_path / ps.MEMTEST_EFI
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old good")
    with pytest.raises(mt.ChecksumError):
        mt.install(fake_release[0] + b"x", tmp_path)
    assert old.read_bytes() == b"old good"
    assert sorted(p.name for p in old.parent.iterdir()) == ["memtest64.efi"]


def test_main_downloads_pinned_url_and_installs(fake_release, tmp_path):
    seen = []
    rc = mt.main(["--root", str(tmp_path)], fetch=lambda url: seen.append(url) or fake_release[0])
    assert rc == 0 and seen == [mt.ZIP_URL]
    assert (tmp_path / ps.MEMTEST_EFI).read_bytes() == fake_release[1]


def test_main_from_local_zip_skips_download(fake_release, tmp_path):
    z = tmp_path / "rel.zip"
    z.write_bytes(fake_release[0])
    rc = mt.main(["--root", str(tmp_path / "t"), "--zip", str(z)],
                 fetch=lambda url: pytest.fail("不该下载"))
    assert rc == 0


def test_main_aborts_on_bad_checksum(fake_release, tmp_path, capsys):
    rc = mt.main(["--root", str(tmp_path)], fetch=lambda url: b"evil")
    assert rc != 0 and not (tmp_path / ps.MEMTEST_EFI).exists()
    assert "sha256" in capsys.readouterr().err


def test_main_zip_over_limit_aborts(fake_release, tmp_path, monkeypatch, capsys):
    z = tmp_path / "big.zip"
    z.write_bytes(fake_release[0])
    monkeypatch.setattr(mt, "MAX_BYTES", len(fake_release[0]) - 1)
    assert mt.main(["--root", str(tmp_path / "t"), "--zip", str(z)]) == 1
    assert not (tmp_path / "t").exists() and "超过" in capsys.readouterr().err


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_fetch_over_limit_raises(monkeypatch):
    monkeypatch.setattr(mt, "MAX_BYTES", 4)

    class Opener:
        def open(self, url, timeout):
            return _Resp(b"12345")
    monkeypatch.setattr(mt, "_opener", lambda: Opener())
    with pytest.raises(ValueError, match="超过"):
        mt.fetch("https://memtest.org/x.zip")


def test_fetch_returns_body_within_limit(monkeypatch):
    class Opener:
        def open(self, url, timeout):
            return _Resp(b"ok")
    monkeypatch.setattr(mt, "_opener", lambda: Opener())
    assert mt.fetch("https://memtest.org/x.zip") == b"ok"


def test_redirect_to_http_is_refused():
    import urllib.request
    h = mt.HttpsOnlyRedirect()
    req = urllib.request.Request("https://memtest.org/a.zip")
    with pytest.raises(ValueError, match="https"):
        h.redirect_request(req, None, 302, "Found", {}, "http://mirror.example/a.zip")


def test_redirect_to_https_is_followed():
    import urllib.request
    h = mt.HttpsOnlyRedirect()
    req = urllib.request.Request("https://memtest.org/a.zip")
    new = h.redirect_request(req, None, 302, "Found", {}, "https://mirror.example/a.zip")
    assert new.full_url == "https://mirror.example/a.zip"


def test_opener_uses_https_only_redirect():
    assert any(isinstance(h, mt.HttpsOnlyRedirect) for h in mt._opener().handlers)


def test_fetch_refuses_non_https():
    with pytest.raises(ValueError):
        mt.fetch("http://memtest.org/x.zip")


# ---------------------------------------------------------------- 部署脚本

def test_setup_script_calls_core_and_stops_on_error():
    sh = (ROOT / "scripts" / "setup-memtest.sh").read_text()
    assert "set -euo pipefail" in sh
    assert "python3 /opt/pxe/memtest_core.py" in sh
    assert "curl" not in sh and "wget" not in sh       # 下载和校验都在 memtest_core 里, 不两处各写一份


def test_doc_mentions_secure_boot():
    doc = (ROOT / "docs" / "MEMTEST.md").read_text()
    assert "安全启动" in doc and "没有签名" in doc and mt.VERSION in doc
    assert "MOK" in doc and "pxelinux.cfg/default" in doc
