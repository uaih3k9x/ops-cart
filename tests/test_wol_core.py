"""wol_core 测试: MAC 解析、魔术包、目标清单读写."""

import pytest

import wol_core as wol


def test_parse_mac_accepts_common_formats():
    for s in ("24:1c:04:17:68:a1", "24-1C-04-17-68-A1", "241c.0417.68a1", "241c041768a1", " 24:1c:04:17:68:a1 "):
        assert wol.parse_mac(s) == "24:1c:04:17:68:a1"


def test_parse_mac_rejects_garbage():
    for s in ("", "24:1c:04:17:68", "zz:1c:04:17:68:a1", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "01:00:5e:00:00:01"):
        with pytest.raises(ValueError):
            wol.parse_mac(s)


def test_magic_packet():
    pkt = wol.magic_packet("24:1c:04:17:68:a1")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:] == bytes.fromhex("241c041768a1") * 16


def test_targets_roundtrip_and_sorted(tmp_path):
    p = str(tmp_path / "wol.json")
    assert wol.load(p) == []
    t = wol.add([], "推车", "241C041768A1", "enp3s0")
    t = wol.add(t, "445B-01", "aa-bb-cc-00-11-22", "eno1")
    wol.save(t, p)
    got = wol.load(p)
    assert [x["name"] for x in got] == ["445B-01", "推车"]
    assert got[1] == {"name": "推车", "mac": "24:1c:04:17:68:a1", "iface": "enp3s0"}


def test_add_validates_and_replaces_same_mac():
    t = wol.add([], "a", "24:1c:04:17:68:a1", "enp3s0")
    t = wol.add(t, "改名", "24-1c-04-17-68-a1", "eno1")       # 同一 MAC = 更新, 不重复
    assert t == [{"name": "改名", "mac": "24:1c:04:17:68:a1", "iface": "eno1"}]
    with pytest.raises(ValueError):
        wol.add(t, "", "24:1c:04:17:68:a2", "eno1")
    with pytest.raises(ValueError):
        wol.add(t, "x", "24:1c:04:17:68:a2", "eth9")          # 只认推车的三个口


def test_remove():
    t = wol.add([], "a", "24:1c:04:17:68:a1", "enp3s0")
    assert wol.remove(t, "24-1C-04-17-68-A1") == []
