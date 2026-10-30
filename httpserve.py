#!/usr/bin/env python3
"""静态文件 HTTP 服务, 替代 `python3 -m http.server`: 地址还没配上 (LAN1 没插线) 就每 2 秒重试,
不退出也不被 systemd 反复拉起刷日志. 绑定逻辑复用 pxeserver.bind_retry.

    httpserve.py 8000 --directory /opt/pxe/http --bind 192.0.2.1

装机兜底 (车机界面不在时也不会误装):
- ks*.cfg 只发给 pxe-mode.json 里解锁、没过期的那台机器. 认机器靠 anaconda 的 inst.ks.sendmac
  带来的 X-RHN-Provisioning-MAC-N 头 (装机菜单里有); 不报 MAC 的 (浏览器、curl) 一律 403
- 带 inst.ks= 或 WinPE 项的 grub.cfg* (pxeserver.unlock_only_menu) 同样只给文件名里那台 MAC;
  boot.ipxe 和 win/ 不拦 (PE 不动硬盘, 而且菜单被取走就上锁, 之后还得取得到)
- *.bak* 备份 (ks_write 轮换出来的, 里面有旧 root 密码哈希) 永远 404

这是防"误装"的核对, 不是访问控制: MAC 头 (和 TFTP 的文件名一样) 是客户端自己报的, LAN1 上能抓到
DHCP 的机器可以冒充. 真正的边界是物理的 —— LAN1 只接一台被测机、守卫发现别的 DHCP 就停服、
解锁要在车机屏幕上输码且 30 分钟过期. ks 里的 root 哈希按"LAN1 上的人看得到"来对待.
"""

import argparse
import functools
import http.server
import logging
import os
import socket
import sys
import time

from pxeserver import (PXE_MODE, bind_retry, cart_event, install_allowed, menu_mac, norm_mac, pxe_mode,
                       unlock_only_menu)

log = logging.getLogger("httpserve")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("port", type=int)
    p.add_argument("--directory", default=".")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--pxe-mode", default=PXE_MODE, help="装机模式状态文件 (kiosk 写)")
    return p.parse_args(argv)


def is_ks(base):
    return base.startswith("ks") and base.endswith(".cfg")


def request_macs(headers):
    """X-RHN-Provisioning-MAC-0: eth0 02:00:00:00:00:01 -> {"02:00:00:00:00:01", ...}"""
    macs = set()
    for k, v in headers.items():
        if k.lower().startswith("x-rhn-provisioning-mac-"):
            try:
                macs.add(norm_mac(v.split()[-1]))
            except (ValueError, IndexError):
                pass
    return macs


class GuardedHandler(http.server.SimpleHTTPRequestHandler):
    pxe_mode_path = PXE_MODE
    now = staticmethod(time.time)
    event = staticmethod(cart_event)

    def send_head(self):
        path = self.translate_path(self.path)
        base = os.path.basename(path)
        if ".bak" in base:
            self.send_error(404, "File not found")
            return None
        if is_ks(base):
            mode = pxe_mode(self.pxe_mode_path, self.now())
            macs = request_macs(self.headers)
            mac = mode.get("mac")
            if mac not in macs or not install_allowed(mac, mode):
                log.warning("拒绝 %s 取 %s: 装机模式 %s, 请求方 MAC %s", self.client_address[0], base,
                            mode["mode"], sorted(macs) or "未报")
                self.send_error(403, "Install mode is locked")
                return None
            self.event(f"ks 被取走 {base} mac={mac} from {self.client_address[0]}")
        elif base.startswith("grub.cfg") and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    armed = unlock_only_menu(f.read(64 * 1024).decode("utf-8", "replace"))
            except OSError:
                armed = False
            if armed and not install_allowed(menu_mac(base), pxe_mode(self.pxe_mode_path, self.now())):
                self.send_error(403, "Install mode is locked")
                return None
        return super().send_head()


def make_server(ip, port, directory, sleep=time.sleep, sock_factory=None, activate=True,
                pxe_mode=PXE_MODE, event=cart_event):
    """建好 ThreadingHTTPServer, 但 bind 走 bind_retry. activate=False 只给测试用 (假 socket)."""
    handler_cls = type("Handler", (GuardedHandler,), {
        "pxe_mode_path": pxe_mode, "event": staticmethod(event)})
    handler = functools.partial(handler_cls, directory=directory)
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
    srv = make_server(a.bind, a.port, a.directory, pxe_mode=a.pxe_mode)
    log.info("出文件 %s @ %s:%d", a.directory, a.bind, a.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
