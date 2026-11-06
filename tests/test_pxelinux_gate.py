"""#55 审查第二轮: pxelinux 路径的门禁. 推车不生成按 MAC/IP 命名的 pxelinux 配置, 只有 default (救援).
上锁时 pxelinux.cfg/ 下 default 以外的一律删; TFTP / HTTP 对它们一律回找不到, default 被改成装机就换救援."""

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
ROCKY = "DEFAULT i\nLABEL i\n KERNEL vmlinuz\n APPEND initrd=initrd.img inst.ks=http://x/ks.cfg\n"
DEBIAN = "DEFAULT d\nLABEL d\n KERNEL debian/linux\n APPEND auto=true priority=critical url=http://x/preseed.cfg\n"
UBUNTU = "DEFAULT u\nLABEL u\n KERNEL ubuntu/vmlinuz\n APPEND autoinstall ds=nocloud-net;s=http://x/\n"
CHAIN = "DEFAULT p\nLABEL p\n COM32 pxechn.c32\n APPEND win/ipxe.efi\n"


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


# ---------------------------------------------------------------- pxe_disarm

def test_disarm_removes_every_pxelinux_file_but_default(tmp_path, env):
    d = tmp_path / "tftp" / "pxelinux.cfg"
    d.mkdir(parents=True)
    for name, text in (("01-02-00-00-00-00-01", ROCKY), ("01-02-00-00-00-00-02", DEBIAN),
                       ("C000020A", UBUNTU), ("C0", CHAIN), ("notes", "LABEL r\n KERNEL vmlinuz\n")):
        (d / name).write_text(text)
    (d / "default").write_text(DEBIAN)
    kc.pxe_disarm(root=tmp_path / "tftp", path=tmp_path / "pxe-mode.json")
    assert [p.name for p in d.iterdir()] == ["default"]
    assert (d / "default").read_text().endswith(ps.pxelinux_menu(IP))


@pytest.mark.parametrize("kind", ["file", "readonly"])
def test_disarm_pxelinux_write_failure_still_locks(tmp_path, env, kind):
    root = tmp_path / "tftp"
    root.mkdir()
    if kind == "file":
        (root / "pxelinux.cfg").write_text("x")
    else:
        (root / "pxelinux.cfg").mkdir()
        os.chmod(root / "pxelinux.cfg", 0o500)
    (root / ("grub.cfg-01-" + MAC.replace(":", "-"))).write_text("linux /vmlinuz inst.ks=x\n")
    state = tmp_path / "pxe-mode.json"
    _armed(state)
    try:
        kc.pxe_disarm(root=root, path=state)
    finally:
        if kind == "readonly":
            os.chmod(root / "pxelinux.cfg", 0o700)
    assert not state.exists()
    assert not (root / ("grub.cfg-01-" + MAC.replace(":", "-"))).exists()
    assert "Memtest86+" in (root / "grub.cfg").read_text()
    assert any("pxelinux.cfg/default" in e and "失败" in e for e in env)


# ---------------------------------------------------------------- TFTP

def _tftp(tmp_path, armed=False):
    mode = tmp_path / "pxe-mode.json"
    if armed:
        _armed(mode)
    events = []
    return ps.TftpServer(IP, str(tmp_path), logging.getLogger("t"), pxe_mode_path=str(mode),
                         event=events.append), events


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("name", ["01-02-00-00-00-00-01", "C000020A", "C0", "notes"])
def test_tftp_pxelinux_non_default_is_not_found(tmp_path, armed, name):
    (tmp_path / "pxelinux.cfg").mkdir()
    (tmp_path / "pxelinux.cfg" / name).write_text("LABEL r\n KERNEL vmlinuz\n")
    srv, _ = _tftp(tmp_path, armed)
    with pytest.raises(FileNotFoundError):
        srv.open_file(f"pxelinux.cfg/{name}", ("192.0.2.50", 1))
    for alias in (f"/pxelinux.cfg/{name}", f"PXELINUX.CFG/{name}", f"pxelinux.cfg\\{name}"):
        with pytest.raises(FileNotFoundError):       # 大小写不敏感的文件系统 (Windows 部署) 也拦得住
            srv.open_file(alias, ("192.0.2.50", 1))


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("text", [ROCKY, DEBIAN, UBUNTU, CHAIN])
def test_tftp_tampered_default_gets_rescue(tmp_path, armed, text):
    (tmp_path / "pxelinux.cfg").mkdir()
    (tmp_path / "pxelinux.cfg" / "default").write_text(text)
    srv, events = _tftp(tmp_path, armed)
    f, size, tag = srv.open_file("pxelinux.cfg/default", ("192.0.2.50", 1))
    data = f.read()
    assert data == ps.pxelinux_menu(IP).encode() and size == len(data)
    assert tag == "TFTP RRQ pxelinux.cfg/default 未解锁, 回救援菜单"
    assert events == []


def test_tftp_rescue_default_served_as_is(tmp_path, env):
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "pxe-mode.json")
    srv, _ = _tftp(tmp_path)
    f, size, tag = srv.open_file("pxelinux.cfg/default", ("192.0.2.50", 1))
    assert f.read() == (tmp_path / "pxelinux.cfg" / "default").read_bytes()
    assert tag == "TFTP RRQ pxelinux.cfg/default"


def test_cart_rescue_menu_is_not_suspect(env):
    assert not ps.pxelinux_suspect(kc.pxelinux_cfg_rescue())
    assert not ps.pxelinux_suspect(ps.pxelinux_menu(IP))
    for t in (ROCKY, DEBIAN, UBUNTU, CHAIN):
        assert ps.pxelinux_suspect(t)


# ---------------------------------------------------------------- HTTP (pxe-http-boot 出的就是 tftp 根)

def _get(srv, path):
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/{path}", timeout=3).status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_pxelinux_gate(tmp_path):
    d = tmp_path / "tftp" / "pxelinux.cfg"
    d.mkdir(parents=True)
    (d / "01-02-00-00-00-00-01").write_text("LABEL r\n KERNEL vmlinuz\n")
    (d / "default").write_text(DEBIAN)
    mode = tmp_path / "pxe-mode.json"
    _armed(mode)
    srv = httpserve.make_server("127.0.0.1", 0, str(tmp_path / "tftp"), pxe_mode=str(mode),
                                event=lambda m: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _get(srv, "pxelinux.cfg/01-02-00-00-00-00-01") == 404
        assert _get(srv, "pxelinux.cfg/default") == 403
        (d / "default").write_text(ps.pxelinux_menu(IP))
        assert _get(srv, "pxelinux.cfg/default") == 200
    finally:
        srv.shutdown()
        srv.server_close()
