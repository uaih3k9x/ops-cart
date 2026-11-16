"""#55 审查第二、三轮: pxelinux 路径的门禁 (白名单).
推车只生成 pxelinux.cfg/default (默认本地盘, 救援和 Memtest 手选): TFTP 发它时不读磁盘, 直接发 pxelinux_menu 生成的内容;
pxelinux.cfg/ 下别的 (按 MAC/IP、子目录、符号链接别名) 一律找不到; HTTP 对 pxelinux.cfg/ 一律 404.
上锁先删状态文件, 再清 pxelinux.cfg/, 清理失败只记事件."""

import json
import logging
import os
import threading
import time
import urllib.request

import pytest

import httpserve
import kiosk_core as kc
import pxeserver as ps

IP = "192.0.2.1"
MAC = "02:00:00:00:00:01"
CLIENT = ("192.0.2.50", 1)
ROCKY = "DEFAULT i\nLABEL i\n KERNEL vmlinuz\n APPEND initrd=initrd.img inst.ks=http://x/ks.cfg\n"
DEBIAN = "DEFAULT d\nLABEL d\n KERNEL debian/linux\n APPEND auto=true priority=critical url=http://x/preseed.cfg\n"
UBUNTU = "DEFAULT u\nLABEL u\n KERNEL ubuntu/vmlinuz\n APPEND autoinstall ds=nocloud-net;s=http://x/\n"
CHAIN = "DEFAULT p\nLABEL p\n COM32 pxechn.c32\n APPEND win/ipxe.efi\n"
# 第三轮审查列的绕过写法: 黑名单都判"不可疑"
BYPASS = {
    "include": "DEFAULT i\nINCLUDE inst.cfg\n",
    "config": "DEFAULT i\nLABEL i\n CONFIG wipe.cfg\n",
    "kernel-cfg": "DEFAULT i\nLABEL i\n KERNEL x.cfg\n",
    "preseeded-initrd": "DEFAULT d\nLABEL d\n KERNEL d-i/linux\n APPEND initrd=d-i/initrd-preseeded.gz priority=critical\n",
    "initrd-ks": "DEFAULT r\nLABEL r\n KERNEL vmlinuz\n APPEND initrd=initrd-with-ks.img inst.ks=file:/ks.cfg\n",
    "initrd-dd": "DEFAULT w\nLABEL w\n KERNEL vmlinuz\n APPEND initrd=wipe.img\n",
    "memdisk-iso": "DEFAULT w\nLABEL w\n KERNEL memdisk\n APPEND iso initrd=win-autounattend.iso\n",
    "padded": "#" * (64 * 1024 + 10) + "\n" + ROCKY,
}
ALL_BAD = dict(BYPASS, rocky=ROCKY, debian=DEBIAN, ubuntu=UBUNTU, chain=CHAIN)


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / "pxe.env"
    p.write_text(f"PXE_IP={IP}\n")
    monkeypatch.setattr(kc, "PXE_ENV", str(p))
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    return events


def _armed(path):
    path.write_text(json.dumps({"mac": MAC, "since": time.time(), "until": time.time() + 600}))


def _w(root, rel, text="LABEL i\n APPEND inst.ks=x\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---------------------------------------------------------------- 生成的菜单

def test_generated_menu_locks_command_line():
    """ALLOWOPTIONS 0: boot: 下不能给标签加参数; IMPLICIT 0: 不能敲标签以外的内核/配置文件名."""
    cfg = ps.pxelinux_menu(IP)
    assert "\nALLOWOPTIONS 0\n" in cfg and "\nIMPLICIT 0\n" in cfg
    assert "INCLUDE" not in cfg and "CONFIG" not in cfg


def test_blacklist_is_gone():
    assert not hasattr(ps, "pxelinux_suspect") and not hasattr(ps, "PXELINUX_SUSPECT")


# ---------------------------------------------------------------- TFTP

def _tftp(tmp_path, armed=False):
    mode = tmp_path / "pxe-mode.json"
    if armed:
        _armed(mode)
    events = []
    return ps.TftpServer(IP, str(tmp_path), logging.getLogger("t"), pxe_mode_path=str(mode),
                         event=events.append), events


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("kind", sorted(ALL_BAD))
def test_tftp_default_is_generated_not_read(tmp_path, armed, kind):
    _w(tmp_path, "pxelinux.cfg/default", ALL_BAD[kind])
    _w(tmp_path, "inst.cfg")
    srv, events = _tftp(tmp_path, armed)
    for alias in ("pxelinux.cfg/default", "/pxelinux.cfg/default", "PXELINUX.CFG/Default",
                  "pxelinux.cfg\\default", "pxelinux.cfg/./default", "x/../pxelinux.cfg/default"):
        f, size, tag = srv.open_file(alias, CLIENT)
        data = f.read()
        assert data == ps.pxelinux_menu(IP).encode() and size == len(data)
        assert tag == f"TFTP RRQ {alias} (推车生成)"
    assert events == []


def test_tftp_default_served_even_if_missing_on_disk(tmp_path):
    srv, _ = _tftp(tmp_path)
    f, _size, _tag = srv.open_file("pxelinux.cfg/default", CLIENT)
    assert f.read() == ps.pxelinux_menu(IP).encode()


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("name", [
    "pxelinux.cfg/01-02-00-00-00-00-01", "pxelinux.cfg/C000020A", "pxelinux.cfg/notes",
    "PXELINUX.CFG/01-02-00-00-00-00-01", "pxelinux.cfg\\01-02-00-00-00-00-01",
    "pxelinux.cfg/./01-02-00-00-00-00-01", "pxelinux.cfg//01-02-00-00-00-00-01",
    "x/../pxelinux.cfg/01-02-00-00-00-00-01",
    "pxelinux.cfg/sub/01-aa", "pxelinux.cfg/sub/deeper/default", "pxelinux.cfg/sub/default",
    "bios/pxelinux.cfg/01-02-00-00-00-00-01", "bios/pxelinux.cfg/default",
])
def test_tftp_everything_else_under_pxelinux_cfg_is_not_found(tmp_path, armed, name):
    _w(tmp_path, os.path.normpath(name.replace("\\", "/")).lower())
    srv, _ = _tftp(tmp_path, armed)
    with pytest.raises(FileNotFoundError):
        srv.open_file(name, CLIENT)


@pytest.mark.parametrize("link", ["dir", "file"])
def test_tftp_symlink_alias_into_pxelinux_cfg_is_not_found(tmp_path, link):
    _w(tmp_path, "pxelinux.cfg/01-x")
    if link == "dir":
        os.symlink(tmp_path / "pxelinux.cfg", tmp_path / "cfg")
        name = "cfg/01-x"
    else:
        os.symlink(tmp_path / "pxelinux.cfg" / "01-x", tmp_path / "alias")
        name = "alias"
    srv, _ = _tftp(tmp_path, True)
    with pytest.raises(FileNotFoundError):
        srv.open_file(name, CLIENT)


@pytest.mark.parametrize("armed", [False, True])
def test_tftp_oversized_grub_cfg_is_never_sent(tmp_path, armed):
    """grub.cfg* 超过 MENU_MAX 不能当"普通大文件"原样发: 换默认菜单 (main #70: 解锁那台也不发, pxe_arm 本来就不写超长菜单)."""
    big = "#" * (ps.TftpServer.MENU_MAX + 10) + "\n" + ROCKY
    for name in ("grub.cfg-01-02-00-00-00-00-02", "grub.cfg-01-" + MAC.replace(":", "-")):
        _w(tmp_path, name, big)
        srv, events = _tftp(tmp_path, armed)
        f, _size, tag = srv.open_file(name, CLIENT)
        assert f.read() == ps.grub_menu(IP).encode() and "菜单超长" in tag and not events


# ---------------------------------------------------------------- HTTP (pxe-http-boot 出的就是 tftp 根)

@pytest.fixture
def http(tmp_path):
    root = tmp_path / "tftp"
    root.mkdir()
    mode = tmp_path / "pxe-mode.json"
    _armed(mode)
    srv = httpserve.make_server("127.0.0.1", 0, str(root), pxe_mode=str(mode), event=lambda m: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/{path}", timeout=3)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
    yield root, get
    srv.shutdown()
    srv.server_close()


def test_http_pxelinux_cfg_always_404(http):
    root, get = http
    _w(root, "pxelinux.cfg/default", ps.pxelinux_menu(IP))
    _w(root, "pxelinux.cfg/01-02-00-00-00-00-01")
    _w(root, "pxelinux.cfg/sub/01-aa")
    _w(root, "bios/pxelinux.cfg/default")
    os.symlink(root / "pxelinux.cfg", root / "cfg")
    for p in ("pxelinux.cfg/default", "pxelinux.cfg/01-02-00-00-00-00-01", "PXELINUX.CFG/default",
              "pxelinux.cfg%2Fdefault", "pxelinux.cfg/sub/01-aa", "bios/pxelinux.cfg/default",
              "cfg/default", "pxelinux.cfg/", "pxelinux.cfg"):
        assert get(p)[0] == 404, p


def test_http_never_lists_directories(http):
    root, get = http
    _w(root, "memtest/memtest64.efi", "x")
    _w(root, "win/boot.wim", "x")
    for p in ("", "memtest/", "win/", "pxelinux.cfg/"):
        st, body = get(p)
        assert st == 404 and b"memtest64" not in body and b"boot.wim" not in body, p
    assert get("memtest/memtest64.efi") == (200, b"x")


def test_http_oversized_grub_cfg_is_always_refused(http):
    """原来只看前 64KB 却发整个文件: 装机项塞在 64KB 之后就漏过去. 超过上限一律 403 (解锁那台也是, 同 TFTP)."""
    root, get = http
    big = "#" * (64 * 1024 + 10) + "\n" + ROCKY
    _w(root, "grub.cfg-01-02-00-00-00-00-02", big)
    _w(root, "EFI/BOOT/grub.cfg", big)
    _w(root, "grub.cfg-01-" + MAC.replace(":", "-"), big)
    assert get("grub.cfg-01-02-00-00-00-00-02")[0] == 403
    assert get("EFI/BOOT/grub.cfg")[0] == 403
    assert get("grub.cfg-01-" + MAC.replace(":", "-"))[0] == 403


# ---------------------------------------------------------------- pxe_disarm

def test_disarm_removes_everything_in_pxelinux_cfg_but_default(tmp_path, env):
    root = tmp_path / "tftp"
    for name, text in ALL_BAD.items():
        _w(root, f"pxelinux.cfg/{name}", text)
    _w(root, "pxelinux.cfg/sub/deeper/01-aa")
    _w(root, "bios/pxelinux.cfg/default", ROCKY)
    os.mkfifo(root / "pxelinux.cfg" / "fifo")
    outside = _w(tmp_path, "outside/keep.cfg")
    os.symlink(outside.parent, root / "pxelinux.cfg" / "linkdir")
    os.symlink(outside, root / "pxelinux.cfg" / "linkfile")
    _w(root, "pxelinux.cfg/default", DEBIAN)
    kc.pxe_disarm(root=root, path=tmp_path / "pxe-mode.json")
    assert [p.name for p in (root / "pxelinux.cfg").iterdir()] == ["default"]
    assert not list((root / "bios" / "pxelinux.cfg").iterdir())
    assert outside.read_text()                       # 不跟随符号链接
    assert (root / "pxelinux.cfg" / "default").read_text().endswith(ps.pxelinux_menu(IP))


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_disarm_replaces_non_directory_pxelinux_cfg(tmp_path, env, kind):
    root = tmp_path / "tftp"
    root.mkdir()
    if kind == "file":
        (root / "pxelinux.cfg").write_text(ROCKY)
    else:
        _w(tmp_path, "elsewhere/default", ROCKY)
        os.symlink(tmp_path / "elsewhere", root / "pxelinux.cfg")
    kc.pxe_disarm(root=root, path=tmp_path / "pxe-mode.json")
    assert (root / "pxelinux.cfg").is_dir() and not (root / "pxelinux.cfg").is_symlink()
    assert (root / "pxelinux.cfg" / "default").read_text().endswith(ps.pxelinux_menu(IP))
    if kind == "symlink":
        assert (tmp_path / "elsewhere" / "default").read_text() == ROCKY


def test_disarm_readonly_pxelinux_cfg_still_locks(tmp_path, env):
    root = tmp_path / "tftp"
    _w(root, "pxelinux.cfg/01-aa")
    _w(root, "pxelinux.cfg/sub/01-bb")
    mac_menu = _w(root, "grub.cfg-01-" + MAC.replace(":", "-"), "linux /vmlinuz inst.ks=x\n")
    os.chmod(root / "pxelinux.cfg", 0o500)
    state = tmp_path / "pxe-mode.json"
    _armed(state)
    try:
        kc.pxe_disarm(root=root, path=state)
    finally:
        os.chmod(root / "pxelinux.cfg", 0o700)
    assert not state.exists() and not mac_menu.exists()
    assert "Memtest86+" in (root / "grub.cfg").read_text()
    assert any("删不掉" in e and "pxelinux.cfg/01-aa" in e for e in env)
    assert any("pxelinux.cfg/default" in e and "失败" in e for e in env)


def test_disarm_removes_state_before_anything_can_fail(tmp_path, env, monkeypatch):
    state = tmp_path / "pxe-mode.json"
    _armed(state)
    seen = []
    monkeypatch.setattr(kc, "_scrub_tftp", lambda root: seen.append(state.exists()) or ([], []))
    kc.pxe_disarm(root=tmp_path / "tftp", path=state)
    assert seen == [False]


# ---------------------------------------------------------------- 合 main (#70) 之后: 两边门禁叠在一起

def test_tftp_fifo_grub_cfg_with_odd_case_does_not_hang(tmp_path):
    """norm_name 认出 GRUB.CFG-01-…, 再走 main 的 S_ISREG 检查: FIFO 不打开 (会卡住), 换默认菜单."""
    os.mkfifo(tmp_path / ("GRUB.CFG-01-" + MAC.replace(":", "-")))
    srv, events = _tftp(tmp_path, True)
    f, _size, tag = srv.open_file("GRUB.CFG-01-" + MAC.replace(":", "-"), CLIENT)
    assert f.read() == ps.grub_menu(IP).encode() and "不是普通文件" in tag and not events


def test_tftp_legacy_rescue_default_with_odd_case_falls_back(tmp_path):
    from test_pxeserver import LEGACY_RESCUE
    _w(tmp_path, "EFI/BOOT/Grub.Cfg", LEGACY_RESCUE)
    srv, _ = _tftp(tmp_path)
    f, _size, tag = srv.open_file("EFI/BOOT/Grub.Cfg", CLIENT)
    assert f.read() == ps.grub_menu(IP).encode() and "默认项是救援" in tag


def test_disarm_fifo_and_legacy_rescue_with_norm_name(tmp_path, env):
    """上锁: FIFO 跳过不卡; 大小写变体的 #70 老救援菜单照删; EFI/Boot/GRUB.CFG 一律删; 救援默认的老备份不动."""
    from test_pxeserver import LEGACY_RESCUE
    root = tmp_path / "tftp"
    root.mkdir()
    os.mkfifo(root / "GRUB.CFG-C0000203")
    _w(root, "GRUB.CFG-C0000201", LEGACY_RESCUE)
    _w(root, "EFI/Boot/GRUB.CFG", ps.grub_menu(IP))
    _w(root, "grub.cfg.bak-1", LEGACY_RESCUE)
    _w(root, "grub.cfg.bak-2", ROCKY)
    kc.pxe_disarm(root=root, path=tmp_path / "m.json")
    assert (root / "GRUB.CFG-C0000203").exists()
    assert not (root / "GRUB.CFG-C0000201").exists()
    assert not (root / "EFI" / "Boot" / "GRUB.CFG").exists()
    assert (root / "grub.cfg.bak-1").exists() and not (root / "grub.cfg.bak-2").exists()
    assert not ps.rescue_default_menu((root / "grub.cfg").read_text())


def test_http_legacy_rescue_and_fifo_refused_with_odd_case(http):
    from test_pxeserver import LEGACY_RESCUE
    root, get = http
    _w(root, "Grub.Cfg-C0000201", LEGACY_RESCUE)
    os.mkfifo(root / "GRUB.CFG-C0000202")
    assert get("Grub.Cfg-C0000201")[0] == 403
    assert get("GRUB.CFG-C0000202")[0] == 403


# ---------------------------------------------------------------- #58 第二轮审查

MEMTEST_DEFAULT = ("set default=0\nset timeout=3\n\n"
                   f"menuentry 'mt' {{\n    chainloader (http,{IP})/memtest/memtest64.efi\n}}\n" + ps.LOCAL_ENTRY)


def test_memtest_default_menu_counts_as_manual_only():
    """Memtest 只能手选: 默认项是 Memtest 的手写 / 遗留菜单跟救援默认一样处理."""
    assert ps.rescue_default_menu(MEMTEST_DEFAULT)
    assert ps.rescue_default_menu(MEMTEST_DEFAULT.replace("memtest/memtest64.efi", "MEMTEST/MT86PLUS.EFI"))
    assert not ps.rescue_default_menu(ps.grub_menu(IP))           # 生成的: Memtest 是最后一项, 不是默认


def test_tftp_memtest_default_falls_back(tmp_path):
    _w(tmp_path, "grub.cfg-C0000201", MEMTEST_DEFAULT)
    srv, _ = _tftp(tmp_path)
    f, _size, tag = srv.open_file("grub.cfg-C0000201", CLIENT)
    assert f.read() == ps.grub_menu(IP).encode() and "回默认菜单" in tag


def test_http_memtest_default_refused(http):
    root, get = http
    _w(root, "grub.cfg-C0000201", MEMTEST_DEFAULT)
    assert get("grub.cfg-C0000201")[0] == 403


def test_disarm_removes_memtest_default_menu(tmp_path, env):
    root = tmp_path / "tftp"
    _w(root, "grub.cfg-C0000201", MEMTEST_DEFAULT)
    kc.pxe_disarm(root=root, path=tmp_path / "m.json")
    assert not (root / "grub.cfg-C0000201").exists()


@pytest.mark.parametrize("exc", [OSError(37, "No locks available"), BlockingIOError(11, "busy")])
def test_disarm_not_blocked_by_lock(tmp_path, env, monkeypatch, exc):
    """上锁绝不能被锁挡住: flock 抛错 (ENOLCK) 或一直拿不到 (别人占着), 都不加锁照做并记事件."""
    calls = []

    def flock(fd, op):
        calls.append(op)
        raise exc
    monkeypatch.setattr(kc.fcntl, "flock", flock)
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)
    state = tmp_path / "m.json"
    _armed(state)
    kc.pxe_disarm(root=tmp_path / "tftp", path=state)
    assert not state.exists() and (tmp_path / "tftp" / "grub.cfg").exists()
    assert calls and all(op & kc.fcntl.LOCK_NB for op in calls) and len(calls) <= kc.DISARM_LOCK_TRIES
    assert any("不加锁" in e for e in env)


def test_disarm_leaves_pxelinux_cfg_under_win_and_clonezilla(tmp_path, env):
    """docstring: clonezilla/ 和 win/ 不删. 那里的 pxelinux.cfg TFTP/HTTP 本来就不发 (白名单)."""
    root = tmp_path / "tftp"
    a = _w(root, "win/pxelinux.cfg/default", ROCKY)
    b = _w(root, "clonezilla/pxelinux.cfg/default", ROCKY)
    c = _w(root, "bios/pxelinux.cfg/default", ROCKY)
    kc.pxe_disarm(root=root, path=tmp_path / "m.json")
    assert a.exists() and b.exists() and not c.exists()
    srv, _ = _tftp(root)
    with pytest.raises(FileNotFoundError):
        srv.open_file("win/pxelinux.cfg/default", CLIENT)
