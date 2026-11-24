"""PassMark MemTest86 Site Edition (要自备, 商业授权, 文件一律不进仓库): GRUB 菜单里的手动项, x86 和 ARM64 都有, 永远不是默认项."""

import pathlib
import subprocess

import pytest

import pxeserver as ps

IP = "192.0.2.1"
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("menu,efi", [(ps.grub_menu(IP), "mt86site/BOOTX64.efi"),
                                      (ps.grub_menu_arm64(IP), "mt86site/BOOTAA64.efi")])
def test_menu_has_manual_mt86_site_entry(menu, efi):
    assert f"menuentry '{ps.MT86_SITE_TITLE}' {{\n    chainloader /{efi}\n}}\n" in menu
    assert menu.startswith("set default=0\n")
    assert menu.split("menuentry ")[1].startswith(f"'{ps.LOCAL_TITLE}'")
    assert not ps.rescue_default_menu(menu) and not ps.unlock_only_menu(menu)


@pytest.mark.parametrize("menu", [ps.grub_menu(IP), ps.grub_menu_arm64(IP)])
def test_mt86_site_default_is_refused(menu):
    """手写成默认项的菜单, TFTP/HTTP 门禁当救援默认菜单拦下 (_manual_only 认 mt86site)."""
    entries = menu.split("menuentry ")[1:]
    idx = [i for i, e in enumerate(entries) if ps.MT86_SITE_DIR in e][0]
    assert ps.rescue_default_menu(menu.replace("set default=0", f"set default={idx}", 1))


def test_install_menu_has_no_mt86_site():
    assert ps.MT86_SITE_DIR not in ps.grub_menu(IP, install=True)


def test_no_licensed_binaries_tracked():
    """商业授权: 任何 MemTest86 Site 的文件 (和别的 EFI 二进制) 都不许进仓库."""
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("不是 git 仓库")
    bad = [f for f in files if f.lower().endswith((".efi", ".spd")) or pathlib.PurePosixPath(f).name.lower() in
           ("unifont.bin", "mt86.cfg", "blacklist.cfg", "customtests.cfg", "mt86.png", "mt86head.htm", "mt86foot.htm")]
    assert bad == []
