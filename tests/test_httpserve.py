"""httpserve 测试: 地址没配上就等, 配上后照常出文件."""

import errno
import threading
import urllib.request

import httpserve


class FlakySock:
    """前 n 次 bind 报 EADDRNOTAVAIL (网口没插线), 之后成功."""

    def __init__(self, n):
        self.n, self.calls = n, 0

    def bind(self, addr):
        self.calls += 1
        if self.calls <= self.n:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")


def test_parse_args():
    a = httpserve.parse_args(["8000", "--directory", "/opt/pxe/http", "--bind", "192.0.2.1"])
    assert (a.port, a.directory, a.bind) == (8000, "/opt/pxe/http", "192.0.2.1")


def test_server_waits_for_address_instead_of_exiting(tmp_path):
    sleeps = []
    sock = FlakySock(3)
    srv = httpserve.make_server("192.0.2.1", 0, str(tmp_path), sleep=sleeps.append,
                                sock_factory=lambda: sock, activate=False)
    assert sock.calls == 4 and sleeps == [2, 2, 2]
    assert srv.socket is sock


def test_serves_files_after_bind(tmp_path):
    (tmp_path / "vmlinuz").write_text("hello kernel")
    srv = httpserve.make_server("127.0.0.1", 0, str(tmp_path))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/vmlinuz", timeout=3).read() == b"hello kernel"
    finally:
        srv.shutdown()
        srv.server_close()


# ---- A4: ks 只发给解锁的那台机器 (anaconda 的 inst.ks.sendmac 会带 X-RHN-Provisioning-MAC-N 头)

import json  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402

import pytest  # noqa: E402

MAC = "02:00:00:00:00:01"


def serve(tmp_path, armed=True, until=None):
    (tmp_path / "http").mkdir()
    (tmp_path / "http" / "ks.cfg").write_text("rootpw --iscrypted x\n")
    (tmp_path / "http" / "ks.cfg.bak-20261025-010101").write_text("rootpw --iscrypted old\n")
    (tmp_path / "http" / "repo.txt").write_text("repo")
    (tmp_path / "http" / "grub.cfg-01-02-00-00-00-00-02").write_text("linux /vmlinuz inst.ks=x\n")
    mode = tmp_path / "pxe-mode.json"
    if armed:
        mode.write_text(json.dumps({"mac": MAC, "since": 0,
                                    "until": until if until is not None else time.time() + 600}))
    srv = httpserve.make_server("127.0.0.1", 0, str(tmp_path / "http"), pxe_mode=str(mode),
                                event=lambda m: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path, mac=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/{path}")
    if mac:
        req.add_header("X-RHN-Provisioning-MAC-0", f"eth0 {mac}")
    try:
        return urllib.request.urlopen(req, timeout=3).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("armed,until,mac,want", [
    (True, None, MAC, 200),
    (True, None, "02:00:00:00:00:02", 403),          # 别的机器
    (True, None, None, 403),                          # 不报 MAC (浏览器/curl)
    (True, 1, MAC, 403),                              # 过期
    (False, None, MAC, 403),                          # 没解锁
])
def test_ks_gate(tmp_path, armed, until, mac, want):
    srv = serve(tmp_path, armed, until)
    try:
        assert get(srv, "ks.cfg", mac) == want
    finally:
        srv.shutdown()
        srv.server_close()


def test_backups_never_served_and_plain_files_are(tmp_path):
    srv = serve(tmp_path)
    try:
        assert get(srv, "ks.cfg.bak-20261025-010101", MAC) == 404
        assert get(srv, "repo.txt") == 200
        assert get(srv, "grub.cfg-01-02-00-00-00-00-02") == 403      # 没解锁那台的装机菜单
    finally:
        srv.shutdown()
        srv.server_close()


def test_parse_args_pxe_mode_default():
    a = httpserve.parse_args(["80"])
    assert a.pxe_mode == "/opt/pxe/pxe-mode.json"


def test_winpe_menu_gated_like_install_menu(tmp_path):
    """#11: 带 WinPE 项 (没有 ks) 的 MAC 菜单也只给解锁的那台; boot.ipxe 和 win/ 不拦."""
    srv = serve(tmp_path)
    http = tmp_path / "http"
    winpe = "menuentry 'WinPE' {\n    chainloader (http,192.0.2.1)/win/ipxe.efi\n}\n"
    (http / "grub.cfg-01-02-00-00-00-00-01").write_text(winpe)
    (http / "grub.cfg-01-02-00-00-00-00-02").write_text(winpe)
    (http / "boot.ipxe").write_text("#!ipxe\nboot\n")
    (http / "win").mkdir()
    (http / "win" / "ipxe.efi").write_bytes(b"MZ")
    try:
        assert get(srv, "grub.cfg-01-02-00-00-00-00-01") == 200
        assert get(srv, "grub.cfg-01-02-00-00-00-00-02") == 403
        assert get(srv, "boot.ipxe") == 200 and get(srv, "win/ipxe.efi") == 200
    finally:
        srv.shutdown()
        srv.server_close()
