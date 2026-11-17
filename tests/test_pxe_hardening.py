"""#55 审查第四轮: 文件名归一 (大小写不敏感文件系统 / Windows 部署)、grub.cfg.* 与 *.tmp、arm/disarm 跨进程锁、
disarm 出错也写事件并最后抛出第一个错误."""

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
OTHER = "grub.cfg-01-02-00-00-00-00-02"
ROCKY = "linux /vmlinuz inst.ks=http://x/ks.cfg\n"
CLIENT = ("192.0.2.50", 1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = tmp_path / "pxe.env"
    p.write_text(f"PXE_IP={IP}\n")
    monkeypatch.setattr(kc, "PXE_ENV", str(p))
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    return events


@pytest.mark.parametrize("raw,want", [
    ("KS.CFG", "ks.cfg"), ("Grub.cfg-01-AA", "grub.cfg-01-aa"), ("ks.cfg.", "ks.cfg"), ("ks.cfg . ", "ks.cfg"),
    ("ks.cfg::$DATA", "ks.cfg"), ("ks.cfg:stream", "ks.cfg"), ("default", "default"),
])
def test_norm_name(raw, want):
    assert ps.norm_name(raw) == want


# ---------------------------------------------------------------- TFTP

def _tftp(root):
    return ps.TftpServer(IP, str(root), logging.getLogger("t"), pxe_mode_path=str(root / "nostate"),
                         event=lambda m: None)


@pytest.mark.parametrize("disk,req", [
    (OTHER, OTHER.upper()), (OTHER, "Grub.cfg-01-02-00-00-00-00-02"), (OTHER, OTHER + "."),
    ("grub.cfg", "GRUB.CFG"), ("EFI/BOOT/grub.cfg", "EFI/BOOT/Grub.Cfg"),
])
def test_tftp_grub_gate_ignores_case_and_trailing_dot(tmp_path, disk, req):
    """NTFS 上大写/尾点指向同一个文件: 门禁按归一后的名字判断. (测试直接放大写名的文件, 不依赖本机文件系统)"""
    (tmp_path / os.path.dirname(req)).mkdir(parents=True, exist_ok=True)
    (tmp_path / req).write_text(ROCKY)
    f, _size, tag = _tftp(tmp_path).open_file(req, CLIENT)
    assert f.read() == ps.grub_menu(IP).encode() and "未解锁" in tag


@pytest.mark.parametrize("name", ["grub.cfg.old", "grub.cfg.bak-20261025", "GRUB.CFG.OLD", "grub.cfg.tmp",
                                  "EFI/BOOT/grub.cfg.old", "vmlinuz.tmp", "ks.cfg.BAK-x"])
def test_tftp_grub_dot_and_tmp_are_not_found(tmp_path, name):
    (tmp_path / os.path.dirname(name)).mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(ROCKY)
    with pytest.raises(FileNotFoundError):
        _tftp(tmp_path).open_file(name, CLIENT)


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def http(tmp_path):
    root = tmp_path / "http"
    root.mkdir()
    srv = httpserve.make_server("127.0.0.1", 0, str(root), pxe_mode=str(tmp_path / "nostate"), event=lambda m: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/{path}", timeout=3)
            return r.status
        except urllib.error.HTTPError as e:
            return e.code
    yield root, get
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("name", ["KS.CFG", "Ks.cfg", "ks.cfg.", "ks-rocky.CFG", "ks.cfg.tmp", "ksx.cfg.tmp",
                                  "ks.cfg.BAK-x", "KS.CFG.BAK-20261025", "GRUB.CFG-01-02-00-00-00-00-02",
                                  "Grub.cfg-01-02-00-00-00-00-02", "grub.cfg.old", "GRUB.CFG.OLD", "vmlinuz.tmp",
                                  "EFI/BOOT/Grub.cfg"])
def test_http_gates_ignore_case_and_hide_tmp(http, name):
    root, get = http
    (root / os.path.dirname(name)).mkdir(parents=True, exist_ok=True)
    (root / name).write_text(ROCKY + "rootpw --iscrypted $6$x\n")
    assert get(name) in (403, 404), name


def test_http_ksh_rpm_still_served(http):
    """归一后 ks 开头但不是配置 (仓库里的 ksh 包) 照常给."""
    root, get = http
    (root / "Packages").mkdir()
    (root / "Packages" / "ksh-1.0.6-3.el9.x86_64.rpm").write_text("rpm")
    assert get("Packages/ksh-1.0.6-3.el9.x86_64.rpm") == 200


# ---------------------------------------------------------------- _scrub_tftp: grub.cfg.* 与大小写

def test_scrub_removes_install_grub_dot_files_and_uppercase_names(tmp_path, env):
    root = tmp_path / "tftp"
    (root / "EFI" / "BOOT").mkdir(parents=True)
    for n in ("grub.cfg.old", "EFI/BOOT/grub.cfg.bak-1", "GRUB.CFG-01-02-00-00-00-00-03", "EFI/BOOT/GRUB.CFG"):
        (root / n).write_text(ROCKY)
    (root / "grub.cfg.bak-plain").write_text("set timeout=5\n")
    kc.pxe_disarm(root=root, path=tmp_path / "pxe-mode.json")
    left = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert "grub.cfg.bak-plain" in left
    for n in ("grub.cfg.old", "EFI/BOOT/grub.cfg.bak-1", "GRUB.CFG-01-02-00-00-00-00-03", "EFI/BOOT/GRUB.CFG"):
        assert n not in left


# ---------------------------------------------------------------- arm / disarm 跨进程锁

def test_arm_during_disarm_waits_and_ends_consistent(tmp_path, env, monkeypatch):
    """MCP 的 disarm 和车机的 arm 是两个进程. disarm 删掉状态文件之后插进一次 arm:
    没锁时 arm 写好菜单+状态, 随后 disarm 的清理删掉菜单 -> 状态是 install 却没菜单 (不一致).
    有锁时 arm 等 disarm 做完再写, 结果一致."""
    root, state = tmp_path / "tftp", tmp_path / "pxe-mode.json"
    root.mkdir()
    real = kc._scrub_tftp
    t = {}

    def scrub(r):
        t["th"] = threading.Thread(target=kc.pxe_arm, args=(MAC,), kwargs={"root": root, "path": state})
        t["th"].start()
        t["th"].join(0.3)
        t["blocked"] = t["th"].is_alive()
        return real(r)
    monkeypatch.setattr(kc, "_scrub_tftp", scrub)
    kc.pxe_disarm(root=root, path=state)
    t["th"].join(5)
    assert t["blocked"]
    menu = root / kc.mac_cfg_name(MAC)
    assert state.exists() == menu.exists()
    assert kc.pxe_mode_read(str(state))["mode"] == "install" and menu.exists()


def test_lock_file_next_to_state(tmp_path, env):
    kc.pxe_disarm(root=tmp_path / "tftp", path=tmp_path / "pxe-mode.json")
    assert (tmp_path / "pxe-mode.json.lock").exists()


def test_disarm_proceeds_when_lock_cannot_be_opened(tmp_path, env):
    """锁文件建不了 (目录没了等) 不挡上锁: 照做并记事件. arm 则必须拿到锁."""
    state = tmp_path / "missing-dir" / "pxe-mode.json"
    kc.pxe_disarm(root=tmp_path / "tftp", path=state)
    assert "Memtest86+" in (tmp_path / "tftp" / "grub.cfg").read_text()
    assert any("锁" in e for e in env)
    with pytest.raises(OSError):
        kc.pxe_arm(MAC, root=tmp_path / "tftp", path=state)


# ---------------------------------------------------------------- disarm 出错: 事件照写, 最后抛第一个错

def test_disarm_grub_write_failure_still_runs_rest_and_raises(tmp_path, env, monkeypatch):
    root, state = tmp_path / "tftp", tmp_path / "pxe-mode.json"
    (root / "pxelinux.cfg").mkdir(parents=True)
    state.write_text("{}")
    real = kc._write_atomic
    wrote = []

    def w(p, text):
        if str(p).endswith(os.sep + "grub.cfg"):
            raise PermissionError(13, "Permission denied", str(p))
        wrote.append(str(p))
        return real(p, text)
    monkeypatch.setattr(kc, "_write_atomic", w)
    with pytest.raises(PermissionError):
        kc.pxe_disarm(root=root, path=state)
    assert not state.exists()
    assert any(p.endswith("default") for p in wrote)               # pxelinux 那步照做
    assert any("上锁" in e and "grub.cfg" in e and "失败" in e for e in env)


def test_disarm_raises_first_error(tmp_path, env, monkeypatch):
    state = tmp_path / "pxe-mode.json"
    state.write_text("{}")
    real_remove = os.remove

    def rm(p, *a, **k):
        if str(p) == str(state):
            raise PermissionError(13, "state")
        return real_remove(p, *a, **k)
    monkeypatch.setattr(kc.os, "remove", rm)
    monkeypatch.setattr(kc, "grub_cfg_rescue", lambda: (_ for _ in ()).throw(OSError(5, "grub")))
    with pytest.raises(PermissionError, match="state"):
        kc.pxe_disarm(root=tmp_path / "tftp", path=state)
    assert any("状态文件" in e for e in env)
