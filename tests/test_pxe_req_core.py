"""pxe_req_core 的单测 (#24): 申请 book、过期、急停作废、确认码比对、清单对不上拒绝、只收紧."""

import json

import pytest

import pxe_req_core as pr

T0 = 1_700_000_000.0
MAC = "52:54:00:aa:bb:01"
MAC2 = "52:54:00:aa:bb:02"


def _plan(purpose="rocky", mac=MAC):
    return pr.simple_plan(purpose, mac)


def _clone_plan(macs=(MAC, MAC2), digest="d" * 16):
    return {"purpose": "clone-restore", "error": None, "digest": digest,
            "rows": [{"mac": m, "image": "img1", "device": "SERIALNO=S1", "reason": None, "params": ["x"]}
                     for m in macs]}


class Codes:
    """可预测的确认码: 111111, 222222, ..."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"{self.n}" * 6


def _book(text=None, now=T0):
    b = pr.Book.from_text(text, now, code=Codes())
    return b


def _submit(b, purpose="rocky", mac=MAC, batch=False, plan=None, now=T0, who="stdio:ops"):
    plan = plan or _plan(purpose, mac)
    return b.submit(who, purpose, mac, batch, plan, now, stopped=False)


# ------------------------------------------------------------ 目标 / 清单

def test_target_rules():
    assert pr.target("rocky", "52-54-00-AA-BB-01", False) == MAC
    assert pr.target("clone-save", MAC, None) == MAC
    assert pr.target("clone-restore", None, True) is None
    for args in [("rocky", None, False), ("rocky", MAC, True), ("winpe", "nope", False),
                 ("clone-restore", MAC, True), ("clone-restore", None, False), ("clone-restore", None, "yes"),
                 ("install", MAC, False), ("clone-save", None, True), (None, MAC, False)]:
        with pytest.raises(pr.Refused):
            pr.target(*args)


def test_simple_plan_rocky_and_winpe():
    p = pr.simple_plan("rocky", MAC)
    assert p["error"] is None and p["rows"][0]["mac"] == MAC and len(p["digest"]) == 16
    assert pr.simple_plan("rocky", MAC)["digest"] == p["digest"]
    assert pr.simple_plan("winpe", MAC)["digest"] != p["digest"]
    assert pr.simple_plan("rocky", MAC2)["digest"] != p["digest"]
    bad = pr.simple_plan("winpe", MAC, missing=["win/boot.wim"])
    assert bad["digest"] is None and "win/boot.wim" in bad["error"]
    assert "清空" in pr.plan_lines(p)[0]


def test_plan_lines_clone():
    lines = pr.plan_lines(_clone_plan())
    assert lines[0].startswith(MAC) and "SERIALNO=S1" in lines[0]


# ------------------------------------------------------------ 提交

def test_submit_hides_code_and_digest():
    b = _book()
    it = _submit(b)
    assert it["id"] == 1 and it["status"] == "pending" and it["macs"] == [MAC]
    assert "code" not in it and "digest" not in it
    assert it["expires_in"] == pr.REQ_TTL_S
    assert "111111" not in json.dumps(it) and "111111" not in json.dumps(b.view(T0))
    assert b.view(T0, secret=True)["pending"][0]["code"] == "111111"
    assert [e["ev"] for e in b.events] == ["request"]
    assert "111111" not in json.dumps(b.events)          # 日志里也没有码


def test_submit_refuses_plan_error_stopped_dup_and_flood():
    b = _book()
    with pytest.raises(pr.Refused) as e:
        b.submit("a", "winpe", MAC, False, pr.simple_plan("winpe", MAC, missing=["x"]), T0, stopped=False)
    assert e.value.code == "plan"
    with pytest.raises(pr.Refused) as e:
        b.submit("a", "rocky", MAC, False, _plan(), T0, stopped=True)
    assert e.value.code == "estop"
    _submit(b)
    with pytest.raises(pr.Refused) as e:
        _submit(b)
    assert e.value.code == "dup" and "#1" in str(e.value)
    _submit(b, mac=MAC2)
    _submit(b, purpose="winpe")
    with pytest.raises(pr.Refused) as e:
        _submit(b, purpose="winpe", mac=MAC2)
    assert e.value.code == "busy"
    assert len(b.view(T0)["pending"]) == pr.MAX_PENDING


def test_submit_rejects_plan_for_other_purpose():
    with pytest.raises(pr.Refused):
        _book().submit("a", "winpe", MAC, False, _plan("rocky"), T0, stopped=False)


# ------------------------------------------------------------ 过期 / 急停

def test_expire():
    b = _book()
    _submit(b)
    b.tick(T0 + pr.REQ_TTL_S, stopped=False)
    assert b.view(T0 + pr.REQ_TTL_S)["pending"]
    b.tick(T0 + pr.REQ_TTL_S + 1, stopped=False)
    v = b.view(T0 + pr.REQ_TTL_S + 1)
    assert v["pending"] == [] and v["history"][-1]["status"] == "expired"
    assert b.events[-1]["ev"] == "expire"


def test_stopped_drops_everything_pending():
    b = _book()
    _submit(b)
    _submit(b, mac=MAC2)
    b.tick(T0 + 1, stopped=True)
    v = b.view(T0 + 1)
    assert v["pending"] == [] and [h["status"] for h in v["history"]] == ["dropped", "dropped"]


def test_view_while_stopped_hides_pending_without_writing():
    b = _book()
    _submit(b)
    assert b.view(T0, stopped=True)["pending"] == []
    assert b.view(T0)["pending"]


def test_drop_by_mac_and_all():
    b = _book()
    _submit(b)
    _submit(b, purpose="clone-restore", mac=None, batch=True, plan=_clone_plan())
    _submit(b, purpose="winpe", mac=MAC2)
    assert b.drop("agent:x", T0, "提前上锁", mac=MAC2) == [2, 3]
    assert [p["id"] for p in b.view(T0)["pending"]] == [1]
    assert b.drop("agent:x", T0, "提前上锁") == [1]
    assert b.view(T0)["pending"] == []


# ------------------------------------------------------------ 确认

def test_take_right_code_and_settle():
    b = _book()
    it = _submit(b)
    d = b.view(T0, secret=True)["pending"][0]["digest"]
    got = b.take(it["id"], " 11 11 11 ", d, T0 + 5, stopped=False)
    assert got["digest"] == d and got["purpose"] == "rocky" and got["mac"] == MAC
    b.settle(it["id"], "armed", "human:kiosk@local", T0 + 5)
    v = b.view(T0 + 5)
    assert v["pending"] == [] and v["history"][-1]["status"] == "armed"
    with pytest.raises(pr.Refused) as e:          # 重放: 已经确认过的不能再确认
        b.take(it["id"], "111111", d, T0 + 6, stopped=False)
    assert e.value.code == "nope"


def test_take_wrong_code_rekeys():
    b = _book()
    it = _submit(b)
    d = b.view(T0, secret=True)["pending"][0]["digest"]
    for typed in ("", "000000", "11111", "1111111", "abcdef", "111111\n1", None):
        with pytest.raises(pr.Refused) as e:
            b.take(it["id"], typed, d, T0, stopped=False)
        assert e.value.code == "code"
    code = b.view(T0, secret=True)["pending"][0]["code"]
    assert code != "111111"
    with pytest.raises(pr.Refused):                  # 旧码作废
        b.take(it["id"], "111111", d, T0, stopped=False)
    assert b.take(it["id"], b.view(T0, secret=True)["pending"][0]["code"], d, T0, stopped=False)
    assert "bad_code" in [e["ev"] for e in b.events]


def test_take_stale_digest_keeps_pending():
    b = _book()
    it = _submit(b)
    with pytest.raises(pr.Refused) as e:
        b.take(it["id"], "111111", "0" * 16, T0, stopped=False)
    assert e.value.code == "stale"
    assert b.view(T0)["pending"]


def test_take_after_expiry_or_estop_refused():
    b = _book()
    it = _submit(b)
    d = b.view(T0, secret=True)["pending"][0]["digest"]
    with pytest.raises(pr.Refused) as e:
        b.take(it["id"], "111111", d, T0 + pr.REQ_TTL_S + 1, stopped=False)
    assert e.value.code == "nope"
    b2 = _book()
    it = _submit(b2)
    with pytest.raises(pr.Refused) as e:
        b2.take(it["id"], "111111", d, T0, stopped=True)
    assert e.value.code == "nope"
    assert b2.view(T0)["history"][-1]["status"] == "dropped"


def test_reject():
    b = _book()
    it = _submit(b)
    b.reject(it["id"], "human:kiosk@local", T0, why="车机上拒绝")
    assert b.view(T0)["history"][-1]["status"] == "rejected"
    with pytest.raises(pr.Refused):
        b.reject(it["id"], "h", T0)


def test_settle_refused_records_why():
    b = _book()
    it = _submit(b)
    b.settle(it["id"], "refused", "human:kiosk@local", T0, why="清单变了")
    h = b.view(T0)["history"][-1]
    assert h["status"] == "refused" and h["why"] == "清单变了"


# ------------------------------------------------------------ 状态文件

def test_roundtrip_and_corrupt_is_empty():
    b = _book()
    _submit(b)
    b2 = pr.Book.from_text(b.dumps(), T0)
    assert b2.view(T0, secret=True)["pending"][0]["code"] == "111111"
    for text in ("{", "[]", '{"v": 1}', json.dumps({"v": 1, "seq": 1, "reqs": [{"id": 1}], "history": []}),
                 json.dumps({"v": 99, "seq": 0, "reqs": [], "history": []}), "\0"):
        b3 = pr.Book.from_text(text, T0)
        assert b3.view(T0)["pending"] == [], text
        assert b3.events and b3.events[0]["ev"] == "corrupt"
    assert pr.Book.from_text("", T0).events == []


def test_history_is_capped():
    b = _book()
    for i in range(pr.HISTORY_MAX + 5):
        it = b.submit("a", "rocky", MAC, False, _plan(), T0 + i, stopped=False)
        b.reject(it["id"], "h", T0 + i)
    assert len(b.view(T0)["history"]) == pr.HISTORY_MAX


# ------------------------------------------------------------ 提前上锁: 只收紧

def test_retire_action():
    one = {"mode": "install", "mac": MAC, "since": T0, "until": T0 + 60}
    batch = dict(one, macs=[MAC, MAC2])
    assert pr.retire_action({"mode": "rescue"}, None) == "disarm"
    assert pr.retire_action(one, None) == "disarm"
    assert pr.retire_action(one, MAC) == "disarm"
    assert pr.retire_action(dict(one, mode="expired"), MAC) == "disarm"
    assert pr.retire_action(one, MAC2) is None
    assert pr.retire_action({"mode": "rescue"}, MAC) is None
    assert pr.retire_action(batch, MAC2) == "retire"
    assert pr.retire_action(dict(batch, macs=[MAC2]), MAC2) == "disarm"


def test_code_ok():
    assert pr.code_ok("123456", "123456") and pr.code_ok(" 1 2 3 4 5 6 ", "123456")
    for typed in ("", None, 123456, "12345", "1234567", "654321"):
        assert not pr.code_ok(typed, "123456")
    assert not pr.code_ok("", "")
    assert len(pr.new_code()) == pr.CODE_DIGITS and pr.new_code().isdigit()


# ------------------------------------------------------------ 车机申请框内容

def test_card():
    assert pr.card(None) is None and pr.card({"pending": []}) is None and pr.card({}) is None
    b = _book()
    _submit(b, who="stdio:ops@198.51.100.7")
    _submit(b, purpose="clone-restore", mac=None, batch=True, plan=_clone_plan(), now=T0 + 1)
    c = pr.card(b.view(T0 + 65, secret=True))
    assert c["id"] == 1 and c["code"] == "111111" and c["code_text"] == "1 1 1 1 1 1"
    assert c["digest"] == b.view(T0, secret=True)["pending"][0]["digest"]
    assert c["title"] == "Agent 申请 PXE 解锁 #1" and "stdio:ops@198.51.100.7" in c["meta"]
    assert "剩 8:55" in c["meta"] and "清空整块硬盘" in c["warn"] and c["more"] == 1
    assert c["lines"] == [f"{MAC}  {pr.WHAT['rocky']}"]
    c2 = pr.card({"pending": b.view(T0, secret=True)["pending"][1:]})
    assert c2["id"] == 2 and "覆盖" in c2["warn"] and c2["more"] == 0
    assert pr.card({"pending": [{"id": 1}]}) is None          # 缺字段 = 当没有
