"""wol_core 测试: MAC 解析、魔术包、目标清单读写."""

import pytest

import wol_core as wol


def test_parse_mac_accepts_common_formats():
    for s in ("02:00:00:00:00:0a", "02-00-00-00-00-0A", "0200.0000.000a", "02000000000a", " 02:00:00:00:00:0a "):
        assert wol.parse_mac(s) == "02:00:00:00:00:0a"


def test_parse_mac_rejects_garbage():
    for s in ("", "02:00:00:00:00", "zz:00:00:00:00:0a", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "01:00:5e:00:00:01"):
        with pytest.raises(ValueError):
            wol.parse_mac(s)


def test_magic_packet():
    pkt = wol.magic_packet("02:00:00:00:00:0a")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:] == bytes.fromhex("02000000000a") * 16


def test_targets_roundtrip_and_sorted(tmp_path):
    p = str(tmp_path / "wol.json")
    assert wol.load(p) == []
    t = wol.add([], "推车", "02000000000A", "enp3s0")
    t = wol.add(t, "ROOM-01", "aa-bb-cc-00-11-22", "eno1")
    wol.save(t, p)
    got = wol.load(p)
    assert [x["name"] for x in got] == ["ROOM-01", "推车"]
    assert got[1] == {"name": "推车", "mac": "02:00:00:00:00:0a", "iface": "enp3s0"}


def test_add_validates_and_replaces_same_mac():
    t = wol.add([], "a", "02:00:00:00:00:0a", "enp3s0")
    t = wol.add(t, "改名", "02-00-00-00-00-0a", "eno1")       # 同一 MAC = 更新, 不重复
    assert t == [{"name": "改名", "mac": "02:00:00:00:00:0a", "iface": "eno1"}]
    with pytest.raises(ValueError):
        wol.add(t, "", "02:00:00:00:00:0b", "eno1")
    with pytest.raises(ValueError):
        wol.add(t, "x", "02:00:00:00:00:0b", "eth9")          # 只认推车的三个口


def test_remove():
    t = wol.add([], "a", "02:00:00:00:00:0a", "enp3s0")
    assert wol.remove(t, "02-00-00-00-00-0A") == []
