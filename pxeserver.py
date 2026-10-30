#!/usr/bin/env python3
"""单文件 PXE 服务端: DHCP + TFTP. Windows/Linux 通用, 无第三方依赖.

用法:
  python pxeserver.py serve --ip 192.0.2.1 --prefix 24 --pool 192.0.2.10,192.0.2.20 \
      --tftp-root D:/pxe/tftp --log D:/pxe/pxeserver.log

DHCP: 按 option 93 (client system architecture) 分发 bootloader:
  0007/0009 -> shimx64.efi (UEFI x86-64),  0000 -> pxelinux.0 (BIOS)
  option 77 带 iPXE (GRUB 链式加载的 iPXE) -> http://<ip>/boot.ipxe
TFTP: 支持 blksize / tsize 协商, octet 模式, 拒绝根目录穿越.
"""

import argparse
import errno
import io
import ipaddress
import json
import logging
import os
import socket
import struct
import sys
import threading
import time

MAGIC = b"\x63\x82\x53\x63"

try:                    # 推车上和 cartlog.py 放在一起; Windows 单文件用时没有, 就不记
    import cartlog
except ImportError:     # pragma: no cover
    cartlog = None


def cart_event(msg):
    """装机相关事件进推车的 events.log ([pxe]); 写不了就算了."""
    if cartlog is not None:
        cartlog.event("pxe", msg)


# ---------------------------------------------------------------- DHCP

def build_dhcp_reply(msg, op, yiaddr, siaddr, opts, bootfile=None):
    """在收到的客户端报文基础上构造 BOOTREPLY. bootfile 同时写入 file 头字段."""
    reply = bytearray(msg[:236])  # 头部原样回: xid/flags/chaddr 一致
    if bootfile:
        reply[108:236] = (bootfile.encode() + b"\x00" * 128)[:128]
    reply[0] = op  # 2 = BOOTREPLY
    reply[1] = msg[1]
    reply[2] = msg[2]
    reply[3] = 0  # hops
    reply[16:20] = socket.inet_aton(yiaddr)  # yiaddr 分给客户端的 IP
    reply[20:24] = socket.inet_aton(siaddr)  # siaddr next-server (TFTP)
    reply[24:28] = b"\x00" * 4               # giaddr
    reply[236:240] = MAGIC
    for opt in opts:
        reply += opt
    reply += b"\xff"
    return bytes(reply)


def encode_opt(num, value):
    if isinstance(value, str):
        value = value.encode()
    elif isinstance(value, (tuple, list)):  # IP 列表
        value = b"".join(socket.inet_aton(v) for v in value)
    return bytes([num, len(value)]) + value


def parse_dhcp(msg):
    """返回 (xid, client_mac, option dict). 非 DHCP 报文返回 None."""
    if len(msg) < 240 or msg[236:240] != MAGIC:
        return None
    xid = msg[4:8]
    mac = msg[28:34]
    opts = {}
    i = 240
    while i < len(msg):
        if msg[i] == 0:  # pad
            i += 1
            continue
        if msg[i] == 255:  # end
            break
        num, ln = msg[i], msg[i + 1]
        opts[num] = msg[i + 2:i + 2 + ln]
        i += 2 + ln
    return xid, mac, opts


def arch_bootfile(opts, uefi_file, bios_file):
    """按 option 93 / 60 选 bootloader 文件名."""
    arch = opts.get(93)
    if arch is None:
        return uefi_file
    kind = struct.unpack("!H", arch)[0]
    if kind == 0:  # 0000: BIOS x86
        return bios_file
    return uefi_file  # 0007/0009: EFI x86-64


def is_ipxe(opts):
    """option 77 (user class) 带 iPXE: 是被 GRUB 链式加载起来的 iPXE 在要地址."""
    return b"iPXE" in opts.get(77, b"")


SO_BINDTODEVICE = 25   # Linux; Python 的 socket 模块不一定导出这个常量


def bind_retry(sock, addr, sleep=time.sleep, log=None, every_s=2):
    """地址还没配上 (开机时网线没插) 就等它出现; 其他错误照常抛."""
    waited = False
    while True:
        try:
            sock.bind(addr)
            if waited and log:
                log.info("%s:%d 绑定成功", *addr)
            return
        except OSError as e:
            if e.errno != errno.EADDRNOTAVAIL:
                raise
            if not waited and log:
                log.warning("%s:%d 暂不可用 (网口没插线?), 每 %ds 重试", addr[0], addr[1], every_s)
            waited = True
            sleep(every_s)


def open_dhcp_socket(sock, ip, port, iface=None, platform=sys.platform, sleep=time.sleep, log=None):
    """DISCOVER 是发往 255.255.255.255 的广播. Linux 上绑具体 IP 收不到广播, 要绑 0.0.0.0;
    但必须同时 SO_BINDTODEVICE 锁死在装机口, 否则就成了管理口上的野 DHCP.
    没给网卡名时宁可收不到广播也不绑 0.0.0.0. Windows 绑具体 IP 就收得到广播, 保持原样."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface and platform.startswith("linux"):
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode())
        sock.bind(("", port))
    else:
        bind_retry(sock, (ip, port), sleep=sleep, log=log)
    return sock


# ---------------------------------------------------------------- 守卫: 别在别人的网段上发 DHCP

GUARD_STATE = "/run/ops-cart/pxe-guard.json"


def read_carrier(iface):
    """/sys/class/net/<iface>/carrier: 1 插着; 读不到 (网口 down 时内核回 EINVAL) 当没插."""
    try:
        with open(f"/sys/class/net/{iface}/carrier") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def iface_ipv4(iface):
    """网口的主 IPv4 地址 (SIOCGIFADDR, 不起子进程); 没有/不是 Linux 返回 None.
    只看主地址: 装机口按约定只配 PXE_IP 一个地址, 配成次地址时这里会当成没有, 宁可不回包."""
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))
        finally:
            s.close()
        return socket.inet_ntoa(res[20:24])
    except (OSError, ImportError):
        return None


def default_probe():
    """netprof_core.dhcp_probe (推车上有); 拿不到就不做主动探测."""
    try:
        from netprof_core import dhcp_probe
        return dhcp_probe
    except ImportError:
        return None


class PxeGuard:
    """装机口守卫. 只有 state == "serving" 才回 DHCP/proxyDHCP:
    - 启动时和 carrier 从 0 变 1 时主动探测一次 (probe 广播 DISCOVER, 收别人的 OFFER)
    - 被动看到 server-id 不是自己的 OFFER/ACK, 或客户端 REQUEST 选了别的服务器
    - 网口上得真有 --ip
    发现别人的 DHCP 就 tripped, 进程以 EXIT_FOREIGN_DHCP 退出, 单元里 RestartPreventExitStatus 不再拉起.
    状态写 state_path (JSON), 车机和排障时看."""

    def __init__(self, ip, iface, log, probe=None, carrier=None, addr=None, state_path=GUARD_STATE,
                 event=cart_event, now=time.time):
        self.ip, self.iface, self.log = ip, iface, log
        self.probe = probe
        self.carrier = carrier or (lambda: read_carrier(iface))
        self.addr = addr or (lambda: iface_ipv4(iface))
        self.state_path, self.event, self.now = state_path, event, now
        self.state, self.detail, self.server = "starting", "", None
        self.tripped, self._link = False, False
        self._lock = threading.Lock()

    def may_reply(self):
        return self.state == "serving"

    def foreign(self, server, how):
        with self._lock:
            if self.tripped:
                return
            self.tripped, self.server = True, server
        self._set("foreign", f"{how}发现别的 DHCP 服务器 {server}")
        self.log.error("装机口 %s 上有别的 DHCP 服务器 %s (%s), 停止应答", self.iface, server, how)
        self.event(f"装机口 {self.iface} 上发现别的 DHCP 服务器 {server} ({how}), PXE 服务停止")

    def tick(self):
        if self.tripped:
            return
        if not self.iface:
            self._set("serving", "没指定网卡, 不做探测")
            return
        link = self.carrier()
        if link and not self._link:
            self._set("probing", f"{self.iface} 刚接上, 探测有没有别的 DHCP")
            if not self._run_probe():
                return                  # 探测不算数: 不记"已接上", 下一轮重探, 这期间不回包
        self._link = link
        if self.tripped:
            return
        if not link:
            self._set("no-link", f"{self.iface} 没插线")
        elif self.addr() != self.ip:
            self._set("no-ip", f"{self.iface} 上没有 {self.ip}")
        else:
            self._set("serving", "")

    def _run_probe(self):
        """-> 探测算不算数. 结果未知 (None, 或 netprof_core.ProbeResult.unknown: 探测那一刻读不到插线)
        返回 False 让下一轮重探; 没有探测函数或探测本身报错返回 True, 交给被动监听兜底."""
        if self.probe is None:
            self.log.warning("没有 dhcp_probe, 跳过主动探测 (被动监听照常)")
            return True
        try:
            found = self.probe(self.iface, timeout=4.0, ignore=(self.ip,))
        except Exception as e:      # noqa: BLE001  探测坏了不拖垮服务, 被动监听兜底
            self.log.warning("主动探测失败: %s (被动监听照常)", e)
            return True
        if found is None or getattr(found, "unknown", False):
            self.log.warning("主动探测结果未知, 下一轮重探")
            return False
        others = [o for o in found if o.get("server") != self.ip]
        if others:
            self.foreign(others[0].get("server"), "主动探测")
        return True

    def _set(self, state, detail):
        if self.tripped and state != "foreign":     # 别的线程刚判了 foreign, 不许被 tick 改回去
            return
        if (state, detail) == (self.state, self.detail):
            return
        self.state, self.detail = state, detail
        self.log.info("守卫状态 %s %s", state, detail)
        self._write()

    def _write(self):
        doc = {"state": self.state, "detail": self.detail, "server": self.server,
               "iface": self.iface, "ip": self.ip, "since": self.now(), "pid": os.getpid()}
        tmp = f"{self.state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(doc, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log.warning("守卫状态写不了 %s: %s", self.state_path, e)


def foreign_server(msg, my_ip):
    """别人的 OFFER/ACK (BOOTREPLY, server-id 不是 my_ip) -> 它的地址; 否则 None."""
    parsed = parse_dhcp(msg)
    if not parsed or msg[0] != 2:
        return None
    opts = parsed[2]
    sid = opts.get(54, b"")
    if opts.get(53, b"")[0:1] not in (b"\x02", b"\x05") or len(sid) != 4:
        return None
    sid = socket.inet_ntoa(sid)
    return None if sid == my_ip else sid



EXIT_THREAD_DIED = 1        # 工作线程死了: 交给 systemd 重启
EXIT_FOREIGN_DHCP = 3       # 装机口上有别人的 DHCP: 不许重启 (RestartPreventExitStatus)
EXIT_CONFIG = 4             # 参数不对: 重启也没用


def serve_loop(sock, handler, log, what, sleep=time.sleep):
    """收包 -> handler(sock, msg, addr). 收、处理、回包整体兜住, 一个坏包/一次网卡抖动不让线程静默死掉."""
    while True:
        try:
            msg, addr = sock.recvfrom(2048)
            handler(sock, msg, addr)
        except Exception:
            log.exception("%s 收发/处理异常, 继续", what)
            sleep(0.2)          # 套接字持续出错时别空转


def supervise(threads, log, guard=None, sleep=time.sleep, every_s=2):
    """主线程: 每 every_s 秒看一眼. 工作线程死了 -> EXIT_THREAD_DIED; 守卫判定有别人的 DHCP -> EXIT_FOREIGN_DHCP."""
    while True:
        if guard is not None:
            guard.tick()
            if guard.tripped:
                log.error("装机口上发现别的 DHCP 服务器, 停止 PXE 服务: %s", guard.detail)
                return EXIT_FOREIGN_DHCP
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.error("工作线程退出: %s, 整个进程退出交给 systemd 重启", ", ".join(dead))
            return EXIT_THREAD_DIED
        sleep(every_s)


class DhcpServer:
    LEASE_S = 3600          # ACK 给的租期; 到期不续就回收
    OFFER_HOLD_S = 60       # 只 DISCOVER 不 REQUEST 的机器, 地址先替它留这么久
    DECLINE_HOLD_S = 600    # 客户端 DECLINE (ARP 发现有人占着) 的地址隔离这么久

    def __init__(self, ip, pool_start, pool_end, uefi_file, bios_file, log, port=67, iface=None,
                 prefix=24):
        self.ip = ip
        self.iface = iface
        net = ipaddress.ip_interface(f"{ip}/{prefix}").network
        lo, hi = ipaddress.ip_address(pool_start), ipaddress.ip_address(pool_end)
        if lo not in net or hi not in net or lo > hi:
            raise ValueError(f"地址池 {pool_start}-{pool_end} 必须在 {net} 内且起 <= 止")
        if lo <= ipaddress.ip_address(ip) <= hi:
            raise ValueError("地址池不能包含推车自己的地址")
        self.pool = [str(ipaddress.ip_address(i)) for i in range(int(lo), int(hi) + 1)]
        self.netmask, self.broadcast = str(net.netmask), str(net.broadcast_address)
        self.port = port
        self.uefi_file, self.bios_file = uefi_file, bios_file
        self.ipxe_script = f"http://{ip}/{IPXE_SCRIPT}"
        self.log = log
        self.leases = {}        # mac(bytes) -> [ip, 到期时间]
        self.declined = {}      # ip -> 隔离到什么时候
        self._lock = threading.Lock()
        self.guard = None       # PxeGuard; None = 不设防 (测试/Windows)

    def _quiet(self):
        return self.guard is not None and not self.guard.may_reply()

    def watch_reply(self, msg):
        """收到的 BOOTREPLY: 别人的 OFFER/ACK 就报给守卫."""
        sid = foreign_server(msg, self.ip)
        if sid and self.guard is not None:
            self.guard.foreign(sid, "被动监听")

    # ---- 租约表

    def _holder(self, ip, now):
        """ip 现在被谁占着 (租约没过期); 没人占返回 None."""
        for mac, (lip, until) in self.leases.items():
            if lip == ip and until > now:
                return mac
        return None

    def _usable(self, ip, mac, now):
        return self.declined.get(ip, 0) <= now and self._holder(ip, now) in (None, mac)

    def offer_ip(self, mac, now=None):
        now = time.time() if now is None else now
        lease = self.leases.get(mac)
        if lease and self._usable(lease[0], mac, now):
            lease[1] = max(lease[1], now + self.OFFER_HOLD_S)
            return lease[0]
        for ip in self.pool:
            if self._usable(ip, mac, now):
                for other in [m for m, (lip, _) in self.leases.items() if lip == ip and m != mac]:
                    del self.leases[other]          # 过期的旧租约, 回收
                self.leases[mac] = [ip, now + self.OFFER_HOLD_S]
                return ip
        return None             # 池子用完: 宁可不回, 也不把同一个地址发给两台机器

    def _bootfile(self, opts):
        # iPXE 再要地址时给脚本, 不然又拿到 shim 绕回 GRUB (Windows PE 走 GRUB -> iPXE -> wimboot)
        if is_ipxe(opts):
            return self.ipxe_script
        return arch_bootfile(opts, self.uefi_file, self.bios_file)

    def _reply(self, msg, kind, ip, opts):
        bootfile = self._bootfile(opts)
        return build_dhcp_reply(msg, 2, ip, self.ip, [
            encode_opt(53, kind),
            encode_opt(60, "PXEClient"),
            encode_opt(54, (self.ip,)),
            encode_opt(51, struct.pack("!I", self.LEASE_S)),
            encode_opt(1, (self.netmask,)),
            encode_opt(3, (self.ip,)),
            encode_opt(66, self.ip),
            encode_opt(67, bootfile),
        ], bootfile)

    def handle(self, msg, addr, now=None):
        parsed = parse_dhcp(msg)
        if not parsed:
            return None
        if msg[0] == 2:                     # 67 口上看到别人的回包 (中继之类)
            self.watch_reply(msg)
            return None
        if msg[0] != 1:
            return None
        sid = parsed[2].get(54, b"")
        if self.guard is not None and parsed[2].get(53, b"")[0:1] == b"\x03" and len(sid) == 4 \
                and sid != socket.inet_aton(self.ip):
            # SELECTING 时才带 server-id: 客户端在这个网段上收到过别人的 OFFER
            self.guard.foreign(socket.inet_ntoa(sid), "客户端选了")
        if self._quiet():
            return None
        now = time.time() if now is None else now
        with self._lock:
            return self._handle(msg, addr, parsed, now)

    def _handle(self, msg, addr, parsed, now):
        xid, mac, opts = parsed
        mac_hex = mac.hex(":", 1)
        msg_type = opts.get(53, b"")[0:1]
        vendor = opts.get(60, b"").decode("ascii", "replace")
        server_id = opts.get(54)
        ours = server_id == socket.inet_aton(self.ip)
        ciaddr = socket.inet_ntoa(msg[12:16])
        req_ip = socket.inet_ntoa(opts[50]) if len(opts.get(50, b"")) == 4 else \
            (ciaddr if ciaddr != "0.0.0.0" else None)
        lease = self.leases.get(mac)
        if vendor and msg_type == b"\x01" and addr:
            self.log.info("DHCP 客户端 %s vendor=%r arch=%s src=%s:%d", mac_hex,
                          vendor, opts.get(93, b"").hex(), addr[0], addr[1])
        if msg_type == b"\x01":  # DISCOVER
            ip = self.offer_ip(mac, now)
            if ip is None:
                self.log.warning("DHCP DISCOVER %s: 地址池已用完, 不回 OFFER", mac_hex)
                return None
            self.log.info("DHCP DISCOVER %s -> OFFER %s bootfile=%s", mac_hex, ip,
                          self._bootfile(opts))
            return self._reply(msg, b"\x02", ip, opts)
        if msg_type == b"\x03":  # REQUEST
            if server_id and not ours:
                return None     # 客户端选了别的 DHCP 服务器: 不 ACK 也不 NAK
            mine = lease is not None and (req_ip is None or req_ip == lease[0]) and \
                self._usable(lease[0], mac, now)
            if not mine:
                if not ours:    # INIT-REBOOT / 续租, 但不是我们的租约: 不是我们的事, 不吭声
                    return None
                self.log.info("DHCP REQUEST %s 拒绝 %s", mac_hex, req_ip)
                return build_dhcp_reply(msg, 2, "0.0.0.0", self.ip, [
                    encode_opt(53, b"\x06"), encode_opt(54, (self.ip,))])  # NAK
            lease[1] = now + self.LEASE_S
            self.log.info("DHCP REQUEST %s -> ACK %s", mac_hex, lease[0])
            return self._reply(msg, b"\x05", lease[0], opts)
        if msg_type in (b"\x04", b"\x07") and (ours or not server_id):  # DECLINE / RELEASE
            ip = req_ip if msg_type == b"\x04" else ciaddr
            if lease and lease[0] == ip:
                del self.leases[mac]
                if msg_type == b"\x04":
                    self.declined[ip] = now + self.DECLINE_HOLD_S
                    self.log.warning("DHCP DECLINE %s %s: 地址有冲突, 隔离 %ds", mac_hex, ip,
                                     self.DECLINE_HOLD_S)
                else:
                    self.log.info("DHCP RELEASE %s %s", mac_hex, ip)
        return None

    def handle_proxy(self, msg, addr):
        """proxyDHCP (UDP 4011): PXE 二阶段, 只回引导信息不租 IP (yiaddr=0)."""
        parsed = parse_dhcp(msg)
        if not parsed or msg[0] != 1 or self._quiet():
            return None
        xid, mac, opts = parsed
        msg_type = opts.get(53, b"")[0:1]
        if msg_type not in (b"\x01", b"\x03"):
            return None
        reply_type = b"\x02" if msg_type == b"\x01" else b"\x05"
        kind = "OFFER" if msg_type == b"\x01" else "ACK"
        bootfile = self._bootfile(opts)
        self.log.info("proxyDHCP %s %s -> %s:%s bootfile=%s", kind, mac.hex(":", 1),
                      addr[0], addr[1], bootfile)
        return build_dhcp_reply(msg, 2, "0.0.0.0", self.ip, [
            encode_opt(53, reply_type),
            encode_opt(60, "PXEClient"),
            encode_opt(54, (self.ip,)),
            encode_opt(66, self.ip),
            encode_opt(67, bootfile),
        ], bootfile)

    def serve_proxy(self, ready_event=None, sock=None):
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_retry(sock, (self.ip, 4011), log=self.log)
        if ready_event:
            ready_event.set()
        serve_loop(sock, self._on_proxy, self.log, "proxyDHCP")

    def _on_proxy(self, sock, msg, addr):
        reply = self.handle_proxy(msg, addr)
        if reply:
            sock.sendto(reply, addr)  # 单播回源

    def serve_watch(self, sock=None, sleep=time.sleep):
        """被动监听 UDP/68 (锁在装机口): 看别人的 OFFER/ACK. 和 dhcp_probe 一样带 SO_REUSEADDR 共用端口;
        绑不上 (端口被别的程序独占) 就每 30 秒再试, 不让线程死掉."""
        while sock is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.iface.encode())
                s.bind(("", 68))
                sock = s
            except OSError as e:
                s.close()
                self.log.warning("被动监听 UDP/68 绑不上: %s, 30 秒后重试", e)
                sleep(30)
        serve_loop(sock, lambda _s, msg, _a: self.watch_reply(msg), self.log, "DHCP 被动监听")

    def serve(self, ready_event=None, sock=None):
        if sock is None:
            sock = open_dhcp_socket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
                                    self.ip, self.port, self.iface, log=self.log)
        if ready_event:
            ready_event.set()
        serve_loop(sock, self._on_dhcp, self.log, "DHCP")

    def _on_dhcp(self, sock, msg, addr):
        result = self.handle(msg, addr)
        if result:
            port = addr[1] if addr[1] != 67 else 68
            self.log.info("DHCP 回包 -> 255.255.255.255:%d + %s:%d", port, self.broadcast, port)
            sock.sendto(result, ("255.255.255.255", port))
            sock.sendto(result, (self.broadcast, port))


# ---------------------------------------------------------------- 装机菜单与装机模式
# 默认只有救援菜单; 推车上解锁时 kiosk_core 只给选定 MAC 写 grub.cfg-01-<mac>, 状态在 pxe-mode.json.
# 菜单模板只在这里一份 (kiosk_core 也用它); TFTP/HTTP 发菜单和 ks 之前再看一遍 pxe-mode.json 兜底,
# 车机界面不在时也不会误装.

PXE_MODE = "/opt/pxe/pxe-mode.json"

# Windows PE: GRUB 链式加载 iPXE (Rocky 的 GRUB 直接 linux wimboot 报 kernel too old), iPXE 二次 DHCP
# 拿到 boot.ipxe, 再用 wimboot 起 PE. 文件都在 tftp 根目录下, 由 80 口的 httpserve 出.
# ipxe.efi 必须是完整版 ipxe-x86_64.efi (snponly 被 GRUB 链式加载时认不到网卡);
# 微PE 的 wim 里没有 bootmgfw.efi, 要单独放.
WINPE_DIR = "win"
WINPE_FILES = ("ipxe.efi", "wimboot", "bootmgfw.efi", "bcd", "boot.sdi", "boot.wim")
WINPE_CHAIN = f"{WINPE_DIR}/ipxe.efi"
IPXE_SCRIPT = "boot.ipxe"
WINPE_TITLE = "WinPE (GRUB -> iPXE -> wimboot)"


def grub_menu(ip, install=False, winpe=False):
    """GRUB 菜单: 救援 (默认, 不装系统) / 自动装机. 每项都给 HTTP (快) 和 TFTP (慢) 两条路.
    装机项带 inst.ks.sendmac: anaconda 取 ks 时报 MAC, httpserve 据此核对是不是解锁的那台
    (防误装; MAC 头是客户端自报的, 不当访问控制).
    winpe=True 在最后加一项 WinPE (不当默认项), 只给解锁时的 MAC 菜单用."""
    repo = f"http://{ip}:8000"
    if install:
        title, args, timeout = ("Rocky 9.8 auto install",
                                f"inst.repo={repo}/rocky inst.ks={repo}/ks.cfg inst.ks.sendmac", 5)
    else:
        title, args, timeout = ("Rescue shell", f"inst.stage2={repo}/rocky inst.rescue inst.sshd inst.text", 10)
    out = [f"set default=0\nset timeout={timeout}\n"]
    for src, label in ((f"(http,{ip})", "HTTP fast"), ("", "TFTP slow")):
        out.append(f"menuentry '{title} ({label})' {{\n"
                   f"    linux {src}/vmlinuz ip=dhcp {args}\n"
                   f"    initrd {src}/initrd.img\n}}\n")
    if winpe:
        out.append(f"menuentry '{WINPE_TITLE}' {{\n"
                   f"    chainloader (http,{ip})/{WINPE_CHAIN}\n}}\n")
    return "\n".join(out)


def ipxe_script():
    """boot.ipxe: 路径相对脚本自己的 URL (http://<ip>/boot.ipxe), 不用写地址."""
    lines = ["#!ipxe", f"kernel {WINPE_DIR}/wimboot"]
    lines += [f"initrd -n {n} {WINPE_DIR}/{n}" for n in WINPE_FILES if n not in ("ipxe.efi", "wimboot")]
    return "\n".join(lines + ["boot"]) + "\n"


def winpe_missing(root):
    """tftp 根目录下 win/ 缺的 (或空的, 拷到一半) 文件, 按 WINPE_FILES 顺序. 空列表 = 齐了."""
    missing = []
    for n in WINPE_FILES:
        try:
            ok = os.path.getsize(os.path.join(root, WINPE_DIR, n)) > 0
        except OSError:
            ok = False
        if not ok:
            missing.append(f"{WINPE_DIR}/{n}")
    return missing


def unlock_only_menu(text):
    """只能给解锁那台机器的菜单: 带自动装机 (inst.ks=) 或 WinPE 链式加载项."""
    return "inst.ks=" in text or WINPE_CHAIN in text


def norm_mac(mac):
    m = mac.strip().lower().replace("-", ":")
    if len(m.split(":")) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p)
                                          for p in m.split(":")):
        raise ValueError(f"不是 MAC 地址: {mac!r}")
    return m


def pxe_mode(path=PXE_MODE, now=None):
    """{"mode": "rescue"} / {"mode": "install", mac, since, until} / 过期则 mode="expired".
    文件没有、坏了、字段不对一律当救援."""
    now = time.time() if now is None else now
    try:
        with open(path) as f:
            st = json.load(f)
        mac, since, until = norm_mac(st["mac"]), st["since"], st["until"]
        armed = now <= until
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"mode": "rescue"}
    return {"mode": "install" if armed else "expired", "mac": mac, "since": since, "until": until}


def menu_mac(name):
    """grub.cfg-01-<mac> -> mac; 别的名字 None."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not base.startswith("grub.cfg-01-"):
        return None
    try:
        return norm_mac(base[len("grub.cfg-01-"):])
    except ValueError:
        return None


def install_allowed(mac, mode):
    return bool(mac) and mode.get("mode") == "install" and mode.get("mac") == mac


# ---------------------------------------------------------------- TFTP

TFTP_MIN_BLKSIZE, TFTP_MAX_BLKSIZE = 8, 65464     # RFC 2348


def tftp_error(code, text):
    return b"\x00\x05" + struct.pack("!H", code) + text.encode() + b"\x00"


def parse_rrq(msg):
    """RRQ -> (文件名, mode, {选项: 值}); blksize 转成 int 并校验范围. 格式不对抛 ValueError."""
    if msg[0:2] != b"\x00\x01" or not msg.endswith(b"\x00"):
        raise ValueError("RRQ 格式不对")
    parts = msg[2:-1].split(b"\x00")
    if len(parts) < 2 or len(parts) % 2 or not parts[0]:
        raise ValueError("RRQ 缺文件名/模式, 或选项不成对")
    name = parts[0].decode("utf-8", "replace")
    mode = parts[1].decode("ascii", "replace").lower()
    opts = {}
    for i in range(2, len(parts), 2):
        opts[parts[i].decode("ascii", "replace").lower()] = parts[i + 1].decode("ascii", "replace")
    if "blksize" in opts:
        b = opts["blksize"]
        if not b.isdigit() or not TFTP_MIN_BLKSIZE <= int(b) <= TFTP_MAX_BLKSIZE:
            raise ValueError(f"blksize 不合法: {b!r}")
        opts["blksize"] = int(b)
    return name, mode, opts


def _await_ack(sock, client, block):
    """等对方的 ACK block. True=收到; False=超时/对方报错; 别处来的包忽略."""
    while True:
        try:
            pkt, src = sock.recvfrom(2048)
        except socket.timeout:
            return False
        if src != client:
            continue
        if pkt[0:2] == b"\x00\x05":
            raise ConnectionAbortedError(pkt[4:].split(b"\x00")[0].decode("utf-8", "replace"))
        if pkt[0:2] == b"\x00\x04" and len(pkt) >= 4 and struct.unpack("!H", pkt[2:4])[0] == block:
            return True
        # 重复的旧 ACK: 接着等, 超时后外层重发


def _send_and_wait(sock, client, pkt, block, retries):
    for _ in range(retries):
        sock.sendto(pkt, client)
        if _await_ack(sock, client, block):
            return True
    return False


def tftp_send_file(sock, client, f, size, blksize, retries=5):
    """按块 seek+read 发出去, 块号按 16 位回绕 (大于 32MB 的 initrd 在 blksize=512 时会超).
    最后一块不足 blksize (整除时补一个空块). 返回是否传完; 对方报错抛 ConnectionAbortedError."""
    total = size // blksize + 1
    for n in range(1, total + 1):
        f.seek((n - 1) * blksize)
        chunk = f.read(blksize)
        wire = n & 0xFFFF
        if not _send_and_wait(sock, client, b"\x00\x03" + struct.pack("!H", wire) + chunk, wire, retries):
            return False
    return True


def tftp_safe_name(root, name):
    name = name.replace("\\", "/").lstrip("/")
    path = os.path.normpath(os.path.join(root, name))
    if not path.startswith(os.path.abspath(root) + os.sep) and path != os.path.abspath(root):
        raise PermissionError(name)
    return path


class TftpServer:
    MENU_MAX = 64 * 1024        # GRUB 菜单是小文本; 比这大的不当菜单看

    def __init__(self, ip, root, log, blksize=1428, timeout=2.0, retries=5, port=69,
                 sock_factory=None, pxe_mode_path=PXE_MODE, now=time.time, event=cart_event):
        self.ip, self.root, self.log = ip, os.path.abspath(root), log
        self.blksize, self.timeout, self.retries = blksize, timeout, retries
        self.port = port
        self.sock_factory = sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self.pxe_mode_path, self.now, self.event = pxe_mode_path, now, event

    def serve(self, ready_event=None, sock=None):
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_retry(sock, (self.ip, self.port), log=self.log)
        if ready_event:
            ready_event.set()
        serve_loop(sock, self._dispatch, self.log, "TFTP")

    def _dispatch(self, sock, msg, addr):
        if msg[0:2] == b"\x00\x01":  # RRQ
            threading.Thread(target=self._rrq, args=(msg, addr), daemon=True).start()
        else:
            self.log.warning("TFTP 未知 opcode %s from %s", msg[0:2].hex(), addr)
            sock.sendto(tftp_error(4, "Only RRQ supported"), addr)

    def open_file(self, name, client):
        """-> (文件对象, 大小, 日志前缀). 找不到/越界抛 OSError.
        GRUB 会取的菜单 (grub.cfg*) 里带 inst.ks= 或 WinPE 项的 (unlock_only_menu), 只有已解锁、没过期、
        文件名就是那台 MAC 时才照发,
        否则换成救援菜单 (日志前缀不同, 车机不会当成菜单被取走)."""
        path = tftp_safe_name(self.root, name)
        f = open(path, "rb")
        size = os.fstat(f.fileno()).st_size
        base = os.path.basename(path)
        if not base.startswith("grub.cfg") or base.startswith("grub.cfg.") or size > self.MENU_MAX:
            return f, size, f"TFTP RRQ {name}"
        with f:
            data = f.read()
        if unlock_only_menu(data.decode("utf-8", "replace")):
            mac = menu_mac(base)
            if not install_allowed(mac, pxe_mode(self.pxe_mode_path, self.now())):
                data = grub_menu(self.ip).encode()
                return io.BytesIO(data), len(data), f"TFTP RRQ {name} 未解锁, 回救援菜单"
            self.event(f"装机菜单被取走 mac={mac} from {client[0]}")
        return io.BytesIO(data), len(data), f"TFTP RRQ {name}"

    def _rrq(self, msg, addr):
        client = (addr[0], addr[1])
        sock = self.sock_factory()
        try:
            self._session(sock, msg, client)
        except Exception:
            self.log.exception("TFTP 会话异常 %s", client)
        finally:
            sock.close()

    def _session(self, sock, msg, client):
        sock.settimeout(self.timeout)
        try:
            name, mode, opts = parse_rrq(msg)
        except ValueError as e:
            self.log.info("TFTP 坏请求 from %s: %s", client[0], e)
            sock.sendto(tftp_error(8 if b"blksize" in msg.lower() else 4, str(e)), client)
            return
        if mode != "octet":
            self.log.warning("TFTP RRQ %s 非 octet 模式: %r", name, mode)
        blksize = min(opts.get("blksize", 512), self.blksize)
        try:
            f, size, what = self.open_file(name, client)
        except OSError as e:          # PermissionError 也是 OSError
            self.log.info("TFTP RRQ %s 失败: %s", name, e)
            sock.sendto(tftp_error(1, "File not found"), client)
            return
        with f:
            self.log.info("%s (%d bytes, blksize=%d) from %s", what, size, blksize, client[0])
            try:
                if "blksize" in opts or "tsize" in opts:
                    oack = b"\x00\x06"
                    if "blksize" in opts:
                        oack += b"blksize\x00" + str(blksize).encode() + b"\x00"
                    if "tsize" in opts:
                        oack += b"tsize\x00" + str(size).encode() + b"\x00"
                    if not _send_and_wait(sock, client, oack, 0, self.retries):
                        self.log.info("TFTP %s OACK 无 ACK0, 放弃", name)
                        return
                done = tftp_send_file(sock, client, f, size, blksize, self.retries)
            except ConnectionAbortedError as e:
                self.log.info("TFTP %s 客户端中止: %s", name, e)   # PXE ROM 先问 tsize 再断开是常态
                return
        self.log.info("TFTP %s %s", name, "传输完成" if done else "无 ACK, 放弃")


# ---------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--ip", required=True)
    p.add_argument("--prefix", type=int, default=24, help="装机网段前缀长度, 算掩码和广播地址")
    p.add_argument("--pool", required=True)
    p.add_argument("--tftp-root", required=True)
    p.add_argument("--uefi-file", default="shimx64.efi")
    p.add_argument("--bios-file", default="pxelinux.0")
    p.add_argument("--log", default=None)
    p.add_argument("--iface", default=None, help="装机口网卡名 (Linux 必填, 否则收不到 DHCP 广播)")
    p.add_argument("--guard-state", default=GUARD_STATE, help="守卫状态文件 (JSON)")
    p.add_argument("--pxe-mode", default=PXE_MODE, help="装机模式状态文件 (kiosk 解锁时写)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler()] +
                 ([logging.FileHandler(args.log, encoding="utf-8")] if args.log else []),
    )
    log = logging.getLogger("pxe")
    log.info("PXE 服务启动 ip=%s/%d pool=%s tftp-root=%s", args.ip, args.prefix, args.pool,
             args.tftp_root)

    try:
        dhcp = DhcpServer(args.ip, *args.pool.split(","), args.uefi_file, args.bios_file, log,
                          iface=args.iface, prefix=args.prefix)
    except (ValueError, TypeError) as e:
        log.error("参数不对: %s", e)
        sys.exit(EXIT_CONFIG)
    linux = sys.platform.startswith("linux")
    if linux and not args.iface:
        log.warning("Linux 上没给 --iface: DHCP 只绑 %s, 收不到客户端的广播 DISCOVER", args.ip)
    guard = PxeGuard(args.ip, args.iface if linux else None, log, probe=default_probe(),
                     state_path=args.guard_state)
    dhcp.guard = guard
    guard.tick()                    # 先探测: 装机口接在别人的网段上就一个包都不发
    if guard.tripped:
        sys.exit(EXIT_FOREIGN_DHCP)
    tftp = TftpServer(args.ip, args.tftp_root, log, pxe_mode_path=args.pxe_mode)
    ev1, ev2, ev3 = threading.Event(), threading.Event(), threading.Event()
    threads = [threading.Thread(target=dhcp.serve, args=(ev1,), name="dhcp", daemon=True),
               threading.Thread(target=tftp.serve, args=(ev2,), name="tftp", daemon=True),
               threading.Thread(target=dhcp.serve_proxy, args=(ev3,), name="proxydhcp", daemon=True)]
    if linux and args.iface:
        threads.append(threading.Thread(target=dhcp.serve_watch, name="watch", daemon=True))
    for t in threads:
        t.start()
    for ev in (ev1, ev2, ev3):     # 真绑上了才报就绪; 地址没出现就一直等 (bind_retry 里会提示)
        while not ev.wait(2):
            guard.tick()
            if guard.tripped or not all(t.is_alive() for t in threads):
                break
    log.info("DHCP UDP/67 + proxyDHCP UDP/4011 + TFTP UDP/69 就绪, 守卫 %s", guard.state)
    sys.exit(supervise(threads, log, guard=guard))


if __name__ == "__main__":
    main()
