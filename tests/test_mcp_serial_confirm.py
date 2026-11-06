"""mcp_serial 给确认 UI 的部分 (#23): 只读 peek、车机确认后发送、网页急停 / 拒绝请求的投递和执行."""

import json
import os

import pytest

import confirm_core as cc
import mcp_serial as ms
from test_mcp_serial import T0, Rig, cli


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _held(rig, slot=1, cmd="save"):
    tok = rig.lease(slot)
    return tok, rig.ok("send_command", {"slot": slot, "command": cmd, "lease": tok})


# ------------------------------------------------------------ peek: 只读, 不加锁不写盘

def test_peek_reads_without_writing(rig):
    _tok, p = _held(rig)
    before = os.stat(rig.store.path).st_mtime_ns
    v = ms.peek(rig.store.path, clock=rig.clock)
    assert v["pending"][0]["id"] == p["id"] and v["slots"][0]["writer"] == rig.who
    assert os.stat(rig.store.path).st_mtime_ns == before
    assert "token" not in json.dumps(v) and "lease" not in json.dumps(v["pending"])


def test_peek_missing_file_is_fresh(tmp_path):
    v = ms.peek(str(tmp_path / "none.json"), clock=lambda: T0)
    assert not any(s["estop"] for s in v["slots"]) and v["pending"] == []
    assert not os.path.exists(tmp_path / "none.json")


def test_peek_expiry_applied_in_memory(rig):
    _held(rig)
    rig.clock.t += 3600                                  # 租约早过期了, 状态文件还没人改
    v = ms.peek(rig.store.path, clock=rig.clock)
    assert v["pending"] == [] and v["slots"][0]["writer"] is None


@pytest.mark.parametrize("content", [b"{oops", b"\xff\xfe", b'{"v": 99}'])
def test_peek_unreadable_means_all_stopped(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_bytes(content)
    v = ms.peek(str(p), clock=lambda: T0)
    assert all(s["estop"] for s in v["slots"])


def test_peek_permission_error_means_all_stopped(tmp_path, monkeypatch):
    import builtins
    real = builtins.open

    def deny(path, *a, **k):
        if str(path).endswith("s.json"):
            raise PermissionError(13, "denied")
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", deny)
    v = ms.peek(str(tmp_path / "s.json"), clock=lambda: T0)
    assert all(s["estop"] for s in v["slots"])


# ------------------------------------------------------------ 车机确认后发送 (和命令行同一条路)

def test_send_confirmed_sends_and_finishes(rig):
    _tok, p = _held(rig)
    it, status, out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=rig.tail)
    assert status == "sent" and rig.sent == [("/dev/con-1", "save", False)]
    hist = rig.ok("pending_list")["history"][-1]
    assert hist["by"] == cc.BY_KIOSK and hist["status"] == "sent"
    assert [r["ev"] for r in rig.log(1)][-2:] == ["confirm", "send"]


def test_send_confirmed_after_estop_raises_and_sends_nothing(rig):
    _tok, p = _held(rig)
    rig.ok("estop", {"slot": 1})
    with pytest.raises(ms.sw.Refused):
        ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=rig.tail)
    assert rig.sent == []


def test_send_confirmed_send_crash_clears_inflight(rig):
    _tok, p = _held(rig)

    def boom(*a):
        raise RuntimeError("x")
    _it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, boom, tail=rig.tail)
    assert status == "error" and "RuntimeError" in note
    assert rig.store.view()["slots"][0]["inflight"] is None


def test_cli_confirm_still_works_after_refactor(rig):
    _tok, p = _held(rig)
    rc, text = cli(rig, "confirm", str(p["id"]))
    assert rc == 0 and "sent" in text and rig.sent


# ------------------------------------------------------------ 网页请求: 投递 (ops-web) -> 执行 (root 单元)

def test_write_request_file_private_and_exclusive(tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    path = ms.write_request(cc.make_request("estop", 2, "web:a", T0), str(d))
    assert os.path.dirname(path) == str(d) and path.endswith(".json")
    assert os.stat(path).st_mode & 0o077 == 0
    assert cc.parse_request(open(path).read(), T0) == {"op": "estop", "slot": 2, "by": "web:a", "t": T0}


def test_process_requests_applies_and_empties_dir(rig, tmp_path):
    d = str(tmp_path / "req")
    os.makedirs(d)
    _tok, p = _held(rig)
    ms.write_request(cc.make_request("reject", p["id"], "web:a", T0), d)
    ms.write_request(cc.make_request("estop", 3, "web:a", T0), d)
    res = ms.process_requests(rig.store, d, clock=rig.clock, sleep=lambda s: None)
    assert os.listdir(d) == [] and len(res) == 2
    v = rig.store.view()
    assert v["pending"] == [] and v["slots"][2]["estop"]["by"] == "web:a"
    assert [r["ev"] for r in rig.log(1)][-1] == "reject" and rig.log(3)[-1]["ev"] == "estop"


@pytest.mark.parametrize("body", [
    {"op": "confirm", "id": 1, "by": "web:a", "t": T0},
    {"op": "resume", "slot": None, "by": "web:a", "t": T0},
    {"op": "estop", "slot": 1, "by": "web:a", "t": T0 - 3600},
])
def test_process_requests_drops_go_direction_and_stale(rig, tmp_path, body):
    rig.ok("estop", {"slot": 1})
    d = tmp_path / "req"
    d.mkdir()
    (d / "x.json").write_text(json.dumps(body))
    os.utime(d / "x.json", (T0 - 10, T0 - 10))
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert os.listdir(d) == [] and "不认" in res[0][1]
    assert rig.store.view()["slots"][0]["estop"]                    # 没被解除
    assert all(s["estop"] is None for s in rig.store.view()["slots"][1:])


def test_process_requests_ignores_symlink_and_dirs(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(cc.make_request("estop", None, "web:a", T0)))
    os.symlink(secret, d / "link.json")
    (d / "sub").mkdir()
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert not any(s["estop"] for s in rig.store.view()["slots"])     # 链接不跟
    assert secret.exists() and not os.path.lexists(d / "link.json")
    assert len(res) == 2


def test_process_requests_waits_for_half_written_then_drops(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    (d / "a.json").write_text("")                    # 刚 O_CREAT 还没 write
    os.utime(d / "a.json", (rig.clock.t, rig.clock.t))
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 1:                           # 第一轮等的时候网页写完了
            (d / "a.json").write_text(json.dumps(cc.make_request("estop", 4, "web:a", T0)))
    ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=sleep)
    assert rig.store.view()["slots"][3]["estop"] and os.listdir(d) == []

    (d / "b.json").write_text("{")
    os.utime(d / "b.json", (rig.clock.t, rig.clock.t))

    def tick(s):
        rig.clock.t += s
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=tick)
    assert os.listdir(d) == [] and "不认" in res[-1][1]   # 一直写不完: 过了等待期就删


def test_process_requests_refused_is_reported_not_raised(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("reject", 99, "web:a", T0), str(d))
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert "不行" in res[0][1] and os.listdir(d) == []


def test_cli_requests_subcommand(rig, tmp_path, monkeypatch):
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("estop", None, "web:a", T0), str(d))
    monkeypatch.setattr(ms, "REQ_DIR", str(d))
    assert "requests" in ms.CLI_CMDS
    rc, text = cli(rig, "requests", local=False)          # 不要求本机: 只有停的方向
    assert rc == 0 and "已急停" in text
    assert all(s["estop"] for s in rig.store.view()["slots"])


# ------------------------------------------------------------ 车机按钮: 一个入口, 后台线程里调

def test_human_action_confirm_reject_estop_resume(rig):
    _tok, p = _held(rig)
    act = lambda op, arg: ms.human_action(rig.store, op, arg, cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=rig.tail)  # noqa: E731
    assert "sent" in act("confirm", p["id"]) and rig.sent
    _tok2, p2 = _held(rig, slot=2, cmd="reboot")
    assert "拒绝" in act("reject", p2["id"])
    assert rig.store.view()["history"][-1]["by"] == cc.BY_KIOSK
    assert "急停" in act("estop", 2) and rig.store.view()["slots"][1]["estop"]["src"] == "human"
    assert "全部" in act("estop", None)
    assert "解除" in act("resume", 2) and rig.store.view()["slots"][1]["estop"] is None
    assert "全部" in act("resume", None) and not any(s["estop"] for s in rig.store.view()["slots"])
    with pytest.raises(ms.sw.Refused):
        act("reject", 999)
    with pytest.raises(ValueError):
        act("nope", 1)


def test_human_action_confirm_failure_reported(rig):
    _tok, p = _held(rig)
    rig.reply = ("refused", "[conbroker: 拒绝发送, 密码]", "[conbroker: 拒绝发送, 密码]")
    msg = ms.human_action(rig.store, "confirm", p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=rig.tail)
    assert "refused" in msg and "密码" in msg


# ------------------------------------------------------------ 审查 #74 第二轮: 确认挂起的回车 / 空格时重看设备

def _held_enter(rig):
    tok = rig.lease(1)
    rig.tail_text["/dev/con-1"] = "Router#"
    rig.slots[0]["mtime"] = rig.store.clock() - 1                       # 刚动过: 挂起
    p = rig.ok("send_key", {"slot": 1, "key": "enter", "lease": tok})
    assert p["status"] == "pending"
    return p


def test_confirm_held_enter_rechecks_and_refuses_on_confirm_prompt(rig):
    """chain.py: 先挂 reload、再挂 enter; 人先批 reload, 设备停在 [confirm], 再批 enter 不能替人回答."""
    p = _held_enter(rig)
    rig.tail_text["/dev/con-1"] = "Router#reload\nProceed with reload? [confirm]"
    it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                               tail=rig.tail)
    assert rig.sent == [] and status == "refused"
    assert "Proceed with reload? [confirm]" in note and "重新" in note
    assert rig.store.view()["slots"][0]["inflight"] is None
    assert rig.ok("pending_list")["history"][-1]["status"] == "refused"


@pytest.mark.parametrize("now_text", ["display x\n  Input: 12 packets", None])
def test_confirm_held_enter_refuses_off_whitelist_or_unreadable(rig, now_text):
    p = _held_enter(rig)
    if now_text is None:
        rig.tail_text.pop("/dev/con-1")
    else:
        rig.tail_text["/dev/con-1"] = now_text
    _it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                tail=rig.tail)
    assert status == "refused" and rig.sent == [] and note


def test_confirm_held_enter_sends_when_still_at_prompt(rig):
    p = _held_enter(rig)                                                 # 人确认时就是人在场: 不再看 5 秒窗口
    _it, status, _out, _note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                 tail=rig.tail)
    assert status == "sent" and rig.sent == [("/dev/con-1", "", False)]


def test_human_action_confirm_held_enter_reports_current_line(rig):
    p = _held_enter(rig)
    rig.tail_text["/dev/con-1"] = "Proceed with reload? [confirm]"
    msg = ms.human_action(rig.store, "confirm", p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=rig.tail)
    assert "refused" in msg and "[confirm]" in msg and rig.sent == []


def test_cli_confirm_held_enter_rechecks(rig):
    from test_mcp_serial import cli
    p = _held_enter(rig)
    rig.tail_text["/dev/con-1"] = "Proceed with reload? [confirm]"
    rc, text = cli(rig, "confirm", str(p["id"]))
    assert rig.sent == [] and "[confirm]" in text


# ------------------------------------------------------------ 审查 #74 第三轮: send_command 发 y 的同一条链

def _sc(rig, cmd, tok):
    return rig.ok("send_command", {"slot": 1, "command": cmd, "lease": tok})


def test_poc_reload_then_y_chain_is_blocked(rig):
    """设备停 Router#: 挂起 reload, 再挂起 y -> 第二条不收 (回答词不能排在别的挂起项后面)."""
    rig.rules = dict(rig.rules, slots={1: "cisco"})
    rig.tail_text["/dev/con-1"] = "Router#"
    tok = rig.lease(1)
    p1 = _sc(rig, "reload", tok)
    assert p1["status"] == "pending"
    for ans in ("y", "yes", "n", "no", "Y", "c", "confirm"):
        r = rig.call("send_command", {"slot": 1, "command": ans, "lease": tok})
        assert r["isError"] and "前面" in r["content"][0]["text"], ans
    assert rig.sent == []


def test_confirm_line_refused_when_device_moved_onto_confirm_prompt(rig):
    """挂起时停在 Router#, 人确认时设备已经停在 [confirm]: 不发, 显示当前最后一行."""
    rig.rules = dict(rig.rules, slots={1: "cisco"})
    rig.tail_text["/dev/con-1"] = "Router#"
    tok = rig.lease(1)
    p = _sc(rig, "y", tok)
    assert p["status"] == "pending"
    held = rig.store.view()["pending"][0]
    assert held["gate"]["line"] == "Router#"                              # 挂起命令也记下当时最后一行
    rig.tail_text["/dev/con-1"] = "Router#reload\nProceed with reload? [confirm]"
    _it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                tail=rig.tail, rules=lambda: rig.rules)
    assert status == "refused" and rig.sent == [] and "Proceed with reload? [confirm]" in note
    assert "send_command 发 y" not in note


def test_confirm_line_sent_when_prompt_unchanged(rig):
    """save 后设备停在 [Y/N], Agent 挂起 y, 人确认时仍停在 [Y/N]: 照常发."""
    rig.tail_text["/dev/con-1"] = "<SW1>save\nContinue? [Y/N]:"
    tok = rig.lease(1)
    p = _sc(rig, "y", tok)
    _it, status, _out, _note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                 tail=rig.tail, rules=lambda: rig.rules)
    assert status == "sent" and rig.sent == [("/dev/con-1", "y", False)]


def test_confirm_line_refused_when_tail_unreadable(rig):
    tok = rig.lease(1)
    p = _sc(rig, "save", tok)
    rig.tail_text.pop("/dev/con-1")
    _it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                tail=rig.tail, rules=lambda: rig.rules)
    assert status == "refused" and rig.sent == [] and "看不到" in note


def test_recheck_passes_vendor(rig):
    """存活变异: recheck_key 要按登记的厂商判 (登记思科时华为式 <SW1> 不算提示符)."""
    rig.tail_text["/dev/con-1"] = "R1#"
    rig.rules = dict(rig.rules, slots={1: "cisco"})
    p = _held_enter(rig)
    rig.tail_text["/dev/con-1"] = "<SW1>"
    _it, status, _out, _note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                 tail=rig.tail, rules=lambda: rig.rules)
    assert status == "refused" and rig.sent == []


def test_recheck_covers_space(rig):
    tok = rig.lease(1)
    rig.tail_text["/dev/con-1"] = "Router#"
    rig.slots[0]["mtime"] = rig.store.clock() - 1
    p = rig.ok("send_key", {"slot": 1, "key": "space", "lease": tok})
    assert p["status"] == "pending"
    rig.tail_text["/dev/con-1"] = "Proceed with reload? [confirm]"
    _it, status, _out, _note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send,
                                                 tail=rig.tail, rules=lambda: rig.rules)
    assert status == "refused" and rig.sent == []


def test_send_key_passes_vendor(rig):
    rig.rules = dict(rig.rules, slots={1: "cisco"})
    rig.tail_text["/dev/con-1"] = "<SW1>"                                 # rig 默认一分钟没动静
    tok = rig.lease(1)
    assert rig.ok("send_key", {"slot": 1, "key": "enter", "lease": tok})["status"] == "pending"
    assert rig.sent == []


def test_direct_key_refused_if_slot_touched_between_look_and_send(rig):
    """先判后占: 读 tail 之后、begin 之前该槽位有过人确认 / 发送结果, 就不直发."""
    tok = rig.lease(1)
    p = _sc(rig, "save", tok)                                            # 挂一条, 待会儿在"读 tail 之后"被人确认
    orig = rig.tail

    def tail_then_human_confirms(dev, n):
        out = orig(dev, n)
        ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send, tail=orig,
                          rules=lambda: rig.rules)
        return out
    rig.tail = tail_then_human_confirms
    rig.store.run(lambda b, now: None)
    r = rig.call("send_key", {"slot": 1, "key": "enter", "lease": tok})
    assert r["isError"] and "变" in r["content"][0]["text"]
    assert [s[1] for s in rig.sent] == ["save"]                          # 回车没跟着发出去


def test_log_last_line_reads_conbroker_log(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / (ms.lamps_core._safe("/dev/con-1") + ".log")).write_bytes(b"x" * 9000 + b"\nRouter#reload\nProceed with reload? [confirm]")
    assert ms.log_last_line("/dev/con-1", ddir=str(tmp_path)) == "Proceed with reload? [confirm]"
    assert ms.log_last_line("/dev/con-2", ddir=str(tmp_path)) is None
