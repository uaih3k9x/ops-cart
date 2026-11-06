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


QUIET = 60                              # 设备 / 人一分钟没动静


def test_classify_key():
    assert sw.classify_key("enter", "<SW1>", QUIET)["verdict"] == "pass"
    assert sw.classify_key("space", "<SW1>", QUIET)["verdict"] == "pass"
    g = sw.classify_key("ctrl_c", "<SW1>", QUIET)
    assert g["verdict"] == "confirm" and "打断" in g["reason"]
    with pytest.raises(sw.Refused):
        sw.classify_key("ctrl_z", "<SW1>")


# #73: 回车 / 空格在确认类提示上等于替人回答, 一律要人确认
CONFIRM_PROMPTS = [
    "Proceed with reload? [confirm]",                                            # 思科 reload
    "Destination filename [startup-config]?",                                    # 思科 copy run start
    "Delete filename [vlan.dat]?",
    "Delete flash:/vlan.dat? [confirm]",
    "Erasing the nvram filesystem will remove all configuration files! Continue? [confirm]",
    "Would you like to enter the initial configuration dialog? [yes/no]:",
    "Warning: The current configuration will be written to the device. Continue? [Y/N]:",   # 华为 save
    "System will reboot! Continue ? [y/n]:",                                     # 华为 reboot
    "The current configuration will be written to the device. Are you sure? [Y/N]:",       # H3C
    "Are you sure to continue?[Y/N]",
    "Info: Are you sure",
    "Do you want to continue? [Y/n]",                                            # Linux 包管理
    "Proceed ([y]/n)?",
    "Are you sure you want to continue connecting (yes/no/[fingerprint])?",      # ssh 首次连
    "Remove this file? (y/n)",
    "Do you want to save the current configuration?",                            # 以问号收尾的问句
    "Save? [Y/N]",
]
NOT_CONFIRM = [
    "<SW1>", "[~SW1]", "[*SW1-GigabitEthernet0/0/1]", "[yard-sw]", "R1#", "R1(config)#", "R1(config-if)#",
    "ruijie(config-if-range)#", "ops@host:~$", "[root@host ~]#", "bash-4.4#",
    "--More--", "  ---- More ----", " --More-- ", "Password:", "Username:", "login:",
]
PASSES = [ln for ln in NOT_CONFIRM if not ln.rstrip().endswith(":")]   # 白名单: 只有提示符和翻页放行


@pytest.mark.parametrize("line", CONFIRM_PROMPTS)
@pytest.mark.parametrize("key", ["enter", "space"])
def test_key_on_confirm_prompt_needs_human(key, line):
    text = f"display current\nsome output\n{line}"
    assert sw.is_confirm_prompt(line), line
    g = sw.classify_key(key, text, QUIET)
    assert g["verdict"] == "confirm" and g["layer"] == 3 and "确认提示" in g["reason"], line


@pytest.mark.parametrize("line", NOT_CONFIRM)
def test_not_confirm_prompt(line):
    assert not sw.is_confirm_prompt(line), line


@pytest.mark.parametrize("line", PASSES)
def test_key_on_normal_prompt_passes(line):
    for key in ("enter", "space"):
        assert sw.classify_key(key, f"output\n{line}", QUIET)["verdict"] == "pass", (key, line)


def test_key_skips_conbroker_marker_lines():
    text = "Proceed with reload? [confirm]\n[2026-11-06 10:00:00 Claude 断开]\n"
    assert sw.classify_key("enter", text, QUIET)["verdict"] == "confirm"


@pytest.mark.parametrize("text", [None, "", "\n\n", "[2026-11-06 10:00:00 Claude 接入]", "\x1b[0m\n\x07\n"])
def test_key_without_last_line_is_unknown(text):
    for key in ("enter", "space"):
        g = sw.classify_key(key, text, QUIET)
        assert g["verdict"] == "confirm" and "看不到" in g["reason"], (key, text)


def test_classify_key_defaults_are_unknown():
    assert sw.classify_key("enter")["verdict"] == "confirm"           # 没给设备输出 = 未知, 要人确认
    assert sw.classify_key("enter", "<SW1>")["verdict"] == "confirm"  # 不知道最近有没有人在敲 = 可能有人


# ---- 审查 #74: 白名单 / 控制字符 / 人最近在敲

@pytest.mark.parametrize("text", [
    "R1#reload\nProceed with reload? [confirm]\n*Nov  6 09:00:01.123: %LINK-3-UPDOWN: Interface "
    "GigabitEthernet0/1, changed state to up",
    "<HUAWEI>save\nWarning: The current configuration will be written to the device. Continue? [Y/N]:\n"
    "Nov  6 2026 09:00:01 HUAWEI %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface "
    "GigabitEthernet0/0/1 has entered the UP state.",
    "Proceed with reload? [confirm]\n.",
    "display interface\n  Input:  1234 packets, 5678 bytes",                       # 半截输出
    "%Mar  1 00:00:05.123: %SYS-5-RESTART: System restarted --",                    # 开机日志滚屏
])
def test_key_after_async_log_or_half_output_needs_human(text):
    for key in ("enter", "space"):
        g = sw.classify_key(key, text, QUIET)
        assert g["verdict"] == "confirm" and "不在提示符" in g["reason"], (key, text)


@pytest.mark.parametrize("text", [
    "R1#reload\nProceed with reload? [confirm]\n\x1b[0m",
    "R1#reload\nProceed with reload? [confirm]\n\x07",
    "Continue?\x1b[0m", "Continue? \x1b[K", "Continue?\x00", "Continue?\x1b[1;32m\r\n",
])
def test_control_chars_do_not_hide_confirm(text):
    for key in ("enter", "space"):
        assert sw.classify_key(key, text, QUIET)["verdict"] == "confirm", (key, text)


@pytest.mark.parametrize("text", ["<SW1>\x1b[K", "display x\n  ---- More ----\x1b[16D", "R1#\x07", "\x1b[1mR1#\x1b[0m"])
def test_prompt_with_control_chars_still_passes(text):
    assert sw.classify_key("enter", text, QUIET)["verdict"] == "pass", text


def test_last_line_strips_escapes():
    assert sw.last_line("a\nContinue?\x1b[0m\n\x07\n") == "Continue?"


@pytest.mark.parametrize("quiet", [None, 0, 2.5, sw.HUMAN_QUIET_S - 0.1])
def test_key_held_when_someone_may_be_typing(quiet):
    for key in ("enter", "space"):
        g = sw.classify_key(key, "<SW1>", quiet)
        assert g["verdict"] == "confirm" and "动静" in g["reason"], (key, quiet)
    assert sw.HUMAN_QUIET_S >= 3                                         # 不短于 conbroker 的人类优先窗口


EXTRA_CONFIRM = [
    "继续吗？", "是否继续（是/否）", "Overwrite? [Y|N]", "Overwrite? y or n", "是/否", "Delete? [N/y]",
    "Continue (yes|no)", "Erase (y,N)", "[Y]es/[N]o:", "Type 'yes' to continue",
    "Info: Please input the file name(*.cfg, *.zip, *.dat)[vrpcfg.zip]:", "Save running config [no]:",
]


@pytest.mark.parametrize("line", EXTRA_CONFIRM)
def test_more_confirm_prompt_forms(line):
    assert sw.is_confirm_prompt(line), line
    for key in ("enter", "space"):
        assert sw.classify_key(key, f"x\n{line}", QUIET)["verdict"] == "confirm", (key, line)


@pytest.mark.parametrize("line", ["[Y/N]", "[confirm]", "[y/n]:", "[yes/no]"])
def test_slot_vendor_does_not_take_confirm_line_as_huawei_prompt(line):
    rules = dict(sw.load_rules(None), auto_huawei=True)
    assert sw.slot_vendor(1, rules, f"save\n{line}") == (None, False)
    rules["slots"] = {1: "huawei"}
    assert sw.slot_vendor(1, rules, f"save\n{line}") == ("huawei", False)


def test_key_gate_carries_last_line():
    g = sw.classify_key("enter", "x\nProceed with reload? [confirm]\x1b[0m", QUIET)
    assert g["line"] == "Proceed with reload? [confirm]"


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
    first = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=30)
    p = b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30)   # 有挂起时只有打断键能排
    b.confirm(first["id"], "human", T0)                       # 第一条在途
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
    b.submit(1, "tokA", "agentA", "line", "x0", _gate_confirm(), T0, timeout=30)
    for i in range(sw.PENDING_MAX - 1):                        # 有挂起时只有打断键能排 (#74 第四轮)
        b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30)
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30)
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
    nxt = b2.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0 + 1, timeout=30)
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


# ---- 审查 #74 第二轮: probe.py 全部写法、厂商专用提示符、回答词 / 引导提示、翻页空格、平方复杂度

# (最后一行, 未登记厂商时回车的结论). 引导程序 / 回答词 / "yes or no" 都不算提示符.
PROBE = [
    ("U-Boot>", "confirm"), ("=>", "confirm"), ("boot>", "confirm"), ("grub>", "confirm"),
    ("mysql>", "pass"), ("ftp>", "pass"), ("#", "pass"), ("$", "pass"),
    ("[Y]", "confirm"), ("[N]", "confirm"), ("[yes]", "confirm"), ("[continue]", "confirm"),
    ("Save(yes or no)>", "confirm"), ("Continue(yes or no)#", "confirm"), ("Ruijie(continue yes or no)#", "confirm"),
    ("R(config)#", "pass"), ("[root@h ~]#", "pass"), ("root@h:~$", "pass"),
    ("Proceed with reload? [confirm]", "confirm"), ("Router#Proceed? [confirm]", "confirm"),
    ("Delete flash:x? [confirm]", "confirm"), ("--More-- (y/n)", "confirm"),
    ("-- More --", "pass"), ("--More--", "pass"), ("---- More ----", "pass"), ("Overwrite? -- More --", "confirm"),
    ("<HUAWEI>", "pass"), ("[~HUAWEI-GigabitEthernet0/0/1]", "pass"), ("[*HUAWEI]", "pass"),
    ("Erase nvram: Continue>", "confirm"), ("Continue>", "confirm"), ("Proceed>", "confirm"), ("yes>", "confirm"),
    ("y#", "confirm"), ("OK>", "confirm"), ("ok", "confirm"), ("Press any key>", "confirm"),
    ("Delete>", "confirm"), ("Confirm>", "confirm"), ("[Delete]", "confirm"), ("[Y/N]", "confirm"),
    ("[startup-config]", "pass"), ("user@h:[Y/N]$", "confirm"), ("[confirm-reload]", "pass"),
    ("(y/n)#", "confirm"), ("Router(y/n)#", "confirm"), ("R(yes/no)#", "confirm"),
    ("Router(config-if range)#", "pass"), ("Ruijie(Confirm continue y or n)#", "confirm"),
    ("Ruijie(Are you sure)#", "confirm"), ("Ruijie(type yes to go)#", "confirm"),
]


@pytest.mark.parametrize("line,want", PROBE)
def test_probe_lines(line, want):
    assert sw.classify_key("enter", line, QUIET)["verdict"] == want, line


@pytest.mark.parametrize("text,want", [
    ("Proceed? [confirm]\rRouter#", "pass"), ("Router#\x1b(B", "pass"),
    ("\x1b]0;root@h: ~\x07root@h:~# ", "pass"),                         # OSC 标题序列整个剥掉
    (" --More-- \r          \rRouter#", "pass"),                         # \r 擦掉翻页再出提示符
    ("Proceed? [confirm]\x1b[2K\x1b[1GRouter#", "confirm"),              # 光标擦行: 往多拦的方向错
    ("Proceed? [confirm]\x1b[1000D\x1b[KRouter#", "confirm"),
    ("Proceed? [confirm]\x1bcRouter#", "confirm"), ("Proceed [confirm]\x1b[1;1HRouter#", "confirm"),
    (" --More-- " + "\b" * 10 + " " * 10 + "\b" * 10 + "Router#", "confirm"),
])
def test_probe_escape_sequences(text, want):
    assert sw.classify_key("enter", text, QUIET)["verdict"] == want, repr(text)


@pytest.mark.parametrize("line", ["[Y/N]", "[confirm]", "(y/n)", "[yes/no]:", "[Y]", "[continue]", "U-Boot>", "y#"])
def test_at_any_prompt_rejects_answers(line):
    assert sw.at_any_prompt(line) is False


def test_registered_vendor_uses_only_its_prompt():
    assert sw.at_any_prompt("<SW1>") and sw.at_any_prompt("<SW1>", "huawei")
    assert not sw.at_any_prompt("<SW1>", "cisco")                         # 登记了思科, 华为式 <名> 不算
    assert not sw.at_any_prompt("root@h:~$", "huawei")
    assert sw.at_any_prompt("R1(config)#", "cisco") and sw.at_any_prompt("--More--", "cisco")
    assert sw.classify_key("enter", "<SW1>", QUIET, vendor="cisco")["verdict"] == "confirm"
    assert sw.classify_key("enter", "R1#", QUIET, vendor="cisco")["verdict"] == "pass"


def test_pager_space_skips_quiet_window():
    assert sw.classify_key("space", "out\n---- More ----", 0.5)["verdict"] == "pass"     # 翻页空格不会回答确认
    assert sw.classify_key("enter", "out\n---- More ----", 0.5)["verdict"] == "confirm"  # 回车照旧等 5 秒
    assert sw.classify_key("space", "<SW1>", 0.5)["verdict"] == "confirm"
    assert sw.classify_key("space", "--More-- (y/n)", 0.5)["verdict"] == "confirm"


@pytest.mark.parametrize("line", ["?" + " " * 32000 + "x", "[a]" + " " * 32000 + "x", "?" * 32000 + "x",
                                  "x" + " " * 32000 + "?"])
def test_long_line_is_fast(line):
    import time
    t = time.perf_counter()
    sw.is_confirm_prompt(line)
    sw.classify_key("enter", line, QUIET)
    assert time.perf_counter() - t < 0.2


def test_long_question_still_confirm():
    assert sw.is_confirm_prompt("x" * 5000 + " continue ?") and sw.is_confirm_prompt("继续吗？：")


# ---- 审查 #74 第二轮【中】: 同槽位有更早的挂起项时不收新的回车 / 空格 (chain.py)

def _book_with_lease():
    b = sw.Book.from_text(None, 1000.0)
    return b, b.acquire(1, "agent", 1000.0, ttl=300)["lease"]


def test_chain_enter_after_pending_reload_refused():
    b, tok = _book_with_lease()
    rules = {"slots": {1: "cisco"}, "danger": list(sw.DANGER)}
    b.submit(1, tok, "agent", "line", "reload", sw.classify("reload", "cisco", True, rules), 1000.0, 30)
    g = sw.classify_key("enter", "Router#", 1)
    assert g["verdict"] == "confirm"
    with pytest.raises(sw.Refused) as e:
        b.submit(1, tok, "agent", "key", "enter", g, 1001.0, 5)
    assert e.value.code == "busy" and "前面" in str(e.value)
    for key in ("space",):
        with pytest.raises(sw.Refused):
            b.submit(1, tok, "agent", "key", key, sw.classify_key(key, "Router#", 1), 1001.0, 5)


def test_only_ctrl_c_queues_behind_pending():
    """第四轮: 同槽位有挂起项时一律不收新的命令和按键 (挂起或直发), 只有打断键能排."""
    b, tok = _book_with_lease()
    b.submit(1, tok, "agent", "line", "reload", sw._gate("confirm", "x"), 1000.0, 30)
    b.submit(1, tok, "agent", "key", "ctrl_c", sw.classify_key("ctrl_c"), 1001.0, 5)
    assert len(b.view(1002.0)["pending"]) == 2
    for kind, cmd in (("line", "display version"), ("line", "interface g0/1"), ("key", "enter")):
        with pytest.raises(sw.Refused) as e:
            b.submit(1, tok, "agent", kind, cmd, sw._gate("confirm", "x"), 1001.0, 30)
        assert e.value.code == "busy"
        with pytest.raises(sw.Refused):
            b.begin(1, tok, "agent", kind, cmd, sw._gate("pass", "x"), 1001.0, 30)


# ---- 审查 #74 第三轮: 尖括号是否选项、括号里的回答词、OSC 不跨行

@pytest.mark.parametrize("line", ["Reboot now <Y/N>", "<yes/no>", "Continue <y/n>?", "Reboot(y)>", "R(yes)#", "Sw(n)#"])
def test_angle_brackets_and_paren_answers_need_human(line):
    assert sw.classify_key("enter", line, QUIET)["verdict"] == "confirm", line
    assert not sw.at_any_prompt(line), line


def test_angle_yes_no_is_confirm_prompt():
    assert sw.is_confirm_prompt("<Y/N>") and sw.is_confirm_prompt("Save? <yes/no>")
    assert not sw.is_confirm_prompt("<HUAWEI>")


@pytest.mark.parametrize("text,keep", [
    ("\x1b]0;x\nProceed with reload? [confirm]\x07", "[confirm]"),      # OSC 没在本行收尾: 不能跨行吞掉确认提示
    ("\x1b]2;t\nContinue? [Y/N]:\x07", "[Y/N]"),
])
def test_osc_does_not_cross_lines(text, keep):
    assert keep in sw.clean(text)
    assert sw.classify_key("enter", text, QUIET)["verdict"] == "confirm"


@pytest.mark.parametrize("ans", ["y", "YES", "n", "no", "c", "confirm", "continue", "ye", "y.", "yes.", "yes please",
                                 "yep", "yeah", "Y!", "ok.", "y y", "sure"])
def test_any_line_cannot_queue_behind_pending(ans):
    """第四轮: 不再判"像不像回答词" (ye / yes please / Y! 都能绕), 前面有挂起项就一律不收."""
    b, tok = _book_with_lease()
    b.submit(1, tok, "agent", "line", "reload", sw._gate("confirm", "x"), 1000.0, 30)
    with pytest.raises(sw.Refused):
        b.submit(1, tok, "agent", "line", ans, sw._gate("confirm", "x"), 1001.0, 30)
    with pytest.raises(sw.Refused):
        b.begin(1, tok, "agent", "line", ans, sw._gate("pass", "x"), 1001.0, 30)
