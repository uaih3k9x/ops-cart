#!/usr/bin/env python3
"""udisp_usb: udisp 副屏的 USB 传输层 (pyusb). 协议打包在 udisp.py, 这里只管开设备/发帧.

推车上设备节点默认只有 root 能开. cart-dash 以 root 跑 (见 cart-dash.service);
手动调试想用普通用户, 推车上另有 udev 规则 60-xfz1986-udisp.rules 给了组权限.
没有 uaccess: 推车没有 seat 会话.
"""

import time

import udisp

VENDOR_ID = udisp.VENDOR_ID
PRODUCT_IDS = udisp.PRODUCT_IDS


def _usb():
    """延迟导入: 没装 pyusb 时模块仍可导入(测试/离线环境)."""
    import usb.core
    import usb.util
    return usb


class Display:
    """一个已 claim 的 udisp 设备. send() 收一帧已编码好的 JPEG 载荷."""

    def __init__(self, dev, intf, out_ep, usb, cfg_text="", width=0, height=0):
        self.dev = dev
        self.intf = intf
        self.out_ep = out_ep
        self.usb = usb
        self.mps = out_ep.wMaxPacketSize or 512
        self.cfg_text = cfg_text
        self.cfg = udisp.parse_config(cfg_text)
        self.width = width or self.cfg["width"]
        self.height = height or self.cfg["height"]
        self.frame_id = 0
        self.frames = 0
        self.bytes = 0
        self.last_error = None

    # ---------------------------------------------------------------- 发送
    def send(self, payload, ptype=udisp.TYPE_JPG, x=0, y=0, width=None, height=None):
        """发一帧: 帧头 + 载荷, 长度是包长整数倍时补假 zlp. 返回发出的字节数."""
        width = width if width is not None else self.width
        height = height if height is not None else self.height
        frame = udisp.build_frame(ptype, payload, self.frame_id, x, y, width, height)
        self.dev.write(self.out_ep.bEndpointAddress, frame, timeout=5000)
        if udisp.needs_zlp(len(frame), self.mps):
            self.dev.write(self.out_ep.bEndpointAddress, udisp.zlp_frame(), timeout=2000)
        self.frame_id = (self.frame_id + 1) & 0x3FF
        self.frames += 1
        self.bytes += len(frame)
        return len(frame)

    def close(self):
        """放接口 + 释放 libusb 句柄: 拔插反复重连时不攒句柄."""
        for f in (lambda: self.usb.util.release_interface(self.dev, self.intf.bInterfaceNumber),
                  lambda: self.usb.util.dispose_resources(self.dev)):
            try:
                f()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_display(vid=VENDOR_ID, pids=PRODUCT_IDS, usb=None):
    """找第一个 vendor 接口 + bulk OUT 端点并 claim. 找不到抛 DisplayNotFound."""
    usb = usb or _usb()
    for pid in pids:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            continue
        for cfg in dev:
            for intf in cfg:
                if intf.bInterfaceClass != 0xFF:
                    continue
                out_ep = None
                for ep in intf:
                    if (ep.bmAttributes & 0x03) == 0x02 and not (ep.bEndpointAddress & 0x80):
                        out_ep = ep
                if out_ep is None:
                    continue
                try:
                    cfg_text = (usb.util.get_string(dev, intf.iInterface) or "")
                except Exception:
                    cfg_text = ""          # 读不到描述符就按默认值走
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                return Display(dev, intf, out_ep, usb, cfg_text)
    raise DisplayNotFound("没找到 udisp 设备 (303a:2987/2986, vendor 接口)")


class DisplayNotFound(RuntimeError):
    pass


def push(jpeg, retries=3, delay=0.5, **kw):
    """一次性推一张图; 设备没插就抛 DisplayNotFound. 给命令行和测试用."""
    last = None
    for i in range(retries):
        try:
            with open_display(**kw) as d:
                n = d.send(jpeg)
                return n, d.frames
        except DisplayNotFound:
            raise
        except Exception as e:        # 拔线/USB 忙: 退避再试
            last = e
            time.sleep(delay * (i + 1))
    raise last
