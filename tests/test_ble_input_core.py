"""ble_input_core 测试 (#52): 分包编码 / 重组、乱序 / 重复 / 超长 / 半包超时、中文和 emoji、
未配对拒绝、审计不含正文、和安卓侧共用的测试向量."""

import json
import pathlib
import zlib

import pytest

import ble_input_core as bc

T0 = 1_700_000_000.0
DEV = "02:00:00:00:00:01"
VECTORS = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ble_input_vectors.json"


def _feed_all(rx, frames, src=DEV, now=T0, trusted=True):
    out = []
    for f in frames:
        out += rx.feed(src, f, now, trusted)
    return out


def _texts(evs):
    return [e.text for e in evs if e.kind == "text"]


# ------------------------------------------------------------ 帧格式

def test_uuids_fixed_and_distinct():
    ids = {bc.SERVICE_UUID, bc.RX_UUID, bc.TX_UUID}
    assert len(ids) == 3
    assert bc.SERVICE_UUID == "c7fa50cf-d4f6-4e23-a71d-7b725c250660"


def test_start_frame_layout_by_hand():
    data = "hi".encode()
    (f,) = bc.encode(0x1234, data, mtu=23)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    assert f == bytes([0x01, 0x12, 0x34, 0x00, 0x02]) + crc.to_bytes(4, "big") + data


def test_frames_fit_mtu_and_cont_carries_offset():
    data = bytes(range(40))
    frames = bc.encode(7, data, mtu=23)
    assert all(len(f) <= 20 for f in frames)
    assert frames[0][0] == bc.T_START and len(frames[0]) == 20      # 9 字节头 + 11 字节正文
    assert frames[1][:5] == bytes([0x02, 0x00, 0x07, 0x00, 11])
    assert b"".join(f[9:] if i == 0 else f[5:] for i, f in enumerate(frames)) == data


@pytest.mark.parametrize("bad", [b"", b"\x01\x00", b"\x02\x00\x01\x00", b"\x09\x00\x01\x00\x00\x00\x00\x00\x00x"])
def test_decode_rejects_garbage(bad):
    with pytest.raises(bc.Bad) as ei:
        bc.decode_frame(bad)
    assert ei.value.code == "frame"


def test_encode_limits():
    with pytest.raises(ValueError):
        bc.encode(1, b"", mtu=23)
    with pytest.raises(ValueError):
        bc.encode(1, b"x" * (bc.MAX_LEN + 1), mtu=23)
    with pytest.raises(ValueError):
        bc.encode(1, b"x", mtu=12)                                    # 首包放不下 1 字节正文
    with pytest.raises(ValueError):
        bc.encode(0x10000, b"x", mtu=23)


# ------------------------------------------------------------ 重组

@pytest.mark.parametrize("text", ["hello", "你好，推车", "装机完成 ✅🚀👨‍👩‍👧", "换行\n和\t制表"])
@pytest.mark.parametrize("mtu", [23, 185, 517])
def test_roundtrip_chinese_emoji(text, mtu):
    rx = bc.Receiver()
    evs = _feed_all(rx, bc.encode(5, text.encode(), mtu))
    assert [(e.kind, e.msg_id, e.text) for e in evs] == [("text", 5, text)]
    assert evs[0].src == DEV and evs[0].size == len(text.encode())


def test_max_length_ok_and_over_limit_rejected():
    rx = bc.Receiver()
    big = ("字" * (bc.MAX_LEN // 3)).encode()
    assert len(big) <= bc.MAX_LEN
    assert _texts(_feed_all(rx, bc.encode(1, big, 247))) == [big.decode()]
    over = bc.encode(2, b"x" * bc.MAX_LEN, 247)
    over[0] = over[0][:3] + (bc.MAX_LEN + 1).to_bytes(2, "big") + over[0][5:]   # 首包谎报长度
    evs = rx.feed(DEV, over[0], T0, True)
    assert [(e.kind, e.code) for e in evs] == [("err", "len")]
    assert rx.feed(DEV, over[1], T0, True)[0].code == "nostart"    # 被拒的消息后续分片不再收


def test_out_of_order_continuations():
    rx = bc.Receiver()
    text = "乱序到达也要拼对" * 5
    frames = bc.encode(3, text.encode(), 23)
    first, rest = frames[0], frames[1:]
    assert _texts(_feed_all(rx, [first] + rest[::-1])) == [text]


def test_duplicate_fragments_ignored():
    rx = bc.Receiver()
    frames = bc.encode(4, "重复分片".encode() * 4, 23)
    evs = _feed_all(rx, [frames[0], frames[1], frames[1]] + frames[2:])
    assert _texts(evs) == ["重复分片" * 4]


def test_duplicate_message_after_done_is_dup_not_redelivered():
    rx = bc.Receiver()
    frames = bc.encode(9, "只投一次".encode(), 23)
    assert _texts(_feed_all(rx, frames)) == ["只投一次"]
    again = _feed_all(rx, frames, now=T0 + 1)                       # 断线重连后手机重发同一条
    assert [e.kind for e in again] == ["dup"] and again[0].msg_id == 9
    other = _feed_all(rx, frames, src="02:00:00:00:00:02", now=T0 + 1)
    assert _texts(other) == ["只投一次"]                             # 去重按来源设备分开


def test_dup_memory_expires():
    rx = bc.Receiver()
    frames = bc.encode(9, b"x", 23)
    _feed_all(rx, frames)
    assert _texts(_feed_all(rx, frames, now=T0 + bc.DEDUP_TTL_S + 1)) == ["x"]


def test_overlapping_conflicting_fragment_drops_message():
    rx = bc.Receiver()
    frames = bc.encode(6, b"a" * 40, 23)
    bad = frames[1][:5] + b"b" * (len(frames[1]) - 5)
    evs = _feed_all(rx, [frames[0], frames[1], bad])
    assert [(e.kind, e.code) for e in evs] == [("err", "overlap")]
    assert rx.feed(DEV, frames[2], T0, True)[0].code == "nostart"


def test_fragment_past_total_rejected():
    rx = bc.Receiver()
    frames = bc.encode(6, b"a" * 30, 23)
    past = bytes([0x02, 0x00, 0x06, 0x00, 29]) + b"zz"
    evs = _feed_all(rx, [frames[0], past])
    assert [(e.kind, e.code) for e in evs] == [("err", "len")]


def test_crc_mismatch():
    rx = bc.Receiver()
    frames = bc.encode(8, "校验".encode() * 5, 23)
    frames[-1] = frames[-1][:-1] + bytes([frames[-1][-1] ^ 0xFF])
    evs = _feed_all(rx, frames)
    assert [(e.kind, e.code) for e in evs] == [("err", "crc")]


@pytest.mark.parametrize("raw,code", [(b"\xff\xfe", "utf8"), (b"rm\x00", "ctrl"), (b"\x1b[2J", "ctrl"),
                                      ("a‮b".encode(), "ctrl")])
def test_bad_text_rejected(raw, code):
    rx = bc.Receiver()
    evs = _feed_all(rx, bc.encode(1, raw, 23))
    assert [(e.kind, e.code) for e in evs] == [("err", code)]


def test_half_packet_timeout():
    rx = bc.Receiver()
    frames = bc.encode(11, b"y" * 50, 23)
    rx.feed(DEV, frames[0], T0, True)
    assert rx.expire(T0 + bc.PARTIAL_TTL_S - 1) == []
    rx.feed(DEV, frames[1], T0 + bc.PARTIAL_TTL_S - 1, True)          # 有新分片就续命
    gone = rx.expire(T0 + 2 * bc.PARTIAL_TTL_S)
    assert [(e.kind, e.src, e.msg_id, e.code) for e in gone] == [("err", DEV, 11, "timeout")]
    assert rx.feed(DEV, frames[2], T0 + 2 * bc.PARTIAL_TTL_S, True)[0].code == "nostart"


def test_feed_also_expires_stale_partials():
    rx = bc.Receiver()
    frames = bc.encode(11, b"y" * 50, 23)
    rx.feed(DEV, frames[0], T0, True)
    evs = rx.feed(DEV, bc.encode(12, b"ok", 23)[0], T0 + bc.PARTIAL_TTL_S + 1, True)
    assert [(e.kind, e.msg_id, e.code) for e in evs] == [("err", 11, "timeout"), ("text", 12, None)]


def test_partial_cap_per_device_drops_oldest():
    rx = bc.Receiver()
    starts = [bc.encode(i, b"z" * 50, 23)[0] for i in range(1, bc.MAX_PARTIAL + 2)]
    evs = []
    for k, f in enumerate(starts):
        evs += rx.feed(DEV, f, T0 + k, True)
    assert [(e.kind, e.msg_id, e.code) for e in evs] == [("err", 1, "busy")]


def test_restart_same_id_replaces_partial():
    rx = bc.Receiver()
    a = bc.encode(13, b"a" * 30, 23)
    b = bc.encode(13, b"b" * 30, 23)
    assert _texts(_feed_all(rx, [a[0]] + b)) == ["b" * 30]


# ------------------------------------------------------------ 只认已配对

def test_untrusted_write_refused_without_state():
    rx = bc.Receiver()
    frames = bc.encode(2, "陌生设备".encode(), 23)
    evs = _feed_all(rx, frames, trusted=False)
    assert {(e.kind, e.code) for e in evs} == {("err", "unpaired")}
    assert _texts(_feed_all(rx, frames[1:])) == []                   # 没留下半包


@pytest.mark.parametrize("props,ok", [
    ({"Bonded": True, "Paired": True, "Trusted": True}, True),
    ({"Paired": True, "Trusted": True}, True),                       # 老 BlueZ 没有 Bonded 属性
    ({"Bonded": False, "Paired": True, "Trusted": True}, False),     # 配了对但没存密钥
    ({"Bonded": True, "Paired": True, "Trusted": False}, False),     # 没在车机上 trust
    ({"Bonded": True, "Paired": True, "Trusted": True, "Blocked": True}, False),
    ({}, False),
    (None, False),
])
def test_device_trusted(props, ok):
    assert bc.device_trusted(props) is ok


def test_may_broadcast_only_when_all_connected_trusted():
    good = {"Bonded": True, "Paired": True, "Trusted": True, "Connected": True}
    stranger = {"Paired": False, "Connected": True}
    offline = {"Paired": False, "Connected": False}
    assert bc.may_broadcast([good, offline]) is True
    assert bc.may_broadcast([good, stranger]) is False
    assert bc.may_broadcast([]) is False


# ------------------------------------------------------------ 回执 / 回复

def test_status_payloads():
    assert json.loads(bc.ack(5)) == {"t": "ack", "id": 5}
    assert json.loads(bc.ack(5, dup=True)) == {"t": "ack", "id": 5, "dup": True}
    assert json.loads(bc.err(5, "crc")) == {"t": "err", "id": 5, "code": "crc"}
    assert json.loads(bc.err(None, "frame")) == {"t": "err", "id": None, "code": "frame"}


def test_reply_truncated_on_char_boundary():
    p = bc.reply("回复" * 1000)
    assert len(p) <= bc.REPLY_MAX + 64
    d = json.loads(p)
    assert d["t"] == "reply" and d["text"].endswith("…") and len(d["text"].encode()) <= bc.REPLY_MAX
    assert json.loads(bc.reply("短")) == {"t": "reply", "text": "短"}


def test_event_to_status():
    assert json.loads(bc.status_for(bc.Event("text", DEV, 3, text="x", size=1))) == {"t": "ack", "id": 3}
    assert json.loads(bc.status_for(bc.Event("dup", DEV, 3))) == {"t": "ack", "id": 3, "dup": True}
    assert json.loads(bc.status_for(bc.Event("err", DEV, 3, code="crc")))["code"] == "crc"


def test_tx_roundtrip_through_receiver():
    rx = bc.Receiver()
    tx = bc.Sender()
    frames = tx.frames(bc.reply("大肥鱼：好的 👍"), mtu=23)
    evs = _feed_all(rx, frames)
    assert json.loads(evs[0].text) == {"t": "reply", "text": "大肥鱼：好的 👍"}
    assert tx.frames(b"x", 23)[0][1:3] != frames[0][1:3]             # 序号递增


def test_sender_id_wraps():
    tx = bc.Sender(next_id=0xFFFF)
    assert tx.frames(b"a", 23)[0][1:3] == b"\xff\xff"
    assert tx.frames(b"a", 23)[0][1:3] == b"\x00\x00"


# ------------------------------------------------------------ 审计

def test_audit_has_length_and_source_but_no_text():
    secret = "口令是 hunter2 👀"
    ev = bc.Event("text", DEV, 7, text=secret, size=len(secret.encode()))
    line = bc.audit(ev, result="queued")
    assert DEV in line and f"len={len(secret.encode())}" in line and "id=7" in line and "result=queued" in line
    assert "hunter2" not in line and "口令" not in line
    err = bc.audit(bc.Event("err", DEV, 7, code="crc"))
    assert "code=crc" in err


# ------------------------------------------------------------ 两侧共用的测试向量

def test_shared_vectors_match():
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert doc["vectors"]
    for v in doc["vectors"]:
        payload = bytes.fromhex(v["payload_hex"])
        if "text" in v:
            assert payload == v["text"].encode()
        assert [f.hex() for f in bc.encode(v["msg_id"], payload, v["mtu"])] == v["frames_hex"], v["name"]
        rx = bc.Receiver()
        evs = _feed_all(rx, [bytes.fromhex(h) for h in v["frames_hex"]])
        assert [e.kind for e in evs] == ["text"] and evs[0].text.encode() == payload
    for v in doc["rejects"]:
        rx = bc.Receiver()
        evs = _feed_all(rx, [bytes.fromhex(h) for h in v["frames_hex"]])
        assert [(e.kind, e.code) for e in evs][-1] == ("err", v["code"]), v["name"]
