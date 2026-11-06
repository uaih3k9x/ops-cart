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


@pytest.mark.parametrize("cmd, hit", [
    ("ls;rm -rf /", "rm"), ("ls&&reboot", "reboot"), ("echo `reboot`", "reboot"), ("x $(reboot)", "reboot"),
    ("r'm' -rf /", "rm"), ('"reboot"', "reboot"), ("\\rm -rf /", "rm"), ("cat a>b;dd if=x", "dd"),
])
def test_danger_words_see_through_shell_tricks(cmd, hit):
    """Linux shell 能拼接、引号、反斜杠拆词; 切词时都按分隔算, 引号反斜杠去掉 (往多拦的方向错)."""
    assert hit in sw.danger_words(cmd)


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
    assert r["vendors"]["linux"]["extra_verbs"] == []   # 只有内置有只读规则的厂商能加只读动词
    assert set(r["danger"]) >= {"patch", "license"}


def test_example_rules_file_loads():
    r = sw.load_rules("examples/serial_gate.example.json")
    assert "ping" in r["vendors"]["huawei"]["extra_verbs"]
    assert set(r["vendors"]) >= {"huawei", "cisco", "ruijie", "linux"}
    assert r["model"]["enabled"] is False               # 样例默认不开模型: 装上不会凭空多出一层判断
    assert r["vendors"]["linux"]["model"] is False      # root shell 开了模型也先不交它判


def test_load_rules_model_defaults_off():
    r = sw.load_rules(None)
    assert r["model"] == sw.MODEL_DEFAULT and r["model"]["enabled"] is False
    for v in ("huawei", "cisco", "ruijie", "linux"):
        assert r["vendors"][v]["model"] is True and r["vendors"][v]["notes"] == ""


def test_load_rules_model_section(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"model": {"enabled": True, "timeout_s": 15, "budget_usd": 0.1, "context_chars": 2000,
                                       "model": "deepseek-chat"},
                             "vendors": {"linux": {"model": False}, "huawei": {"notes": "V8 要 commit"}}}))
    r = sw.load_rules(str(p))
    assert r["model"] == {"enabled": True, "timeout_s": 15, "budget_usd": "0.1", "context_chars": 2000,
                          "model": "deepseek-chat"}
    assert r["vendors"]["linux"]["model"] is False
    assert r["vendors"]["huawei"]["notes"] == "V8 要 commit" and r["vendors"]["huawei"]["model"] is True


@pytest.mark.parametrize("model", [
    {"enabled": "yes"}, {"enabled": 1}, {"timeout_s": "x"}, {"budget_usd": "abc"}, {"budget_usd": -1},
    {"budget_usd": 99}, {"model": "a b"}, {"model": "--dangerously-skip-permissions"}, {"context_chars": None},
    "on", [1],
])
def test_load_rules_bad_model_values_fall_back(tmp_path, model):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"model": model}))
    assert sw.load_rules(str(p))["model"] == sw.MODEL_DEFAULT


def test_load_rules_model_numbers_clamped(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"model": {"timeout_s": 9999, "context_chars": 10 ** 7}}))
    r = sw.load_rules(str(p))["model"]
    assert r["timeout_s"] == sw.MODEL_TIMEOUT_MAX_S and r["context_chars"] == sw.MODEL_CONTEXT_MAX


def test_load_rules_vendor_notes_capped(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"vendors": {"huawei": {"notes": "长" * 5000, "model": "no"}, "cisco": {"notes": 3}}}))
    r = sw.load_rules(str(p))
    assert len(r["vendors"]["huawei"]["notes"]) == sw.NOTES_MAX and r["vendors"]["huawei"]["model"] is True
    assert r["vendors"]["cisco"]["notes"] == ""


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


def test_slot_vendor_ruijie_and_linux_need_registering():
    rules = dict(RULES, slots={3: "ruijie", 4: "linux"})
    assert sw.slot_vendor(3, rules, "Ruijie#") == ("ruijie", True)
    assert sw.slot_vendor(3, rules, "Ruijie(config-if-GigabitEthernet 0/1)#") == ("ruijie", True)
    assert sw.slot_vendor(3, rules, "Password:") == ("ruijie", False)
    for ln in ("root@host:~#", "ops@cart-01:/var/log$", "[root@host ~]#", "bash-4.4#", "sh-5.1$", "$", "#"):
        assert sw.slot_vendor(4, rules, "x\n" + ln) == ("linux", True), ln
    for ln in ("Password:", "[sudo] password for ops:", "Are you sure? [y/N]", "--More--", "root@host:~# ls"):
        assert sw.slot_vendor(4, rules, ln) == ("linux", False), ln
    assert sw.slot_vendor(1, RULES, "root@host:~#") == (None, False)    # 不登记认不出


def test_load_rules_slots_accept_new_vendors(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"slots": {"3": "ruijie", "4": "linux", "2": "juniper"}}))
    assert sw.load_rules(str(p))["slots"] == {3: "ruijie", 4: "linux"}


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


# ------------------------------------------------------------ 闸门第 2 层接口 (#22): 模型经 judge 注入

def _judge(allow, reason="理由", calls=None, **model):
    def j(cmd, vendor):
        if calls is not None:
            calls.append((cmd, vendor))
        return {"allow": allow, "reason": reason, "model": dict(model, verdict="allow" if allow else "block")}
    return j


def test_classify_without_judge_is_unchanged_confirm():
    g = sw.classify("system-view", "huawei", True, RULES)
    assert g["verdict"] == "confirm" and g["layer"] == 3 and "没开" in g["reason"]
    assert "model" not in g


def test_classify_judge_allow_is_layer2_pass():
    calls = []
    g = sw.classify("system-view", "huawei", True, RULES, judge=_judge(True, "进配置视图, 不改配置", calls))
    assert calls == [("system-view", "huawei")]
    assert g["verdict"] == "pass" and g["layer"] == 2 and g["danger"] == [] and g["vendor"] == "huawei"
    assert "进配置视图, 不改配置" in g["reason"] and g["model"]["verdict"] == "allow"


def test_classify_judge_block_confirms_with_reason():
    g = sw.classify("undo ip address", "huawei", True, RULES, judge=_judge(False, "会断管理口"))
    assert g["verdict"] == "confirm" and g["layer"] == 3 and "会断管理口" in g["reason"]
    assert g["model"]["verdict"] == "block"


def test_classify_judge_not_consulted_for_readonly_danger_unknown_or_off_prompt():
    calls = []
    j = _judge(True, calls=calls)
    assert sw.classify("display version", "huawei", True, RULES, judge=j)["layer"] == 1
    assert sw.classify("save", "huawei", True, RULES, judge=j)["verdict"] == "confirm"
    assert sw.classify("reboot fast", "linux", True, RULES, judge=j)["verdict"] == "confirm"
    assert sw.classify("system-view", None, True, RULES, judge=j)["verdict"] == "confirm"
    assert sw.classify("system-view", "huawei", False, RULES, judge=j)["verdict"] == "confirm"
    assert calls == []                                  # 高危的不送模型


@pytest.mark.parametrize("bad", [
    lambda c, v: (_ for _ in ()).throw(RuntimeError("boom")),
    lambda c, v: None, lambda c, v: "allow", lambda c, v: {"allow": "true", "reason": "x"},
    lambda c, v: {"allow": 1, "reason": "x"}, lambda c, v: {"reason": "x"}, lambda c, v: {"allow": True},
    lambda c, v: {"allow": True, "reason": ""}, lambda c, v: {"allow": True, "reason": 5},
])
def test_classify_judge_misbehaving_is_confirm(bad):
    g = sw.classify("system-view", "huawei", True, RULES, judge=bad)
    assert g["verdict"] == "confirm" and g["layer"] == 3


@pytest.mark.parametrize("cmd", ["ls;id", "ls && id", "ls > /tmp/a", "cat < a", "echo `id`", "echo $(id)",
                                 "ls \\", "sleep 9 &", "sudo !!"])
def test_classify_linux_shell_meta_never_goes_to_model(cmd):
    calls = []
    g = sw.classify(cmd, "linux", True, RULES, judge=_judge(True, calls=calls))
    assert g["verdict"] == "confirm" and calls == []
    assert "拼接" in g["reason"]


def test_classify_linux_pipe_goes_to_model():
    g = sw.classify("dmesg | tail -n 50", "linux", True, RULES, judge=_judge(True))
    assert g["verdict"] == "pass" and g["layer"] == 2


def test_classify_vendor_model_off_by_rules():
    rules = dict(RULES, vendors=dict(RULES["vendors"], linux=dict(RULES["vendors"]["linux"], model=False)))
    calls = []
    g = sw.classify("uptime", "linux", True, rules, judge=_judge(True, calls=calls))
    assert g["verdict"] == "confirm" and calls == [] and "linux" in g["reason"]


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
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0 + 1, timeout=60, name="SW1", context="<SW1>")
    assert p["status"] == "pending" and p["danger"] == ["save"] and "lease" not in p
    view = b.view(T0 + 2)
    assert [x["id"] for x in view["pending"]] == [p["id"]]
    assert "tokA" not in json.dumps(view)                   # 令牌不给别人看
    item = b.confirm(p["id"], "human:ops", T0 + 5, current="<SW1>")
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
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0 + 1, timeout=30, context="<SW1>")
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 40, current="<SW1>")                 # Agent 卡死, 租约过期: 它的命令作废
    assert e.value.code in ("lease", "nope")
    assert b.view(T0 + 40)["pending"] == []
    assert b.view(T0 + 40)["history"][-1]["status"] == "dropped"


def test_confirm_needs_slot_idle():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.begin(1, "tokA", "agentA", "line", "interface GigabitEthernet0/0/2", _gate_pass(), T0, timeout=30)
    with pytest.raises(sw.Refused) as e:                     # 在途那条还没生效, 现在挂起的上下文是假的 (#68 审查)
        b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context="<SW1>")
    assert e.value.code == "inflight"
    assert b.view(T0)["pending"] == []
    b.finish(1, {"status": "sent", "echo": ""}, T0 + 1)
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0 + 2, timeout=30, context="<SW1>")
    a2 = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0 + 2, timeout=30, context="<SW1>")
    b.confirm(p["id"], "human", T0 + 3, current="<SW1>")
    with pytest.raises(sw.Refused) as e:                     # 确认另一条在途时, 这条等着
        b.confirm(a2["id"], "human", T0 + 4, current="<SW1>")
    assert e.value.code == "inflight"
    assert [x["id"] for x in b.view(T0 + 4)["pending"]] == [a2["id"]]      # 还挂着, 等会儿再确认


def test_reject():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(["reboot"]), T0, timeout=30, context="<SW1>")
    b.reject(p["id"], "human:ops", T0 + 1, why="不许")
    assert b.view(T0 + 1)["pending"] == []
    assert b.view(T0 + 1)["history"][-1]["status"] == "rejected"
    with pytest.raises(sw.Refused):
        b.confirm(p["id"], "human:ops", T0 + 2, current="<SW1>")


def test_confirm_unknown_id():
    with pytest.raises(sw.Refused) as e:
        book().confirm(42, "human", T0, current="<SW1>")
    assert e.value.code == "nope"


def test_pending_needs_lease_and_is_capped():
    b = book()
    with pytest.raises(sw.Refused):
        b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.PENDING_MAX):
        b.submit(1, "tokA", "agentA", "line", f"x{i}", _gate_confirm(), T0, timeout=30, context="<SW1>")
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "line", "y", _gate_confirm(), T0, timeout=30, context="<SW1>")
    assert e.value.code == "busy"


def test_pending_expires():
    b = book()
    b.acquire(1, "agentA", T0, ttl=300, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
    for k in range(1, 10):                                    # Agent 一直续着
        b.renew(1, "tokA", T0 + k * 200)
    b.tick(T0 + sw.PENDING_MAX_S + 1)
    assert b.view(T0 + sw.PENDING_MAX_S + 1)["pending"] == []
    with pytest.raises(sw.Refused):
        b.confirm(p["id"], "human", T0 + sw.PENDING_MAX_S + 2, current="<SW1>")


def test_release_drops_own_pending():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
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
    p1 = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
    p2 = b.submit(2, "tokB", "agentB", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
    dropped = b.estop(1, "human:ops", "human", T0 + 1)
    assert [d["id"] for d in dropped] == [p1["id"]]
    assert b.stopped(1) and not b.stopped(2)
    assert b.holder(1, T0 + 1) is None and b.holder(2, T0 + 1) == "agentB"
    with pytest.raises(sw.Refused) as e:
        b.confirm(p1["id"], "human", T0 + 2, current="<SW1>")                # 急停后排队命令都不会发出
    assert e.value.code == "nope"
    for fn in (lambda: b.acquire(1, "agentA", T0 + 2, token="x"),
               lambda: b.writer(1, "tokA", T0 + 2),
               lambda: b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + 2, timeout=5),
               lambda: b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0 + 2, timeout=5, context="<SW1>")):
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
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context="<SW1>")
    b.estop(4, "human", "human", T0)
    b2 = sw.Book.from_text(b.dumps(), T0 + 1)
    assert b2.events == []                                     # 读进来不算事件
    assert b2.holder(1, T0 + 1) == "agentA" and b2.stopped(4)
    assert b2.view(T0 + 1)["pending"][0]["id"] == p["id"]
    nxt = b2.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(), T0 + 1, timeout=30, context="<SW1>")
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


# ------------------------------------------------------------ 挂起期间不许直发 (#65)

def test_direct_send_refused_while_slot_has_pending():
    """挂起 shutdown 等人确认时, Agent 不能再经第 1/2 层直发 interface X 改设备上下文."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(["shutdown"]), T0, timeout=30, context="<SW1>")
    for kind, cmd in (("line", "interface GigabitEthernet0/0/2"), ("line", "display version"), ("key", "enter")):
        with pytest.raises(sw.Refused) as e:
            b.begin(1, "tokA", "agentA", kind, cmd, _gate_pass(), T0 + 1, timeout=30)
        assert e.value.code == "pending" and f"#{p['id']}" in str(e.value)
    assert b.view(T0 + 1)["slots"][0]["inflight"] is None
    assert [x["id"] for x in b.view(T0 + 1)["pending"]] == [p["id"]]      # 挂起项原样还在


def test_direct_send_on_other_slot_unaffected():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.acquire(2, "agentA", T0, token="tokB")
    b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context="<SW1>")
    assert b.begin(2, "tokB", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)["status"] == "sending"


def test_direct_send_allowed_again_after_pending_resolved():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context="<SW1>")
    b.reject(p["id"], "human", T0 + 1)
    assert b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + 2, timeout=30)["status"] == "sending"


def test_confirm_voided_if_slot_sent_something_after_submit():
    """纵深: 挂起之后槽位上又发过别的 (例如人先确认了同槽位另一条), 现场可能变了, 这条批准作废."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    a = b.submit(1, "tokA", "agentA", "line", "interface GigabitEthernet0/0/2", _gate_confirm(), T0, timeout=30, context="<SW1>")
    s = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(["shutdown"]), T0, timeout=30, context="<SW1>")
    b.confirm(a["id"], "human", T0 + 1, current="<SW1>")
    b.finish(a["id"], {"status": "sent", "echo": ""}, T0 + 2)
    with pytest.raises(sw.Refused) as e:
        b.confirm(s["id"], "human", T0 + 3, current="<SW1>")
    assert e.value.code == "stale"
    last = b.view(T0 + 3)["history"][-1]
    assert last["id"] == s["id"] and last["status"] == "dropped" and "重新提交" in last["why"]


def test_confirm_ok_when_nothing_sent_since_submit():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)
    b.finish(1, {"status": "sent", "echo": ""}, T0 + 1)
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0 + 2, timeout=30, context="<SW1>")
    assert b.confirm(p["id"], "human", T0 + 3, current="<SW1>")["status"] == "sending"


def test_old_state_item_without_send_mark_is_voided():
    """升级前就挂着的条目没有发送序号: 按现场可能变了算, 不放行."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context="<SW1>")
    doc = json.loads(b.dumps())
    for it in doc["items"]:
        it.pop("sent_mark", None)
    b2 = sw.Book.from_text(json.dumps(doc), T0 + 1)
    with pytest.raises(sw.Refused) as e:
        b2.confirm(p["id"], "human", T0 + 2, current="<SW1>")
    assert e.value.code == "stale"


def test_pending_records_device_context_untrusted_and_bounded():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    text = "\n".join(f"line {i}" for i in range(50)) + "\n x" * 0 + "\n" + "Z" * 5000 + "\n\x1b[2J<SW1>\r\n"
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context=text)
    ctx = b.view(T0)["pending"][0]["context"]
    assert ctx["src"] == "device" and ctx["untrusted"] is True
    assert len(ctx["lines"]) <= sw.CONTEXT_LINES and ctx["lines"][-1] == "<SW1>"
    assert all(len(ln) <= sw.CONTEXT_LINE_MAX for ln in ctx["lines"])
    assert sum(len(ln) for ln in ctx["lines"]) <= sw.CONTEXT_MAX
    assert "context" not in json.dumps([e for e in b.events if e["ev"] == "pending"])   # 不进执行绪日志
    assert p["context"] == ctx


def test_pending_context_masks_secrets():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    text = (" local-user admin password irreversible-cipher $1a$Secr3t$\n snmp-agent community read cipher Pub1c\n"
            " enable secret 5 $1$abc$\n username ops password 0 hunter2\n<SW1>")
    b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context=text)
    blob = json.dumps(b.view(T0)["pending"][0]["context"])
    for s in ("Secr3t", "Pub1c", "$1$abc$", "hunter2"):
        assert s not in blob, s
    assert "<SW1>" in blob and "local-user admin password" in blob


def test_pending_context_neutralises_format_chars():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30,
             context="接口\u202edown\u200b\t<SW1>")
    assert b.view(T0)["pending"][0]["context"]["lines"] == ["接口?down? <SW1>"]


@pytest.mark.parametrize("ctx", [None, "", "\r\n  \n", "\x1b[2J"])
def test_submit_without_device_output_refused(ctx):
    """读不到设备输出就没法核对确认时有没有变 (#68 第二轮低5): 不挂起."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context=ctx)
    assert e.value.code == "context"
    assert b.view(T0)["pending"] == []


# ------------------------------------------------------------ #68 审查: 时序缺口 / 确认时重读提示符 / 打码

GE1 = "interface GigabitEthernet0/0/1\n[~SW1-GigabitEthernet0/0/1]"
GE2 = "interface GigabitEthernet0/0/2\n[~SW1-GigabitEthernet0/0/2]"


def test_submit_refused_if_slot_sent_since_output_was_read():
    """读完设备输出、等第 2 层模型的时候另一路直发完了: 挂起时用的上下文已经旧了, 不挂."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    mark = b.send_mark(1, "tokA", T0)
    b.begin(1, "tokA", "agentA", "line", "interface GigabitEthernet0/0/2", _gate_pass(), T0, timeout=30)
    b.finish(1, {"status": "sent", "echo": ""}, T0 + 0.5)
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0 + 1, timeout=30, context=GE1, mark=mark)
    assert e.value.code == "stale"
    assert b.view(T0 + 1)["pending"] == []
    ok = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0 + 2, timeout=30, context=GE2,
                  mark=b.send_mark(1, "tokA", T0 + 2))
    assert ok["status"] == "pending"


def test_send_mark_needs_lease():
    with pytest.raises(sw.Refused) as e:
        book().send_mark(1, "nope", T0)
    assert e.value.code == "lease"


def test_confirm_voided_if_prompt_changed():
    """人用通道 / conbroker 超时后迟到的字节改了设备视图: 确认时重读最后一行, 和挂起时不同就作废."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context=GE1)
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 1, current=GE2)
    assert e.value.code == "stale"
    last = b.view(T0 + 1)["history"][-1]
    assert last["status"] == "dropped" and "新输出" in last["why"] and "[~SW1-GigabitEthernet0/0/2]" in last["why"]


def test_confirm_ok_when_prompt_same():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context=GE1)
    assert b.confirm(p["id"], "human", T0 + 1, current="interface GigabitEthernet0/0/1\r\n[~SW1-GigabitEthernet0/0/1]\r\n")["status"] == "sending"


def test_confirm_unreadable_output_refused_but_kept():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context=GE1)
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 1, current=None)
    assert e.value.code == "prompt"
    assert [x["id"] for x in b.view(T0 + 1)["pending"]] == [p["id"]]


@pytest.mark.parametrize("bad", [-1, "x", True, 1.5, None])
def test_from_text_bad_send_counter_means_all_stopped(bad):
    b = book()
    doc = json.loads(b.dumps())
    doc["slots"]["2"]["sent"] = bad
    b2 = sw.Book.from_text(json.dumps(doc), T0)
    assert all(b2.stopped(n) for n in sw.SLOTS)


def test_from_text_old_slots_without_counter_ok():
    doc = json.loads(book().dumps())
    for v in doc["slots"].values():
        v.pop("sent")
    assert not sw.Book.from_text(json.dumps(doc), T0).stopped(1)


def test_old_pending_without_mark_has_own_reason():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(["save"]), T0, timeout=30, context="<SW1>")
    doc = json.loads(b.dumps())
    doc["items"][-1].pop("sent_mark")
    b2 = sw.Book.from_text(json.dumps(doc), T0 + 1)
    with pytest.raises(sw.Refused) as e:
        b2.confirm(p["id"], "human", T0 + 2, current="<SW1>")
    assert e.value.code == "stale" and "升级前" in str(e.value)


def test_context_dropped_when_item_leaves_pending():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    a = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context=GE1)
    r = b.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(), T0, timeout=30, context=GE1)
    b.reject(r["id"], "human", T0 + 1)
    b.confirm(a["id"], "human", T0 + 2, current=GE1)
    assert all("context" not in it for it in b.view(T0 + 2)["history"])
    assert all("context" not in it for it in json.loads(b.dumps())["items"])


@pytest.mark.parametrize("line,secrets", [
    ("snmp-server user u1 g1 v3 auth sha Auth#Pass1 priv aes 128 Priv#Pass2", ["Auth#Pass1", "Priv#Pass2"]),
    ("tacacs-server key 7 0822455D0A16", ["0822455D0A16"]),
    ("radius-server host 192.0.2.1 key S3cretKey", ["S3cretKey"]),
    ("crypto isakmp key MyPSK123 address 192.0.2.4", ["MyPSK123"]),
    (" ip ospf message-digest-key 1 md5 OspfKey9", ["OspfKey9"]),
    ("set psksecret ENC abcdef", ["abcdef"]),
    ("set admin_password Foo", ["Foo"]),
    ("wpa-passphrase hunter2", ["hunter2"]),
    ("local-user admin password irreversible-cipher $1c$abc", ["$1c$abc"]),
    ("snmp-agent community read cipher %^%#abc", ["%^%#abc"]),
    (" key 1 md5 BgpKey", ["BgpKey"]),
    ("username admin privilege 15 secret 9 $9$xyz", ["$9$xyz"]),
    ("neighbor 192.0.2.9 password BgpPw", ["BgpPw"]),
    ('set system root-authentication encrypted-password "$6$abc"', ["$6$abc"]),
    ("PASS\u200bWORD cipher Xy9", ["Xy9"]),
    ("pass\x1b[0mword Foo", ["Foo"]),
    ("pa\u00adssword Foo", ["Foo"]),
    ("\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44 Foo", ["Foo"]),
    ("user-password=Foo", ["Foo"]),
    ("login-password Foo", ["Foo"]),
    ("pre_shared_key=Foo", ["Foo"]),
])
def test_context_masks_review_samples(line, secrets):
    ctx = sw.device_context(line + "\n<SW1>")
    blob = "\n".join(ctx["lines"])
    for sec in secrets:
        assert sec not in blob, (line, blob)
    assert ctx["lines"][-1] == "<SW1>"


def test_context_masks_private_key_block_and_base64():
    text = ("display current\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxyzSECRET\nAbCdEfGh\n"
            "-----END RSA PRIVATE KEY-----\nZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFy\n<SW1>")
    blob = "\n".join(sw.device_context(text)["lines"])
    for sec in ("MIIEowIBAAKCAQEAxyzSECRET", "AbCdEfGh", "Zm9vYmFyZm9vYmFy"):
        assert sec not in blob
    assert blob.endswith("<SW1>")


def test_context_block_start_outside_window_still_masked():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(f"k{i}secretline" for i in range(20)) + "\n"
    blob = "\n".join(sw.device_context(text)["lines"])
    assert "secretline" not in blob


def test_context_plain_words_not_masked():
    ctx = sw.device_context("interface GigabitEthernet0/0/1\n description uplink-keyboard\n<SW1>")
    assert ctx["lines"] == ["interface GigabitEthernet0/0/1", " description uplink-keyboard", "<SW1>"]


@pytest.mark.parametrize("raw,shown", [("a b", "a b"), ("a\u2028b", "a?b"), ("a\u2029b", "a?b"), ("a\u3000b", "a?b"),
                                       ("a\u00a0b", "a?b"), ("a\u0085b", "a?b"), ("x\u202ey", "x?y"),
                                       ("\x1b[31mred\x1b[0m", "red")])
def test_context_separators_and_escapes(raw, shown):
    assert sw.device_context(raw)["lines"] == [shown]


# ------------------------------------------------------------ #68 第二轮审查

CISCO_A = "SW1(config)#interface Gi0/1\nSW1(config-if)#"
CISCO_B = "SW1(config)#interface Gi0/1\nSW1(config-if)#interface Gi0/2\nSW1(config-if)#"


def test_send_mark_refused_while_inflight():
    """在途时取序号: 那条的回显还没到, 读到的上下文是生效前的 (中2)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.begin(1, "tokA", "agentA", "line", "interface Gi0/2", _gate_pass(), T0, timeout=30)
    with pytest.raises(sw.Refused) as e:
        b.send_mark(1, "tokA", T0)
    assert e.value.code == "inflight"


def test_confirm_voided_on_any_new_output_same_prompt():
    """思科类提示符不带对象名: 最后一行一样, 但中间多了一行 interface Gi0/2, 照样作废 (中1a)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context=CISCO_A)
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 1, current=CISCO_B)
    assert e.value.code == "stale" and "新输出" in str(e.value)


def test_confirm_same_output_ok_modulo_cr_and_escapes():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30, context=CISCO_A)
    assert b.confirm(p["id"], "human", T0 + 1, current="SW1(config)#interface Gi0/1\r\n\x1b[0mSW1(config-if)#\r\n")[
        "status"] == "sending"


@pytest.mark.parametrize("host", ["AUTH-SW1", "key-core", "SW-PSK", "core_md5"])
def test_compare_not_fooled_by_masked_hostname(host):
    """主机名里带 auth/key/psk/md5: 打码会把提示符抹平, 比对用的是没打码的原文 (低3)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0, timeout=30,
                 context=f"[{host}-GigabitEthernet0/0/1]")
    with pytest.raises(sw.Refused) as e:
        b.confirm(p["id"], "human", T0 + 1, current=f"[{host}-GigabitEthernet0/0/2]")
    assert e.value.code == "stale"


@pytest.mark.parametrize("prompt", ["[AUTH-SW1-GigabitEthernet0/0/1]", "<key-core>", "[SW-PSK]", "R1(config-keychain-key)#",
                                    "root@key-host:~#"])
def test_prompt_shown_unmasked(prompt):
    assert sw.device_context(prompt)["lines"] == [prompt]


def test_prompt_prefix_kept_but_rest_masked():
    assert sw.device_context("[SW1-aaa]local-user admin password Foo")["lines"] == ["[SW1-aaa]local-user admin password ******"]
    line = sw.device_context("root@key-host:~# mysql -uroot -pHunter2")["lines"][0]
    assert line.startswith("root@key-host:~# mysql") and "Hunter2" not in line


def test_tail_hash_not_public_and_old_items_without_it_voided():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30, context=CISCO_A)
    assert "tail_hash" not in json.dumps(b.view(T0)) and "tail_hash" not in json.dumps(p)
    doc = json.loads(b.dumps())
    doc["items"][-1].pop("tail_hash")
    b2 = sw.Book.from_text(json.dumps(doc), T0 + 1)
    with pytest.raises(sw.Refused) as e:
        b2.confirm(p["id"], "human", T0 + 2, current=CISCO_A)
    assert e.value.code == "stale" and "升级前" in str(e.value)


def test_view_lists_recent_sent_commands_per_slot():
    """可信信息: 推车自己记的本槽位最近实际发出的命令 (含人确认的), 和设备输出分开 (中1b)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.acquire(2, "agentA", T0, token="tokB")
    b.begin(1, "tokA", "agentA", "line", "interface Gi0/2", _gate_pass(), T0, timeout=30)
    b.finish(1, {"status": "sent", "echo": ""}, T0 + 1)
    b.begin(2, "tokB", "agentA", "line", "display version", _gate_pass(), T0, timeout=30)
    b.finish(2, {"status": "sent", "echo": ""}, T0 + 1)
    r = b.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(), T0 + 2, timeout=30, context="<SW1>")
    b.reject(r["id"], "human", T0 + 3)
    c = b.submit(1, "tokA", "agentA", "line", "undo shutdown", _gate_confirm(), T0 + 4, timeout=30, context="<SW1>")
    b.confirm(c["id"], "human:ops", T0 + 5, current="<SW1>")
    b.finish(c["id"], {"status": "timeout", "echo": ""}, T0 + 6)
    b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0 + 7, timeout=30, context="<SW1>")
    recent = b.view(T0 + 8)["slots"][0]["recent"]
    assert [(x["cmd"], x["status"]) for x in recent] == [("interface Gi0/2", "sent"), ("undo shutdown", "timeout")]
    assert recent[-1]["by"] == "human:ops" and "lease" not in json.dumps(recent)
    assert [x["cmd"] for x in b.view(T0 + 8)["slots"][1]["recent"]] == ["display version"]


def test_recent_capped():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.RECENT_MAX + 3):
        b.begin(1, "tokA", "agentA", "line", f"display x{i}", _gate_pass(), T0 + i, timeout=30)
        b.finish(i + 1, {"status": "sent", "echo": ""}, T0 + i)
    recent = b.view(T0 + 50)["slots"][0]["recent"]
    assert len(recent) == sw.RECENT_MAX and recent[-1]["cmd"] == f"display x{sw.RECENT_MAX + 2}"


def test_begin_with_stale_mark_refused():
    """直发也绑上下文: 第 2 层模型看的输出读完之后槽位又发过别的, 不发 (低6)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    mark = b.send_mark(1, "tokA", T0)
    b.begin(1, "tokA", "agentA", "line", "interface Gi0/2", _gate_pass(), T0, timeout=30)
    b.finish(1, {"status": "sent", "echo": ""}, T0 + 1)
    with pytest.raises(sw.Refused) as e:
        b.begin(1, "tokA", "agentA", "line", "undo shutdown", _gate_pass(), T0 + 2, timeout=30, mark=mark)
    assert e.value.code == "stale"
    assert b.begin(1, "tokA", "agentA", "line", "display version", _gate_pass(), T0 + 3, timeout=30,
                   mark=b.send_mark(1, "tokA", T0 + 3))["status"] == "sending"


@pytest.mark.parametrize("line,secrets", [
    ("<SW1>snmp-agent usm-user v3 admin authentication-mode sha Admin@1234 privacy-mode aes128 Priv@1234",
     ["Admin@1234", "Priv@1234"]),
    ("root@h:~# echo 'root:Hunter2!' | chpasswd", ["Hunter2"]),
    ("root@h:~# PGPASSWORD=Hunter2 psql -h db", ["Hunter2"]),
    ("root@h:~# mysql -uroot -pHunter2", ["Hunter2"]),
    ("root@h:~# curl -u admin:Hunter2 https://example.org", ["Hunter2"]),
    ("root@h:~# curl https://admin:Hunter2@example.org/", ["Hunter2"]),
    ("root@h:~# export TOKEN=ghp_abcdefABCDEF0123456789", ["ghp_abcdef"]),
    ("root@h:~# export GITHUB_TOKEN=ghp_x1", ["ghp_x1"]),
    ("root@h:~# apikey=sk-live-123", ["sk-live-123"]),
    ("root@h:~# htpasswd -b f u Hunter2", ["Hunter2"]),
    ("Authorization: Bearer eyJhbGciOi.xx.yy", ["eyJhbGciOi"]),
    ("root:$6$salt$hashhashhash:19000:0:99999:7:::", ["hashhash", "salt"]),
    ("$1c$abcdefghijklmnopqrstuvwxyz0123456789$", ["abcdefghij"]),
    ("  %^%#qrstuvwxyzABCDEF0123456789%^%#", ["qrstuvwxyz"]),
    (" $9$shortX", ["shortX"]),
    ("$6$s$h", ["$s$h"]),
])
def test_context_masks_round2_samples(line, secrets):
    blob = "\n".join(sw.device_context(line + "\n<SW1>")["lines"])
    for sec in secrets:
        assert sec not in blob, (line, blob)
    assert blob.endswith("<SW1>")


def test_wrapped_cipher_continuation_masked():
    text = "[SW1-aaa]local-user admin password irreversible-cipher\n$1c$abcdefghijklmnopqrstuvwxyz0123456789$\n" \
           "[SW1]ike peer p1\n pre-shared-key cipher %^%#abcdefghijklmnop\n  %^%#qrstuvwxyzABCDEF0123456789%^%#\n[SW1]"
    blob = "\n".join(sw.device_context(text)["lines"])
    for sec in ("abcdefghij", "qrstuvwxyz"):
        assert sec not in blob


def test_round2_plain_lines_not_masked():
    for ln in ("interface GigabitEthernet0/0/1", " description uplink-keyboard", "ping -c 3 192.0.2.1",
               "show interfaces status", "root@h:~# ls -la /tmp"):
        assert sw.device_context(ln)["lines"] == [ln], ln
