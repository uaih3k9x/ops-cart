"""mcp_pxe 的离线测试 (#24): 状态文件 + 日志 / MCP 工具 pxe_request / pxe_requests / pxe_retire /
车机上人确认 (照抄确认码、重算清单) / 串口全部急停作废申请. 解锁、上锁、清单全换成假的, 不碰 tftp."""

import json
import os
import stat

import pytest

import mcp_core as mc
import mcp_pxe as mp
import mcp_serial as ms
import pxe_req_core as pr

T0 = 1_700_000_000.0
MAC = "52:54:00:aa:bb:01"
MAC2 = "52:54:00:aa:bb:02"
BY = "human:kiosk@local"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Rig:
    """状态文件 / 日志在 tmp; 清单、装机口模式、解锁、上锁都是假的, 记下调用."""

    def __init__(self, tmp_path):
        self.clock = Clock()
        self.events = []
        self.stopped = False
        self.path = str(tmp_path / "run" / "pxe-req.json")
        self.log = str(tmp_path / "log" / "pxe-req.log")
        self.store = mp.Store(self.path, self.log, clock=self.clock,
                              event=lambda kind, msg: self.events.append((kind, msg)),
                              stopped=lambda: self.stopped)
        self.plans = {}                     # (purpose, mac) -> 清单; 没登记的按 simple_plan
        self.mode = {"mode": "rescue"}
        self.armed, self.disarmed, self.retired = [], [], []
        self.arm_exc = None
        self.who = "stdio:ops@198.51.100.7"
        tools = mp.build_tools(self.store, plan=self.plan, mode=lambda: self.mode, disarm=self.disarm,
                               retire=self.retired.append, who=lambda: self.who)
        self.server = mc.Server(tools)

    def plan(self, purpose, mac):
        if (purpose, mac) in self.plans:
            return self.plans[(purpose, mac)]
        return pr.simple_plan(purpose, mac)

    def disarm(self):
        self.disarmed.append(True)

    def arm(self, it, p):
        if self.arm_exc:
            raise self.arm_exc
        self.armed.append((it["purpose"], it["mac"], p["digest"]))

    def call(self, name, **args):
        out = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
        res = out["result"]
        text = res["content"][0]["text"]
        return res["isError"], (text if res["isError"] else json.loads(text))

    def secret(self):
        return mp.peek(self.path, clock=self.clock, stopped=lambda: False)["pending"]

    def confirm(self, iid, typed=None, shown=None):
        p = next(x for x in self.secret() if x["id"] == iid)
        return mp.human_confirm(self.store, iid, p["code"] if typed is None else typed,
                                p["digest"] if shown is None else shown, BY,
                                plan=self.plan, arm=self.arm, mode=lambda: self.mode)

    def lines(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _clone(macs=(MAC, MAC2), digest="c" * 16):
    return {"purpose": "clone-restore", "error": None, "digest": digest,
            "rows": [{"mac": m, "image": "img1", "device": "SERIALNO=S1", "reason": None, "params": ["x"]}
                     for m in macs]}


# ------------------------------------------------------------ pxe_request

def test_request_only_pends(rig):
    err, res = rig.call("pxe_request", purpose="rocky", mac="52-54-00-AA-BB-01")
    assert not err and res["status"] == "pending" and res["id"] == 1 and res["mac"] == MAC
    assert "code" not in res and "digest" not in res and "确认码" in res["hint"]
    code = rig.secret()[0]["code"]
    assert code not in json.dumps(res)
    assert rig.armed == [] and rig.disarmed == []
    assert stat.S_IMODE(os.stat(rig.path).st_mode) == 0o600
    assert rig.lines()[0]["ev"] == "request" and rig.lines()[0]["who"] == rig.who
    assert code not in open(rig.log).read()
    assert rig.events and rig.events[0][0] == "pxe-req" and code not in rig.events[0][1]


def test_request_clone_restore_batch(rig):
    rig.plans[("clone-restore", None)] = _clone()
    err, res = rig.call("pxe_request", purpose="clone-restore", batch=True)
    assert not err and res["macs"] == [MAC, MAC2] and res["batch"] is True and len(res["lines"]) == 2


@pytest.mark.parametrize("args", [
    {"purpose": "rocky"},
    {"purpose": "rocky", "mac": MAC, "batch": True},
    {"purpose": "clone-restore"},
    {"purpose": "clone-restore", "batch": "true"},
    {"purpose": "clone-restore", "batch": 1},
    {"purpose": "clone-restore", "batch": True, "mac": MAC},
    {"purpose": "install", "mac": MAC},
    {"purpose": "rocky", "mac": "not-a-mac"},
    {"purpose": "rocky", "mac": MAC, "code": "123456"},
    {"purpose": "rocky", "mac": MAC, "confirm": "c" * 16},
])
def test_request_bad_args(rig, args):
    err, text = rig.call("pxe_request", **args)
    assert err and rig.secret() == []


def test_request_plan_error_and_estop(rig):
    rig.plans[("winpe", MAC)] = pr.simple_plan("winpe", MAC, missing=["win/boot.wim"])
    err, text = rig.call("pxe_request", purpose="winpe", mac=MAC)
    assert err and "boot.wim" in text
    rig.stopped = True
    err, text = rig.call("pxe_request", purpose="rocky", mac=MAC)
    assert err and "急停" in text and rig.secret() == []


def test_request_plan_raises(rig):
    def boom(purpose, mac):
        raise OSError("读不了 clone.json")
    tools = mp.build_tools(rig.store, plan=boom, mode=lambda: rig.mode, disarm=rig.disarm,
                           retire=rig.retired.append, who=lambda: "x")
    out = mc.Server(tools).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "pxe_request", "arguments": {"purpose": "rocky", "mac": MAC}}})
    assert out["result"]["isError"] and "clone.json" in out["result"]["content"][0]["text"]


def test_requests_list_has_no_secrets(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    err, res = rig.call("pxe_requests")
    assert not err and res["pending"][0]["id"] == 1
    assert "code" not in json.dumps(res) and "digest" not in json.dumps(res)
    rig.clock.t += pr.REQ_TTL_S + 1
    err, res = rig.call("pxe_requests")
    assert res["pending"] == [] and res["history"][-1]["status"] == "expired"
    assert rig.lines()[-1]["ev"] == "expire"


def test_tools_annotations(rig):
    specs = {t["name"]: t for t in rig.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
             ["result"]["tools"]}
    assert set(specs) == {"pxe_request", "pxe_requests", "pxe_retire"}
    assert specs["pxe_requests"]["annotations"]["readOnlyHint"] is True
    assert specs["pxe_request"]["inputSchema"]["properties"]["batch"]["type"] == "boolean"


# ------------------------------------------------------------ pxe_retire

def test_retire_all(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    rig.mode = {"mode": "install", "mac": MAC2, "since": T0, "until": T0 + 60}
    err, res = rig.call("pxe_retire")
    assert not err and res["dropped"] == [1] and res["action"] == "disarm"
    assert rig.disarmed == [True] and rig.retired == [] and rig.armed == []
    assert rig.secret() == [] and rig.lines()[-1]["ev"] == "retire"


def test_retire_one_mac_of_batch(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    rig.call("pxe_request", purpose="rocky", mac=MAC2)
    rig.mode = {"mode": "install", "mac": MAC, "macs": [MAC, MAC2], "since": T0, "until": T0 + 60}
    err, res = rig.call("pxe_retire", mac=MAC2.upper())
    assert not err and res["dropped"] == [2] and res["action"] == "retire"
    assert rig.retired == [[MAC2]] and rig.disarmed == []
    assert [p["id"] for p in rig.secret()] == [1]


def test_retire_unarmed_mac_does_nothing_to_tftp(rig):
    err, res = rig.call("pxe_retire", mac=MAC)
    assert not err and res["action"] is None and rig.disarmed == [] and rig.retired == []


def test_retire_disarm_fails(rig):
    def bad():
        raise OSError("tftp 只读")
    tools = mp.build_tools(rig.store, plan=rig.plan, mode=lambda: rig.mode, disarm=bad,
                           retire=rig.retired.append, who=lambda: "x")
    out = mc.Server(tools).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "pxe_retire", "arguments": {}}})
    assert out["result"]["isError"] and "tftp 只读" in out["result"]["content"][0]["text"]


# ------------------------------------------------------------ 车机上人确认

def test_confirm_right_code_arms(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    msg = rig.confirm(1)
    assert rig.armed == [("rocky", MAC, pr.simple_plan("rocky", MAC)["digest"])] and "已解锁" in msg
    assert rig.secret() == [] and rig.lines()[-1]["ev"] == "confirm" and rig.lines()[-1]["by"] == BY
    with pytest.raises(pr.Refused):            # 重放
        mp.human_confirm(rig.store, 1, "000000", "x", BY, plan=rig.plan, arm=rig.arm, mode=lambda: rig.mode)
    assert len(rig.armed) == 1


def test_confirm_wrong_code_rekeys_and_does_not_arm(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    old = rig.secret()[0]["code"]
    wrong = "000000" if old != "000000" else "111111"
    with pytest.raises(pr.Refused) as e:
        rig.confirm(1, typed=wrong)
    assert e.value.code == "code" and rig.armed == []
    assert rig.secret()[0]["code"] != old            # 换码落盘了
    with pytest.raises(pr.Refused):
        rig.confirm(1, typed=old)
    assert rig.armed == [] and "bad_code" in [x["ev"] for x in rig.lines()]


def test_confirm_plan_changed_refuses(rig):
    rig.plans[("clone-restore", None)] = _clone()
    rig.call("pxe_request", purpose="clone-restore", batch=True)
    rig.plans[("clone-restore", None)] = _clone(macs=(MAC,), digest="e" * 16)     # 清单被改过
    with pytest.raises(pr.Refused) as e:
        rig.confirm(1)
    assert e.value.code == "changed" and rig.armed == []
    h = mp.peek(rig.path, clock=rig.clock, stopped=lambda: False)["history"][-1]
    assert h["status"] == "refused" and "不一样" in h["why"]


def test_confirm_plan_error_refuses(rig):
    rig.call("pxe_request", purpose="winpe", mac=MAC)
    rig.plans[("winpe", MAC)] = pr.simple_plan("winpe", MAC, missing=["win/boot.wim"])
    with pytest.raises(pr.Refused):
        rig.confirm(1)
    assert rig.armed == []


def test_confirm_while_armed_keeps_pending(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    rig.mode = {"mode": "install", "mac": MAC2, "since": T0, "until": T0 + 60}
    with pytest.raises(pr.Refused) as e:
        rig.confirm(1)
    assert e.value.code == "busy" and rig.armed == [] and rig.secret()


def test_confirm_after_expiry_or_estop(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    p = rig.secret()[0]
    rig.clock.t += pr.REQ_TTL_S + 1
    with pytest.raises(pr.Refused):
        mp.human_confirm(rig.store, 1, p["code"], p["digest"], BY, plan=rig.plan, arm=rig.arm, mode=lambda: rig.mode)
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    p = rig.secret()[0]
    rig.stopped = True
    with pytest.raises(pr.Refused):
        mp.human_confirm(rig.store, p["id"], p["code"], p["digest"], BY, plan=rig.plan, arm=rig.arm,
                         mode=lambda: rig.mode)
    assert rig.armed == []


def test_confirm_arm_fails(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    rig.arm_exc = OSError("tftp 满了")
    with pytest.raises(pr.Refused) as e:
        rig.confirm(1)
    assert e.value.code == "failed"
    h = mp.peek(rig.path, clock=rig.clock, stopped=lambda: False)["history"][-1]
    assert h["status"] == "failed" and "tftp 满了" in h["why"]


def test_reject(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    assert "已拒绝" in mp.human_reject(rig.store, 1, BY)
    assert rig.secret() == [] and rig.lines()[-1]["ev"] == "reject"


# ------------------------------------------------------------ 状态文件 / peek

def test_corrupt_state_means_no_requests(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    with open(rig.path, "w") as f:
        f.write('{"v": 1, "seq": 1, "reqs": [{"id": 1, "purpose": "rocky"}], "history": []}')
    assert mp.peek(rig.path, clock=rig.clock, stopped=lambda: False) == {"pending": [], "history": []}
    err, res = rig.call("pxe_requests")
    assert res["pending"] == [] and rig.lines()[-1]["ev"] == "corrupt"


def test_peek_missing_and_unreadable(tmp_path):
    assert mp.peek(str(tmp_path / "nope.json"), stopped=lambda: False) == {"pending": [], "history": []}
    d = tmp_path / "dir.json"
    d.mkdir()
    assert mp.peek(str(d), stopped=lambda: False) == {"pending": [], "history": []}


def test_peek_hides_when_stopped(rig):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    assert mp.peek(rig.path, clock=rig.clock, stopped=lambda: True)["pending"] == []


# ------------------------------------------------------------ 急停: 复用串口的全部急停

def _serial(tmp_path, **kw):
    return ms.Store(str(tmp_path / "serial-write.json"), str(tmp_path / "slog"), event=None, **kw)


def test_serial_stopped(tmp_path):
    path = str(tmp_path / "serial-write.json")
    assert mp.serial_stopped(path) is False             # 没有文件 = 刚开机, 没急停
    st = _serial(tmp_path)
    st.run(lambda b, now: b.estop(1, "h", "human", now))
    assert mp.serial_stopped(path) is False
    st.run(lambda b, now: b.estop(None, "h", "human", now))
    assert mp.serial_stopped(path) is True
    with open(path, "w") as f:
        f.write("{坏了")
    assert mp.serial_stopped(path) is True               # 读不懂 = 全部急停


def test_serial_estop_all_calls_hook_only_for_all(tmp_path):
    calls = []
    st = _serial(tmp_path, on_estop_all=calls.append)
    st.run(lambda b, now: b.estop(2, "h", "human", now))
    assert calls == []
    st.run(lambda b, now: b.estop(None, "human:kiosk@local", "human", now))
    assert calls == ["human:kiosk@local"]
    st.run(lambda b, now: b.view(now))                   # 没有新的急停事件不再调
    assert calls == ["human:kiosk@local"]


def test_serial_hook_failure_does_not_break_estop(tmp_path):
    def boom(_by):
        raise OSError("pxe 状态写不了")
    st = _serial(tmp_path, on_estop_all=boom)
    st.run(lambda b, now: b.estop(None, "h", "human", now))
    assert all(st.view()["slots"][i]["estop"] for i in range(4))


def test_serial_default_hook_only_on_real_path(tmp_path):
    assert _serial(tmp_path).on_estop_all is None
    assert ms.Store().on_estop_all is mp.drop_on_estop


def test_drop_on_estop(rig, monkeypatch):
    rig.call("pxe_request", purpose="rocky", mac=MAC)
    monkeypatch.setattr(mp, "Store", lambda: rig.store)
    mp.drop_on_estop("human:kiosk@local")
    h = mp.peek(rig.path, clock=rig.clock, stopped=lambda: False)
    assert h["pending"] == [] and h["history"][-1]["status"] == "dropped"
    assert "human:kiosk@local" in h["history"][-1]["why"]
