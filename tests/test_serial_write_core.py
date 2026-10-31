"""serial_write_core 的离线测试: 行校验 / 高危词 / 闸门第 1 层 / 写租约 / 挂起与确认 / 急停 / 状态文件 fail-closed."""

import json

import pytest

import serial_write_core as sw

T0 = 1_700_000_000.0
HUAWEI = "display clock\n2026-10-30 12:00:00\n<SW1>"
RULES = sw.load_rules(None)


# ------------------------------------------------------------ 行级命令

@pytest.mark.parametrize("cmd", ["display version", "show ip int brief", "ping 192.0.2.1", "y", "a" * sw.LINE_MAX,
                                 "display current-configuration | include sysname"])
def test_check_line_ok(cmd):
    assert sw.check_line(cmd) == cmd


@pytest.mark.parametrize("cmd", [
    "", "   ", "a" * (sw.LINE_MAX + 1),
    "display version\rsave", "display version\nsave", "display\tversion", "save\x08\x08\x08\x08display",
    "\x03", "display \x1b[A", "display\x7f", "display​version", "ｓａｖｅ", "显示",
    "-h", "--force display",          # 开头是 - 会被 conbroker 命令行当成选项
    123, None, b"display version",
])
def test_check_line_rejects(cmd):
    with pytest.raises(sw.Refused) as e:
        sw.check_line(cmd)
    assert e.value.code == "bad"


# ------------------------------------------------------------ 高危词

@pytest.mark.parametrize("cmd, hit", [
    ("save", "save"), ("SAVE", "save"), ("sav", "save"), ("save force", "save"),
    ("reboot", "reboot"), ("reb", "reboot"), ("reset saved-configuration", "reset"),
    ("format flash:", "format"), ("delete /unreserved flash:/a.cfg", "delete"), ("del flash:/a.cfg", "delete"),
    ("write memory", "write"), ("wr", "write"), ("copy running-config startup-config", "copy"),
    ("startup saved-configuration a.cfg", "startup"), ("undo shutdown", "shutdown"),
    ("display current-configuration | include save", "save"), ("erase startup-config", "erase"),
    ("rm -rf /", "rm"), ("reload", "reload"),
])
def test_danger_words(cmd, hit):
    assert hit in sw.danger_words(cmd)


@pytest.mark.parametrize("cmd", ["display version", "show clock", "ping 192.0.2.1", "display interface brief",
                                 "y", "n", "quit", "system-view", "d"])
def test_not_danger(cmd):
    assert sw.danger_words(cmd) == []


def test_danger_extra_words_only_add():
    assert sw.danger_words("license active", extra=["license"]) == ["license"]
    assert "save" in sw.danger_words("save", extra=[])


# ------------------------------------------------------------ 规则文件

def test_load_rules_defaults_when_missing(tmp_path):
    r = sw.load_rules(str(tmp_path / "nope.json"))
    assert r["slots"] == {} and r["auto_huawei"] is True
    assert r["vendors"]["huawei"]["extra_verbs"] == []


def test_load_rules_bad_file_falls_back(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("{not json")
    assert sw.load_rules(str(p)) == sw.load_rules(None)


def test_load_rules_reads_slots_verbs_danger(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"slots": {"2": "cisco", "9": "huawei", "3": 5}, "auto_huawei": False,
                             "vendors": {"huawei": {"extra_verbs": ["ping", "tracert", "bad verb", 3],
                                                    "danger": ["license"]},
                                         "linux": {"extra_verbs": ["ls"]}},
                             "danger": ["patch"]}))
    r = sw.load_rules(str(p))
    assert r["slots"] == {2: "cisco"}                   # 槽位号不对 / 值不是字符串的丢掉
    assert r["auto_huawei"] is False
    assert r["vendors"]["huawei"]["extra_verbs"] == ["ping", "tracert"]
    assert "linux" not in r["vendors"]                  # 只有内置有只读规则的厂商才认
    assert set(r["danger"]) >= {"patch", "license"}


def test_example_rules_file_loads():
    r = sw.load_rules("examples/serial_gate.example.json")
    assert "ping" in r["vendors"]["huawei"]["extra_verbs"]


# ------------------------------------------------------------ 认厂商 / 提示符

def test_slot_vendor_auto_huawei():
    assert sw.slot_vendor(1, RULES, HUAWEI) == ("huawei", True)
    assert sw.slot_vendor(1, RULES, "x\n[~SW1-GigabitEthernet0/0/1]") == ("huawei", True)


def test_slot_vendor_skips_conbroker_marks():
    text = HUAWEI + "\n[2026-10-30 12:00:01 Claude 断开]\n"
    assert sw.slot_vendor(1, RULES, text) == ("huawei", True)


@pytest.mark.parametrize("text", ["Username:", "Password:", "  ---- More ----", "Are you sure? [Y/N]:",
                                  "<SW1>display ver", "", None, "[root@host]#", "root@host:~#"])
def test_slot_vendor_not_at_prompt(text):
    v, at = sw.slot_vendor(1, RULES, text)
    assert at is False


def test_slot_vendor_unknown_without_huawei_prompt():
    assert sw.slot_vendor(1, RULES, "SW1#") == (None, False)


def test_slot_vendor_from_rules():
    rules = dict(RULES, slots={2: "cisco"})
    assert sw.slot_vendor(2, rules, "SW1#") == ("cisco", True)
    assert sw.slot_vendor(2, rules, "SW1(config)#") == ("cisco", True)
    assert sw.slot_vendor(2, rules, "<SW1>") == ("cisco", False)     # 登记成思科, 华为提示符不算到提示符上


def test_slot_vendor_auto_off():
    rules = dict(RULES, auto_huawei=False)
    assert sw.slot_vendor(1, rules, HUAWEI) == (None, False)


# ------------------------------------------------------------ 闸门第 1 层

def test_classify_readonly_passes():
    g = sw.classify("display version", "huawei", True, RULES)
    assert g["verdict"] == "pass" and g["layer"] == 1 and g["danger"] == []


@pytest.mark.parametrize("cmd", ["dis ver", "display version ; save", "system-view", "y", "interface g0/0/1",
                                 "display diagnostic-information x.txt"])
def test_classify_non_readonly_confirms(cmd):
    g = sw.classify(cmd, "huawei", True, RULES)
    assert g["verdict"] == "confirm"


def test_classify_danger_always_confirms_even_if_readonly():
    g = sw.classify("display current-configuration | include save", "huawei", True, RULES)
    assert g["verdict"] == "confirm" and g["danger"] == ["save"]
    assert "高危" in g["reason"]


def test_classify_unknown_vendor_or_not_at_prompt_confirms():
    assert sw.classify("display version", None, True, RULES)["verdict"] == "confirm"
    g = sw.classify("display version", "huawei", False, RULES)
    assert g["verdict"] == "confirm" and "提示符" in g["reason"]


def test_classify_extra_verbs_from_rules():
    rules = sw.load_rules("examples/serial_gate.example.json")
    assert sw.classify("ping 192.0.2.1", "huawei", True, RULES)["verdict"] == "confirm"
    assert sw.classify("ping 192.0.2.1", "huawei", True, rules)["verdict"] == "pass"
    assert sw.classify("ping 192.0.2.1 ; reboot", "huawei", True, rules)["verdict"] == "confirm"


def test_classify_wrong_vendor_verb():
    assert sw.classify("show version", "huawei", True, RULES)["verdict"] == "confirm"
    assert sw.classify("show version", "cisco", True, RULES)["verdict"] == "pass"


def test_classify_key():
    assert sw.classify_key("enter")["verdict"] == "pass"
    assert sw.classify_key("space")["verdict"] == "pass"
    g = sw.classify_key("ctrl_c")
    assert g["verdict"] == "confirm" and "打断" in g["reason"]
    with pytest.raises(sw.Refused):
        sw.classify_key("ctrl_z")


# ------------------------------------------------------------ 写租约

def book():
    return sw.Book.from_text(None, T0)


def test_acquire_renew_release():
    b = book()
    lease = b.acquire(1, "agentA", T0, ttl=60, token="tokA")
    assert lease == {"slot": 1, "lease": "tokA", "ttl_s": 60, "expires_in": 60}
    assert b.holder(1, T0 + 10) == "agentA"
    b.writer(1, "tokA", T0 + 59)                          # 还在期内
    assert b.renew(1, "tokA", T0 + 50)["expires_in"] == 60
    b.writer(1, "tokA", T0 + 100)                         # 续过期: 50 + 60 = 110
    b.release(1, "tokA", T0 + 101)
    assert b.holder(1, T0 + 101) is None
    evs = [e["ev"] for e in b.events]
    assert evs == ["lease_acquire", "lease_release"]       # 续期不进日志 (每半分钟一条太吵)


def test_single_writer():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    with pytest.raises(sw.Refused) as e:
        b.acquire(1, "agentB", T0 + 1, token="tokB")
    assert e.value.code == "busy" and "agentA" in str(e.value)
    with pytest.raises(sw.Refused) as e:
        b.acquire(1, "agentA", T0 + 1, token="tokC")      # 同名也不行: 只认令牌
    assert e.value.code == "busy"
    b.acquire(2, "agentB", T0 + 1, token="tokB")          # 别的槽位不受影响
    with pytest.raises(sw.Refused) as e:
        b.writer(1, "tokB", T0 + 2)
    assert e.value.code == "lease"


def test_lease_expires_and_is_released():
    b = book()
    b.acquire(1, "agentA", T0, ttl=30, token="tokA")
    with pytest.raises(sw.Refused) as e:
        b.writer(1, "tokA", T0 + 31)
    assert e.value.code == "lease"
    with pytest.raises(sw.Refused):
        b.renew(1, "tokA", T0 + 31)                       # 过期了不能续, 要重新申请
    b.tick(T0 + 31)
    assert any(e["ev"] == "lease_expired" for e in b.events)
    assert b.acquire(1, "agentB", T0 + 32, token="tokB")["lease"] == "tokB"


def test_ttl_bounds():
    b = book()
    assert b.acquire(1, "a", T0, ttl=1, token="t1")["ttl_s"] == sw.LEASE_TTL_MIN_S
    assert b.acquire(2, "a", T0, ttl=99999, token="t2")["ttl_s"] == sw.LEASE_TTL_MAX_S


def test_release_needs_own_token():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    with pytest.raises(sw.Refused):
        b.release(1, "nope", T0 + 1)
    assert b.holder(1, T0 + 1) == "agentA"


def test_generated_token_is_random():
    b = book()
    t1 = b.acquire(1, "a", T0)["lease"]
    t2 = b.acquire(2, "a", T0)["lease"]
    assert t1 != t2 and len(t1) >= 16


def test_bad_slot():
    with pytest.raises(sw.Refused):
        book().acquire(5, "a", T0)


# ------------------------------------------------------------ 直发 (第 1 层放行) 与 在途

def _gate_pass():
    return {"verdict": "pass", "layer": 1, "reason": "只读命令", "danger": [], "vendor": "huawei"}


def _gate_confirm(danger=()):
    return {"verdict": "confirm", "layer": 3, "reason": "要人确认", "danger": list(danger), "vendor": "huawei"}


def test_begin_finish_direct_send():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    item = b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + 1, timeout=30, name="SW1")
    assert item["status"] == "sending" and "lease" not in item
    with pytest.raises(sw.Refused) as e:                   # 一条执行绪同时只有一条在途
        b.begin(1, "tokA", "agentA", "line", "display clock", _gate_pass(), T0 + 2, timeout=30)
    assert e.value.code == "inflight"
    done = b.finish(item["id"], {"status": "sent", "echo": "Huawei VRP", "ms": 120}, T0 + 3)
    assert done["status"] == "sent"
    assert b.lease_left(1, "tokA", T0 + 3) == 60             # 发完续上: Agent 等慢命令时没法续
    rec = [e for e in b.events if e["ev"] == "send"][-1]
    assert rec["slot"] == 1 and rec["src"] == "agent" and rec["who"] == "agentA" and rec["cmd"] == "display version"
    assert rec["gate"]["verdict"] == "pass" and rec["echo"] == "Huawei VRP" and rec["name"] == "SW1"


def test_begin_needs_lease():
    b = book()
    with pytest.raises(sw.Refused) as e:
        b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)
    assert e.value.code == "lease"


def test_begin_refuses_confirm_gate():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    with pytest.raises(ValueError):
        b.begin(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)


def test_stale_inflight_cleared():
    b = book()
    b.acquire(1, "agentA", T0, ttl=300, token="tokA")
    b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0, timeout=10)
    b.tick(T0 + 10 + sw.INFLIGHT_GRACE_S + 1)                # 发送进程死了: 在途标记过期
    b.begin(1, "tokA", "agentA", "line", "display clock", _gate_pass(), T0 + 200, timeout=10)


# ------------------------------------------------------------ 挂起 / 确认 / 拒绝

def test_submit_then_confirm_then_finish():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0 + 1, timeout=60, name="SW1")
    assert p["status"] == "pending" and p["danger"] == ["save"] and "lease" not in p
    view = b.view(T0 + 2)
    assert [x["id"] for x in view["pending"]] == [p["id"]]
    assert "tokA" not in json.dumps(view)                   # 令牌不给别人看
    item = b.confirm(p["id"], "human:ops", T0 + 5)
    assert item["status"] == "sending" and item["cmd"] == "save" and item["timeout"] == 60
    assert b.view(T0 + 5)["pending"] == []
    b.finish(p["id"], {"status": "sent", "echo": "ok"}, T0 + 6)
    hist = b.view(T0 + 6)["history"]
    assert hist[-1]["id"] == p["id"] and hist[-1]["status"] == "sent" and hist[-1]["by"] == "human:ops"
    evs = [e["ev"] for e in b.events]
    assert evs[-3:] == ["pending", "confirm", "send"]


def test_confirm_refused_after_lease_expired():
    b = book()
    b.acquire(1, "agentA", T0, ttl=30, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0 + 1, timeout=30)
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 40)                 # Agent 卡死, 租约过期: 它的命令作废
    assert e.value.code in ("lease", "nope")
    assert b.view(T0 + 40)["pending"] == []
    assert b.view(T0 + 40)["history"][-1]["status"] == "dropped"


def test_confirm_needs_slot_idle():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 1)
    assert e.value.code == "inflight"
    assert b.view(T0 + 1)["pending"][0]["id"] == p["id"]      # 还挂着, 等会儿再确认


def test_reject():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(["reboot"]), T0, timeout=30)
    b.reject(p["id"], "human:ops", T0 + 1, why="不许")
    assert b.view(T0 + 1)["pending"] == []
    assert b.view(T0 + 1)["history"][-1]["status"] == "rejected"
    with pytest.raises(sw.Refused):
        b.confirm(p["id"], "human:ops", T0 + 2)


def test_confirm_unknown_id():
    with pytest.raises(sw.Refused) as e:
        book().confirm(42, "human", T0)
    assert e.value.code == "nope"


def test_pending_needs_lease_and_is_capped():
    b = book()
    with pytest.raises(sw.Refused):
        b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.PENDING_MAX):
        b.submit(1, "tokA", "agentA", "line", f"x{i}", _gate_confirm(), T0, timeout=30)
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "line", "y", _gate_confirm(), T0, timeout=30)
    assert e.value.code == "busy"


def test_pending_expires():
    b = book()
    b.acquire(1, "agentA", T0, ttl=300, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    for k in range(1, 10):                                    # Agent 一直续着
        b.renew(1, "tokA", T0 + k * 200)
    b.tick(T0 + sw.PENDING_MAX_S + 1)
    assert b.view(T0 + sw.PENDING_MAX_S + 1)["pending"] == []
    with pytest.raises(sw.Refused):
        b.confirm(p["id"], "human", T0 + sw.PENDING_MAX_S + 2)


def test_release_drops_own_pending():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    b.release(1, "tokA", T0 + 1)
    assert b.view(T0 + 1)["pending"] == []


def test_history_capped():
    b = book()
    b.acquire(1, "agentA", T0, ttl=300, token="tokA")
    for i in range(sw.HISTORY_MAX + 5):
        it = b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + i, timeout=5)
        b.finish(it["id"], {"status": "sent"}, T0 + i)
    assert len(b.view(T0 + 100)["history"]) == sw.HISTORY_MAX


def test_log_reject_records_gate_refusal():
    b = book()
    b.log_refused(1, "agentA", "line", "save\rreboot", "bad", "一行里不许有控制字符", T0)
    rec = b.events[-1]
    assert rec["ev"] == "refused" and rec["slot"] == 1 and rec["why"] == "bad"


# ------------------------------------------------------------ 急停

def test_estop_one_slot_revokes_and_drops():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.acquire(2, "agentB", T0, token="tokB")
    p1 = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    p2 = b.submit(2, "tokB", "agentB", "line", "save", _gate_confirm(), T0, timeout=30)
    dropped = b.estop(1, "human:ops", "human", T0 + 1)
    assert [d["id"] for d in dropped] == [p1["id"]]
    assert b.stopped(1) and not b.stopped(2)
    assert b.holder(1, T0 + 1) is None and b.holder(2, T0 + 1) == "agentB"
    with pytest.raises(sw.Refused) as e:
        b.confirm(p1["id"], "human", T0 + 2)                # 急停后排队命令都不会发出
    assert e.value.code == "nope"
    for fn in (lambda: b.acquire(1, "agentA", T0 + 2, token="x"),
               lambda: b.writer(1, "tokA", T0 + 2),
               lambda: b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + 2, timeout=5),
               lambda: b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0 + 2, timeout=5)):
        with pytest.raises(sw.Refused):
            fn()
    assert b.view(T0 + 2)["pending"][0]["id"] == p2["id"]
    ev = [e for e in b.events if e["ev"] == "estop"][-1]
    assert ev["slot"] == 1 and ev["by"] == "human:ops" and ev["dropped"] == [p1["id"]]


def test_estop_all_is_each_slot():
    b = book()
    b.acquire(3, "agentA", T0, token="tokA")
    b.estop(None, "agentA", "agent", T0)
    assert all(b.stopped(n) for n in sw.SLOTS)
    assert sorted(e["slot"] for e in b.events if e["ev"] == "estop") == [1, 2, 3, 4]
    b.resume(2, "human:ops", T0 + 1)                         # 解单条就只解那一条
    assert not b.stopped(2) and b.stopped(1)
    b.resume(None, "human:ops", T0 + 2)
    assert not any(b.stopped(n) for n in sw.SLOTS)
    assert b.holder(3, T0 + 2) is None                      # 解除不还租约, 要重新申请


def test_estop_during_inflight_is_noted_not_recalled():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    it = b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)
    b.estop(1, "human", "human", T0 + 1)
    ev = [e for e in b.events if e["ev"] == "estop"][-1]
    assert ev["inflight"] == "display version"
    b.finish(it["id"], {"status": "sent", "echo": "x"}, T0 + 2)   # 已交出去的那条照实记
    assert b.view(T0 + 2)["history"][-1]["status"] == "sent"
    assert b.holder(1, T0 + 2) is None                            # 发完不会把租约续回来


def test_estop_twice_keeps_first_record():
    b = book()
    b.estop(1, "human:a", "human", T0)
    b.estop(1, "agentA", "agent", T0 + 5)
    assert b.view(T0 + 6)["slots"][0]["estop"]["by"] == "human:a"


# ------------------------------------------------------------ 状态文件: 存取与 fail-closed

def test_roundtrip():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    b.estop(4, "human", "human", T0)
    b2 = sw.Book.from_text(b.dumps(), T0 + 1)
    assert b2.events == []                                     # 读进来不算事件
    assert b2.holder(1, T0 + 1) == "agentA" and b2.stopped(4)
    assert b2.view(T0 + 1)["pending"][0]["id"] == p["id"]
    nxt = b2.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(), T0 + 1, timeout=30)
    assert nxt["id"] > p["id"]                                 # 编号接着走


@pytest.mark.parametrize("text", ["{bad", "[]", '{"v": 1, "slots": "x"}', '{"v": 99}', "null"])
def test_corrupt_state_is_estop_all(text):
    b = sw.Book.from_text(text, T0)
    assert all(b.stopped(n) for n in sw.SLOTS)
    assert b.view(T0)["slots"][0]["estop"]["by"] == "state-corrupt"
    assert any(e["ev"] == "estop" for e in b.events)


def test_malformed_items_are_estop_all():
    good = json.loads(book().dumps())
    good["items"] = [{"id": 1, "status": "pending"}]          # 缺字段
    b = sw.Book.from_text(json.dumps(good), T0)
    assert all(b.stopped(n) for n in sw.SLOTS)
    good["items"] = []
    good["slots"]["2"]["lease"] = {"who": "x"}                 # 租约缺字段
    assert sw.Book.from_text(json.dumps(good), T0).stopped(1)


def test_missing_state_is_fresh():
    b = sw.Book.from_text(None, T0)
    assert not any(b.stopped(n) for n in sw.SLOTS)
    assert sw.Book.from_text("", T0).stopped(1) is False


# ------------------------------------------------------------ 日志行 / 回显摘要

def test_echo_summary_clips_middle():
    text = "\n".join(f"line {i}" for i in range(1000))
    s = sw.echo_summary(text)
    assert len(s) <= sw.ECHO_MAX + 40 and s.startswith("line 0") and s.endswith("line 999") and "…" in s
    assert sw.echo_summary("short") == "short"
    assert sw.echo_summary(None) == ""


def test_log_line_is_one_line():
    line = sw.log_line({"t": "x", "ev": "send", "cmd": "a\nb"})
    assert "\n" not in line and json.loads(line)["cmd"] == "a\nb"


def test_events_have_timestamps_and_slot():
    b = book()
    b.acquire(2, "agentA", T0, token="tokA")
    e = b.events[0]
    assert e["slot"] == 2 and e["t"].startswith("20") and e["src"] == "agent"
