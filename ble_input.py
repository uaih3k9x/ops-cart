#!/usr/bin/env python3
"""ble_input: 手机经蓝牙 BLE 给推车"隔空打字"的薄外壳 (#52). 逻辑在 ble_input_core.

BlueZ D-Bus 上注册一个 GATT 服务 + LE 广播: 手机往 RX 特征写分包, 这里重组后投到投递目标 (默认是
/run/ops-cart/ble-input/in/ 下一条一个 JSON 文件, root 0600), 由车机 / 大肥鱼去取; 回执、错误和
/run/ops-cart/ble-input/reply/ 下的回复摘要经 TX 特征 notify 回去. **不执行任何命令**.

依赖 python3-dbus、python3-gobject (推车上不一定装了); 没装时 import 本模块照样行, main() 说清楚缺什么.
配对 / trust 用 bluetoothctl 在车机上确认, 见 docs/BLE_INPUT.md.

用法: python3 ble_input.py [--adapter hci0] [--inbox DIR] [--reply DIR]
"""

import argparse
import json
import os
import re
import sys
import time

import atomicfile
import ble_input_core as bc
import cartlog

BASE = "/run/ops-cart/ble-input"
INBOX = f"{BASE}/in"
REPLY = f"{BASE}/reply"
INBOX_MAX = 64              # 队列里最多压多少条没人取的
REPLY_READ_MAX = 64 * 1024
LOCAL_NAME = "ops-cart"
DEFAULT_MTU = 23

_DEV_PATH = re.compile(r"/dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})$")


def dev_addr(path):
    m = _DEV_PATH.search(str(path or ""))
    return m.group(1).replace("_", ":").upper() if m else None


# ------------------------------------------------------------ 投递目标 (可插拔: 任何有 put 的对象)

class DirSink:
    """一条一个 JSON 文件, 目录 0700 文件 0600. 满了 put 返回 False (手机收到 full), 不覆盖旧的."""

    def __init__(self, path, max_files=INBOX_MAX):
        self.path, self.max_files = path, max_files

    def _ready(self):
        os.makedirs(self.path, mode=0o700, exist_ok=True)
        os.chmod(self.path, 0o700)

    def put(self, text, src, msg_id, now=None):
        now = time.time() if now is None else now
        try:
            self._ready()
            if len([n for n in os.listdir(self.path) if n.endswith(".json")]) >= self.max_files:
                return False
            name = f"{int(now * 1000):013d}-{(src or 'unknown').replace(':', '')}-{msg_id:05d}.json"
            atomicfile.write_json(os.path.join(self.path, name),
                                  {"v": 1, "t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                                   "src": src, "id": msg_id, "text": text}, mode=0o600, ensure_ascii=False)
            return True
        except OSError:
            return False


def take_reply(path):
    """回复目录里最老的一条 (*.txt, 按文件名排序) -> 文字, 取完删掉. 没有 / 读不懂是 None."""
    try:
        names = sorted(n for n in os.listdir(path) if n.endswith(".txt"))
    except OSError:
        return None
    for n in names:
        p = os.path.join(path, n)
        try:
            with open(p, "rb") as f:
                raw = f.read(REPLY_READ_MAX)
            os.unlink(p)
        except OSError:
            continue
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


# ------------------------------------------------------------ D-Bus 拿来的东西串起来

class Handler:
    """认设备 -> 重组 -> 投递 -> 回执 -> 审计. notify(帧) 把一帧发出去; log(正文) 写审计."""

    def __init__(self, sink, notify, log):
        self.sink, self.notify, self.log = sink, notify, log
        self.rx, self.tx = bc.Receiver(), bc.Sender()
        self.mtu = DEFAULT_MTU

    def _send(self, payload):
        try:
            frames = self.tx.frames(payload, self.mtu)
        except ValueError:
            return
        for f in frames:
            self.notify(f)

    def _handle(self, evs):
        for ev in evs:
            result = None
            if ev.kind == "text":
                if self.sink.put(ev.text, ev.src, ev.msg_id):
                    result = "queued"
                else:
                    self.rx.forget(ev.src, ev.msg_id)
                    ev = bc.Event("err", ev.src, ev.msg_id, size=ev.size, code="full")
            self.log(bc.audit(ev, result=result))
            self._send(bc.status_for(ev))

    def on_write(self, src, props, frame, mtu=None, now=None):
        if mtu:
            self.mtu = int(mtu)
        now = time.time() if now is None else now
        self._handle(self.rx.feed(src, frame, now, bc.device_trusted(props)))

    def tick(self, now=None):
        self._handle(self.rx.expire(time.time() if now is None else now))

    def send_reply(self, text, devices):
        if not bc.may_broadcast(devices):
            self.log(cartlog.fields(kind="reply", result="dropped", reason="untrusted-connected",
                                    len=len(text.encode("utf-8"))))
            return False
        self._send(bc.reply(text))
        return True


# ------------------------------------------------------------ BlueZ

def _load_dbus():
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
    return dbus, dbus.service, dbus.mainloop.glib, GLib


def _build(dbus, service, GLib, bus, adapter, handler_box):
    """GATT 应用 + 广播的 D-Bus 对象. 照 BlueZ test/example-gatt-server 的写法."""
    BLUEZ, OM = "org.bluez", "org.freedesktop.DBus.ObjectManager"
    PROPS, CHRC, SVC, ADV = ("org.freedesktop.DBus.Properties", "org.bluez.GattCharacteristic1",
                             "org.bluez.GattService1", "org.bluez.LEAdvertisement1")
    APP_PATH, SVC_PATH = "/org/opscart/ble", "/org/opscart/ble/service0"

    def device_props(path):
        try:
            obj = bus.get_object(BLUEZ, path)
            return {str(k): bool(v) if isinstance(v, dbus.Boolean) else v
                    for k, v in obj.GetAll("org.bluez.Device1", dbus_interface=PROPS).items()}
        except dbus.DBusException:
            return None

    def all_devices():
        om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM)
        return [{str(k): bool(v) if isinstance(v, dbus.Boolean) else v for k, v in ifs["org.bluez.Device1"].items()}
                for p, ifs in om.GetManagedObjects().items()
                if "org.bluez.Device1" in ifs and str(p).startswith(f"/org/bluez/{adapter}/")]

    class Chrc(service.Object):
        def __init__(self, idx, uuid, flags):
            self.path = f"{SVC_PATH}/char{idx}"
            self.uuid, self.flags, self.notifying = uuid, flags, False
            service.Object.__init__(self, bus, self.path)

        def props(self):
            return {CHRC: {"Service": dbus.ObjectPath(SVC_PATH), "UUID": self.uuid,
                           "Flags": dbus.Array(self.flags, signature="s")}}

        @service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return self.props()[CHRC]

        @service.method(CHRC, in_signature="aya{sv}")
        def WriteValue(self, value, options):
            if self.uuid != bc.RX_UUID:
                raise dbus.exceptions.DBusException("org.bluez.Error.NotSupported")
            path = options.get("device")
            handler_box[0].on_write(dev_addr(path), device_props(path) if path else None,
                                    bytes(value), mtu=options.get("mtu"))

        @service.method(CHRC)
        def StartNotify(self):
            self.notifying = self.uuid == bc.TX_UUID

        @service.method(CHRC)
        def StopNotify(self):
            self.notifying = False

        @service.signal(PROPS, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        def push(self, frame):
            if self.notifying:
                self.PropertiesChanged(CHRC, {"Value": dbus.Array(frame, signature="y")}, [])

    # 写要求"认证过的加密" (要配对且 MITM 保护), BlueZ 在 ATT 层就拒; 外壳再查一遍 Bonded/Trusted.
    rx = Chrc(0, bc.RX_UUID, ["encrypt-authenticated-write"])
    tx = Chrc(1, bc.TX_UUID, ["notify", "encrypt-authenticated-notify"])

    class Svc(service.Object):
        def __init__(self):
            service.Object.__init__(self, bus, SVC_PATH)

        @service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return {"UUID": bc.SERVICE_UUID, "Primary": True}

    class App(service.Object):
        def __init__(self):
            service.Object.__init__(self, bus, APP_PATH)
            self.svc = Svc()

        @service.method(OM, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            return {dbus.ObjectPath(SVC_PATH): {SVC: {"UUID": bc.SERVICE_UUID, "Primary": True}},
                    dbus.ObjectPath(rx.path): rx.props(), dbus.ObjectPath(tx.path): tx.props()}

    class Adv(service.Object):
        PATH = "/org/opscart/ble/adv0"

        def __init__(self):
            service.Object.__init__(self, bus, self.PATH)

        @service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return {"Type": "peripheral", "ServiceUUIDs": dbus.Array([bc.SERVICE_UUID], signature="s"),
                    "LocalName": LOCAL_NAME}

        @service.method(ADV)
        def Release(self):
            pass

    return App(), Adv(), tx, all_devices


def main(argv=None):
    ap = argparse.ArgumentParser(description="手机经 BLE 给推车打字 (#52)")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--inbox", default=INBOX)
    ap.add_argument("--reply", default=REPLY)
    a = ap.parse_args(argv)
    try:
        dbus, service, glib_loop, GLib = _load_dbus()
    except ImportError as e:
        print(f"ble_input: 缺依赖 ({e}). 推车上装: dnf install python3-dbus python3-gobject bluez", file=sys.stderr)
        return 2
    glib_loop.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    box = [None]
    app, adv, tx, all_devices = _build(dbus, service, GLib, bus, a.adapter, box)
    box[0] = Handler(DirSink(a.inbox), notify=tx.push, log=lambda s: cartlog.event("ble-input", s))
    os.makedirs(a.reply, mode=0o700, exist_ok=True)
    os.chmod(a.reply, 0o700)
    ad = bus.get_object("org.bluez", f"/org/bluez/{a.adapter}")
    loop = GLib.MainLoop()

    def fail(what):
        return lambda e: (print(f"ble_input: {what} 失败: {e}", file=sys.stderr), loop.quit())

    ok = lambda what: lambda: print(f"ble_input: {what} 已注册", file=sys.stderr)    # noqa: E731
    dbus.Interface(ad, "org.bluez.GattManager1").RegisterApplication(
        app, {}, reply_handler=ok("GATT 服务"), error_handler=fail("注册 GATT 服务"))
    dbus.Interface(ad, "org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
        adv, {}, reply_handler=ok("广播"), error_handler=fail("注册广播"))

    def tick():
        h = box[0]
        h.tick()
        text = take_reply(a.reply)
        if text:
            try:
                h.send_reply(text, all_devices())
            except dbus.DBusException as e:
                print(f"ble_input: 读设备列表失败, 回复丢弃: {e}", file=sys.stderr)
        return True

    GLib.timeout_add_seconds(1, tick)
    loop.run()
    return 1


if __name__ == "__main__":
    sys.exit(main())
