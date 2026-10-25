"""画面源 <-> 键鼠 (CH9329) 绑定. 键鼠按它的 CH343 串口芯片序列号认身份 (出厂唯一, 换口不变).

画面源 key 同 kiosk_core.video_nodes (SC0710 = PCI 插槽, USB = vid:pid).
没绑定或绑的那块不在线 = 只看不操作, 绝不退回"随便用一块" —— 插个 USB 摄像头点一下, 不该去动别的机器.
配置 /opt/pxe/kvm.json: {"names": {序列号: 名字}, "bind": {画面源 key: 序列号}}
"""

import json
import os
import re

import atomicfile

KVM_CONF = "/opt/pxe/kvm.json"
BYID_DIR = "/dev/serial/by-id"
_CH343 = re.compile(r"usb-1a86_.*_([0-9A-Za-z]+)-if00$")      # 序列号是 -if00 前最后一段


def ports_from_byid(names, byid_dir=BYID_DIR):
    """by-id 文件名 -> {序列号: 路径}, 只收 1a86 (沁恒 CH343) 的串口."""
    out = {}
    for n in names:
        m = _CH343.match(n)
        if m:
            out[m.group(1)] = os.path.join(byid_dir, n)
    return out


def ports(byid_dir=BYID_DIR):
    try:
        return ports_from_byid(sorted(os.listdir(byid_dir)), byid_dir)
    except OSError:
        return {}


def load(path=KVM_CONF):
    try:
        with open(path) as f:
            conf = json.load(f)
    except (OSError, ValueError):
        conf = {}
    return {"names": conf.get("names", {}), "bind": conf.get("bind", {})}


def save(conf, path=KVM_CONF):
    atomicfile.write_json(path, conf, indent=1)


def label(conf, serial):
    if not serial:
        return "不绑定"
    return conf.get("names", {}).get(serial) or f"键鼠 …{serial[-4:]}"


def bind(conf, video_key, serial):
    """返回新配置; serial=None 解绑."""
    b = dict(conf.get("bind", {}))
    if serial:
        b[video_key] = serial
    else:
        b.pop(video_key, None)
    return {**conf, "bind": b}


def resolve(conf, video_key, online):
    """-> (串口路径, 序列号, 状态). 状态 ok / unbound (没绑, 只看) / offline (绑了但不在, 只看)."""
    serial = conf.get("bind", {}).get(video_key) if video_key else None
    if not serial:
        return None, None, "unbound"
    if serial not in online:
        return None, serial, "offline"
    return online[serial], serial, "ok"


def next_choice(serials, current):
    """「键鼠绑定」按钮轮换: 不绑定 -> 第一块 -> 第二块 -> ... -> 不绑定."""
    order = [None] + list(serials)
    return order[(order.index(current) + 1) % len(order)] if current in order else None


def shared_with(conf, serial, video_key):
    """同一块键鼠还绑在哪些别的画面源上 (同一台机器从两路采集看, 是合理的; 只是提示)."""
    return [k for k, s in conf.get("bind", {}).items() if s == serial and k != video_key]
