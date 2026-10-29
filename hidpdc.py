#!/usr/bin/env python3
"""hidpdc: 读 USB HID Power Device Class (HID PDC) 的 UPS 字段, 只用标准库.

不装 NUT: NUT 的 usbhid-ups 做的事就是"读 report descriptor → 解析字段表 →
HIDIOCGFEATURE 读包 → 按 unit exponent 换算 → 映射成标准名". 我们对一块板子只要 6 个字段,
所以这里只实现前四步里用得上的部分, 维护面比 NUT 小两个数量级.

用法 (root 或给了 udev 属主之后):
    fields = hidpdc.open_device("/dev/hidraw7")     # 读描述符并解析
    values = hidpdc.read_all(fields)                # {ups.load: .., battery.charge: ..}

实测板子 (2026-10-29, APC Back-UPS BX1200CI-CN, USB 051d:0002):
    有 PercentLoad / Voltage / RemainingCapacity / RunTimeToEmpty;
    没有 Temperature / Current / ActivePower —— 硬件本来就不报.
"""

import fcntl

# ---------------------------------------------------------------- 常量

PAGE_POWER = 0x84            # Power Device
PAGE_BATTERY = 0x85          # Battery System

IOCTL_HIDIOCGRAWINFO = 0x80084803
IOCTL_HIDIOCGFEATURE = 0xC0484807     # _IOC(_IOC_READ, 'H', 0x07, 0x2E) 走 46 字节头; 见 read_feature

ITEM_LONG = 0xFE
TYPE_MAIN, TYPE_GLOBAL, TYPE_LOCAL = 0, 1, 2

# 我们关心的 usage -> 标准名 (对照 NUT 的命名, 只挑这块板用得上的)
NAMES = {
    (PAGE_BATTERY, 0x66): "battery.charge",          # RemainingCapacity (%)
    (PAGE_BATTERY, 0x68): "battery.runtime",         # RunTimeToEmpty (秒)
    (PAGE_BATTERY, 0x6A): "battery.runtime.low",     # AverageTimeToEmpty 的同义 usage, 少见
    (PAGE_BATTERY, 0x65): "battery.charge.absolute",  # AbsoluteStateOfCharge
    (PAGE_BATTERY, 0x2C): "battery.capacity.mode",   # CapacityMode
    (PAGE_POWER, 0x35): "ups.load",                  # PercentLoad (%)
    (PAGE_POWER, 0x30): "input.voltage",             # Voltage (Volt)
    (PAGE_POWER, 0x40): "input.voltage.nominal",     # ConfigVoltage
    (PAGE_POWER, 0x65): "ups.status.bits",           # PresentStatus (bit field)
}

# PresentStatus 的位定义 (USB HID PDC 规范 1.0, Power Device -> Present Status)
STATUS_BITS = (
    (0, "off"), (1, "on"), (2, "overload"), (3, "boost"), (4, "buck"),
    (16, "acpresent"), (17, "charging"), (18, "discharging"),
    (19, "needreplacement"), (20, "belowremaincap"), (21, "runtime_low"),
    (22, "shutdown_imminent"),
)

# 把状态位翻译成 NUT 风格的 ups.status 串 (kiosk 只用到 OL/OB/LB/RB)
STATUS_FLAGS = {
    "acpresent": "OL", "discharging": "OB", "charging": "CHRG",
    "belowremaincap": "LB", "runtime_low": "LB",
    "needreplacement": "RB", "overload": "OVER",
}


# ---------------------------------------------------------------- 结构

class Field:
    """一个报告里的一个字段 (字节对齐也支持非对齐的 bit 偏移)."""

    __slots__ = ("report_id", "offset", "size", "usage_page", "usage", "kind",
                 "logmin", "logmax", "unit", "unit_exp", "variable", "constant")

    def __init__(self, report_id, offset, size, usage_page, usage, kind="feature",
                 logmin=0, logmax=0, unit=0, unit_exp=0, variable=True, constant=False):
        self.report_id = report_id
        self.kind = kind                # input / output / feature: 读取通道不同
        self.offset = offset            # 在报告数据里的 bit 位置
        self.size = size                # bit 宽度
        self.usage_page = usage_page
        self.usage = usage
        self.logmin = logmin
        self.logmax = logmax
        self.unit = unit
        self.unit_exp = unit_exp
        self.variable = variable
        self.constant = constant        # Padding / 常量填充, 不往外报

    def __repr__(self):
        return (f"Field(rid=0x{self.report_id:02x} off={self.offset} size={self.size} "
                f"page=0x{self.usage_page:02x} usage=0x{self.usage:02x})")


def iter_fields(fields):
    """parse_descriptor 的返回值; 兼容老式 list[list[Field]]."""
    for f in fields:
        if isinstance(f, list):
            yield from f
        else:
            yield f


# ---------------------------------------------------------------- 描述符解析

_SIZES = {0: 0, 1: 1, 2: 2, 3: 4}
_SIGNED = {1: True, 2: True, 4: True}


def _signed(data):
    """HID 的逻辑最大/最小是有符号小端."""
    return int.from_bytes(data, "little", signed=True)


def _unsigned(data):
    return int.from_bytes(data, "little", signed=False) if data else 0


def parse_descriptor(desc, wanted_pages=(PAGE_POWER, PAGE_BATTERY)):
    """report descriptor -> [[Field, ...], ...] (每个 Collection 一组, 顺手只留下我们关心的页).

    只处理 HID 短项; 长项整段跳过 (APC 的描述符里有, 但都不是我们关心的字段).
    padding (constant main item) 不产生字段, 但照样占位, 所以 bit 偏移会跳过它.
    """
    out = []
    page = rid = 0
    usages = []
    usage_min = None
    logmin = logmax = 0
    unit = unit_exp = 0
    rsize = rcount = 0
    rid_bits = 0                    # report id 占的位宽 (0 = 无 report id)
    bitpos = {}                     # report id -> 已分配的 bit 数

    i, n = 0, len(desc)
    while i < n:
        b = desc[i]
        if b == ITEM_LONG:
            if i + 1 >= n:
                break
            i += 3 + desc[i + 1]
            continue
        size = _SIZES[b & 0x03]
        data = desc[i + 1:i + 1 + size]
        tag, typ = b >> 4, (b >> 2) & 0x03

        if typ == TYPE_GLOBAL:
            if tag == 0x0:
                page = _unsigned(data)
            elif tag == 0x1:
                logmin = _signed(data)
            elif tag == 0x2:
                logmax = _signed(data)
            elif tag == 0x5:
                # HID 规范: Unit Exponent 是 4bit 补码 (0x8..0xF = -8..-1)
                unit_exp = _unsigned(data) & 0x0F
                if unit_exp > 7:
                    unit_exp -= 16
            elif tag == 0x6:
                unit = _unsigned(data)
            elif tag == 0x7:
                rsize = _unsigned(data)
            elif tag == 0x8:
                rid = _unsigned(data)
                rid_bits = size * 8
            elif tag == 0x9:
                rcount = _unsigned(data)
        elif typ == TYPE_LOCAL:
            if tag == 0x0:
                usages = [_unsigned(data)]
                usage_min = None
            elif tag == 0x1:
                usage_min = _unsigned(data)
                usages = []
            elif tag == 0x2:
                if usage_min is not None:
                    usages = list(range(usage_min, _unsigned(data) + 1))
                    usage_min = None
        else:
            if tag in (0x8, 0x9, 0xB):          # Input / Output / Feature
                bits = _unsigned(data)
                kind = {0x8: "input", 0x9: "output", 0xB: "feature"}[tag]
                _emit(out, rid, rid_bits, page, usages, rsize, rcount,
                      logmin, logmax, unit, unit_exp, bitpos, wanted_pages,
                      variable=bool(bits & 0x02), constant=bool(bits & 0x01),
                      kind=kind)
            usages = []
            usage_min = None
        i += 1 + size
    return out


def _emit(out, rid, rid_bits, page, usages, rsize, rcount, logmin, logmax,
          unit, unit_exp, bitpos, wanted_pages, variable, constant, kind):
    """给一个 Main item 分配位并生成字段; padding 只推进偏移."""
    if rsize <= 0:
        return
    count = rcount or 1
    offset = bitpos.get((kind, rid), rid_bits)
    for k in range(count):
        usage = usages[k] if k < len(usages) else (usages[-1] if usages else 0)
        if not constant and usages and page in wanted_pages:
            out.append(Field(rid, offset + k * rsize, rsize, page,
                             usage, kind, logmin, logmax, unit, unit_exp,
                             variable, constant))
    bitpos[(kind, rid)] = offset + count * rsize


# ---------------------------------------------------------------- 读写

def report_sizes(fields, kind=None):
    """report id -> 报告总位数 (含 padding). kind 可限定 input/feature."""
    sizes = {}
    for f in iter_fields(fields):
        if kind and f.kind != kind:
            continue
        end = f.offset + f.size
        sizes[f.report_id] = max(sizes.get(f.report_id, 0), end)
    return sizes


def report_bytes(sizes):
    """report id -> 报告字节数 (向上取整; APC 有 15 bit 这种非整字节的报告)."""
    return {rid: (bits + 7) // 8 for rid, bits in sizes.items()}


def extract(buf, offset, size):
    """从字节串里取 bit 字段 (HID 是小端 + LSB first)."""
    total = 0
    for k in range(size):
        bit = offset + k
        byte = bit // 8
        if byte >= len(buf):
            break
        if (buf[byte] >> (bit % 8)) & 1:
            total |= 1 << k
    return total


def put_bits(buf, offset, size, value):
    """extract 的反向操作, 测试里用来造报告."""
    for k in range(size):
        bit = offset + k
        byte = bit // 8
        if byte >= len(buf):
            return
        mask = 1 << (bit % 8)
        if (value >> k) & 1:
            buf[byte] |= mask
        else:
            buf[byte] &= ~mask


EXP_SANE = (-3, 3)          # 超出这个范围的指数当成厂商笔误 (APC 的 Voltage 报 7)


def scale(raw, unit_exp=0):
    """按 unit exponent 换成工程单位: value * 10**exp.

    APC Back-UPS 在这块板的 Voltage 上给了 unit exponent = 7 (2.32e9 伏),
    明显是笔误, 所以超出 ±3 的指数一律忽略, 按裸值用.
    """
    if not unit_exp or not (EXP_SANE[0] <= unit_exp <= EXP_SANE[1]):
        return raw
    return raw * (10.0 ** unit_exp)


def decode_report(fields, report_id, data, kind="feature"):
    """一个报告的数据 -> {标准名: 值}. 认不出的 usage 直接跳过."""
    out = {}
    for f in iter_fields(fields):
        if f.report_id != report_id or f.constant or f.kind != kind:
            continue
        name = NAMES.get((f.usage_page, f.usage))
        if not name:
            continue
        raw = extract(data, f.offset, f.size)
        if f.logmin < 0:                       # 有符号字段
            if raw & (1 << (f.size - 1)):
                raw -= 1 << f.size
        out[name] = scale(raw, f.unit_exp)
    return out


STATUS_SOURCES = (
    (PAGE_BATTERY, 0xD0, "acpresent"),      # ACPresent
    (PAGE_BATTERY, 0x44, "charging"),       # Charging
    (PAGE_BATTERY, 0x45, "discharging"),    # Discharging
    (PAGE_BATTERY, 0x4B, "needreplacement"),  # NeedReplacement
    (PAGE_BATTERY, 0x42, "belowremaincap"),  # BelowRemainingCapacityLimit
)


def status_flags(fields, data_by_rid, kind="feature"):
    """由 PDC 的布尔量拼 NUT 风格状态串, 例如 "OL" / "OB LB".

    APC Back-UPS 把 AC 在线/充电/放电做成独立的 1 bit Feature;
    Power Device 那个 PresentStatus 位域在这块板上恒 0, 不能用.
    """
    flags = set()
    for f in iter_fields(fields):
        if f.constant or f.kind != kind:
            continue
        for page, usage, name in STATUS_SOURCES:
            if (f.usage_page, f.usage) != (page, usage):
                continue
            if extract(data_by_rid.get(f.report_id, b""), f.offset, f.size):
                flags.add(STATUS_FLAGS.get(name, name))
    return " ".join(sorted(flags))


# ---------------------------------------------------------------- 设备 (上车才用)

def read_feature(fd, report_id, length):
    """HIDIOCGFEATURE: 返回 length 字节 (首字节是 report id). 需要 root 或 udev 属主."""
    buf = bytearray(length + 1)
    buf[0] = report_id
    fcntl.ioctl(fd, IOCTL_HIDIOCGFEATURE, buf)
    return bytes(buf[:length])


def descriptor_path(path, sysfs="/sys/class/hidraw"):
    """/dev/hidrawN -> 该设备的 report_descriptor 路径.

    /dev/hidrawN 不是指向 sysfs 的软链, 所以按设备名去 /sys/class/hidraw/<名>/device/ 找.
    """
    import os
    name = os.path.basename(path)
    cand = os.path.join(sysfs, name, "device", "report_descriptor")
    if os.path.exists(cand):
        return cand
    return os.path.join(os.path.realpath(path), "device", "report_descriptor")


def open_device(path):
    """读 sysfs 描述符 + 打开设备. 返回 (fields, fd, sizes).

    权限不够时抛 OSError, 调用方退回到 upower.
    """
    import os
    with open(descriptor_path(path), "rb") as f:
        desc = f.read()
    fields = parse_descriptor(desc)
    fd = os.open(path, os.O_RDONLY)
    return fields, fd, report_sizes(fields, kind="feature")


def read_all(fields, fd, sizes):
    """按 report id 各读一次, 合起来解码."""
    data = {}
    for rid, length in report_bytes(sizes).items():
        if length <= 0 or length > 256:
            continue
        data[rid] = read_feature(fd, rid, length)
    values = {}
    for rid, raw in data.items():
        values.update(decode_report(fields, rid, raw))
    status = status_flags(fields, data)
    if status:
        values["ups.status"] = status
    return values
