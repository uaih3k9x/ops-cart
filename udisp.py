#!/usr/bin/env python3
"""udisp: xfz1986 USB 副屏的协议层 (只打包, 不碰 USB).

一次 bulk OUT 传输 = 16 字节帧头 + 负载, 小端紧凑.
  u16 crc16          主机填 0, 固件不校验
  u8  type           0=RGB565 1=RGB888 2=YUV420 3=JPEG; 0xff = 假 zlp 收尾
  u8  cmd            填 0
  u16 x, y           区域左上角
  u16 width, height  区域大小
  u32 frame_id:10 | payload_total:22

帧结束靠短包或收满 payload_total; 总长恰为端点包长整数倍时补一个
type=0xff 的 16 字节帧 (假 zlp, ESP32 固件不支持真零长度包).

设备配置在 iProduct 字符串里, `_` 分隔的前缀字段:
  R<宽>x<高>   Ejpg<n>   Ergb<n>   Fps<n>   Bl<n>(按字节)
实测 2026-10-29, ESP32-P4 板 303a:2987:
  esp32p4udisp0_R1280x800_Ejpg6_Fps46_Bl300000
"""

import re
import struct

VENDOR_ID = 0x303A
PRODUCT_IDS = (0x2986, 0x2987)      # 0x2986 = 复合设备, 用接口 0

TYPE_RGB565 = 0
TYPE_RGB888 = 1
TYPE_YUV420 = 2
TYPE_JPG = 3
TYPE_ZLP = 0xFF

HEADER_SIZE = 16
HEADER = struct.Struct("<HBBHHHHI")
MAX_PAYLOAD = (1 << 22) - 1

DEFAULT_WIDTH = 480
DEFAULT_HEIGHT = 480


def build_header(ptype, payload_total, frame_id=0, x=0, y=0, width=0, height=0, cmd=0):
    if not 0 <= payload_total <= MAX_PAYLOAD:
        raise ValueError("负载 %d 超出 22 位" % payload_total)  # 推车 Python 3.9 也能跑
    word = (frame_id & 0x3FF) | (payload_total << 10)
    return HEADER.pack(0, ptype, cmd, x, y, width, height, word)


def build_frame(ptype, payload, frame_id=0, x=0, y=0, width=0, height=0):
    """整帧 = 帧头 + 负载. width/height 默认取 0, 由调用方给真实尺寸."""
    return build_header(ptype, len(payload), frame_id, x, y, width, height) + payload


def needs_zlp(total_bytes, max_packet):
    """总长恰为包长整数倍时, 固件看不到短包, 要主机补假 zlp."""
    return max_packet > 0 and total_bytes % max_packet == 0


def zlp_frame():
    return build_header(TYPE_ZLP, 0)


def rot90_rect(x, y, w, h, disp_width):
    """横屏显示的矩形 -> 竖屏内容的矩形 (面板物理转 90°).

    帧头坐标永远按配置串报告的横屏尺寸 (实测板子 R1280x800), 竖屏画面
    要么整张转好再发, 要么只发脏矩形时按这个映射换算.
    换算: 横屏 (xl,yl) -> 竖屏 (yl, disp_width-1-xl), 横屏左上角落到竖屏左下角.
    """
    return y, disp_width - (x + w), h, w


_RE_RES = re.compile(r"R(\d+)x(\d+)")


def parse_config(text):
    """解析 iProduct, 返回 dict: width/height/enc/quality/fps/blimit.

    和 Windows 驱动 parse_usb_dev_info 的默认值一致:
      fps=60, enc=JPG, quality=5, blimit=1080P*4 字节.
    字段少于 2 段 (只有设备名) 视为不合规, 全用默认.
    """
    text = (text or "").split("\x00", 1)[0]
    cfg = {"width": 0, "height": 0, "enc": TYPE_JPG, "quality": 5, "fps": 60,
           "blimit": 1920 * 1080 * 4}
    parts = [p for p in text.split("_") if p]
    if len(parts) < 2:
        return cfg
    for part in parts[1:]:
        head = part[:1].upper()
        if head == "R":
            m = _RE_RES.search(part)
            if m:
                cfg["width"], cfg["height"] = int(m.group(1)), int(m.group(2))
        elif head == "E":
            m = re.search(r"jpg(\d+)", part, re.IGNORECASE)
            if m:
                cfg["enc"], cfg["quality"] = TYPE_JPG, int(m.group(1))
            elif re.match(r"rgb(\d+)", part[1:], re.IGNORECASE):
                bits = int(re.search(r"\d+", part[1:]).group(0))
                cfg["enc"] = TYPE_RGB565 if bits <= 16 else TYPE_RGB888
        elif head == "F":
            # Fps60; 不能沿用 B 的 Bl<n> 规则, 否则 Fps 里的 p 会被当成 Bl
            m = re.search(r"(\d+)", part)
            if m:
                cfg["fps"] = int(m.group(1))
        elif head == "B":
            m = re.search(r"(\d+)", part)
            if m:
                cfg["blimit"] = int(m.group(1))
    return cfg
