#!/usr/bin/env python3
"""monita_cs80: cart-dash 的 Monita-CS80 副屏后端 (设计稿 docs/monita-dash.md).

和 udisp_usb 一样给 dash_service 一个 open_display(); 区别是这里的 Display 声明 wants_image:
收竖屏 800x1280 的 PIL RGB 图 (dash_render.render 原样, 不转向、不编 JPEG), 自己转 RGB565、按脏区发 RECT + PRESENT.
dash_service 每轮采集调一次 tick(): 保活 (喂固件 5 s "主机走了" 看门狗)、段码屏、灯、读按键.

只读: 按键只开关背光, 不碰推车任何状态. 不发 SLEEP (见设计稿第 7 节).
"""

import os
import struct
import time

import cs80_link as cl
import cs80_proto as p

VENDOR_ID, PRODUCT_ID, INTERFACE = "2c7c", "0901", 7
SEG_S = 1.0             # 段码屏 1 s 一次, 顺带喂看门狗
PING_S = 2.0            # 这么久没发过任何帧就 PING
REHELLO_S = 3.5         # 这么久没发过: 设备可能已回开机页, 先 HELLO 再推整帧
FULL_RETRY_S = 1.0      # 设备报了错 (该推整帧) 时 tick 自己补推; 补了还错的话最多这么久一次


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _usb_ids(dev_dir, levels=6):
    """从 tty 的 device 目录往上找: 先遇到 USB 接口 (bInterfaceNumber), 再往上是 USB 设备 (idVendor)."""
    d, intf = os.path.realpath(dev_dir), None
    for _ in range(levels):
        if intf is None:
            n = _read(os.path.join(d, "bInterfaceNumber"))
            if n is not None:
                intf = n
        else:
            vid = _read(os.path.join(d, "idVendor"))
            if vid is not None:
                return vid.lower(), (_read(os.path.join(d, "idProduct")) or "").lower(), intf
        d = os.path.dirname(d)
    return None


def find_port(sys_root="/sys", dev_root="/dev"):
    """在 sysfs 里找 CS80 的协议口 (2c7c:0901 接口 07); 没有返回 None. 只看 sysfs, 不打开任何节点:
    推车上的 /dev/con-* 接着交换机 / 防火墙的 console, 不能往错的 tty 写 HELLO."""
    cls = os.path.join(sys_root, "class", "tty")
    try:
        names = sorted(os.listdir(cls))
    except OSError:
        return None
    for name in names:
        if not name.startswith(("ttyUSB", "ttyACM")):
            continue
        ids = _usb_ids(os.path.join(cls, name, "device"))
        if ids and ids[0] == VENDOR_ID and ids[1] == PRODUCT_ID and _int(ids[2], 16) == INTERFACE:
            return os.path.join(dev_root, name)
    return None


def _int(s, base=10):
    try:
        return int(s, base)
    except (TypeError, ValueError):
        return None


def open_display(port=None, log=print, sys_root="/sys", opener=cl.Port, clock=time.monotonic, wall=time.time):
    """找到 (或用指定的) 节点, 打开, HELLO. 没找到 / 打不开 / 不兼容都抛异常, dash_service 3 s 后再试."""
    path = port or find_port(sys_root)
    if not path:
        raise FileNotFoundError("没找到 Monita-CS80 (%s:%s 接口 %02d)" % (VENDOR_ID, PRODUCT_ID, INTERFACE))
    dev = opener(path)
    try:
        return Display(cl.Link(dev, clock=clock), clock=clock, wall=wall, log=log)
    except Exception:
        dev.close()
        raise


class Display:
    """一个已 HELLO 的 CS80. send(img) 推帧; tick() 每轮采集调一次; close() 只关节点."""

    wants_image = True

    def __init__(self, link, clock=time.monotonic, wall=time.time, log=print):
        self.link, self.clock, self.wall, self.log = link, clock, wall, log
        self.committed = None           # 最近一次 PRESENT 有 ACK 的那张图
        self.last_img = None            # 最近一次 send 收到的图 (重新 HELLO 后自己推整帧用)
        self.backlight = True
        self.disabled = set()           # 设备回 status 1 / 3 的功能, 本次会话不再发
        self.frame_id = 0
        self.retry_at = 0.0             # tick 下一次可以自己补推整帧的时刻
        self._hello()

    # ---------------------------------------------------------------- 会话
    def _hello(self):
        now = self.wall()
        info = self.link.hello(now, p.tz_minutes(now))
        why = p.hello_problem(info)
        if why:
            raise ValueError("Monita-CS80 不兼容: %s" % why)
        self.info = info
        self.max_payload = info["max_payload"]
        self.seg_ok = info["seg_bytes"] == p.SEG_BYTES
        self.led_ok = bool(info["caps"] & p.CAP_LED)
        self.err_base = 0               # HELLO 把设备计数清零
        self.full = True                # 设备刚开机 / 回过开机页: 下一帧整帧
        self.led_sent = None            # HELLO 把灯灭掉, 下一轮重发
        self.last_seg = None
        self.key_n = None               # HELLO 把 KEY 计数清零
        self.cfg_text = "Monita-CS80 fw v%d caps 0x%02x max_payload %d" % (
            info["fw_ver"], info["caps"], info["max_payload"])
        # HELLO 不动背光: 5 s 看门狗之前重连时设备可能还关着 (主机走了时固件才会打开). 不管开关都按自己记的重发
        self._feature(p.BACKLIGHT, b"\x01" if self.backlight else b"\x00", "BACKLIGHT")

    def _idle(self):
        last = self.link.last_tx
        return float("inf") if last is None else self.clock() - last

    def _request(self, ptype, payload=b""):
        a = self.link.request(ptype, payload)
        errs = p.ack_errors(a)
        if errs > self.err_base:        # 之前有 RECT 被拒 / 坏帧: 下一帧整帧
            self.full = True
        self.err_base = errs
        return a

    def _feature(self, ptype, payload, name):
        """SEG / LED / BACKLIGHT: 回 status != 0 不当断线, 记一次日志、本次会话停用."""
        if name in self.disabled:
            return False
        a = self._request(ptype, payload)
        if a["status"] != 0:
            self.disabled.add(name)
            self.log("副屏不接受 %s (%s), 本次连接不再发" % (name, p.STATUS.get(a["status"], a["status"])))
            return False
        return True

    # ---------------------------------------------------------------- 推帧
    def send(self, img):
        """推一帧 (竖屏 800x1280 RGB). 只读 img, 不往上画. 返回发了几个 RECT."""
        if self._idle() >= REHELLO_S:
            self._hello()
        self.last_img = img
        return self._push(img)

    def _push(self, img):
        rects = p.dirty_rects(None if self.full else self.committed, img, self.max_payload)
        if not rects:
            return 0
        for box in rects:
            self.link.send(p.RECT, p.rect_payload(img, box))
        self.frame_id = (self.frame_id + 1) & 0xFFFF
        self.full = False
        a = self._request(p.PRESENT, struct.pack("<H", self.frame_id))
        if a["status"] != 0:
            raise OSError("PRESENT 回 status %d" % a["status"])
        self.committed = img            # 只有 ACK 回来才算提交; 超时上面就抛了, 走重连 + 整帧
        return len(rects)

    # ---------------------------------------------------------------- 每轮采集
    def tick(self, now, snap, view):
        """保活 + 段码屏 + 灯 + 按键. now 是墙上时间 (段码屏时钟); 节奏按单调钟."""
        self.link.poll()
        for k in self.link.take_keys():
            self._key(k)
        if self._idle() >= REHELLO_S:
            self._hello()
        if self.full and self.last_img is not None and self.clock() >= self.retry_at:
            self._push(self.last_img)       # 重新 HELLO 过, 或上一帧设备报了错: 不等下一次 send
            self.retry_at = self.clock() + FULL_RETRY_S if self.full else 0.0
        if self.led_ok:
            st = p.led_state(snap, view)
            if st != self.led_sent:
                self._feature(p.LED, p.led_payload(st), "LED")
                self.led_sent = st
        if self.seg_ok and (self.last_seg is None or self.clock() - self.last_seg >= SEG_S):
            self._feature(p.SEG, p.seg_state(snap, view, now), "SEG")
            self.last_seg = self.clock()
        if self._idle() >= PING_S:
            self._request(p.PING)

    def _key(self, k):
        """+ 抬起开背光, − 抬起关背光. ≡ / 查询键 v1 不用 (查询键是 PWRKEY, 长按会自己补 UP, 容易误触)."""
        if self.key_n is not None and k["n"] != self.key_n:
            self.log("副屏按键事件断号: 想要 %d, 来了 %d (只记一笔, 不补)" % (self.key_n, k["n"]))
        self.key_n = (k["n"] + 1) & 0xFFFF
        if k["ev"] != p.EV_UP or k["key"] not in (p.KEY_PLUS, p.KEY_MINUS):
            return
        on = k["key"] == p.KEY_PLUS
        if on != self.backlight and self._feature(p.BACKLIGHT, b"\x01" if on else b"\x00", "BACKLIGHT"):
            self.backlight = on

    def close(self):
        """只关节点. 不发 SLEEP: 出错重连也走这里, 发了下一帧又把设备叫醒; cart-dash 停了设备 5 s 后自己回开机页."""
        self.link.close()
