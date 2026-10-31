"""kvm_broker_core 测试: 请求解析/拆步、人优先 Agent 排队、急停只丢不插、租约钩子、日志行 (#9)."""

import json

import pytest

import ch9329
import kvm_broker_core as kb

DEV = "/dev/ttyACM0"
DEV2 = "/dev/ttyACM1"
ZERO = bytes(8)


def req(**kw):
    r = {"op": "keys", "src": "agent", "who": "cli", "port": DEV, "keys": ["a"]}
    r.update(kw)
    return kb.parse_request(json.dumps(r))


def job(now=0.0, **kw):
    r = req(**kw)
    return kb.Job(r, kb.plan(r), now, dev=r.get("port"))


# ------------------------------------------------------------ 解析


def test_parse_defaults_to_agent_when_src_missing():
    r = kb.parse_request('{"op": "info", "port": "/dev/ttyACM0"}')
    assert r["src"] == "agent" and r["who"] == "?"         # 没说是谁 = 当 Agent, 不能白拿人的优先级


@pytest.mark.parametrize("line", [
    "不是 json", "[]", '{"op": "rm"}', '{"op": "keys", "src": "root", "port": "/dev/ttyACM0"}',
    '{"op": "keys", "keys": ["a"]}',                       # 发键必须指明哪块键鼠
])
def test_parse_rejects_bad_requests(line):
    with pytest.raises(kb.BadRequest):
        kb.parse_request(line)


def test_parse_sanitizes_who():
    r = kb.parse_request(json.dumps({"op": "info", "port": DEV, "who": "a b\n\x1b[31m" + "x" * 80}))
    assert r["who"] == "a_b___31m" + "x" * (kb.WHO_MAX - 9) and len(r["who"]) == kb.WHO_MAX


def test_agent_may_not_send_raw_reports_or_mouse():
    for op in ("report", "mouse", "mouse_abs", "release"):
        with pytest.raises(kb.BadRequest):
            req(op=op, report="00" * 8)
    req(op="report", src="human", report="00" * 8)        # 人可以


def test_control_ops_need_no_port():
    for op in ("estop", "resume", "status"):
        assert kb.parse_request(json.dumps({"op": op}))["op"] == op


# ------------------------------------------------------------ 拆步


def test_plan_keys_is_one_atomic_unit_down_wait_up():
    units = kb.plan(req(keys=["ctrl", "alt", "delete"], press_ms=50))
    down, _ = ch9329.key_report(["ctrl", "alt", "delete"])
    assert units == [(("report", down), ("wait", 0.05), ("report", ZERO))]


def test_plan_type_one_unit_per_char_so_human_can_cut_in():
    units = kb.plan(req(op="type", text="a\n", press_ms=0))
    assert units == [(("report", ch9329.key_report(["a"])[0]), ("report", ZERO)),
                     (("report", ch9329.key_report(["enter"])[0]), ("report", ZERO))]


def test_plan_type_rejects_whole_string_with_untypable_chars():
    with pytest.raises(kb.BadRequest):
        kb.plan(req(op="type", text="ok中"))


def test_plan_unknown_key_and_hold_cap():
    with pytest.raises(kb.BadRequest):
        kb.plan(req(keys=["sausage"]))
    with pytest.raises(kb.BadRequest):
        kb.plan(req(keys=["a"], press_ms=kb.HOLD_MAX_MS + 1))


def test_plan_human_raw_and_mouse():
    assert kb.plan(req(op="report", src="human", report="0200040000000000")) == [
        (("report", bytes.fromhex("0200040000000000")),)]
    assert kb.plan(req(op="mouse", src="human", b=1, dx=300, dy=0, w=0)) == [
        (("mouse", 1, 127, 0, 0), ("mouse", 1, 127, 0, 0), ("mouse", 1, 46, 0, 0))]
    assert kb.plan(req(op="mouse_abs", src="human", b=0, x=5000, y=-1, w=0)) == [
        (("mouse_abs", 0, 4095, 0, 0),)]
    assert kb.plan(req(op="release", src="human")) == [(("report", ZERO), ("mouse", 0, 0, 0, 0))]
    assert kb.plan(req(op="release", src="human", x=10, y=20)) == [
        (("report", ZERO), ("mouse_abs", 0, 10, 20, 0))]


def test_plan_bad_report_length():
    with pytest.raises(kb.BadRequest):
        kb.plan(req(op="report", src="human", report="00"))


def test_activity_ignores_all_up_and_zero_mouse():
    """松开 / 零位移不算"人在操作": 车机空闲放锁前那次全部松开不该再挡 Agent 3 秒."""
    assert kb.is_activity(req(op="report", src="human", report="0000040000000000"))
    assert not kb.is_activity(req(op="report", src="human", report="00" * 8))
    assert not kb.is_activity(req(op="mouse", src="human", b=0, dx=0, dy=0, w=0))
    assert kb.is_activity(req(op="mouse", src="human", b=0, dx=1, dy=0, w=0))
    assert not kb.is_activity(req(op="release", src="human"))
    assert not kb.is_activity(req(op="info"))
    assert kb.is_activity(req(op="type", text="a"))


def test_port_ok_only_tty_acm_usb():
    assert kb.port_ok("/dev/ttyACM0") and kb.port_ok("/dev/ttyUSB12")
    for p in ("/dev/sda", "/etc/passwd", "/dev/ttyACM0/../sda", "/dev/ttyS0", "/dev/ttyACM"):
        assert not kb.port_ok(p)


# ------------------------------------------------------------ 仲裁


def drain(arb, dev, now):
    """把这块键鼠现在能发的全发完 -> 发了哪些 (job.who, 第几步)."""
    out = []
    while True:
        got = arb.take(dev, now)
        if got is None:
            return out
        j, _unit = got
        out.append((j.who, j.i))
        arb.done(j, now)


def test_agent_goes_when_no_human():
    arb = kb.Arbiter()
    j = job(op="type", text="ab", press_ms=0)
    arb.submit(j, 0.0)
    assert drain(arb, DEV, 0.0) == [("cli", 0), ("cli", 1)]
    assert j.state == "done"


def test_human_activity_pauses_agent_for_quiet_s_and_human_never_queues():
    arb = kb.Arbiter(quiet_s=3.0)
    a = job(op="type", text="abc", press_ms=0)
    arb.submit(a, 0.0)
    arb.take(DEV, 0.0)
    arb.done(a, 0.0)                                    # Agent 打了一个字
    h = job(op="report", src="human", who="kiosk", report="0000040000000000")
    arb.submit(h, 1.0)                                  # 人插进来
    assert drain(arb, DEV, 1.0) == [("kiosk", 0)]       # 人立刻发, Agent 停
    assert drain(arb, DEV, 3.9) == []                   # 人停手不到 3 秒, Agent 还等着
    assert arb.wait_hint(DEV, 3.9) == pytest.approx(0.1)
    assert drain(arb, DEV, 4.0) == [("cli", 1), ("cli", 2)]


def test_human_jumps_ahead_of_queued_agent_jobs():
    arb = kb.Arbiter()
    arb.submit(job(who="agent1"), 0.0)
    arb.submit(job(op="report", src="human", who="web", report="00" * 8), 0.0)
    assert arb.take(DEV, 0.0)[0].who == "web"


def test_human_on_one_device_does_not_block_agent_on_another():
    arb = kb.Arbiter()
    arb.submit(job(op="report", src="human", who="kiosk", report="0000040000000000"), 0.0)
    a = job(port=DEV2)
    arb.submit(a, 0.0)
    assert arb.take(DEV2, 0.5)[0] is a


def test_zero_reports_do_not_extend_human_quiet():
    arb = kb.Arbiter(quiet_s=3.0)
    arb.submit(job(op="report", src="human", who="kiosk", report="0000040000000000"), 0.0)
    arb.submit(job(op="release", src="human", who="kiosk"), 2.9)
    drain(arb, DEV, 2.9)
    arb.submit(job(), 3.0)
    assert drain(arb, DEV, 3.0) == [("cli", 0)]


def test_agent_wait_times_out():
    arb = kb.Arbiter(quiet_s=3.0, wait_max_s=10.0)
    a = job()
    arb.submit(a, 0.0)
    for t in range(0, 12):                              # 人一直在敲
        arb.submit(job(op="report", src="human", who="kiosk", report="0000040000000000"), float(t))
        drain(arb, DEV, float(t))
    dropped = arb.expire(10.5)
    assert dropped == [a] and a.state == "dropped" and a.why == "timeout"


def test_info_and_open_bypass_arbitration_even_for_agent():
    arb = kb.Arbiter()
    arb.submit(job(op="report", src="human", who="kiosk", report="0000040000000000"), 0.0)
    drain(arb, DEV, 0.0)
    i = job(op="info")
    arb.submit(i, 0.1)
    assert arb.take(DEV, 0.1)[0] is i                   # 读版本不算输入, 不用等人停手


# ------------------------------------------------------------ 急停 / 租约


def test_estop_drops_pending_agent_units_and_inserts_nothing():
    arb = kb.Arbiter()
    a = job(op="type", text="abc", press_ms=0)
    arb.submit(a, 0.0)
    arb.take(DEV, 0.0)
    arb.done(a, 0.0)
    dropped = arb.estop()
    assert dropped == [a] and a.state == "dropped" and a.why == "estop" and a.i == 1
    assert arb.take(DEV, 0.0) is None                   # 不补 Ctrl+C、不补松开: 什么都不发


def test_estop_lets_in_flight_unit_finish_but_not_continue():
    """正在发的那一步 (按下+抬起) 发完为止, 别把键卡在按下; 后面的一步也不再发."""
    arb = kb.Arbiter()
    a = job(op="type", text="ab", press_ms=0)
    arb.submit(a, 0.0)
    arb.take(DEV, 0.0)                                  # 第 0 步在发
    arb.estop(DEV)
    assert arb.done(a, 0.0) is True and a.state == "dropped"   # 发完回来: 作业已结束 (被急停)
    assert arb.take(DEV, 0.0) is None


def test_estop_refuses_new_agent_until_explicit_resume_but_not_human():
    arb = kb.Arbiter()
    arb.estop()
    with pytest.raises(kb.Refused) as e:
        arb.submit(job(), 0.0)
    assert e.value.code == "estop"
    arb.submit(job(op="info"), 0.0)                     # 读信息不算写
    h = job(op="report", src="human", who="kiosk", report="0000040000000000")
    arb.submit(h, 0.0)                                  # 人照常
    assert drain(arb, DEV, 0.0)[-1] == ("kiosk", 0)
    arb.resume()
    arb.submit(job(), 0.0)


def test_estop_single_device_and_all():
    arb = kb.Arbiter()
    arb.estop(DEV)
    with pytest.raises(kb.Refused):
        arb.submit(job(), 0.0)
    arb.submit(job(port=DEV2), 0.0)                     # 单条急停只停这一块
    arb.estop()
    with pytest.raises(kb.Refused):
        arb.submit(job(port=DEV2), 0.0)
    arb.resume(DEV2)                                    # 全部急停时单独解一块不够
    with pytest.raises(kb.Refused):
        arb.submit(job(port=DEV2), 0.0)
    arb.resume()
    arb.submit(job(port=DEV), 0.0)
    assert arb.status(0.0)[DEV]["stopped"] is False


def test_lease_hook_checked_on_submit_and_each_unit():
    ok = {"v": True}
    arb = kb.Arbiter(lease=lambda who, dev, now: ok["v"])
    a = job(op="type", text="ab", press_ms=0)
    arb.submit(a, 0.0)
    arb.done(*arb.take(DEV, 0.0)[:1], 0.0)
    ok["v"] = False                                     # 租约过期
    assert arb.take(DEV, 0.0) is None
    assert a.state == "dropped" and a.why == "lease"
    with pytest.raises(kb.Refused) as e:
        arb.submit(job(), 0.0)
    assert e.value.code == "lease"
    arb.submit(job(op="report", src="human", who="kiosk", report="00" * 8), 0.0)   # 人不受租约管


def test_fail_drops_job_with_reason():
    arb = kb.Arbiter()
    a = job()
    arb.submit(a, 0.0)
    arb.take(DEV, 0.0)
    arb.fail(a, "gone")
    assert a.state == "dropped" and a.why == "gone" and arb.take(DEV, 0.0) is None


def test_status_reports_queue_and_who_is_typing():
    arb = kb.Arbiter(quiet_s=3.0)
    arb.submit(job(op="report", src="human", who="kiosk", report="0000040000000000"), 0.0)
    drain(arb, DEV, 0.0)
    arb.submit(job(who="claude"), 0.5)
    st = arb.status(1.0)[DEV]
    assert st == {"human": True, "queued": 1, "stopped": False, "last": "kiosk"}


# ------------------------------------------------------------ 日志


def test_record_agent_type_has_source_and_text_masked_when_secret():
    r = req(op="type", text="hunter2", who="claude")
    j = kb.Job(r, kb.plan(r), 0.0, dev=DEV)
    rec = kb.record("sent", j, wall=0)
    assert rec["src"] == "agent" and rec["who"] == "claude" and rec["dev"] == "ttyACM0"
    assert rec["op"] == "type" and rec["text"] == "hunter2" and rec["n"] == 7
    r = req(op="type", text="hunter2", secret=True)
    rec = kb.record("sent", kb.Job(r, kb.plan(r), 0.0, dev=DEV), wall=0)
    assert rec["text"] == "******" and "hunter2" not in kb.log_line(rec)


def test_record_keys_and_drop_reason():
    j = job(keys=["ctrl", "alt", "delete"])
    j.state, j.why = "dropped", "estop"
    rec = kb.record("dropped", j, wall=0)
    assert rec["keys"] == "ctrl+alt+delete" and rec["why"] == "estop" and rec["sent"] == 0


def test_log_line_is_one_json_line_with_time_first():
    line = kb.log_line({"t": "2026-10-30T00:00:00", "ev": "estop", "src": "human", "x": "a\nb"})
    assert "\n" not in line and line.startswith('{"t": "2026-10-30T00:00:00"')
    assert json.loads(line)["x"] == "a\nb"


def test_human_bursts_log_start_and_end_not_every_key():
    b = kb.HumanBursts(quiet_s=3.0)
    assert b.note(DEV, "kiosk", 0.0) is True
    assert b.note(DEV, "kiosk", 1.0) is False
    assert b.note(DEV, "kiosk", 2.0) is False
    assert b.ended(4.0) == []
    assert b.ended(5.0) == [(DEV, "kiosk", 3)]
    assert b.note(DEV, "kiosk", 6.0) is True            # 新一轮
