"""OCR 常驻服务 (#46 #47): 真 Unix socket + 假引擎; 服务没开 / 出错退回 Tesseract. 不依赖真的 rapidocr."""

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import stat
import struct
import tempfile
import threading
import time

import pytest

import cart_ocr as co

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOX = [[10.2, 20.0], [30.0, 20.0], [30.0, 40.9], [10.2, 40.9]]


def load_worker():
    spec = importlib.util.spec_from_file_location("ocr_worker", ROOT / "ocr_rapid_worker.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    return w


W = load_worker()


def row(text="shutdown /r /t 5", conf=92.0):
    return {"text": text, "x": 1, "y": 2, "w": 3, "h": 4, "conf": conf}


@pytest.fixture
def daemon():
    d = tempfile.mkdtemp(prefix="oc")      # macOS 上 Unix socket 路径有 104 字节上限
    path = os.path.join(d, "s")
    calls = []

    def engine(data):
        calls.append(data)
        if data == b"boom":
            raise RuntimeError("bad image")
        if data == b"empty":
            return None, None
        return [[BOX, "hi", 0.915]], [0.1]
    srv = W.make_server(path)
    threading.Thread(target=W.serve, args=(srv, engine), daemon=True).start()
    yield {"path": path, "calls": calls, "dir": d}
    srv.close()
    shutil.rmtree(d, ignore_errors=True)


def image(tmp_path, data=b"png-bytes"):
    p = tmp_path / "a.png"
    p.write_bytes(data)
    return str(p)


# ---- 服务

def test_roundtrip_sends_bytes_not_path(daemon, tmp_path):
    rows = co.ocr_rapid(image(tmp_path), sock_path=daemon["path"])
    assert rows == [{"text": "hi", "x": 10, "y": 20, "w": 20, "h": 20, "conf": 91.5}]
    assert daemon["calls"] == [b"png-bytes"]


def test_socket_is_private(daemon):
    assert stat.S_IMODE(os.stat(daemon["path"]).st_mode) == 0o600


def test_blank_screen_is_empty_result(daemon, tmp_path):
    assert co.ocr_rapid(image(tmp_path, b"empty"), sock_path=daemon["path"]) == []


def test_engine_error_is_value_error_and_service_survives(daemon, tmp_path):
    with pytest.raises(ValueError):
        co.ocr_rapid(image(tmp_path, b"boom"), sock_path=daemon["path"])
    assert co.ocr_rapid(image(tmp_path), sock_path=daemon["path"])


def _raw(path, payload):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect(path)
    s.sendall(payload)
    return s


@pytest.mark.parametrize("n", [0, W.MAX_IMAGE + 1])
def test_bad_size_refused_without_reading(daemon, n):
    s = _raw(daemon["path"], struct.pack("!I", n))
    assert "error" in json.loads(s.makefile().readline())
    assert daemon["calls"] == []


def test_truncated_request_does_not_kill_service(daemon, tmp_path):
    _raw(daemon["path"], struct.pack("!I", 100) + b"x").close()
    assert co.ocr_rapid(image(tmp_path), sock_path=daemon["path"])


def test_stalled_client_cannot_block_others(tmp_path):
    d = tempfile.mkdtemp(prefix="oc")
    path = os.path.join(d, "s")
    srv = W.make_server(path)
    threading.Thread(target=W.serve, args=(srv, lambda data: ([[BOX, "hi", 0.9]], [])),
                     kwargs={"conn_timeout": 0.2}, daemon=True).start()
    stalled = _raw(path, struct.pack("!I", 100))
    t0 = time.monotonic()
    assert co.ocr_rapid(image(tmp_path), sock_path=path)
    assert time.monotonic() - t0 < 2
    stalled.close()
    srv.close()
    shutil.rmtree(d, ignore_errors=True)


def test_make_server_replaces_stale_socket():
    d = tempfile.mkdtemp(prefix="oc")
    path = os.path.join(d, "s")
    W.make_server(path).close()
    W.make_server(path).close()
    shutil.rmtree(d, ignore_errors=True)


# ---- 客户端

@pytest.mark.parametrize("bad", [b"", b"not json\n", b"[]\n", b'{"rows": 1}\n', b'{"rows": [{"text": 1}]}\n'])
def test_bad_reply_is_value_error(tmp_path, bad):
    d = tempfile.mkdtemp(prefix="oc")
    path = os.path.join(d, "s")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def once():
        c, _ = srv.accept()
        c.recv(1 << 16)
        c.sendall(bad)
        c.close()
    threading.Thread(target=once, daemon=True).start()
    with pytest.raises(ValueError):
        co.ocr_rapid(image(tmp_path), sock_path=path)
    srv.close()
    shutil.rmtree(d, ignore_errors=True)


def test_noise_filter_drops_low_conf_and_lone_symbols():
    rows = [row("MATLAB", 99.0), row("口", 56.0), row("+", 73.0), row("器", 100.0), row("X", 80.0), row("?", 95.0)]
    assert [r["text"] for r in co.drop_noise(rows)] == ["MATLAB", "器", "X", "?"]


def test_best_prefers_service():
    assert co.ocr_best("x", "eng", rapid=lambda p: [row()], tess=lambda p, lang: 1 / 0) == ([row()], co.ENGINE)


@pytest.mark.parametrize("exc,why", [(FileNotFoundError("no sock"), "服务没开"), (ConnectionRefusedError(), "服务没开"),
                                     (socket.timeout(), "timeout"), (ValueError("bad"), "ValueError"), (OSError("x"), "OSError")])
def test_best_falls_back_and_says_why(exc, why):
    def rapid(p):
        raise exc
    lines, engine = co.ocr_best("x", "eng", rapid=rapid, tess=lambda p, lang: [row("tess")])
    assert lines[0]["text"] == "tess" and engine.startswith("tesseract") and why in engine


def test_best_empty_screen_is_a_result_not_a_failure():
    assert co.ocr_best("x", "eng", rapid=lambda p: [], tess=lambda p, lang: 1 / 0) == ([], co.ENGINE)


def test_recipes_stay_on_tesseract():
    src = (ROOT / "recipe.py").read_text(encoding="utf-8")
    assert "cart_ocr.ocr_image" in src and "ocr_best" not in src


# ---- 部署

def test_requirements_are_pinned_to_openvino():
    lines = [ln.split("#")[0].strip() for ln in (ROOT / "deploy" / "ocr-requirements.txt").read_text().splitlines()]
    lines = [ln for ln in lines if ln]
    assert all("==" in ln for ln in lines) and any(ln.startswith("rapidocr-openvino==") for ln in lines)
    assert not any(ln.startswith("onnxruntime") for ln in lines)


def test_worker_is_standalone_and_limits_threads():
    src = (ROOT / "ocr_rapid_worker.py").read_text(encoding="utf-8")
    assert "inference_num_threads" in src and "import cart_" not in src


def test_unit_is_unprivileged_and_offline():
    u = (ROOT / "deploy" / "systemd" / "cart-ocr.service").read_text(encoding="utf-8")
    for want in ("DynamicUser=yes", "PrivateNetwork=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_UNIX",
                 "ProtectSystem=strict", "NoNewPrivileges=yes", "MemoryMax=", "Restart=on-failure",
                 "RuntimeDirectory=ops-ocr", "/opt/pxe/ocr-venv/bin/python -I /opt/pxe/ocr_rapid_worker.py"):
        assert want in u, want
    assert co.RAPID_SOCK == "/run/ops-ocr/ocr.sock" and co.RAPID_SOCK in u
