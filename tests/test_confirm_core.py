"""confirm_core 测试 (#23): 执行绪方块怎么画、确认框里写什么、动作前置校验、网页请求的格式和执行."""

import json

import pytest

import confirm_core as cc
import lamps_core as lc
import serial_write_core as sw

T0 = 1_700_000_000.0
PASS = {"verdict": "pass", "layer": 1, "reason": "只读", "danger": [], "vendor": "huawei"}
HOLD = {"verdict": "confirm", "layer": 1, "reason": "不在只读白名单里", "danger": [], "vendor": "huawei"}
DANGER = {"verdict": "confirm", "layer": 1, "reason": "带高危词", "danger": ["save"], "vendor": "huawei"}


def _book():
    return sw.Book.from_text(None, T0)


def _slot(present=True, baud="9600"):
    return {"dev": "/dev/con-1", "present": present, "baud": baud, "count": 0}


def _held(b, slot=1, cmd="save", gate=DANGER, who="stdio:ops@x"):
    tok = b.acquire(slot, who, T0)["lease"]
    return tok, b.submit(slot, tok, who, "line", cmd, gate, T0, 30, name="SW1")


# ------------------------------------------------------------ 每槽位状态 (读不懂 = 全部急停)

def test_slot_states_from_view():
    b = _book()
    _held(b, 1)
    tok = b.acquire(2, "http:agent", T0)["lease"]
    b.begin(2, tok, "http:agent", "line", "display version", PASS, T0, 30)
    b.estop(3, "human:ops", "human", T0)
    st = cc.slot_states(b.view(T0 + 5))
    assert st[1] == {"estop": False, "pending": 1, "writer": "stdio:ops@x", "sending": False}
    assert st[2]["sending"] and st[2]["writer"] == "http:agent"
    assert st[3]["estop"] and st[4] == {"estop": False, "pending": 0, "writer": None, "sending": False}


@pytest.mark.parametrize("bad", [None, {}, {"slots": "x"}, {"slots": [{"slot": 1}]}, "oops", 3])
def test_slot_states_unreadable_means_all_stopped(bad):
    st = cc.slot_states(bad)
    assert sorted(st) == list(sw.SLOTS) and all(s["estop"] for s in st.values())


def test_stopped_view_is_a_valid_view():
    v = cc.stopped_view("读不到状态文件")
    assert all(s["estop"]["by"] == "读不到状态文件" for s in v["slots"]) and v["pending"] == []
    assert all(s["estop"] for s in cc.slot_states(v).values())


# ------------------------------------------------------------ 执行绪方块 (#2 指示灯同一套颜色)

def _st(**kw):
    d = {"estop": False, "pending": 0, "writer": None, "sending": False}
    d.update(kw)
    return d


def test_head_plain_is_unchanged_from_lamps():
    v = cc.head_view(1, _slot(), _st(), active=True, on=True)
    assert v["text"] == "1\n9600" and v["bg"] == lc.HEALTH_COLOR["OK"] and not v["blink"]
    v = cc.head_view(3, _slot(present=False), _st(), active=False, on=True)
    assert v["text"] == "3\n未插" and v["bg"] == lc.EMPTY_BODY


def test_head_pending_blinks_yellow_with_count():
    on = cc.head_view(2, _slot(), _st(pending=2, writer="a"), active=False, on=True)
    off = cc.head_view(2, _slot(), _st(pending=2, writer="a"), active=False, on=False)
    assert on["text"] == off["text"] == "2\n待确认 2"
    assert on["bg"] == cc.PENDING_COLOR and on["fg"] == lc.LIT_FG and on["blink"]
    assert off["bg"] == lc.BODY and off["bar"] == cc.PENDING_COLOR


def test_head_estop_solid_red_beats_everything():
    for on in (True, False):
        v = cc.head_view(1, _slot(present=False), _st(estop=True, pending=1, sending=True), active=True, on=on,
                         bell=True)
        assert v["text"] == "1\n急停" and v["bg"] == cc.ESTOP_COLOR and not v["blink"]


def test_head_agent_sending_blinks_agent_color():
    on = cc.head_view(1, _slot(), _st(writer="a", sending=True), active=False, on=True)
    off = cc.head_view(1, _slot(), _st(writer="a", sending=True), active=False, on=False)
    assert on["text"] == "1\nAgent 输入" and on["bg"] == cc.AGENT_COLOR and on["blink"]
    assert off["bg"] == lc.BODY and off["bar"] == cc.AGENT_COLOR


def test_head_agent_lease_marks_who_types():
    idle = cc.head_view(1, _slot(), _st(writer="a"), active=False, on=True)
    assert idle["text"] == "1 Agent\n9600" and idle["bar"] == cc.AGENT_COLOR and idle["bg"] == lc.BODY
    moving = cc.head_view(1, _slot(), _st(writer="a"), active=True, on=True)
    assert moving["bg"] == cc.AGENT_COLOR
    human = cc.head_view(1, _slot(), _st(), active=True, on=True)    # 没租约在动 = 人 (或设备自己吐)
    assert human["bg"] == lc.HEALTH_COLOR["OK"]


def test_head_bell_still_red_flash_when_no_agent_state():
    v = cc.head_view(1, _slot(), _st(), active=False, on=True, bell=True)
    assert v["bg"] == lc.HEALTH_COLOR["FAIL"]


def test_head_texts_fit_lamp_width():
    for st in (_st(estop=True), _st(pending=5), _st(writer="a", sending=True), _st(writer="a")):
        for line in cc.head_view(4, _slot(baud="115200"), st, active=False, on=True)["text"].split("\n"):
            assert len(line) <= 8, line


# ------------------------------------------------------------ 副屏: 挂起数和急停

def test_summary_line():
    assert cc.summary(cc.slot_states(_book().view(T0))) is None
    b = _book()
    _held(b, 1)
    _held(b, 2, cmd="reboot")
    b.estop(3, "h", "human", T0)
    s = cc.summary(cc.slot_states(b.view(T0)))
    assert s["level"] == "bad" and "待确认 2" in s["text"] and "急停 3" in s["text"]
    s = cc.summary(cc.slot_states(None))
    assert s == {"text": "Agent 全部急停", "level": "bad"}
    b = _book()
    _held(b, 4)
    assert cc.summary(cc.slot_states(b.view(T0)))["level"] == "warn"


# ------------------------------------------------------------ 确认框

def test_card_shows_what_agent_wants_and_why():
    b = _book()
    _held(b, 1)
    c = cc.card(b.view(T0 + 12), 1)
    assert c["title"] == "操作头 1 · SW1" and not c["stopped"]
    it = c["items"][0]
    assert it["head"] == "Agent 想执行：save"
    assert "stdio:ops@x" in it["meta"] and "12 秒前" in it["meta"]
    assert it["reason"] == "闸门理由：带高危词" and it["danger"] == "高危词：save"
    assert any("写入者 stdio:ops@x" in s for s in c["status"])


def test_card_key_and_estop_and_other_slots():
    b = _book()
    tok = b.acquire(2, "a", T0)["lease"]
    b.submit(2, tok, "a", "key", "ctrl_c", sw.classify_key("ctrl_c"), T0, 10)
    c = cc.card(b.view(T0), 2)
    assert c["items"][0]["head"] == "Agent 想按：Ctrl+C" and c["items"][0]["danger"] == ""
    assert cc.card(b.view(T0), 1)["items"] == []                  # 只列本槽位的
    b.estop(2, "human:ops", "human", T0)
    c = cc.card(b.view(T0), 2)
    assert c["stopped"] and c["items"] == [] and any("急停中" in s for s in c["status"])


# ------------------------------------------------------------ 动作前置校验 (最终还是 Book 说了算)

def test_check_confirm():
    b = _book()
    tok, it = _held(b, 1)
    assert cc.check(b.view(T0), "confirm", it["id"]) is None
    assert "不在挂起" in cc.check(b.view(T0), "confirm", 999)
    b2 = _book()
    tok2, it2 = _held(b2, 1)
    b2.begin(1, tok2, "stdio:ops@x", "line", "display x", PASS, T0, 30)
    assert "还没回来" in cc.check(b2.view(T0), "confirm", it2["id"])
    assert cc.check(cc.stopped_view(), "confirm", it["id"])          # 读不懂: 什么都不让确认


def test_check_confirm_stopped_slot_with_pending_item():
    v = _book()
    _tok, it = _held(v, 1)
    view = v.view(T0)
    view["slots"][0]["estop"] = {"by": "x", "src": "human", "t": ""}    # 视图刚好赶上急停
    assert "急停" in cc.check(view, "confirm", it["id"])


def test_check_reject_estop_resume():
    b = _book()
    _tok, it = _held(b, 1)
    v = b.view(T0)
    assert cc.check(v, "reject", it["id"]) is None and cc.check(v, "reject", 999)
    assert cc.check(v, "estop", None) is None and cc.check(v, "estop", 2) is None
    assert cc.check(v, "estop", 9) and cc.check(v, "nope", 1)
    assert "没有急停" in cc.check(v, "resume", None) and "没在急停" in cc.check(v, "resume", 1)
    b.estop(1, "h", "human", T0)
    v = b.view(T0)
    assert cc.check(v, "resume", 1) is None and cc.check(v, "resume", None) is None


# ------------------------------------------------------------ 网页请求: 只有停的方向 (急停 / 拒绝)

def test_request_roundtrip_estop_and_reject():
    for op, arg in (("estop", None), ("estop", 3), ("reject", 7)):
        req = cc.make_request(op, arg, "web:198.51.100.2", T0)
        got = cc.parse_request(json.dumps(req), T0 + 1)
        assert got == req


@pytest.mark.parametrize("op,arg", [("confirm", 1), ("resume", 1), ("resume", None), ("estop", 9),
                                    ("reject", None), ("reject", "1"), ("reject", True)])
def test_make_request_refuses_go_direction_and_junk(op, arg):
    with pytest.raises(ValueError):
        cc.make_request(op, arg, "web:x", T0)


@pytest.mark.parametrize("text", [
    "", "{", "[]", json.dumps({"op": "confirm", "id": 1, "by": "w", "t": T0}),
    json.dumps({"op": "resume", "slot": 1, "by": "w", "t": T0}),
    json.dumps({"op": "estop", "slot": 1, "by": "w", "t": T0 - cc.REQ_MAX_AGE_S - 1}),     # 太旧
    json.dumps({"op": "estop", "slot": 1, "by": "w", "t": T0 + 60}),                       # 来自未来
    json.dumps({"op": "estop", "slot": 1, "by": "w" * 100, "t": T0}),
    json.dumps({"op": "estop", "slot": 1, "by": "a\nb", "t": T0}),
    json.dumps({"op": "reject", "id": True, "by": "w", "t": T0}),
    "x" * (cc.REQ_MAX + 1),
])
def test_parse_request_refuses(text):
    with pytest.raises(ValueError):
        cc.parse_request(text, T0)


def test_apply_request_estop_and_reject():
    b = _book()
    _tok, it = _held(b, 1)
    msg = cc.apply_request(b, cc.make_request("reject", it["id"], "web:a", T0), T0)
    assert "拒绝" in msg and b.view(T0)["pending"] == []
    hist = b.view(T0)["history"][-1]
    assert hist["status"] == "rejected" and hist["by"] == "web:a"
    msg = cc.apply_request(b, cc.make_request("estop", None, "web:a", T0), T0)
    assert "全部" in msg and all(s["estop"]["by"] == "web:a" and s["estop"]["src"] == "human"
                                 for s in b.view(T0)["slots"])
    with pytest.raises(sw.Refused):
        cc.apply_request(b, cc.make_request("reject", 999, "web:a", T0), T0)
