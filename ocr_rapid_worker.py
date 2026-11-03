#!/usr/bin/env python3
"""OCR 常驻服务 (#47): /opt/pxe/ocr-venv/bin/python -I /opt/pxe/ocr_rapid_worker.py <socket>
跑在单元 cart-ocr 里: 普通权限、断网, 常驻持有 RapidOCR (OpenVINO) 模型. 只用 venv 里的包, 不 import 仓库模块.
协议: 客户端发 4 字节长度 (大端) + 图片字节, 回一行 JSON: {"rows": [{text, x, y, w, h, conf(0-100)}]} 或 {"error": "..."}.
收字节不收路径: 截图是 root 的私有文件, 这里读不到也不该读. 一次处理一张; 单连接有超时, 卡住的客户端挡不住别人.
"""

import contextlib
import json
import os
import socket
import struct
import sys

THREADS = 4
MAX_IMAGE = 32 * 1024 * 1024
CONN_TIMEOUT = 30


def to_row(box, text, conf):
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    x, y = int(min(xs)), int(min(ys))
    return {"text": text, "x": x, "y": y, "w": int(max(xs)) - x, "h": int(max(ys)) - y,
            "conf": round(float(conf) * 100, 1)}


def _read(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ValueError("请求不完整")
        buf += chunk
    return bytes(buf)


def _answer(conn, engine):
    try:
        n = struct.unpack("!I", _read(conn, 4))[0]
        if not 0 < n <= MAX_IMAGE:
            return {"error": f"图片大小不对: {n}"}
        result, _ = engine(_read(conn, n))
        return {"rows": [to_row(*r) for r in result or []]}
    except Exception as e:              # noqa: BLE001  一张坏图不能把服务带走
        return {"error": f"{type(e).__name__}: {e}"}


def make_server(path):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX)
    old = os.umask(0o177)               # bind 出来就是 0600, 不留窗口
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    srv.listen(8)
    return srv


def serve(srv, engine, conn_timeout=CONN_TIMEOUT):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:                 # 监听口关了
            return
        with conn:
            conn.settimeout(conn_timeout)
            reply = _answer(conn, engine)
            with contextlib.suppress(OSError):
                conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")


def main(path):
    with contextlib.redirect_stdout(sys.stderr):        # 库的日志进 journald
        from rapidocr_openvino import RapidOCR
        engine = RapidOCR(inference_num_threads=THREADS)
    serve(make_server(path), engine)


if __name__ == "__main__":
    main(sys.argv[1])
