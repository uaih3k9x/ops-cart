"""画面源 <-> 键鼠 (CH9329) 绑定. 键鼠按它的 CH343 串口芯片序列号认身份 (出厂唯一, 换口不变).

画面源 key 同 kiosk_core.video_nodes (SC0710 = PCI 插槽, USB = vid:pid).
没绑定或绑的那块不在线 = 只看不操作, 绝不退回"随便用一块" —— 插个 USB 摄像头点一下, 不该去动别的机器.
配置 /opt/pxe/kvm.json: {"names": {序列号: 名字}, "bind": {画面源 key: 序列号}}

IdleCh: 车机用的 CH9329 句柄, 有输入才开, 空闲就关 (放掉 flock), 远程命令行在人没动的时候能发键 (#9).
"""

import json
import os
import re
import time

import atomicfile
from ch9329 import Ch9329Busy

KVM_CONF = "/opt/pxe/kvm.json"
BYID_DIR = "/dev/serial/by-id"
IDLE_RELEASE_S = 3.0        # 键鼠空闲多久放锁, 同网页口 CH_IDLE_S
HOLD_MAX_S = 30.0           # 按着键/鼠标键也算在用, 但超过这么久没新输入照样放 (多半是松开事件丢了)
BUSY_NOTE_S = 1.0           # "键鼠被占用" 提示最多每秒一次
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


class IdleCh:
    """CH9329 懒打开 + 空闲放锁. 只在 ChWorker 线程里用 (所有方法都可能碰串口).

    opener() -> 已打开的 Ch9329. 第一次发键/鼠标才开; reap() 发现空闲够 idle_s 秒
    (有键或鼠标键按着就等 hold_max_s) 就先全部松开再关口, 下次输入再开.
    开的时候被别人占着 (Ch9329Busy): 这次输入直接丢, 不抛、不排队重放, on_busy() 限频提示;
    别的 OSError (拔线) 照常抛给 ChWorker."""

    def __init__(self, opener, on_busy=lambda: None, idle_s=IDLE_RELEASE_S, hold_max_s=HOLD_MAX_S,
                 busy_note_s=BUSY_NOTE_S, clock=time.monotonic):
        self.opener, self.on_busy, self.clock = opener, on_busy, clock
        self.idle_s, self.hold_max_s, self.busy_note_s = idle_s, hold_max_s, busy_note_s
        self.dev = None
        self._used = 0.0
        self._keys_held = self._btn_held = False
        self._busy_noted = None

    @property
    def is_open(self):
        return self.dev is not None

    def _get(self):
        if self.dev is None:
            try:
                self.dev = self.opener()
            except Ch9329Busy:
                now = self.clock()
                if self._busy_noted is None or now - self._busy_noted >= self.busy_note_s:
                    self._busy_noted = now
                    self.on_busy()
                return None
        self._used = self.clock()
        return self.dev

    def send_report(self, report):
        dev = self._get()
        if dev is not None:
            dev.send_report(report)
            self._keys_held = any(report[:1]) or any(report[2:])

    def send_mouse(self, buttons=0, dx=0, dy=0, wheel=0):
        dev = self._get()
        if dev is not None:
            dev.send_mouse(buttons, dx, dy, wheel)
            self._btn_held = bool(buttons & 0x07)

    def send_mouse_abs(self, buttons=0, x=0, y=0, wheel=0):
        dev = self._get()
        if dev is not None:
            dev.send_mouse_abs(buttons, x, y, wheel)
            self._btn_held = bool(buttons & 0x07)

    def release(self, abs_xy=None):
        """全部松开 (退出透传用). 已经放锁就不为它重开: 放锁前已经松过了."""
        if self.dev is None:
            return
        if abs_xy:
            self.dev.send_mouse_abs(0, abs_xy[0], abs_xy[1], 0)     # 光标留原地
        else:
            self.dev.send_mouse(0, 0, 0, 0)
        self.dev.send_report(bytes(8))
        self._keys_held = self._btn_held = False

    def reap(self):
        """空闲够了就放锁 -> 是否放了. ChWorker 里定时排一个 "reap" 进来调."""
        if self.dev is None:
            return False
        limit = self.hold_max_s if (self._keys_held or self._btn_held) else self.idle_s
        if self.clock() - self._used < limit:
            return False
        try:
            self.release()
        except (OSError, ValueError):
            pass
        self.close()
        return True

    def close(self):
        dev, self.dev = self.dev, None
        self._keys_held = self._btn_held = False
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass
