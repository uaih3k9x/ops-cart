#!/usr/bin/env python3
"""收 ARM64 诊断包: PUT /diag/<名字>.tar.gz -> 存进 --dir, 回 201 + 存下的名字. 只绑装机口地址 (PXE_IP).

被测板子 (Alpine 内存系统里的 arm64_diag_collect.py) 收集完往这里传. 只收这一种请求:
  名字白名单 (字母数字开头, 只含字母数字 . _ -, 以 .tar.gz 结尾, 不超过 128 字符), 不带目录;
  必须带 Content-Length, 不超过 --max-mb; 内容得是 gzip (1f 8b); 剩余空间不到 1 GB 不收;
  同名不覆盖, 加 -1 -2 ...; 先写隐藏临时文件, 收全了再改名 (O_EXCL 抢名字).
GET / POST / DELETE 一律 405, 不列目录、不往外发文件 (取包在推车本机看 --dir)."""

import argparse
import http.server
import logging
import os
import re
import shutil
import sys

import pxeserver

log = logging.getLogger("diag_upload")

DIAG_DIR = "/var/lib/ops-cart-arm64-diag"
MIN_FREE = 1 << 30
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.tar\.gz")


class UploadError(Exception):
    def __init__(self, status, why):
        super().__init__(why)
        self.status = status


def safe_name(name):
    """合规返回原名, 否则 None."""
    return name if isinstance(name, str) and _NAME.fullmatch(name) else None


def _claim(directory, name):
    """抢一个不存在的名字 (O_EXCL), 返回 (名字, fd)."""
    stem = name[:-len(".tar.gz")]
    for i in range(1000):
        cand = name if i == 0 else f"{stem}-{i}.tar.gz"
        try:
            return cand, os.open(os.path.join(directory, cand), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError:
            continue
    raise UploadError(409, "同名文件太多")


def store(directory, name, stream, length, max_bytes, min_free=MIN_FREE):
    """从 stream 读 length 字节存成 directory/name (重名加后缀) -> 存下的名字. 出错抛 UploadError, 不留文件."""
    if safe_name(name) is None:
        raise UploadError(400, "文件名不合规")
    if length > max_bytes:
        raise UploadError(413, "太大")
    if shutil.disk_usage(directory).free - length < min_free:
        raise UploadError(507, "推车磁盘空间不够")
    tmp = os.path.join(directory, f".{name}.part-{os.getpid()}-{id(stream)}")
    try:
        with open(tmp, "xb") as f:
            left, head = length, b""
            while left:
                chunk = stream.read(min(left, 1 << 16))
                if not chunk:
                    raise UploadError(400, "内容不全")
                if len(head) < 2:
                    head += chunk[:2]
                f.write(chunk)
                left -= len(chunk)
        if head[:2] != b"\x1f\x8b":
            raise UploadError(415, "不是 gzip")
        final, fd = _claim(directory, name)
        os.close(fd)
        os.replace(tmp, os.path.join(directory, final))
        return final
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Handler(http.server.BaseHTTPRequestHandler):
    directory = DIAG_DIR
    max_bytes = 64 << 20
    server_version = "ops-cart-diag/1"

    def _reply(self, status, text):
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self):
        self._reply(405, "only PUT /diag/<name>.tar.gz")

    do_GET = do_HEAD = do_POST = do_DELETE = do_OPTIONS = do_PATCH = _refuse

    def do_PUT(self):
        self.close_connection = True
        parts = self.path.split("/")
        if len(parts) != 3 or parts[0] != "" or parts[1] != "diag" or safe_name(parts[2]) is None:
            return self._reply(400, "bad path")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, "Content-Length required")
        try:
            final = store(self.directory, parts[2], self.rfile, length, self.max_bytes)
        except UploadError as e:
            log.warning("诊断包 %s from %s 不收: %s", parts[2], self.client_address[0], e)
            if e.status == 413 and length <= 2 * self.max_bytes:     # 读掉 (不存), 客户端才收得到 413 而不是断连
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    left -= len(chunk)
            return self._reply(e.status, str(e))
        log.info("诊断包收下 %s (%d bytes) from %s", final, length, self.client_address[0])
        self._reply(201, final)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)


def make_server(bind, port, directory, max_bytes=64 << 20):
    handler = type("BoundHandler", (Handler,), {"directory": directory, "max_bytes": max_bytes})
    return http.server.ThreadingHTTPServer((bind, port), handler)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bind", required=True, help="装机口地址 (PXE_IP)")
    p.add_argument("--port", type=int, default=pxeserver.DIAG_UPLOAD_PORT)
    p.add_argument("--dir", default=DIAG_DIR)
    p.add_argument("--max-mb", type=int, default=64)
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    os.makedirs(a.dir, exist_ok=True)
    srv = make_server(a.bind, a.port, a.dir, a.max_mb << 20)
    log.info("诊断包接收 %s:%d -> %s", a.bind, a.port, a.dir)
    srv.serve_forever()


if __name__ == "__main__":
    main()
