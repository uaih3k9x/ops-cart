#!/usr/bin/env python3
"""静态文件 HTTP 服务, 替代 `python3 -m http.server`: 地址还没配上 (LAN1 没插线) 就每 2 秒重试,
不退出也不被 systemd 反复拉起刷日志. 绑定逻辑复用 pxeserver.bind_retry.

    httpserve.py 8000 --directory /opt/pxe/http --bind 192.168.77.1
"""

import argparse
import functools
import http.server
import logging
import socket
import sys
import time

from pxeserver import bind_retry

log = logging.getLogger("httpserve")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("port", type=int)
    p.add_argument("--directory", default=".")
    p.add_argument("--bind", default="0.0.0.0")
    return p.parse_args(argv)


def make_server(ip, port, directory, sleep=time.sleep, sock_factory=None, activate=True):
    """建好 ThreadingHTTPServer, 但 bind 走 bind_retry. activate=False 只给测试用 (假 socket)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    srv = http.server.ThreadingHTTPServer((ip, port), handler, bind_and_activate=False)
    if sock_factory:
        srv.socket = sock_factory()
    else:
        srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bind_retry(srv.socket, (ip, port), sleep=sleep, log=log)
    if activate:
        srv.server_address = srv.socket.getsockname()
        srv.server_activate()
    return srv


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    a = parse_args(sys.argv[1:] if argv is None else argv)
    srv = make_server(a.bind, a.port, a.directory)
    log.info("出文件 %s @ %s:%d", a.directory, a.bind, a.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
