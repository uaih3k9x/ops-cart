"""推车上收 ARM64 诊断包的 HTTP 接收端 (diag_upload.py): 只收 PUT /diag/<安全文件名>.tar.gz, 不覆盖, 不列目录."""

import gzip
import io
import os
import threading
import urllib.error
import urllib.request

import pytest

import diag_upload as du

GZ = gzip.compress(b"hello")


@pytest.mark.parametrize("name", ["ops-diag-02000000b001-20261124T120000Z.tar.gz", "a.tar.gz",
                                  "ops-diag-02000000b001-sn-ABC_1.2-20261124T120000Z.tar.gz"])
def test_safe_name_ok(name):
    assert du.safe_name(name) == name


@pytest.mark.parametrize("name", ["", "../x.tar.gz", "a/b.tar.gz", ".hidden.tar.gz", "x.tgz", "x.tar.gz.sh",
                                  "a b.tar.gz", "x" * 200 + ".tar.gz", "-x.tar.gz", "x.tar.gz\x00", "%2e%2e.tar.gz"])
def test_safe_name_rejects(name):
    assert du.safe_name(name) is None


def test_store_writes_and_never_overwrites(tmp_path):
    n1 = du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ), len(GZ), 1 << 20)
    n2 = du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ), len(GZ), 1 << 20)
    assert n1 == "a.tar.gz" and n2 == "a-1.tar.gz"
    assert (tmp_path / "a.tar.gz").read_bytes() == GZ and (tmp_path / "a-1.tar.gz").read_bytes() == GZ
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".")]          # 临时文件不留


def test_store_rejects_too_big_short_and_not_gzip(tmp_path):
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ), len(GZ), 4)
    assert e.value.status == 413
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ[:3]), len(GZ), 1 << 20)        # 连接中途断了
    assert e.value.status == 400
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", io.BytesIO(b"PK\x03\x04zip"), 7, 1 << 20)
    assert e.value.status == 415
    assert os.listdir(tmp_path) == []


def test_store_refuses_when_disk_low(tmp_path):
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ), len(GZ), 1 << 20, min_free=1 << 62)
    assert e.value.status == 507


@pytest.fixture
def server(tmp_path):
    srv = du.make_server("127.0.0.1", 0, str(tmp_path), max_bytes=1 << 20)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, data=None):
        req = urllib.request.Request(base + path, data=data, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    yield tmp_path, call
    srv.shutdown()
    srv.server_close()


def test_put_stores_file(server):
    root, call = server
    status, body = call("PUT", "/diag/b.tar.gz", GZ)
    assert status == 201 and body.strip() == b"b.tar.gz"
    assert (root / "b.tar.gz").read_bytes() == GZ


@pytest.mark.parametrize("path", ["/b.tar.gz", "/diag/../b.tar.gz", "/diag/sub/b.tar.gz", "/diag/b.sh", "/diag/"])
def test_put_bad_path_400(server, path):
    root, call = server
    assert call("PUT", path, GZ)[0] in (400, 404)
    assert list(root.iterdir()) == []


def test_get_and_post_not_allowed(server):
    root, call = server
    (root / "b.tar.gz").write_bytes(GZ)
    assert call("GET", "/diag/b.tar.gz")[0] == 405
    assert call("GET", "/diag/")[0] == 405
    assert call("POST", "/diag/c.tar.gz", GZ)[0] == 405
    assert call("DELETE", "/diag/b.tar.gz")[0] == 405
    assert (root / "b.tar.gz").exists()


def test_put_too_big_413(server):
    root, call = server
    assert call("PUT", "/diag/big.tar.gz", b"\x1f\x8b" + b"0" * (1 << 20))[0] == 413
    assert list(root.iterdir()) == []


def test_cli_defaults():
    a = du.parse_args(["--bind", "192.0.2.1"])
    assert a.port == 8081 and a.dir == "/var/lib/ops-cart-arm64-diag" and a.max_mb == 64


def test_unit_binds_install_address_and_is_confined():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    u = (root / "deploy" / "systemd" / "pxe-diag-upload.service").read_text()
    assert "EnvironmentFile=/etc/ops-cart/pxe.env" in u
    assert "ExecStart=/usr/bin/python3 /opt/pxe/diag_upload.py --bind ${PXE_IP}" in u
    for k in ("DynamicUser=yes", "StateDirectory=ops-cart-arm64-diag", "ProtectSystem=strict", "NoNewPrivileges=yes"):
        assert k in u, k
    assert 'port="8081" protocol="tcp"' in (root / "deploy" / "firewalld" / "pxe.xml").read_text()


# ---- 审查 (17d608b) 补的

def test_store_rejects_negative_length(tmp_path):
    """Content-Length: -1 以前会让 read(-1) 读到连接关闭, 绕过大小上限和余量检查."""
    big = io.BytesIO(GZ + b"0" * 4096)
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", big, -1, 16)
    assert e.value.status == 400 and big.tell() == 0
    assert os.listdir(tmp_path) == []


def test_store_counts_inflight_uploads_against_free_space(tmp_path):
    free = du.shutil.disk_usage(str(tmp_path)).free
    with pytest.raises(du.UploadError) as e:
        du.store(str(tmp_path), "a.tar.gz", io.BytesIO(GZ), len(GZ), 1 << 20, min_free=1, inflight=free)
    assert e.value.status == 507


def _raw_put(port, length_header, body=b""):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("PUT", "/diag/neg.tar.gz")
    c.putheader("Content-Length", length_header)
    c.endheaders()
    if body:
        c.send(body)
    r = c.getresponse()
    return r.status


def test_put_negative_content_length_rejected(tmp_path):
    root = tmp_path
    srv = du.make_server("127.0.0.1", 0, str(root), max_bytes=1 << 20)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _raw_put(srv.server_address[1], "-1", GZ + b"0" * 100) in (400, 411)
        assert _raw_put(srv.server_address[1], "abc") == 411
    finally:
        srv.shutdown()
        srv.server_close()
    assert list(root.iterdir()) == []


def test_concurrency_limit_returns_503(tmp_path):
    srv = du.make_server("127.0.0.1", 0, str(tmp_path), max_bytes=1 << 20, max_concurrent=1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert srv.RequestHandlerClass.slots.acquire(blocking=False)      # 占满
        assert _raw_put(srv.server_address[1], str(len(GZ)), GZ) == 503
        srv.RequestHandlerClass.slots.release()
        assert _raw_put(srv.server_address[1], str(len(GZ)), GZ) == 201
    finally:
        srv.shutdown()
        srv.server_close()


def test_slow_client_times_out(tmp_path):
    """只发头不发内容的连接, 超时后断开, 不留文件、不占名额."""
    import socket
    import time
    srv = du.make_server("127.0.0.1", 0, str(tmp_path), max_bytes=1 << 20, timeout=1, max_concurrent=1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
        s.sendall(b"PUT /diag/slow.tar.gz HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n\x1f\x8b")
        t0 = time.time()
        s.recv(4096)                 # 服务端超时后回错误或直接关
        assert time.time() - t0 < 8
        s.close()
        time.sleep(0.3)
        assert [p for p in os.listdir(tmp_path)] == []
        assert _raw_put(srv.server_address[1], str(len(GZ)), GZ) == 201       # 名额已还
    finally:
        srv.shutdown()
        srv.server_close()


def test_handler_default_timeout():
    assert du.Handler.timeout == 60


@pytest.mark.parametrize("bind", ["", "0.0.0.0", "::", " "])
def test_cli_refuses_wildcard_bind(bind):
    with pytest.raises(SystemExit):
        du.parse_args(["--bind", bind])


def test_unit_tightened():
    import pathlib
    u = (pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "pxe-diag-upload.service").read_text()
    lines = u.splitlines()
    for k in ("MemoryMax=256M", "CapabilityBoundingSet=", "SystemCallFilter=@system-service"):
        assert k in lines, k
    assert any(l.startswith("TasksMax=") for l in lines)
