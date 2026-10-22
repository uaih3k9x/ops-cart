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
    a = httpserve.parse_args(["8000", "--directory", "/opt/pxe/http", "--bind", "192.168.77.1"])
    assert (a.port, a.directory, a.bind) == (8000, "/opt/pxe/http", "192.168.77.1")


def test_server_waits_for_address_instead_of_exiting(tmp_path):
    sleeps = []
    sock = FlakySock(3)
    srv = httpserve.make_server("192.168.77.1", 0, str(tmp_path), sleep=sleeps.append,
                                sock_factory=lambda: sock, activate=False)
    assert sock.calls == 4 and sleeps == [2, 2, 2]
    assert srv.socket is sock


def test_serves_files_after_bind(tmp_path):
    (tmp_path / "ks.cfg").write_text("hello ks")
    srv = httpserve.make_server("127.0.0.1", 0, str(tmp_path))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/ks.cfg", timeout=3).read() == b"hello ks"
    finally:
        srv.shutdown()
        srv.server_close()
