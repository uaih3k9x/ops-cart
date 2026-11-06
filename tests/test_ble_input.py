"""ble_input 薄外壳测试 (#52): 没装 dbus 也能 import、缺依赖时说清楚; 投递目录 (DirSink) 权限和上限;
回复目录取件; 设备路径解析; systemd 单元草稿."""

import json
import os
import pathlib
import stat
import time

import pytest

import ble_input as bi
import ble_input_core as bc

DEV = "02:00:00:00:00:01"
CONN_OK = {"Address": DEV, "Bonded": True, "Paired": True, "Trusted": True, "Connected": True}
OK_DEVS = lambda: [CONN_OK]                                          # noqa: E731
UNIT = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "cart-ble-input.service"


def test_import_without_dbus_and_main_explains(monkeypatch, capsys):
    monkeypatch.setattr(bi, "_load_dbus", lambda: (_ for _ in ()).throw(ImportError("No module named 'dbus'")))
    assert bi.main([]) == 2
    msg = capsys.readouterr().err
    assert "python3-dbus" in msg and "python3-gobject" in msg


def test_device_address_from_path():
    assert bi.dev_addr("/org/bluez/hci0/dev_02_00_00_00_00_01") == DEV
    assert bi.dev_addr("/org/bluez/hci0") is None
    assert bi.dev_addr(None) is None


def test_dirsink_writes_0600_in_0700_dir(tmp_path):
    d = tmp_path / "in"
    s = bi.DirSink(str(d), max_files=3)
    assert s.put("你好 🚀", DEV, 5, now=1_700_000_000.0) is True
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    (f,) = list(d.iterdir())
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert doc["text"] == "你好 🚀" and doc["src"] == DEV and doc["id"] == 5 and doc["v"] == 1
    assert not any(p.name.endswith(".tmp") for p in d.iterdir())


def test_dirsink_tightens_existing_dir_and_caps(tmp_path):
    d = tmp_path / "in"
    d.mkdir(mode=0o755)
    s = bi.DirSink(str(d), max_files=2)
    assert s.put("a", DEV, 1, now=1.0) and s.put("b", DEV, 2, now=2.0)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert s.put("c", DEV, 3, now=3.0) is False                    # 满了不吞, 回 full 让手机知道
    assert len(list(d.iterdir())) == 2


def test_take_reply_oldest_first_and_removes(tmp_path):
    d = tmp_path / "reply"
    d.mkdir()
    (d / "2.txt").write_text("第二", encoding="utf-8")
    (d / "1.txt").write_text("第一", encoding="utf-8")
    (d / "x.tmp").write_text("写到一半", encoding="utf-8")
    assert bi.take_reply(str(d)) == "第一"
    assert bi.take_reply(str(d)) == "第二"
    assert bi.take_reply(str(d)) is None
    assert (d / "x.tmp").exists()
    assert bi.take_reply(str(tmp_path / "missing")) is None


def test_take_reply_skips_unreadable_bytes(tmp_path):
    d = tmp_path / "reply"
    d.mkdir()
    (d / "1.txt").write_bytes(b"\xff\xfe")
    assert bi.take_reply(str(d)) is None and not (d / "1.txt").exists()


def test_handler_flow_with_fake_parts(tmp_path):
    """外壳里把 D-Bus 拿到的东西串起来那段: 认设备 -> 重组 -> 投递 -> 回执 -> 审计."""
    sent, logged = [], []
    sink = bi.DirSink(str(tmp_path / "in"), max_files=8)
    h = bi.Handler(sink, notify=sent.append, log=logged.append, devices=OK_DEVS)
    trusted = {"Bonded": True, "Paired": True, "Trusted": True}
    for f in bc.encode(3, "隔空打字".encode(), 23):
        h.on_write(DEV, trusted, f, now=10.0)
    rx = bc.Receiver()
    acks = [e for fr in sent for e in rx.feed("cart", fr, 10.0, True)]
    assert [json.loads(e.text) for e in acks] == [{"t": "ack", "id": 3, "crc": bc.crc32("隔空打字".encode())}]
    assert len(list((tmp_path / "in").iterdir())) == 1
    assert len(logged) == 1 and "隔空打字" not in logged[0] and "len=12" in logged[0]

    sent.clear()
    for f in bc.encode(4, b"x", 23):
        h.on_write(DEV, {"Paired": False}, f, now=11.0)
    assert [json.loads(e.text)["code"] for fr in sent for e in rx.feed("cart", fr, 11.0, True)] == ["unpaired"]
    assert len(list((tmp_path / "in").iterdir())) == 1


def test_handler_full_sink_reports_full(tmp_path):
    sent = []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in"), max_files=0), notify=sent.append, log=lambda s: None, devices=OK_DEVS)
    for f in bc.encode(3, b"x", 23):
        h.on_write(DEV, {"Bonded": True, "Paired": True, "Trusted": True}, f, now=1.0)
    rx = bc.Receiver()
    assert [json.loads(e.text)["code"] for fr in sent for e in rx.feed("c", fr, 1.0, True)] == ["full"]


def test_unit_draft_hardened_not_enabled_by_tests():
    u = UNIT.read_text()
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ExecStart=/usr/bin/python3 /opt/pxe/ble_input.py" in body
    assert "After=bluetooth.service" in body
    for k in ("NoNewPrivileges=yes", "ProtectSystem=strict", "RuntimeDirectory=ops-cart/ble-input ",
              "RuntimeDirectoryMode=0700", "ReadWritePaths=/var/log/ops-cart",
              "UMask=0077", "RestrictAddressFamilies=AF_UNIX"):
        assert k in body, k
    assert "kiosk.service" not in body


# ------------------------------------------------------------ 审查意见 (#61)

TRUSTED = {"Bonded": True, "Paired": True, "Trusted": True}


def test_delivered_json_fields_locked(tmp_path):
    """M3: 投递 JSON 的字段是契约, 取件方据此只把文字放进输入框; multiline 提醒换行不得触发提交."""
    s = bi.DirSink(str(tmp_path / "in"))
    assert s.put("第一行\n第二行", DEV, 5, now=1_700_000_000.0)
    assert s.put("一行", DEV, 6, now=1_700_000_001.0)
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((tmp_path / "in").iterdir())]
    assert [sorted(d) for d in docs] == [["id", "multiline", "src", "t", "text", "v"]] * 2
    assert [d["multiline"] for d in docs] == [True, False]
    assert docs[0]["v"] == 1


def test_contract_written_in_doc_and_module():
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "BLE_INPUT.md").read_text(encoding="utf-8")
    rule = "文本只进输入框，\\n 不得触发提交；送往 HID 或串口必须再走现有危险操作确认"
    assert rule in doc
    assert "换行不得触发提交" in bc.__doc__ and "危险操作确认" in bc.__doc__


def _acks(sent, now=1.0):
    rx = bc.Receiver()
    return [json.loads(e.text) for fr in sent for e in rx.feed("cart", fr, now, True)]


def test_handler_notifies_with_fixed_small_frames_and_untrusted_cannot_change_anything(tmp_path):
    """P3 / L1: 通知是广播, 发给所有订阅者; 一律按 MTU 23 切, 不受某台设备 (尤其不受信的) 报的 MTU 影响."""
    sent = []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=lambda s: None, devices=OK_DEVS)
    h.on_write("02:00:00:00:00:09", None, b"\x01\x00\x00", now=1.0)
    for f in bc.encode(3, "隔空打字".encode(), 517):
        h.on_write(DEV, TRUSTED, f, now=1.0)
    assert sent and max(len(f) for f in sent) <= bi.TX_MTU - bc.ATT_OVERHEAD == 20
    assert _acks(sent)[-1] == {"t": "ack", "id": 3, "crc": bc.crc32("隔空打字".encode())}


def test_handler_full_keeps_crc_and_forgets(tmp_path):
    sent = []
    sink = bi.DirSink(str(tmp_path / "in"), max_files=0)
    h = bi.Handler(sink, notify=sent.append, log=lambda s: None, devices=OK_DEVS)
    (f,) = bc.encode(3, b"x", 23)
    h.on_write(DEV, TRUSTED, f, now=1.0)
    assert _acks(sent) == [{"t": "err", "id": 3, "crc": bc.crc32(b"x"), "code": "full"}]
    sink.max_files = 8
    sent.clear()
    h.on_write(DEV, TRUSTED, f, now=2.0)                              # 同一条重发能进
    assert _acks(sent) == [{"t": "ack", "id": 3, "crc": bc.crc32(b"x")}]


def test_handler_tx_ids_start_random():
    ids = {bi.Handler(None, notify=None, log=None).tx.next_id for _ in range(8)}
    assert len(ids) > 1                                               # 推车重启后序号不从 0 撞手机的旧记录


# ------------------------------------------------------------ 假 dbus: 测 _build / WriteValue 这层 (L2 / L3 / L7)

class _FakeDBusException(Exception):
    def __init__(self, *args, name=None):
        super().__init__(*args)
        self.name = name

    def get_dbus_name(self):
        return self.name


class _FakeDbus:
    DBusException = _FakeDBusException
    exceptions = type("exceptions", (), {"DBusException": _FakeDBusException})

    class Boolean(int):
        pass

    @staticmethod
    def Array(v, signature=None):
        return list(v)

    @staticmethod
    def ObjectPath(p):
        return str(p)


class _FakeService:
    class Object:
        def __init__(self, bus, path):
            self._path = path

    @staticmethod
    def method(iface, **kw):
        def deco(f):
            f._dbus = (iface, kw)
            return f
        return deco

    @staticmethod
    def signal(iface, **kw):
        def deco(f):
            def emit(self, *a):
                self._emitted = getattr(self, "_emitted", []) + [a]
            emit._dbus = (iface, kw)
            return emit
        return deco


class _FakeBus:
    def __init__(self, devices, owner=":1.7"):
        self.devices, self.owner = devices, owner

    def get_name_owner(self, name):
        assert name == "org.bluez"
        return self.owner

    def get_object(self, name, path):
        bus = self

        class Obj:
            def GetAll(self, iface, dbus_interface=None):
                d = bus.devices[path]
                if isinstance(d, Exception):
                    raise d
                return d
        return Obj()


class _Recorder:
    def __init__(self):
        self.calls = []

    def on_write(self, src, props, frame, now=None):
        self.calls.append((src, props, frame))


def _built(devices=None, owner=":1.7"):
    rec = _Recorder()
    bus = _FakeBus(devices or {}, owner=owner)
    app, adv, tx, _all = bi._build(_FakeDbus, _FakeService, None, bus, "hci0", [rec])
    objs = app.GetManagedObjects()
    rx = next(o for o in [app.rx, app.tx] if o.uuid == bc.RX_UUID)
    return rec, app, rx, tx, objs


def test_build_flags_require_le_secure_connections():
    """L2: RX 只能 secure-write、TX 用 secure-notify (BlueZ 要最高一级链路安全)."""
    _rec, _app, rx, tx, objs = _built()
    flags = {o["org.bluez.GattCharacteristic1"]["UUID"]: o["org.bluez.GattCharacteristic1"]["Flags"]
             for o in objs.values() if "org.bluez.GattCharacteristic1" in o}
    assert flags[bc.RX_UUID] == ["secure-write"]
    assert flags[bc.TX_UUID] == ["notify", "secure-notify"]
    src = (pathlib.Path(__file__).resolve().parent.parent / "ble_input.py").read_text(encoding="utf-8")
    assert "encrypt-authenticated" not in src                          # L7: 注释和 flags 一致


def test_write_value_methods_take_sender():
    _rec, _app, rx, _tx, _objs = _built()
    for name in ("WriteValue", "StartNotify", "StopNotify"):
        assert getattr(type(rx), name)._dbus[1].get("sender_keyword") == "sender", name


def test_write_value_from_bluez_reaches_handler():
    path = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    rec, _app, rx, _tx, _objs = _built({path: {"Paired": _FakeDbus.Boolean(1), "Trusted": True}})
    rx.WriteValue([1, 2, 3], {"device": path, "mtu": 517}, sender=":1.7")
    assert rec.calls == [(DEV, {"Paired": True, "Trusted": True}, b"\x01\x02\x03")]


def test_write_value_from_other_caller_refused():
    """L3: 只认 org.bluez 的属主调用; 本机别的进程冒充 BlueZ 调 WriteValue 抛 NotPermitted."""
    path = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    rec, _app, rx, tx, _objs = _built({path: TRUSTED})
    for bad in (":1.99", None, ""):
        with pytest.raises(_FakeDBusException) as ei:
            rx.WriteValue([1], {"device": path}, sender=bad)
        assert ei.value.name == "org.bluez.Error.NotPermitted"
        with pytest.raises(_FakeDBusException):
            tx.StartNotify(sender=bad)
    assert rec.calls == [] and tx.notifying is False


def test_write_value_without_device_or_props_failure_is_untrusted():
    path = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    rec, _app, rx, _tx, _objs = _built({path: _FakeDBusException("gone")})
    rx.WriteValue([1], {}, sender=":1.7")
    rx.WriteValue([1], {"device": path}, sender=":1.7")
    assert rec.calls == [(None, None, b"\x01"), (DEV, None, b"\x01")]
    rec2, _app, rx2, _tx, _objs = _built({path: RuntimeError("boom")})
    rx2.WriteValue([1], {"device": path}, sender=":1.7")
    assert rec2.calls == [(DEV, None, b"\x01")]


def test_write_on_tx_not_supported():
    _rec, _app, _rx, tx, _objs = _built()
    with pytest.raises(_FakeDBusException) as ei:
        tx.WriteValue([1], {}, sender=":1.7")
    assert ei.value.name == "org.bluez.Error.NotSupported"


def test_push_does_not_depend_on_notifying_state():
    """BlueZ 的 StartNotify / StopNotify 计数不可靠 (已绑定设备断开不减), 外壳不靠它判断有无订阅者;
    能不能发由 Handler 的闸门决定, push 只管交给 BlueZ."""
    _rec, _app, _rx, tx, _objs = _built()
    tx.push(b"\x01")
    tx.StartNotify(sender=":1.7")
    tx.StopNotify(sender=":1.7")
    tx.push(b"\x02")
    assert [a[1]["Value"] for a in tx._emitted] == [[1], [2]]


# ------------------------------------------------------------ L5 / L6

def test_pairable_turned_off_at_start():
    state, logged = {"Pairable": True}, []
    assert bi.ensure_not_pairable(state.get, state.__setitem__, logged.append) == "turned-off"
    assert state["Pairable"] is False and "pairable" in logged[0]
    assert bi.ensure_not_pairable(state.get, state.__setitem__, logged.append) == "ok"
    assert len(logged) == 1


def test_pairable_set_failure_warns():
    logged = []

    def boom(k, v):
        raise _FakeDBusException("denied")
    assert bi.ensure_not_pairable({"Pairable": True}.get, boom, logged.append) == "warn"
    assert "WARN" in logged[0]


def test_bluez_owner_watch_quits_on_change():
    quits = []
    w = bi.OwnerWatch(lambda: quits.append(1))
    w(":1.7")                                                         # watch_name_owner 先报当前属主
    assert quits == [] and w.owner == ":1.7"
    w(":1.7")
    assert quits == []
    w("")                                                             # bluetoothd 没了
    assert quits == [1]
    w2 = bi.OwnerWatch(lambda: quits.append(2))
    w2(":1.7")
    w2(":1.8")                                                        # 换人了
    assert quits == [1, 2]


def test_unit_binds_to_bluetooth():
    body = "\n".join(l for l in UNIT.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert "BindsTo=bluetooth.service" in body


# ------------------------------------------------------------ 第二轮审查 (#61)

STRANGER = {"Paired": False, "Connected": True}


def test_send_reply_checks_connected_devices():
    """3: send_reply 的受信闸门要接上: 有陌生设备连着就不发回复正文."""
    sent, logged = [], []
    devs = [[CONN_OK, STRANGER]]
    h = bi.Handler(None, notify=sent.append, log=logged.append, devices=lambda: devs[0])
    assert h.send_reply("机密回复") is False and sent == []
    assert "dropped" in logged[-1]
    devs[0] = [CONN_OK]
    h.saw_secure_write(DEV)
    assert h.send_reply("好的") is True and sent


def test_receipts_withheld_while_stranger_connected(tmp_path):
    """6: 回执带 crc, 短文本可被反推; 有不受信设备连着时不发回执 (unpaired 除外, 那是陌生设备自己的帧)."""
    sent, logged = [], []
    devs = [[CONN_OK, STRANGER]]
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=logged.append, devices=lambda: devs[0])
    for f in bc.encode(3, b"1234", 23):
        h.on_write(DEV, TRUSTED, f, now=1.0)
    assert sent == [] and len(list((tmp_path / "in").iterdir())) == 1  # 照样投递, 只是不回执
    assert "withheld" in logged[-1]
    h.on_write("02:00:00:00:00:09", None, bc.encode(4, b"x", 23)[0], now=1.0)
    assert [d["code"] for d in _acks(sent)] == ["unpaired"]
    sent.clear()
    devs[0] = [CONN_OK]
    for f in bc.encode(3, b"1234", 23):                               # 陌生设备走了, 手机重发拿到 dup 回执
        h.on_write(DEV, TRUSTED, f, now=2.0)
    assert _acks(sent) == [{"t": "ack", "id": 3, "crc": bc.crc32(b"1234"), "dup": True}]


def test_receipts_withheld_when_device_list_fails(tmp_path):
    sent = []

    def boom():
        raise RuntimeError("dbus")
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=lambda s: None, devices=boom)
    for f in bc.encode(3, b"x", 23):
        h.on_write(DEV, TRUSTED, f, now=1.0)
    assert sent == []


def test_reply_text_cleaned_of_invisible_chars():
    """5: 回复正文也过一遍: 零宽 / U+2028 / 控制字符去掉, 换行留着."""
    sent = []
    h = bi.Handler(None, notify=sent.append, log=lambda s: None, devices=OK_DEVS)
    h.saw_secure_write(DEV)
    h.send_reply("好​的 \u001b[2J\n✓ 已送达")
    (d,) = _acks(sent)
    assert d == {"t": "reply", "text": "好的[2J\n✓ 已送达"}


def test_handler_uses_monotonic_clock(monkeypatch, tmp_path):
    """7: 半包和去重时效用 monotonic, 墙上时间被调也不乱."""
    sent = []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=lambda s: None, devices=OK_DEVS)
    assert h.clock is time.monotonic
    clock = [100.0]
    h.clock = lambda: clock[0]
    frames = bc.encode(5, b"y" * 50, 23)
    h.on_write(DEV, TRUSTED, frames[0])
    clock[0] += bc.PARTIAL_TTL_S + 1
    h.tick()
    assert [d["code"] for d in _acks(sent)] == ["timeout"]


def test_dedup_survives_restart_via_run_file(tmp_path):
    """8: 去重表落盘到 /run (单元 RuntimeDirectoryPreserve), 服务重启后手机重发不重复投递."""
    path = tmp_path / "dedup.json"
    inbox = tmp_path / "in"
    frames = bc.encode(7, "只投一次".encode(), 23)
    h = bi.Handler(bi.DirSink(str(inbox)), notify=lambda f: None, log=lambda s: None, devices=OK_DEVS,
                   dedup_path=str(path))
    for f in frames:
        h.on_write(DEV, TRUSTED, f, now=50.0)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    sent = []
    h2 = bi.Handler(bi.DirSink(str(inbox)), notify=sent.append, log=lambda s: None, devices=OK_DEVS,
                    dedup_path=str(path), clock=lambda: 55.0)       # monotonic 跨进程同一个钟
    for f in frames:
        h2.on_write(DEV, TRUSTED, f, now=60.0)
    assert len(list(inbox.iterdir())) == 1
    assert _acks(sent)[0].get("dup") is True
    path.write_text("{坏的")                                          # 坏文件不影响启动
    bi.Handler(None, notify=None, log=lambda s: None, dedup_path=str(path))


def test_write_value_nonzero_offset_refused():
    """4: 我们不支持长写 / 偏移写."""
    path = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    rec, _app, rx, _tx, _objs = _built({path: TRUSTED})
    with pytest.raises(_FakeDBusException) as ei:
        rx.WriteValue([1], {"device": path, "offset": 3}, sender=":1.7")
    assert ei.value.name == "org.bluez.Error.InvalidOffset" and rec.calls == []
    rx.WriteValue([1], {"device": path, "offset": 0}, sender=":1.7")
    assert len(rec.calls) == 1


def test_pairable_allowed_inside_pairing_window(tmp_path):
    """9: tick 里也查 Pairable; 只有 root 在运行目录 touch 了 pairing 且没过窗口时才放着."""
    marker = tmp_path / "pairing"
    assert bi.pairing_open(str(marker), now=1000.0) is False
    marker.write_text("")
    os.utime(marker, (1000.0, 1000.0))
    assert bi.pairing_open(str(marker), now=1000.0 + bi.PAIR_WINDOW_S - 1) is True
    assert bi.pairing_open(str(marker), now=1000.0 + bi.PAIR_WINDOW_S + 1) is False
    state, logged = {"Pairable": True}, []
    assert bi.ensure_not_pairable(state.get, state.__setitem__, logged.append, window=True) == "window"
    assert state["Pairable"] is True and logged == []
    assert bi.ensure_not_pairable(state.get, state.__setitem__, logged.append, window=False) == "turned-off"


def test_unit_restarts_always_and_follows_bluetooth():
    """10: bluetoothd 停了跟着停, 起来了跟着起 (WantedBy=bluetooth.service); 自己退出也重拉."""
    u = UNIT.read_text()
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "Restart=always" in body and "Restart=on-failure" not in body
    assert "WantedBy=bluetooth.service" in body


def test_core_comment_matches_flags():
    src = (pathlib.Path(__file__).resolve().parent.parent / "ble_input_core.py").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.startswith("RX_UUID"))
    assert "secure-write" in line and "without response" not in line


# ------------------------------------------------------------ 只发给本次连接里刚 secure-write 过的设备

OTHER = "02:00:00:00:00:02"
OTHER_OK = dict(CONN_OK, Address=OTHER)


def test_receipts_need_every_connected_device_to_have_written_recently(tmp_path):
    """冒充已 trust 手机地址的未加密连接, 可能靠 BlueZ 恢复的 CCC 收到通知; 它写不了 secure-write.
    所以连着的每台设备都要本次连接内最近 FRESH_S 秒里 secure-write 成功过, 才发回执 / 回复."""
    sent, logged = [], []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=logged.append,
                   devices=lambda: [CONN_OK, OTHER_OK])
    for f in bc.encode(3, b"1234", 23):
        h.on_write(DEV, TRUSTED, f, now=1.0)
    assert sent == [] and "withheld" in logged[-1]                     # OTHER 连着但没写过
    h.on_write(OTHER, TRUSTED, bc.encode(9, b"z", 23)[0], now=2.0)
    assert [d["id"] for d in _acks(sent)] == [9]                       # 两台都写过了


def test_untrusted_write_does_not_count_as_fresh(tmp_path):
    sent = []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=lambda s: None,
                   devices=lambda: [CONN_OK])
    h.on_write(DEV, {"Paired": False}, bc.encode(4, b"x", 23)[0], now=1.0)   # unpaired 照发
    sent.clear()
    assert h.send_reply("机密", now=1.0) is False and sent == []


def test_reply_freshness_expires_and_disconnect_clears(tmp_path):
    sent = []
    devs = [[CONN_OK]]
    h = bi.Handler(bi.DirSink(str(tmp_path / "in")), notify=sent.append, log=lambda s: None,
                   devices=lambda: devs[0])
    for f in bc.encode(3, b"x", 23):
        h.on_write(DEV, TRUSTED, f, now=100.0)
    assert h.send_reply("好", now=100.0 + bi.FRESH_S - 1) is True
    assert h.send_reply("好", now=100.0 + bi.FRESH_S + 1) is False     # 太久没写
    h.saw_secure_write(DEV, now=500.0)
    devs[0] = [dict(CONN_OK, Connected=False)]
    h.send_reply("好", now=501.0)                                       # 断了: 记录清掉
    devs[0] = [CONN_OK]                                                 # 同地址又连上 (可能是冒充的), 还没写
    assert h.send_reply("好", now=502.0) is False


def test_device_without_address_blocks(tmp_path):
    h = bi.Handler(None, notify=lambda f: None, log=lambda s: None,
                   devices=lambda: [{k: v for k, v in CONN_OK.items() if k != "Address"}])
    h.saw_secure_write(DEV, now=1.0)
    assert h.send_reply("好", now=1.0) is False
