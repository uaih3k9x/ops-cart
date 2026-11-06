"""ble_input 薄外壳测试 (#52): 没装 dbus 也能 import、缺依赖时说清楚; 投递目录 (DirSink) 权限和上限;
回复目录取件; 设备路径解析; systemd 单元草稿."""

import json
import os
import pathlib
import stat

import pytest

import ble_input as bi
import ble_input_core as bc

DEV = "02:00:00:00:00:01"
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
    h = bi.Handler(sink, notify=sent.append, log=logged.append)
    trusted = {"Bonded": True, "Paired": True, "Trusted": True}
    for f in bc.encode(3, "隔空打字".encode(), 23):
        h.on_write(DEV, trusted, f, mtu=23, now=10.0)
    rx = bc.Receiver()
    acks = [e for fr in sent for e in rx.feed("cart", fr, 10.0, True)]
    assert [json.loads(e.text) for e in acks] == [{"t": "ack", "id": 3}]
    assert len(list((tmp_path / "in").iterdir())) == 1
    assert len(logged) == 1 and "隔空打字" not in logged[0] and "len=12" in logged[0]

    sent.clear()
    for f in bc.encode(4, b"x", 23):
        h.on_write(DEV, {"Paired": False}, f, mtu=23, now=11.0)
    assert [json.loads(e.text)["code"] for fr in sent for e in rx.feed("cart", fr, 11.0, True)] == ["unpaired"]
    assert len(list((tmp_path / "in").iterdir())) == 1


def test_handler_full_sink_reports_full(tmp_path):
    sent = []
    h = bi.Handler(bi.DirSink(str(tmp_path / "in"), max_files=0), notify=sent.append, log=lambda s: None)
    for f in bc.encode(3, b"x", 23):
        h.on_write(DEV, {"Bonded": True, "Paired": True, "Trusted": True}, f, mtu=23, now=1.0)
    rx = bc.Receiver()
    assert [json.loads(e.text)["code"] for fr in sent for e in rx.feed("c", fr, 1.0, True)] == ["full"]


def test_unit_draft_hardened_not_enabled_by_tests():
    u = UNIT.read_text()
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ExecStart=/usr/bin/python3 /opt/pxe/ble_input.py" in body
    assert "After=bluetooth.service" in body and "Requires=bluetooth.service" in body
    for k in ("NoNewPrivileges=yes", "ProtectSystem=strict", "RuntimeDirectory=ops-cart/ble-input ",
              "RuntimeDirectoryMode=0700", "ReadWritePaths=/var/log/ops-cart",
              "UMask=0077", "Restart=on-failure", "RestrictAddressFamilies=AF_UNIX"):
        assert k in body, k
    assert "kiosk.service" not in body
