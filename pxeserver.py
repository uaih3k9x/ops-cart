#!/usr/bin/env python3
"""单文件 PXE 服务端: DHCP + TFTP. Windows/Linux 通用, 无第三方依赖.

用法:
  python pxeserver.py serve --ip 192.0.2.1 --prefix 24 --pool 192.0.2.10,192.0.2.20 \
      --tftp-root D:/pxe/tftp --log D:/pxe/pxeserver.log

DHCP: 按 option 93 (client system architecture) 分发 bootloader:
  0007/0009 -> shimx64.efi (UEFI x86-64),  0000 -> pxelinux.0 (BIOS)
TFTP: 支持 blksize / tsize 协商, octet 模式, 拒绝根目录穿越.
"""

import argparse
import errno
import ipaddress
import logging
import os
import socket
import struct
import sys
import threading
import time

MAGIC = b"\x63\x82\x53\x63"


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
        self.log = log
        self.leases = {}        # mac(bytes) -> [ip, 到期时间]
        self.declined = {}      # ip -> 隔离到什么时候
        self._lock = threading.Lock()

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

    def _reply(self, msg, kind, ip, opts):
        bootfile = arch_bootfile(opts, self.uefi_file, self.bios_file)
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
        if not parsed or msg[0] != 1:       # 只管 BOOTREQUEST
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
                          arch_bootfile(opts, self.uefi_file, self.bios_file))
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
        if not parsed:
            return None
        xid, mac, opts = parsed
        msg_type = opts.get(53, b"")[0:1]
        if msg_type not in (b"\x01", b"\x03"):
            return None
        reply_type = b"\x02" if msg_type == b"\x01" else b"\x05"
        kind = "OFFER" if msg_type == b"\x01" else "ACK"
        bootfile = arch_bootfile(opts, self.uefi_file, self.bios_file)
        self.log.info("proxyDHCP %s %s -> %s:%s bootfile=%s", kind, mac.hex(":", 1),
                      addr[0], addr[1], bootfile)
        return build_dhcp_reply(msg, 2, "0.0.0.0", self.ip, [
            encode_opt(53, reply_type),
            encode_opt(60, "PXEClient"),
            encode_opt(54, (self.ip,)),
            encode_opt(66, self.ip),
            encode_opt(67, bootfile),
        ], bootfile)

    def serve_proxy(self, ready_event=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bind_retry(s, (self.ip, 4011), log=self.log)
        if ready_event:
            ready_event.set()
        while True:
            msg, addr = s.recvfrom(2048)
            try:
                reply = self.handle_proxy(msg, addr)
            except Exception:
                self.log.exception("proxyDHCP 处理异常")
                continue
            if reply:
                s.sendto(reply, addr)  # 单播回源

    def serve(self, ready_event=None):
        s = open_dhcp_socket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
                             self.ip, self.port, self.iface, log=self.log)
        if ready_event:
            ready_event.set()
        while True:
            msg, addr = s.recvfrom(2048)
            try:
                result = self.handle(msg, addr)
            except Exception:
                self.log.exception("DHCP 处理异常")
                continue
            if result:
                port = addr[1] if addr[1] != 67 else 68
                self.log.info("DHCP 回包 -> 255.255.255.255:%d + %s:%d", port, self.broadcast, port)
                s.sendto(result, ("255.255.255.255", port))
                s.sendto(result, (self.broadcast, port))


# ---------------------------------------------------------------- TFTP

def tftp_safe_name(root, name):
    name = name.replace("\\", "/").lstrip("/")
    path = os.path.normpath(os.path.join(root, name))
    if not path.startswith(os.path.abspath(root) + os.sep) and path != os.path.abspath(root):
        raise PermissionError(name)
    return path


class TftpServer:
    def __init__(self, ip, root, log, blksize=1428, timeout=2.0, retries=5, port=69):
        self.ip, self.root, self.log = ip, os.path.abspath(root), log
        self.blksize, self.timeout, self.retries = blksize, timeout, retries
        self.port = port

    def serve(self, ready_event=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bind_retry(s, (self.ip, self.port), log=self.log)
        if ready_event:
            ready_event.set()
        while True:
            msg, addr = s.recvfrom(2048)
            if msg[0:2] == b"\x00\x01":  # RRQ
                threading.Thread(target=self._rrq, args=(msg, addr), daemon=True).start()
            else:
                self.log.warning("TFTP 未知 opcode %s from %s", msg[0:2].hex(), addr)

    def _rrq(self, msg, addr):
        parts = msg[2:].split(b"\x00")
        name = parts[0].decode("utf-8", "replace")
        mode = parts[1].decode("ascii", "replace").lower() if len(parts) > 1 else ""
        if mode != "octet":
            self.log.warning("TFTP RRQ %s 非 octet 模式: %r", name, mode)
        opts = {}
        for i in range(2, len(parts) - 1, 2):  # parts[1] 是 mode, 选项从 parts[2] 起
            opts[parts[i].decode().lower()] = parts[i + 1].decode()
        blksize = min(int(opts.get("blksize", 512)), self.blksize) or 512
        client = (addr[0], addr[1])
        try:
            path = tftp_safe_name(self.root, name)
            data = open(path, "rb").read()
        except (PermissionError, OSError) as e:
            self.log.info("TFTP RRQ %s 失败: %s", name, e)
            err = b"\x00\x05" + struct.pack("!H", 1) + b"File not found\x00"
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(err, client)
            return
        self.log.info("TFTP RRQ %s (%d bytes, blksize=%d) from %s", name, len(data), blksize, addr[0])

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        if opts:
            oack = b"\x00\x06"
            if "blksize" in opts:
                oack += b"blksize\x00" + str(blksize).encode() + b"\x00"
            if "tsize" in opts:
                oack += b"tsize\x00" + str(len(data)).encode() + b"\x00"
            sock.sendto(oack, client)
            try:
                pkt, _ = sock.recvfrom(2048)  # 等 ACK0 或 ERROR
                if pkt[0:2] == b"\x00\x04" and struct.unpack("!H", pkt[2:4])[0] == 0:
                    pass
            except socket.timeout:
                pass
        def send_block(block, chunk):
            """发一个 DATA 块并等 ACK. 返回是否成功."""
            pkt = b"\x00\x03" + struct.pack("!H", block) + chunk
            for _ in range(self.retries):
                sock.sendto(pkt, client)
                try:
                    ack, _ = sock.recvfrom(2048)
                    if ack[0:2] == b"\x00\x04" and struct.unpack("!H", ack[2:4])[0] == block:
                        return True
                except socket.timeout:
                    continue
            return False

        total = (len(data) + blksize - 1) // blksize + (1 if data and len(data) % blksize == 0 else 0)
        for block in range(1, total + 1):
            chunk = data[(block - 1) * blksize:block * blksize]
            if not send_block(block, chunk):
                self.log.info("TFTP %s 块 %d 无 ACK, 放弃", name, block)
                sock.close()
                return
        sock.close()
        self.log.info("TFTP %s 传输完成", name)


# ---------------------------------------------------------------- main

def main():
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
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler()] +
                 ([logging.FileHandler(args.log, encoding="utf-8")] if args.log else []),
    )
    log = logging.getLogger("pxe")
    log.info("PXE 服务启动 ip=%s/%d pool=%s tftp-root=%s", args.ip, args.prefix, args.pool,
             args.tftp_root)

    dhcp = DhcpServer(args.ip, *args.pool.split(","), args.uefi_file, args.bios_file, log,
                      iface=args.iface, prefix=args.prefix)
    if sys.platform.startswith("linux") and not args.iface:
        log.warning("Linux 上没给 --iface: DHCP 只绑 %s, 收不到客户端的广播 DISCOVER", args.ip)
    tftp = TftpServer(args.ip, args.tftp_root, log)
    ev1, ev2, ev3 = threading.Event(), threading.Event(), threading.Event()
    threading.Thread(target=dhcp.serve, args=(ev1,), daemon=True).start()
    threading.Thread(target=tftp.serve, args=(ev2,), daemon=True).start()
    threading.Thread(target=dhcp.serve_proxy, args=(ev3,), daemon=True).start()
    for ev in (ev1, ev2, ev3):     # 真绑上了才报就绪; 地址没出现就一直等 (bind_retry 里会提示)
        ev.wait()
    log.info("DHCP UDP/67 + proxyDHCP UDP/4011 + TFTP UDP/69 就绪")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
