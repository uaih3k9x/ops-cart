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
    assert all(r["vendors"][v]["extra_verbs"] == [] for v in r["vendors"])   # #56 第四轮: 样例不再开 ping 类
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


def test_classify_extra_verbs_from_rules(tmp_path):
    p = tmp_path / "g.json"                                 # 样例不再开 ping (#56 第四轮), 这里自己配
    p.write_text(json.dumps({"vendors": {"huawei": {"extra_verbs": ["ping"]}}}))
    rules = sw.load_rules(str(p))
    assert sw.classify("ping 192.0.2.1", "huawei", True, RULES)["verdict"] == "confirm"
    assert sw.classify("ping 192.0.2.1", "huawei", True, rules, user_view=True)["verdict"] == "pass"
    assert sw.classify("ping 192.0.2.1 ; reboot", "huawei", True, rules, user_view=True)["verdict"] == "confirm"


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
    a2 = b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0 + 2, timeout=30, context="<SW1>")  # 有挂起时只有打断键能排 (#74)
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
    b.submit(1, "tokA", "agentA", "line", "x0", _gate_confirm(), T0, timeout=30, context="<SW1>")
    for i in range(sw.PENDING_MAX - 1):                        # 有挂起时只有打断键能排 (#74 第四轮)
        b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30, context="<SW1>")
    with pytest.raises(sw.Refused) as e:
        b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30, context="<SW1>")
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
    nxt = b2.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0 + 1, timeout=30, context="<SW1>")
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
    s = b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30, context="<SW1>")   # 有挂起时只有打断键能排 (#74)
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
    r = b.submit(1, "tokA", "agentA", "key", "ctrl_c", _gate_confirm(), T0, timeout=30, context=GE1)   # 有挂起时只有打断键能排 (#74)
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


@pytest.mark.parametrize("prompt", ["root@key-host:~#", "auth-sw1#", "<SW1>", "[~SW1-GigabitEthernet0/0/1]"])
def test_prompt_shown_unmasked(prompt):
    assert sw.device_context(prompt)["lines"] == [prompt]


@pytest.mark.parametrize("prompt", ["[AUTH-SW1-GigabitEthernet0/0/1]", "<key-core>", "[SW-PSK]", "R1(config-keychain-key)#"])
def test_prompt_with_secret_word_in_brackets_no_longer_exempt(prompt):
    """第四轮低3: 括号 / 尖括号 / 方括号里出现口令词一律不豁免 (第二轮这几个曾是整行原样显示)."""
    assert sw.MASK in sw.device_context(prompt)["lines"][0]


def test_prompt_prefix_kept_but_rest_masked():
    assert sw.device_context("[SW1-aaa]local-user admin password Foo")["lines"] == ["[SW1-aaa]local-user admin password ******"]
    line = sw.device_context("root@db1:~# mysql -uroot -pHunter2")["lines"][0]
    assert line.startswith("root@db1:~# mysql") and "Hunter2" not in line


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


def test_view_lists_trusted_commands_per_slot():
    """可信信息: 推车自己记的本槽位实际发出的命令 (含人确认的), 和设备输出分开 (中1b); 拒绝的 / 没发的不算."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.acquire(2, "agentA", T0, token="tokB")
    _direct(b, 1, "tokA", "line", "interface Gi0/2", gate=_gate_model())
    _direct(b, 2, "tokB", "line", "display version")
    r = b.submit(1, "tokA", "agentA", "line", "reboot", _gate_confirm(), T0 + 2, timeout=30, context="<SW1>")
    b.reject(r["id"], "human", T0 + 3)
    c = b.submit(1, "tokA", "agentA", "line", "undo shutdown", _gate_confirm(), T0 + 4, timeout=30, context="<SW1>")
    b.confirm(c["id"], "human:ops", T0 + 5, current="<SW1>")
    b.finish(c["id"], {"status": "timeout", "echo": ""}, T0 + 6)
    b.submit(1, "tokA", "agentA", "line", "shutdown", _gate_confirm(), T0 + 7, timeout=30, context="<SW1>")
    tr = b.view(T0 + 8)["slots"][0]["trusted"]
    assert [(x["cmd"], x["status"]) for x in tr["changes"]] == [("interface Gi0/2", "sent"), ("undo shutdown", "timeout")]
    assert tr["changes"][-1]["by"] == "human:ops" and "lease" not in json.dumps(tr)
    assert [x["cmd"] for x in b.view(T0 + 8)["slots"][1]["trusted"]["recent"]] == ["display version"]


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


# ------------------------------------------------------------ #68 第三轮审查

def _direct(b, slot, tok, kind, cmd, now=T0, gate=None, status="sent"):
    it = b.begin(slot, tok, "agentA", kind, cmd, gate or _gate_pass(), now, timeout=5,
                 mark=b.send_mark(slot, tok, now))
    b.finish(it["id"], {"status": status, "echo": ""}, now)
    return it


def _gate_model():
    return {"verdict": "pass", "layer": 2, "reason": "模型放行", "danger": [], "vendor": "cisco"}


def test_trusted_log_not_flushed_by_enters_or_readonly():
    """5 次回车 / 5 条只读 show 冲不掉 interface Gi0/2 (中1)."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    _direct(b, 1, "tokA", "line", "interface Gi0/2", gate=_gate_model())
    for _ in range(5):
        _direct(b, 1, "tokA", "key", "enter")
    for i in range(5):
        _direct(b, 1, "tokA", "line", f"show run int gi0/{i}")
    tr = b.view(T0)["slots"][0]["trusted"]
    assert [x["cmd"] for x in tr["changes"]] == ["interface Gi0/2"]
    assert tr["after"] == 6                                   # 之后: 回车 (合并成一条 ×5) + 5 条只读
    assert [x["cmd"] for x in tr["recent"]] == [f"show run int gi0/{i}" for i in range(5)]


def test_trusted_log_merges_repeated_keys():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    _direct(b, 1, "tokA", "line", "interface Gi0/2", gate=_gate_model())
    for _ in range(sw.TRUST_LOG_MAX * 2):
        _direct(b, 1, "tokA", "key", "enter")
    tr = b.view(T0)["slots"][0]["trusted"]
    assert [x["cmd"] for x in tr["changes"]] == ["interface Gi0/2"]
    assert [(x["cmd"], x["count"]) for x in tr["recent"]] == [("enter", sw.TRUST_LOG_MAX * 2)]


def test_trusted_log_survives_global_history_flood():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.acquire(2, "agentA", T0, token="tokB")
    _direct(b, 1, "tokA", "line", "interface Gi0/2", gate=_gate_model())
    tok = "tokB"
    for r in range(7):
        for i in range(5):                          # 有挂起时只有打断键能排 (#74): 第一条命令, 其余打断键
            kind, cmd = ("line", f"x{i}") if i == 0 else ("key", "ctrl_c")
            b.submit(2, tok, "agentA", kind, cmd, _gate_confirm(), T0, timeout=5, context="SW#")
        b.release(2, tok, T0)
        tok = b.acquire(2, "agentA", T0)["lease"]
    tr = b.view(T0)["slots"][0]["trusted"]
    assert [x["cmd"] for x in tr["changes"]] == ["interface Gi0/2"]


def test_trusted_log_capped_per_slot():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.TRUST_LOG_MAX + 5):
        _direct(b, 1, "tokA", "line", f"conf{i}", gate=_gate_model())
    doc = json.loads(b.dumps())
    assert len(doc["slots"]["1"]["log"]) == sw.CHANGE_KEEP           # 第四轮: 改视图的最多留 CHANGE_KEEP 条
    assert doc["slots"]["1"]["log_lost"] is True
    tr = b.view(T0)["slots"][0]["trusted"]
    assert len(tr["changes"]) == sw.CHANGE_KEEP and tr["changes"][-1]["cmd"] == f"conf{sw.TRUST_LOG_MAX + 4}"


def test_trusted_log_includes_human_confirmed_and_status():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "undo shutdown", _gate_confirm(), T0, timeout=5, context="<SW1>")
    b.confirm(p["id"], "human:ops", T0, current="<SW1>")
    b.finish(p["id"], {"status": "timeout", "echo": ""}, T0)
    (e,) = b.view(T0)["slots"][0]["trusted"]["changes"]
    assert e["cmd"] == "undo shutdown" and e["by"] == "human:ops" and e["status"] == "timeout"


@pytest.mark.parametrize("bad", ["x", [1], [{"cmd": 1}], {"a": 1}])
def test_bad_trusted_log_means_all_stopped(bad):
    doc = json.loads(book().dumps())
    doc["slots"]["1"]["log"] = bad
    assert all(sw.Book.from_text(json.dumps(doc), T0).stopped(n) for n in sw.SLOTS)


@pytest.mark.parametrize("line,secret", [
    ("<password>Hunter2</password>", "Hunter2"), ("<key>S3cr3tKey</key>", "S3cr3tKey"),
    ("<secret>abc123</secret>", "abc123"), ("<community>pr1vate</community>", "pr1vate"), ("<psk>abc</psk>", "abc"),
    ("  <password>Hunter2</password>", "Hunter2"), ("[password=Hunter2]", "Hunter2"), ("[key:S3cr3t]", "S3cr3t"),
    ("[client]password=Hunter2", "Hunter2"), ("root@key-host:~# mysql -pHunter2", "Hunter2"),
    ("<AUTH-SW1>display current | include Hunter2", "Hunter2"),
    ("[mysqld]\npassword = Hunter2", "Hunter2"),
])
def test_prompt_shaped_prefix_does_not_hide_secrets(line, secret):
    blob = "\n".join(sw.device_context(line + "\n<SW1>")["lines"])
    assert secret not in blob, blob


def test_marks_and_blank_lines_do_not_void_approval():
    """人开 / 关控制台看一眼, conbroker 写的标记行 (加空行) 不让批准作废 (低3)."""
    body = "\n".join(f"line{i}" for i in range(20)) + "\n<HUAWEI>"
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=5, context=body)
    later = body + "\n\n[2026-11-06 10:00:00 人 接入]\n\n[2026-11-06 10:01:00 人 断开]\n"
    assert b.confirm(p["id"], "human", T0 + 1, current=later)["status"] == "sending"


def test_short_log_mark_does_not_void():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=5, context="<HUAWEI>")
    assert b.confirm(p["id"], "human", T0 + 1, current="<HUAWEI>\n[2026-11-06 10:00:00 人 接入]\n")["status"] == "sending"


def test_hash_uses_last_effective_lines_only():
    a = "\n".join(f"old{i}" for i in range(30)) + "\n" + "\n".join(f"l{i}" for i in range(sw.HASH_LINES))
    c = "\n".join(f"other{i}" for i in range(30)) + "\n" + "\n".join(f"l{i}" for i in range(sw.HASH_LINES))
    assert sw._tail_hash(a) == sw._tail_hash(c)
    assert sw._tail_hash(a) != sw._tail_hash(a + "\n<HUAWEI>")


def test_trusted_keys_readonly_even_without_layer():
    """闸门结果缺 layer 也不让回车把 interface 挤出"可能改视图"那一栏."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    nolayer = {"verdict": "pass", "reason": "x", "danger": []}
    _direct(b, 1, "tokA", "line", "interface Gi0/2", gate=nolayer)
    for _ in range(5):
        _direct(b, 1, "tokA", "key", "enter", gate=nolayer)
    tr = b.view(T0)["slots"][0]["trusted"]
    assert [x["cmd"] for x in tr["changes"]] == ["interface Gi0/2"] and tr["recent"][0]["count"] == 5


# ------------------------------------------------------------ #68 第四轮审查

@pytest.mark.parametrize("keys", [["enter", "space"], ["enter"], ["space"]])
def test_change_entry_survives_readonly_flood(keys):
    """中1: interface 之后交替 / 连续按 20 次只读键, 改视图的那条不能被挤掉."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    _direct(b, 1, "tokA", "line", "interface g2", gate=_gate_model())
    for i in range(sw.TRUST_LOG_MAX * 2):
        _direct(b, 1, "tokA", "key", keys[i % len(keys)])
    tr = b.view(T0)["slots"][0]["trusted"]
    assert [x["cmd"] for x in tr["changes"]] == ["interface g2"] and not tr["lost"]
    assert len(json.loads(b.dumps())["slots"]["1"]["log"]) <= sw.TRUST_LOG_MAX


def test_change_entries_capped_and_loss_flagged():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.CHANGE_KEEP + 3):
        _direct(b, 1, "tokA", "line", f"conf{i}", gate=_gate_model())
    log = json.loads(b.dumps())["slots"]["1"]["log"]
    assert [e["cmd"] for e in log] == [f"conf{i}" for i in range(3, sw.CHANGE_KEEP + 3)]
    tr = b.view(T0)["slots"][0]["trusted"]
    assert tr["lost"] is True and len(tr["changes"]) == sw.CHANGE_KEEP            # 第五轮: 留着的全给
    assert [x["cmd"] for x in tr["changes"]][-1] == f"conf{sw.CHANGE_KEEP + 2}"


def test_readonly_dropped_before_changes():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    _direct(b, 1, "tokA", "line", "interface g2", gate=_gate_model())
    for i in range(sw.TRUST_LOG_MAX + 5):
        _direct(b, 1, "tokA", "line", f"display x{i}")
    log = json.loads(b.dumps())["slots"]["1"]["log"]
    assert log[0]["cmd"] == "interface g2" and len(log) == sw.TRUST_LOG_MAX
    assert not b.view(T0)["slots"][0]["trusted"]["lost"]


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_bad_lost_flag_means_all_stopped(bad):
    doc = json.loads(book().dumps())
    doc["slots"]["1"]["log_lost"] = bad
    assert all(sw.Book.from_text(json.dumps(doc), T0).stopped(n) for n in sw.SLOTS)


def test_inflight_timeout_marks_trusted_entry_unknown():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    it = b.begin(1, "tokA", "agentA", "line", "interface g2", _gate_model(), T0, timeout=5,
                 mark=b.send_mark(1, "tokA", T0))
    b.tick(T0 + 5 + sw.INFLIGHT_GRACE_S + 1)
    (e,) = [x for x in json.loads(b.dumps())["slots"]["1"]["log"] if x["id"] == it["id"]]
    assert e["status"] == "unknown"


@pytest.mark.parametrize("line", ["R1(snmp-server community Pub1ic RO)#", "<secret-Abc123>",
                                  "[~HUAWEI-cipher%^%#abc]", "<SW-password-x>", "[key-Abc123]"])
def test_prompt_with_secret_word_in_brackets_masked(line):
    shown = "\n".join(sw.device_context(line)["lines"])
    assert shown != line and sw.MASK in shown, shown


@pytest.mark.parametrize("line", ["<SW1>", "[SW1-GigabitEthernet0/0/1]", "auth-sw1#", "key-host(config)#"])
def test_prompt_without_bracketed_secret_still_unmasked(line):
    assert sw.device_context(line)["lines"] == [line]


def test_registered_vendor_prompt_regex_only():
    """槽位登记了华为: 思科式 auth-sw1# 不算整行提示符, 照常打码 (没登记时用合集, 原样显示)."""
    assert sw.MASK in sw.device_context("auth-sw1#", vendor="huawei")["lines"][0]
    assert sw.device_context("auth-sw1#")["lines"] == ["auth-sw1#"]
    assert sw.device_context("core-sw1#", vendor="cisco")["lines"] == ["core-sw1#"]
    assert sw.device_context("<SW1>", vendor="huawei")["lines"] == ["<SW1>"]


@pytest.mark.parametrize("line", ["Qx7pL2mZ9vR4tY8wK3nB", "aB3$dE6^gH9*jK2@mN5!", "U2FsdGVkX19vR4tY8wK3nBqZ",
                                  "%^%#Qx7pL2mZ9vR4tY8%^%#"])
def test_wrapped_cipher_like_line_masked(line):
    assert sw.device_context(line)["lines"] == [sw.MASK]


@pytest.mark.parametrize("line", ["GigabitEthernet0/0/1", "--------------------------", "/etc/sysconfig/network",
                                  "00e0-fc12-3456-7890", "2026-11-06T10:00:00Z", "XGigabitEthernet0/0/12",
                                  "192.0.2.10/255.255.255.0", "Ethernet1/0/1(10G)", "==========================",
                                  "https://example.com/doc", "abcdefghijklmnopqrstu"])
def test_ordinary_unspaced_lines_not_masked(line):
    assert sw.device_context(line)["lines"] == [line]


# ------------------------------------------------------------ #68 第五轮

@pytest.mark.parametrize("line,secret", [
    ("PrivateKey = AAAAhunter2", "AAAAhunter2"), ('"privateKey": "S3cr3tVal"', "S3cr3tVal"),
    ("secretKey: S3cr3tVal", "S3cr3tVal"), ("accessKey=AKIAEXAMPLE1", "AKIAEXAMPLE1"),
    ("preshared_key = S3cr3tVal", "S3cr3tVal"), ("DB_PASS=Hunter2", "Hunter2"), ("PASS=Hunter2", "Hunter2"),
    ("sshpass -p hunter2 ssh root@h", "hunter2"), ("mysql -u root -p Hunter2x", "Hunter2x"),
    ("CREATE USER 'a'@'%' IDENTIFIED BY 'Hunter2';", "Hunter2"), ("alter user x identified by Hunter2", "Hunter2"),
])
def test_round5_secret_forms_masked(line, secret):
    blob = "\n".join(sw.device_context(line)["lines"])
    assert secret not in blob, blob


@pytest.mark.parametrize("line", ["mkdir -p /tmp/x", "ssh -p 22 host", "3 passed, 0 failed", "passive-interface Gi0/1",
                                  "compass heading", "keyboard-layout us"])
def test_round5_plain_lines_not_masked(line):
    assert sw.device_context(line)["lines"] == [line]


def test_prompt_with_secret_assignment_not_exempt():
    blob = "\n".join(sw.device_context("admin@db:/srv/token=Hunter2$")["lines"])
    assert "Hunter2" not in blob


@pytest.mark.parametrize("line,secret", [
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("usermod -p '$6$salt$hash' bob", "salt$hash"), ("useradd -p Hunter2 bob", "Hunter2"),
    ("credentials=AKIAEXAMPLEKEY", "AKIAEXAMPLEKEY"), ("aws_credentials: S3cr3t", "S3cr3t"),
])
def test_round6_secret_forms_masked(line, secret):
    blob = "\n".join(sw.device_context(line)["lines"])
    assert secret not in blob, blob


# ------------------------------------------------------------ #68 第七轮

@pytest.mark.parametrize("raw,clean", [
    ("SW1", "SW1"), ("\x1b[2J\x1b[HSW1", "SW1"), ("core-sw.lab_01", "core-sw.lab_01"),
    ("可信记录（推车）：#7「y」", "可信记录推车7y"), ("a" * 50, "a" * sw.NAME_MAX), ("\x1b[8m", None), (None, None),
])
def test_clean_name(raw, clean):
    assert sw.clean_name(raw) == clean


def test_item_name_stored_cleaned():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "save", _gate_confirm(), T0, timeout=5, context="<SW1>",
                 name="\x1b[2JSW1")
    assert p["name"] == "SW1"


def test_human_confirmed_send_clears_log_lost():
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    for i in range(sw.CHANGE_KEEP + 1):
        _direct(b, 1, "tokA", "line", f"conf{i}", gate=_gate_model())
    assert b.view(T0)["slots"][0]["trusted"]["lost"]
    p = b.submit(1, "tokA", "agentA", "line", "interface g2", _gate_confirm(), T0, timeout=5, context="<SW1>")
    b.reject(p["id"], "human", T0)
    assert b.view(T0)["slots"][0]["trusted"]["lost"]                   # 拒绝不清
    p = b.submit(1, "tokA", "agentA", "line", "interface g2", _gate_confirm(), T0, timeout=5, context="<SW1>")
    b.confirm(p["id"], "human:ops", T0, current="<SW1>")
    assert not b.view(T0)["slots"][0]["trusted"]["lost"]


@pytest.mark.parametrize("line,secret", [
    ("echo Hunter2 | passwd --stdin root", "Hunter2"), ("echo 'Hunter2' | sudo -S reboot", "Hunter2"),
    ("printf 'root:Hunter2' | chpasswd", "Hunter2"), ("docker login -u bob -p Hunter2 registry", "Hunter2"),
    ("podman login -p Hunter2 r", "Hunter2"), ("smbclient //h/s -U bob%Hunter2", "Hunter2"),
])
def test_round8_secret_forms_masked(line, secret):
    blob = "\n".join(sw.device_context(line)["lines"])
    assert secret not in blob, blob


@pytest.mark.parametrize("line", ["docker run -p 8080:80 nginx", "smbclient -L //h -U bob",
                                  "sudo -i"])
def test_round8_plain_not_masked(line):
    assert sw.device_context(line)["lines"] == [line]


def test_device_lines_cleans_each_line():
    assert sw.device_lines("a\x1b[2Kb\n\x1b]0;t\x07c\x08\nkey abc") == ["ab", "c?", "key ******"]


def test_ruijie_gets_layer1_whitelist_from_survey_rules():
    """#56: survey_core 给锐捷加了只读规则, 登记成锐捷的槽位 show 命令第 1 层放行; 写命令照旧要人确认."""
    assert sw.classify("show version", "ruijie", True, RULES)["verdict"] == "pass"
    assert sw.classify("terminal length 0", "ruijie", True, RULES)["verdict"] == "pass"
    for cmd in ("configure terminal", "write memory", "reload", "terminal length 0 ; reload"):
        assert sw.classify(cmd, "ruijie", True, RULES)["verdict"] == "confirm", cmd



@pytest.mark.parametrize("vendor,ok,bad", [
    ("huawei", "screen-length 0 temporary", ["screen-length 0", "screen-length 0 temporary ; save",
                                              "screen-length  0 temporary", "Screen-length 0 temporary"]),
    ("cisco", "terminal length 0", ["terminal length 0 x", "terminal length 24", "TERMINAL length 0",
                                    "terminal monitor"]),
])
def test_paging_layer1_exact_only(vendor, ok, bad):
    assert sw.classify(ok, vendor, True, RULES)["verdict"] == "pass"
    for cmd in bad:
        assert sw.classify(cmd, vendor, True, RULES)["verdict"] == "confirm", cmd


def test_ruijie_extra_verbs_take_effect(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"vendors": {"ruijie": {"extra_verbs": ["ping"]}}}))
    r = sw.load_rules(str(p))
    assert r["vendors"]["ruijie"]["extra_verbs"] == ["ping"]
    assert sw.classify("ping 192.0.2.1", "ruijie", True, r, user_view=True)["verdict"] == "pass"
    assert sw.classify("ping 192.0.2.1", "ruijie", True, RULES)["verdict"] == "confirm"



@pytest.mark.parametrize("vendor,cmd", [
    ("cisco", "show tech-support"), ("cisco", "show tech"), ("ruijie", "show tech-support"),
    ("cisco", "show ?"), ("ruijie", "show ?"), ("cisco", "show run ?"),
    ("huawei", 'display "diagnostic-information" flash:/a.txt'), ("huawei", "display ?"),
])
def test_gate_confirms_tech_support_question_and_quotes(vendor, cmd):
    assert sw.classify(cmd, vendor, True, RULES)["verdict"] == "confirm"



@pytest.mark.parametrize("cmd", ["show version [exec reload]", "show version [exec {write memory}]",
                                 "show version [exec {wr}]", "show version [ios_config {hostname X}]",
                                 "show version | include [exec reload]"])
def test_gate_tcl_substitution_needs_human(cmd):
    g = sw.classify(cmd, "cisco", True, RULES)
    assert g["verdict"] == "confirm"


@pytest.mark.parametrize("cmd,word", [("show version [exec reload]", "reload"), ("x [exec {wr}]", "write"),
                                      ("show version {save}", "save")])
def test_danger_words_split_on_tcl_brackets(cmd, word):
    assert word in sw.danger_words(cmd)


def test_gate_brackets_not_sent_to_model():
    seen = []
    judge = lambda cmd, vendor: seen.append(cmd) or {"allow": True, "reason": "x", "model": {}}   # noqa: E731
    rules = dict(RULES, model=dict(RULES["model"], enabled=True))
    g = sw.classify("show ip bgp regexp [0-9]", "cisco", True, rules, judge=judge)
    assert g["verdict"] == "confirm" and seen == []


@pytest.mark.parametrize("text", ["Router(tcl)#", "switch-tcl#", "[startup-config]", "[Y/N]", "[confirm]",
                                  "[running-config]"])
def test_slot_vendor_tcl_and_confirm_not_at_prompt(text):
    assert sw.slot_vendor(1, RULES, text) == (None, False)                         # 华为自动认也不认
    for v in ("cisco", "ruijie", "huawei"):
        assert sw.slot_vendor(1, dict(RULES, slots={1: v}), text) == (v, False), (v, text)


def test_h3c_prompt_auto_recognized_as_huawei_rules():
    """写闸门不单独认 H3C: <名> / [名] 按华为处理 (display 系规则一样); 文档里写明."""
    assert sw.slot_vendor(1, RULES, "<H3C>") == ("huawei", True)



@pytest.mark.parametrize("verb", ["terminal", "debugging", "debug", "undo", "no", "clear", "test", "telnet", "ssh",
                                  "system-view", "reload", "reboot", "copy", "write", "save", "configure", "delete",
                                  "format", "reset", "erase", "wr", "conf", "deb", "tclsh", "enable", "request"])
def test_extra_verbs_hard_denylist(tmp_path, verb):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"vendors": {"cisco": {"extra_verbs": [verb, "ping"]}}}))
    assert sw.load_rules(str(p))["vendors"]["cisco"]["extra_verbs"] == ["ping"]


@pytest.mark.parametrize("text", ["[Y/N/C]", "[yes]", "[OK]", "[cancel]", "[Enter]", "[BootROM]", "[More]",
                                  "<BootLoad>", "rommon 1 >", "loader>", "switch(boot)#", "grub>", "busybox#",
                                  "root#"])
def test_gate_round4_not_prompts(text):
    assert sw.slot_vendor(1, RULES, text) == (None, False)
    for v in ("cisco", "ruijie", "huawei"):
        assert sw.slot_vendor(1, dict(RULES, slots={1: v}), text) == (v, False), (v, text)


def test_gate_still_allows_config_and_system_view_prompts():
    """写闸门 (不是巡检) 照旧认配置视图: 那里的命令本来就走人确认."""
    assert sw.slot_vendor(1, RULES, "[SW-A]") == ("huawei", True)
    assert sw.slot_vendor(1, dict(RULES, slots={1: "cisco"}), "SW-A(config)#") == ("cisco", True)


def test_gate_clear_needs_human():
    assert sw.classify("show platform hardware qfp active statistics drop clear", "cisco", True, RULES)["verdict"] \
        == "confirm"


def test_interface_view_with_slash_still_huawei_prompt():
    assert sw.slot_vendor(1, RULES, "[SW1-GigabitEthernet0/0/1]") == ("huawei", True)



@pytest.mark.parametrize("verb", ["screen-length", "super", "ssh2", "user-interface", "line", "local-user",
                                  "guestshell", "python", "event", "free", "kill", "quit", "logout", "lock", "send",
                                  "monitor", "mount", "umount", "tar", "gunzip", "squeeze", "fixdisk", "undelete"])
def test_extra_verbs_denylist_round5(tmp_path, verb):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"vendors": {"huawei": {"extra_verbs": [verb, "ping"]}}}))
    assert sw.load_rules(str(p))["vendors"]["huawei"]["extra_verbs"] == ["ping"]


@pytest.mark.parametrize("vendor,text,ok", [
    ("huawei", "<SW-A>", True), ("huawei", "[SW-A]", False), ("huawei", "[SW-A-GigabitEthernet0/0/1]", False),
    ("cisco", "R1#", True), ("cisco", "R1>", True), ("cisco", "R1(config)#", False),
    ("ruijie", "Ruijie#", True), ("ruijie", "Ruijie(config-if)#", False), ("linux", "bash-4.4#", False),
])
def test_user_view(vendor, text, ok):
    assert sw.user_view(vendor, "x\n" + text) is ok


def test_extra_verbs_only_in_user_view(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"vendors": {"huawei": {"extra_verbs": ["ping"]}}}))
    r = sw.load_rules(str(p))
    assert sw.classify("ping 192.0.2.1", "huawei", True, r, user_view=True)["verdict"] == "pass"
    assert sw.classify("ping 192.0.2.1", "huawei", True, r, user_view=False)["verdict"] == "confirm"
    assert sw.classify("ping 192.0.2.1", "huawei", True, r)["verdict"] == "confirm"         # 默认不算用户视图
    assert sw.classify("display version", "huawei", True, r, user_view=False)["verdict"] == "pass"


@pytest.mark.parametrize("name", ["SH#", "SH-01#", "ZJSH-3#", "Mesh-2#", "Flash#"])
def test_gate_hostnames_like_shells(name):
    for v in ("cisco", "ruijie"):
        assert sw.slot_vendor(1, dict(RULES, slots={1: v}), name) == (v, True)


def test_gate_linux_slot_shell_prompt_still_ok():
    assert sw.slot_vendor(1, dict(RULES, slots={1: "linux"}), "bash-4.4#") == ("linux", True)
    assert sw.slot_vendor(1, dict(RULES, slots={1: "cisco"}), "bash-4.4#") == ("cisco", False)


def test_mcp_serial_passes_user_view():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_serial.py"),
               encoding="utf-8").read()
    assert "user_view=sw.user_view(vendor, text)" in src


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
    b.submit(1, tok, "agent", "line", "reload", sw.classify("reload", "cisco", True, rules), 1000.0, 30, context="<SW1>")
    g = sw.classify_key("enter", "Router#", 1)
    assert g["verdict"] == "confirm"
    with pytest.raises(sw.Refused) as e:
        b.submit(1, tok, "agent", "key", "enter", g, 1001.0, 5, context="<SW1>")
    assert e.value.code == "busy" and "前面" in str(e.value)
    for key in ("space",):
        with pytest.raises(sw.Refused):
            b.submit(1, tok, "agent", "key", key, sw.classify_key(key, "Router#", 1), 1001.0, 5, context="<SW1>")


def test_only_ctrl_c_queues_behind_pending():
    """第四轮: 同槽位有挂起项时一律不收新的命令和按键 (挂起或直发), 只有打断键能排."""
    b, tok = _book_with_lease()
    b.submit(1, tok, "agent", "line", "reload", sw._gate("confirm", "x"), 1000.0, 30, context="<SW1>")
    b.submit(1, tok, "agent", "key", "ctrl_c", sw.classify_key("ctrl_c"), 1001.0, 5, context="<SW1>")
    assert len(b.view(1002.0)["pending"]) == 2
    for kind, cmd in (("line", "display version"), ("line", "interface g0/1"), ("key", "enter")):
        with pytest.raises(sw.Refused) as e:
            b.submit(1, tok, "agent", kind, cmd, sw._gate("confirm", "x"), 1001.0, 30, context="<SW1>")
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
    b.submit(1, tok, "agent", "line", "reload", sw._gate("confirm", "x"), 1000.0, 30, context="<SW1>")
    with pytest.raises(sw.Refused):
        b.submit(1, tok, "agent", "line", ans, sw._gate("confirm", "x"), 1001.0, 30, context="<SW1>")
    with pytest.raises(sw.Refused):
        b.begin(1, tok, "agent", "line", ans, sw._gate("pass", "x"), 1001.0, 30)


def test_finish_touch_only():
    """第五轮: Agent 自己先前的直发在"看设备"和"占用"之间回来了 (只有 finish 动了计数), 也不直发."""
    b = sw.Book.from_text(None, 1000.0)
    tok = b.acquire(1, "a", 1000.0)["lease"]
    it = b.begin(1, tok, "a", "line", "show x", sw._gate("pass", "x"), 1000.0, 10)
    snap = b.touched(1)
    b.finish(it["id"], {"status": "sent", "echo": "Proceed? [confirm]"}, 1001.0)
    with pytest.raises(sw.Refused) as e:
        b.begin(1, tok, "a", "key", "enter", sw._gate("pass", "x"), 1002.0, 10, snap=snap)
    assert e.value.code == "changed"


# ------------------------------------------------------------ 合并 #65 后: gate.line 不进执行绪日志

def test_gate_line_not_in_event_log():
    """#74 的 gate["line"] 是设备输出, 按 #65 不进执行绪日志: 只记长度和哈希; 挂起项 / 审批界面照常有原文."""
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    raw = "Router(config)#snmp-server community s3cr3t-line RO"
    g = dict(_gate_confirm(), line=raw)
    p = b.submit(1, "tokA", "agentA", "line", "y", g, T0, timeout=30, context=raw)
    assert b.view(T0)["pending"][0]["gate"]["line"] == raw                  # 审批界面要用
    b.confirm(p["id"], "human", T0 + 1, current=raw)
    b.finish(p["id"], {"status": "sent", "echo": ""}, T0 + 2)
    b.begin(1, "tokA", "agentA", "key", "enter", dict(_gate_pass(), line=raw), T0 + 3, timeout=30)
    logged = json.dumps(b.events, ensure_ascii=False)
    assert "s3cr3t" not in logged and raw not in logged
    for e in b.events:
        g = e.get("gate")
        if g:
            assert "line" not in g and g["line_len"] == len(raw) and len(g["line_sha"]) == 16
    assert any(e.get("gate") for e in b.events if e["ev"] == "pending")


# ------------------------------------------------------------ #93: 设备那一行不经理由进日志

@pytest.mark.parametrize("raw", ["Router(config)#snmp-server community s3cr3t-line RO",
                                 "set password s3cr3t-line [Y/N]:"])
def test_device_line_with_secret_not_in_event_log_via_classify_key(raw):
    """真实带口令的最后一行走 classify_key -> submit -> confirm -> finish: 理由和日志都没有明文."""
    g = sw.classify_key("enter", raw, 60.0, vendor="cisco")
    assert g["verdict"] == "confirm" and "s3cr3t" not in g["reason"] and g["line"] == raw   # 比对还要用原文
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "key", "enter", g, T0, timeout=30, context=raw)
    b.confirm(p["id"], "human", T0 + 1, current=raw)
    b.finish(p["id"], {"status": "refused", "echo": "", "note": "x"}, T0 + 2)
    assert any(e.get("gate") for e in b.events)
    assert "s3cr3t" not in json.dumps(b.events, ensure_ascii=False)


@pytest.mark.parametrize("key,text,quiet", [("enter", "", 60.0), ("enter", "Proceed? [confirm]", 60.0),
                                            ("enter", "half output s3cr3t", 60.0), ("enter", "<SW1>", 1.0),
                                            ("space", "<SW1>", None)])
def test_classify_key_still_confirms_after_reason_change(key, text, quiet):
    """反例: 理由不再引用设备行, 判定不能跟着松."""
    g = sw.classify_key(key, text, quiet)
    assert g["verdict"] == "confirm" and "s3cr3t" not in g["reason"]


def test_shown_line_masks_and_keeps_one_line():
    masked = sw.shown_line("Router(config)#snmp-server community s3cr3t-line RO")
    assert "s3cr3t" not in masked and sw.MASK in masked
    assert sw.shown_line("A\nB") == "A B"
    assert sw.shown_line("Proceed with reload? [confirm]") == "Proceed with reload? [confirm]"
    assert sw.shown_line(None) == ""


def test_masked_view_masks_copy_only():
    """给 Agent 的 pending_list: gate.line 打码; 原视图 (确认时比对、卡片要用) 不动."""
    raw = "Router(config)#snmp-server community s3cr3t-line RO"
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    p = b.submit(1, "tokA", "agentA", "line", "y", dict(_gate_confirm(), line=raw), T0, timeout=30, context=raw)
    view = b.view(T0)
    masked = sw.masked_view(view, {1: "cisco"})
    assert "s3cr3t" not in masked["pending"][0]["gate"]["line"] and view["pending"][0]["gate"]["line"] == raw
    b.reject(p["id"], "human", T0 + 1)
    view = b.view(T0 + 1)
    assert "s3cr3t" not in sw.masked_view(view, {})["history"][-1]["gate"]["line"]
    assert view["history"][-1]["gate"]["line"] == raw


# ------------------------------------------------------------ #107 审查: 打码不能把确认提示的问句吃掉

@pytest.mark.parametrize("line", [
    "The password needs to be changed. Change now? [Y/N]:",
    "Warning: The current configuration will be erased and the key pair deleted. Continue? [Y/N]:",
    "Overwrite auth config and reboot? [confirm]",
    "Change password now? [Y/N]",
    "Change password? (yes/no)",
])
def test_confirm_question_with_plain_english_kept_whole(line):
    assert sw.shown_line(line) == line


@pytest.mark.parametrize("line", ["Enter Password:", "Password:", "password", "Enter Password :"])
def test_secret_word_without_value_not_masked(line):
    assert sw.shown_line(line) == line


@pytest.mark.parametrize("line,shown", [
    ("password Abc123 [Y/N]", "password ****** [Y/N]"),
    ("key-string 7 0822455D0A16 [confirm]", "key ****** [confirm]"),
    ("set password Abc123. Change now? [Y/N]:", "set password ****** Change now? [Y/N]:"),
    ("password is Abc123. Continue? [Y/N]", "password ****** Continue? [Y/N]"),
    ('set password "my new config" [Y/N]', "set password ****** [Y/N]"),
    ("set password change [Y/N]", "set password ****** [Y/N]"),      # 一个光秃秃的词, 哪怕是常见词也按值算
    ("secret 5 $1$abc$def (yes/no)?", "secret ****** (yes/no)?"),
    ("set community s3cr3t-line and reboot? [confirm]", "set community ****** and reboot? [confirm]"),
])
def test_confirm_prompt_masks_value_keeps_answer_marker(line, shown):
    assert sw.shown_line(line) == shown


@pytest.mark.parametrize("line,secret", [
    ("password Abc123 [Y/N]", "Abc123"), ("key-string 7 0822455D0A16 [confirm]", "0822455D0A16"),
    ("set password hunter2 now? [Y/N]", "hunter2"), ("password Abc123?", "Abc123"),
    ("Password [Abc123]:", "Abc123"), ("password=Abc123 [Y/N]", "Abc123"),
    ("password: Abc123 [Y/N]", "Abc123"), ("mysql -pAbc123? [Y/N]", "Abc123"),
    ("curl https://u:Abc123@example.org/x? [Y/N]", "Abc123"), ("chpasswd root:Abc123 [Y/N]", "Abc123"),
    ("password hunter2 needs to be changed? [Y/N]", "hunter2"),
    ("password needs Abc123 to be changed? [Y/N]", "Abc123"),
    ("key Abc123 password Def456 [confirm]", "Def456"),
    ("set password​ Abc123 [Y/N]", "Abc123"),
])
def test_confirm_prompt_never_leaks_value(line, secret):
    for out in (sw.shown_line(line), sw.device_context(line)["lines"][0], sw.device_lines(line)[0]):
        assert secret not in out, out


@pytest.mark.parametrize("line,secret", [("set password xxx", "xxx"), ("password needs to be Abc123", "Abc123"),
                                         ("password Abc123", "Abc123")])
def test_non_confirm_lines_still_truncated_after_secret_word(line, secret):
    """不是确认提示的行 (日志 / 设备最近输出 / 回显) 维持口令词后整段打码, 不按"像不像英语"放行."""
    shown = sw.shown_line(line)
    assert secret not in shown and shown.endswith(sw.MASK)


@pytest.mark.parametrize("line", ["set password Enter Password:", "Enter Password: Abc123", "Password:Abc123"])
def test_value_after_colon_is_not_treated_as_empty(line):
    assert sw.MASK in sw.shown_line(line)


# ------------------------------------------------------------ #107 审查: 提示符不打成一行星号, 密文不借提示符形状漏出

@pytest.mark.parametrize("line,vendor", [("[HUAWEI-rsa-public-key]", "huawei"), ("[HUAWEI-rsa-public-key]", None),
                                         ("Router(config-keychain-key)#", "cisco"),
                                         ("Router(config-keychain-key)#", None)])
def test_real_prompt_with_secret_word_not_all_stars(line, vendor):
    assert sw.at_any_prompt(line, vendor)
    shown = sw.shown_line(line, vendor)
    assert shown != sw.MASK and shown.startswith(line[:8]) and sw.MASK in shown


@pytest.mark.parametrize("line,secret", [
    ("[Qx7pL2mZ9vR4tY8wK3nB-key]", "Qx7pL2mZ9vR4tY8wK3nB"),
    ("[~x-cipher%^%#Qx7pL2mZ9vR4tY8%^%#]", "Qx7pL2mZ9vR4tY8"),
    ("[aB3$dE6^gH9*jK2@mN5!-psk]", "aB3$dE6^gH9"),
    ("[Qx7pL-2mZ9v-R4tY8-wK3nB-key]", "Qx7pL"),
    ("[key-Qx7pL2mZ9vR4tY8wK3nB]", "Qx7pL2mZ9vR4tY8wK3nB"),
    ("<secret-Abc123>", "Abc123"),
    ("[key:S3cr3tValue]", "S3cr3tValue"),
    ("R1(snmp-server community Pub1ic RO)#", "Pub1ic"),
])
def test_secret_cannot_hide_in_prompt_shape(line, secret):
    assert secret not in sw.shown_line(line), line


@pytest.mark.parametrize("line", ["Qx7pL2mZ9vR4tY8wK3nB", "aB3$dE6^gH9*jK2@mN5!", "%^%#Qx7pL2mZ9vR4tY8%^%#"])
def test_bare_cipher_line_still_all_stars(line):
    assert sw.shown_line(line) == sw.MASK


# ------------------------------------------------------------ #107 审查: 先打码再截短

LONG_SECRET_LINE = "username ops password " + " ".join(["s3cr3t-tail"] * 30)


def test_gate_keeps_raw_line_and_masked_display_copy():
    assert "password" not in LONG_SECRET_LINE[-sw.LINE_SHOWN:]        # 口令词确实落在截掉的前段
    g = sw.classify_key("enter", LONG_SECRET_LINE, 60.0, vendor="cisco")
    assert g["line"] == LONG_SECRET_LINE[-sw.LINE_SHOWN:]             # 比对用的原文字段不变
    assert "s3cr3t" not in g["line_shown"] and sw.MASK in g["line_shown"]


def test_line_fields_mask_before_truncating():
    f = sw.line_fields(LONG_SECRET_LINE, "cisco")
    assert f["line"] == LONG_SECRET_LINE[-sw.LINE_SHOWN:] and "s3cr3t" not in f["line_shown"]
    assert len(sw.line_fields("x" * 500)["line_shown"]) <= sw.LINE_SHOWN


def test_masked_view_uses_display_copy_for_long_line():
    g = sw.classify_key("enter", LONG_SECRET_LINE, 60.0, vendor="cisco")
    b = book()
    b.acquire(1, "agentA", T0, token="tokA")
    b.submit(1, "tokA", "agentA", "key", "enter", g, T0, timeout=30, context="<SW1>")
    view = b.view(T0)
    masked = sw.masked_view(view, {1: "cisco"})
    assert "s3cr3t" not in json.dumps(masked["pending"], ensure_ascii=False)
    assert view["pending"][0]["gate"]["line"] == LONG_SECRET_LINE[-sw.LINE_SHOWN:]    # Book.view 仍是原文


def test_masked_view_old_state_without_display_copy():
    """升级前的状态文件没有 line_shown: 回退到对 line 打码."""
    raw = "Router(config)#snmp-server community s3cr3t-line RO"
    assert "s3cr3t" not in sw.gate_shown({"line": raw}, "cisco") and sw.gate_shown({}, None) == ""


def test_display_copy_not_in_event_log():
    g = sw.classify_key("enter", LONG_SECRET_LINE, 60.0, vendor="cisco")
    assert "line_shown" not in sw._log_gate(g) and "line" not in sw._log_gate(g)
