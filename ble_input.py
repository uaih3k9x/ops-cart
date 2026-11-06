#!/usr/bin/env python3
"""ble_input: 手机经蓝牙 BLE 给推车"隔空打字"的薄外壳 (#52). 逻辑在 ble_input_core.

BlueZ D-Bus 上注册一个 GATT 服务 + LE 广播: 手机往 RX 特征写分包, 这里重组后投到投递目标 (默认是
/run/ops-cart/ble-input/in/ 下一条一个 JSON 文件, root 0600), 由车机 / 大肥鱼去取; 回执、错误和
/run/ops-cart/ble-input/reply/ 下的回复摘要经 TX 特征 notify 回去. **不执行任何命令**.
契约: 文字只进输入框, 换行不得触发提交; 送往 HID 或串口必须再走现有危险操作确认.

RX 用 secure-write、TX 用 secure-notify: 要最高一级链路安全 (目标是 LE Secure Connections, 待实机验证); 外壳再查一遍
设备属性 (Paired + Trusted, 有 Bonded 时还要 Bonded), 并只认 org.bluez 属主的 D-Bus 调用. 启动时把适配器的
Pairable 关掉; bluetoothd 换了属主 (重启) 就退出, 交给 systemd 重拉.

依赖 python3-dbus、python3-gobject (推车上不一定装了); 没装时 import 本模块照样行, main() 说清楚缺什么.
配对 / trust 用 bluetoothctl 在车机上确认, 见 docs/BLE_INPUT.md.

用法: python3 ble_input.py [--adapter hci0] [--inbox DIR] [--reply DIR]
"""

import argparse
import json
import os
import random
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
TX_MTU = 23                 # 通知是广播, 分不了设备: 一律按最小 MTU 切, 哪台手机都收得下
BLUEZ = "org.bluez"
DEDUP = f"{BASE}/dedup.json"   # /run 下, 单元 RuntimeDirectoryPreserve: 服务重启不丢, 重启机器才丢 (monotonic 也跟着重来)
PAIRING = f"{BASE}/pairing"    # root touch 它之后 PAIR_WINDOW_S 内允许 Pairable 开着
PAIR_WINDOW_S = 180
PAIRABLE_CHECK_S = 10
FRESH_S = 300                  # 回执 / 回复只发给最近这么多秒内 secure-write 成功过的连接

_DEV_PATH = re.compile(r"/dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})$")


def dev_addr(path):
    m = _DEV_PATH.search(str(path or ""))
    return m.group(1).replace("_", ":").upper() if m else None


def addr_of(props, path):
    """设备的稳定身份: Device1 的 Address 属性 (SMP 解析出身份地址后 BlueZ 改它但不改对象路径; bluetoothd 重启后
    路径会换成身份地址, Address 不变). 读不到才退回对象路径里的地址. 记录、闸门、去重来源、审计都用它."""
    a = (props or {}).get("Address")
    return str(a).upper() if a else dev_addr(path)


def device_changed(handler, iface, changed, path, lookup):
    """org.bluez.Device1 的 PropertiesChanged: Connected 变 false 立刻删掉它的 secure-write 记录 (同地址冒充者
    重连时不能沿用). lookup(path) -> Device1 属性 (读不到 None)."""
    if iface != "org.bluez.Device1" or "Connected" not in changed or bool(changed["Connected"]):
        return
    try:
        props = lookup(path)
    except Exception:                       # noqa: BLE001
        props = None
    handler.disconnected(addr_of(props, path))


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
                                   "src": src, "id": msg_id, "text": text, "multiline": "\n" in text},
                                  mode=0o600, ensure_ascii=False)
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
    """认设备 -> 重组 -> 投递 -> 回执 -> 审计. notify(帧) 把一帧发出去; log(正文) 写审计;
    devices() 列出适配器上的设备 (BlueZ Device1 属性), 用来判断通知能不能发; dedup_path 是去重表落盘处."""

    def __init__(self, sink, notify, log, devices=None, dedup_path=None, clock=time.monotonic):
        self.sink, self.notify, self.log = sink, notify, log
        self.devices = devices or (lambda: [])
        self.dedup_path, self.clock = dedup_path, clock
        self.rx, self.tx = bc.Receiver(), bc.Sender(random.getrandbits(16))
        self.recent = {}            # 大写地址 -> 最近一次受信写入 (secure-write 能到这里就说明链路加密了)
        if dedup_path:
            try:
                with open(dedup_path, encoding="utf-8") as f:
                    self.rx.restore(json.load(f), clock())
            except (OSError, ValueError):
                pass

    def _send(self, payload):
        try:
            frames = self.tx.frames(payload, TX_MTU)
        except ValueError:
            return
        for f in frames:
            self.notify(f)

    def saw_secure_write(self, src, now=None):
        if src:
            self.recent[str(src).upper()] = self.clock() if now is None else now

    def disconnected(self, addr):
        if addr:
            self.recent.pop(str(addr).upper(), None)

    def _prune(self):
        """断开的设备清掉记录 (主路是 Device1 断开信号, 这里兜底). 返回设备列表; 读不到就全清, 返回 None."""
        try:
            devs = self.devices()
        except Exception:                   # noqa: BLE001 — 读不到设备列表就当有陌生人
            self.recent.clear()
            return None
        on = {str(d.get("Address") or "").upper() for d in devs if d.get("Connected") is True}
        for a in [a for a in self.recent if a not in on]:
            del self.recent[a]
        return devs

    def _all_trusted(self, now):
        """通知是广播: 连着的设备全受信、且本次连接内最近 FRESH_S 秒 secure-write 过, 才发回执 (带 crc,
        短文本可被反推) 和回复. 断开的设备清掉记录, 同地址再连上要重新写过才算."""
        devs = self._prune()
        return devs is not None and bc.may_notify(devs, self.recent, now, FRESH_S)

    def _save_dedup(self):
        if not self.dedup_path:
            return
        try:
            atomicfile.write_json(self.dedup_path, self.rx.export(), mode=0o600)
        except OSError:
            pass

    def _handle(self, evs, now):
        open_air = None
        for ev in evs:
            result = None
            if ev.kind == "text":
                if self.sink.put(ev.text, ev.src, ev.msg_id):
                    result = "queued"
                else:
                    self.rx.forget(ev)
                    ev = bc.Event("err", ev.src, ev.msg_id, size=ev.size, code="full", crc=ev.crc)
                self._save_dedup()
            if ev.code != "unpaired":       # unpaired 回的是陌生设备自己的帧, 照发, 它才知道要配对
                if open_air is None:
                    open_air = self._all_trusted(now)
                if not open_air:
                    result = (result or ev.kind) + ",receipt-withheld"
                    self.log(bc.audit(ev, result=result))
                    continue
            self.log(bc.audit(ev, result=result))
            self._send(bc.status_for(ev))

    def on_write(self, src, props, frame, now=None):
        now = self.clock() if now is None else now
        trusted = bc.device_trusted(props)
        if trusted:
            self.saw_secure_write(src, now)
        self._handle(self.rx.feed(src, frame, now, trusted), now)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        self._prune()                       # 每秒无条件剪一次, 不等闸门被调
        self._handle(self.rx.expire(now), now)

    def send_reply(self, text, now=None):
        if not self._all_trusted(self.clock() if now is None else now):
            self.log(cartlog.fields(kind="reply", result="dropped", reason="untrusted-connected",
                                    len=len(text.encode("utf-8"))))
            return False
        self._send(bc.reply(text))
        return True


# ------------------------------------------------------------ 适配器 / bluetoothd 看门

def pairing_open(path, now=None):
    """配对窗口: root 在运行目录 touch 了 pairing, 且没过 PAIR_WINDOW_S."""
    now = time.time() if now is None else now
    try:
        return 0 <= now - os.stat(path).st_mtime <= PAIR_WINDOW_S
    except OSError:
        return False


def ensure_not_pairable(get, set_, log, window=False):
    """推车平时不可配对: 启动时和之后每 PAIRABLE_CHECK_S 秒查一次, Pairable 开着 (又不在配对窗口里) 就关掉并记事件;
    关不掉记 WARN. 返回 ok / window / turned-off / warn."""
    if window:
        return "window"
    try:
        if not get("Pairable"):
            return "ok"
        set_("Pairable", False)
    except Exception as e:                  # noqa: BLE001 — D-Bus 错误种类多, 一律只告警不退出
        log(cartlog.fields(kind="adapter", result="WARN", reason="pairable-on", error=type(e).__name__))
        return "warn"
    log(cartlog.fields(kind="adapter", result="pairable-turned-off"))
    return "turned-off"


class OwnerWatch:
    """bus.watch_name_owner('org.bluez', ...) 的回调 (L6): 先报当前属主; 之后属主没了或换了就 on_lost()."""

    def __init__(self, on_lost):
        self.owner, self.on_lost = None, on_lost

    def __call__(self, owner):
        if self.owner is None and owner:
            self.owner = owner
        elif owner != self.owner:
            self.on_lost()


# ------------------------------------------------------------ BlueZ

def _load_dbus():
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
    return dbus, dbus.service, dbus.mainloop.glib, GLib


def _build(dbus, service, GLib, bus, adapter, handler_box):
    """GATT 应用 + 广播的 D-Bus 对象. 照 BlueZ test/example-gatt-server 的写法."""
    OM = "org.freedesktop.DBus.ObjectManager"
    PROPS, CHRC, SVC, ADV = ("org.freedesktop.DBus.Properties", "org.bluez.GattCharacteristic1",
                             "org.bluez.GattService1", "org.bluez.LEAdvertisement1")
    APP_PATH, SVC_PATH = "/org/opscart/ble", "/org/opscart/ble/service0"

    owner = [None]

    def device_props(path):
        try:
            obj = bus.get_object(BLUEZ, path)
            return {str(k): bool(v) if isinstance(v, dbus.Boolean) else v
                    for k, v in obj.GetAll("org.bluez.Device1", dbus_interface=PROPS).items()}
        except Exception:                   # noqa: BLE001 — 读不到属性一律当不受信
            return None

    def from_bluez(sender):
        """只认 org.bluez 的属主调用 (L3). 属主变了进程会退出 (OwnerWatch), 这里缓存一次就够."""
        if owner[0] is None:
            owner[0] = bus.get_name_owner(BLUEZ)
        if not sender or sender != owner[0]:
            raise dbus.exceptions.DBusException("只接受 BlueZ 的调用", name="org.bluez.Error.NotPermitted")

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

        @service.method(CHRC, in_signature="aya{sv}", sender_keyword="sender")
        def WriteValue(self, value, options, sender=None):
            from_bluez(sender)
            if self.uuid != bc.RX_UUID:
                raise dbus.exceptions.DBusException("只有 RX 能写", name="org.bluez.Error.NotSupported")
            if int(options.get("offset", 0) or 0) != 0:
                raise dbus.exceptions.DBusException("不支持偏移写", name="org.bluez.Error.InvalidOffset")
            path = options.get("device")
            props = device_props(path) if path else None
            handler_box[0].on_write(addr_of(props, path), props, bytes(value))

        @service.method(CHRC, sender_keyword="sender")
        def StartNotify(self, sender=None):
            from_bluez(sender)
            self.notifying = self.uuid == bc.TX_UUID

        @service.method(CHRC, sender_keyword="sender")
        def StopNotify(self, sender=None):
            from_bluez(sender)
            self.notifying = False

        @service.signal(PROPS, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        def push(self, frame):
            # 不看 notifying: BlueZ 的 StartNotify / StopNotify 计数不可靠 (已绑定设备断开不减, 恢复 CCC 也不调).
            # 发不发由 Handler 的闸门 (may_notify) 决定, 这里只交给 BlueZ, 它按各设备的 CCC 分发.
            self.PropertiesChanged(CHRC, {"Value": dbus.Array(frame, signature="y")}, [])

    # secure-*: 单独就设了 write / notify 属性并要最高一级链路安全 (目标 LE Secure Connections, 待实机验证);
    # "notify" 是多写的, 无害. 外壳再查一遍 Paired/Trusted/Bonded.
    rx = Chrc(0, bc.RX_UUID, ["secure-write"])
    tx = Chrc(1, bc.TX_UUID, ["notify", "secure-notify"])

    class Svc(service.Object):
        def __init__(self):
            service.Object.__init__(self, bus, SVC_PATH)

        @service.method(PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return {"UUID": bc.SERVICE_UUID, "Primary": True}

    class App(service.Object):
        def __init__(self):
            service.Object.__init__(self, bus, APP_PATH)
            self.svc, self.rx, self.tx = Svc(), rx, tx
            self.device_props = device_props

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
    box[0] = Handler(DirSink(a.inbox), notify=tx.push, log=lambda s: cartlog.event("ble-input", s),
                     devices=all_devices, dedup_path=DEDUP)
    os.makedirs(a.reply, mode=0o700, exist_ok=True)
    os.chmod(a.reply, 0o700)
    ad = bus.get_object(BLUEZ, f"/org/bluez/{a.adapter}")
    loop = GLib.MainLoop()
    adapter_props = dbus.Interface(ad, "org.freedesktop.DBus.Properties")
    def check_pairable():
        ensure_not_pairable(lambda k: adapter_props.Get("org.bluez.Adapter1", k),
                            lambda k, v: adapter_props.Set("org.bluez.Adapter1", k, dbus.Boolean(v)),
                            lambda s: cartlog.event("ble-input", s), window=pairing_open(PAIRING))
    check_pairable()

    def bluez_gone():
        print("ble_input: org.bluez 属主变了 (bluetoothd 重启?), 退出交给 systemd 重拉", file=sys.stderr)
        loop.quit()
    bus.watch_name_owner(BLUEZ, OwnerWatch(bluez_gone))
    bus.add_signal_receiver(
        lambda iface, changed, invalidated, path=None: device_changed(box[0], iface, changed, path, app.device_props),
        dbus_interface="org.freedesktop.DBus.Properties", signal_name="PropertiesChanged", bus_name=BLUEZ,
        path_keyword="path")

    def fail(what):
        return lambda e: (print(f"ble_input: {what} 失败: {e}", file=sys.stderr), loop.quit())

    ok = lambda what: lambda: print(f"ble_input: {what} 已注册", file=sys.stderr)    # noqa: E731
    dbus.Interface(ad, "org.bluez.GattManager1").RegisterApplication(
        app, {}, reply_handler=ok("GATT 服务"), error_handler=fail("注册 GATT 服务"))
    dbus.Interface(ad, "org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
        adv, {}, reply_handler=ok("广播"), error_handler=fail("注册广播"))

    n = [0]

    def tick():
        h = box[0]
        h.tick()
        text = take_reply(a.reply)
        if text:
            h.send_reply(text)
        n[0] += 1
        if n[0] % PAIRABLE_CHECK_S == 0:
            check_pairable()
        return True

    GLib.timeout_add_seconds(1, tick)
    loop.run()
    return 1


if __name__ == "__main__":
    sys.exit(main())
