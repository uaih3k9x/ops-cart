"""公开仓库脱敏检查: scan_text 的规则 + 扫整个仓库 (git ls-files) 要求零发现."""

import os
import subprocess

import pytest

import importlib.util

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "check_public_safe", os.path.join(_HERE, "scripts", "check_public_safe.py"))
cps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cps)


def kinds(text, **kw):
    return [f[1] for f in cps.scan_text(text, **kw)]


# ------------------------------------------------------------ IPv4

@pytest.mark.parametrize("ip", ["8.8.4.4", "100.100.100.100", "100.127.1.1"])  # public-safe-ignore
def test_public_and_cgnat_ipv4_flagged(ip):
    assert kinds(f"addr {ip} here") == ["ipv4"]


def test_private_ipv4_left_to_local_list():
    assert kinds("gw 192.168.1.1 and 10.0.0.1") == []
    assert kinds("gw 192.168.1.1", deny_words=[r"re:\b192\.168\.1\."]) == ["word"]


@pytest.mark.parametrize("ip", ["192.0.2.1", "198.51.100.7", "203.0.113.200", "127.0.0.1",
                                "0.0.0.0", "255.255.255.255", "255.255.255.0"])
def test_doc_and_special_ipv4_pass(ip):
    assert kinds(f"addr {ip}/24") == []


def test_version_numbers_pass():
    assert kinds("Ver 1.06.00  pytest-8.3.4  v4.23346 1.2.3.4.5") == []


def test_inline_ignore_marker():
    assert kinds("NET = '100.64.0.0/10'   # public-safe-ignore") == []


# ------------------------------------------------------------ IPv6 / MAC / 路径

def test_global_ipv6_flagged_doc_prefix_passes():
    assert kinds("2001:250:1:2::1/64") == ["ipv6"]  # public-safe-ignore
    assert kinds("2001:db8::1/64 fe80::1 ::1 fd00::1") == []


def test_universal_mac_flagged_local_mac_passes():
    assert kinds("mac 00:1b:21:aa:bb:cc") == ["mac"]  # public-safe-ignore
    assert kinds("grub.cfg-01-00-1b-21-aa-bb-cc") == ["mac"]  # public-safe-ignore
    assert kinds("02:00:00:00:00:01 ff:ff:ff:ff:ff:ff 00:00:00:00:00:00") == []


def test_time_is_not_mac():
    assert kinds("2026-10-21 15:11:01,729") == []


def test_home_paths():
    assert kinds("/Users/alice/code/x") == ["path"]  # public-safe-ignore
    assert kinds("/home/alice/conbroker") == ["path"]  # public-safe-ignore
    assert kinds("/home/ops/conbroker /home/<user>/x") == []


# ------------------------------------------------------------ 本地词表

def test_deny_words_case_insensitive_and_regex():
    assert kinds("SSID: SomeNet", deny_words=["somenet"]) == ["word"]
    assert kinds("room 7X12 ok", deny_words=[r"re:\b7X\d+\b"]) == ["word"]
    assert kinds("nothing", deny_words=["somenet"]) == []


def test_local_list_parsing(tmp_path):
    p = tmp_path / ".public-safe.local"
    p.write_text("# 注释\n\nfoo\nre:\\bbar\\d\\b\n")
    assert cps.load_local(str(p)) == ["foo", "re:\\bbar\\d\\b"]
    assert cps.load_local(str(tmp_path / "missing")) == []


def test_scan_text_reports_line_numbers_and_every_hit():
    text = "ok 192.0.2.1\nbad 8.8.4.4 and 00:1b:21:aa:bb:cc\n/home/alice/x"  # public-safe-ignore
    assert cps.scan_text(text) == [(2, "ipv4", "8.8.4.4"), (2, "mac", "00:1b:21:aa:bb:cc"),  # public-safe-ignore
                                   (3, "path", "/home/alice")]  # public-safe-ignore


def test_inline_ignore_only_covers_its_own_line():
    text = "a 8.8.4.4  # public-safe-ignore\nb 8.8.4.4"
    assert cps.scan_text(text) == [(2, "ipv4", "8.8.4.4")]  # public-safe-ignore


def test_deny_words_from_local_file_drive_scan(tmp_path):
    p = tmp_path / ".public-safe.local"
    p.write_text("# 现场 SSID 和私网段\nSomeNet\nre:\\b192\\.168\\.200\\.\n")
    words = cps.load_local(str(p))
    assert kinds("ssid somenet / gw 192.168.200.1 / 192.168.20.1", deny_words=words) == ["word", "word"]


def test_scan_repo_checks_file_names_and_skips_vendor(tmp_path):
    (tmp_path / "grub.cfg-01-00-1b-21-aa-bb-cc").write_text("menuentry")  # public-safe-ignore
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "vendor").mkdir()
    (tmp_path / "web" / "vendor" / "lib.js").write_text("8.8.4.4")  # public-safe-ignore
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00 8.8.4.4")  # public-safe-ignore
    found = cps.scan_repo(str(tmp_path), ["grub.cfg-01-00-1b-21-aa-bb-cc", "web/vendor/lib.js", "bin"])  # public-safe-ignore
    assert found == [("grub.cfg-01-00-1b-21-aa-bb-cc", 0, "mac", "00-1b-21-aa-bb-cc")]  # public-safe-ignore


# ------------------------------------------------------------ 整个仓库

def _tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=_HERE, capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.split()


def test_repo_has_no_findings():
    files = _tracked()
    if files is None:
        pytest.skip("不是 git 工作区")
    found = cps.scan_repo(_HERE, files, cps.load_local(os.path.join(_HERE, ".public-safe.local")))
    assert found == [], "\n".join(f"{f}:{n}: {k} {s}" for f, n, k, s in found)


def test_sample_file_itself_is_scanned():
    """样例文件不整份豁免: 反例逐行标 public-safe-ignore, 其余行照扫 (免得真值混进样例没人发现)."""
    assert not any("test_public_safe" in p for p in cps.SKIP_PREFIXES)
