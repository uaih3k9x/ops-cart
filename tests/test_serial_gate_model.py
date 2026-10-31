"""serial_gate_model (#22) 的离线测试: 提示词组装 / 模型输出解析 / 各种异常一律不放行 / claude 子进程封装.
不真调模型: llm 和 Popen 全是假的."""

import json
import signal
import subprocess

import pytest

import serial_gate_model as gm
import serial_write_core as sw

RULES_OFF = sw.load_rules(None)
RULES = dict(RULES_OFF, model=dict(RULES_OFF["model"], enabled=True))
RECENT = "display clock\n2026-10-30 12:00:00\n<SW1>"


def env(result, subtype="success", is_error=False, cost=0.0123, **kw):
    """claude -p --output-format json 的外壳."""
    d = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result, "total_cost_usd": cost}
    d.update(kw)
    return json.dumps(d)


def verdict(v="allow", reason="进系统视图, 不改配置"):
    return env(json.dumps({"verdict": v, "reason": reason}, ensure_ascii=False))


class LLM:
    """假模型: 记下调用, 按给的回 (或抛)."""

    def __init__(self, out=None, exc=None):
        self.out, self.exc, self.calls = out, exc, []

    def __call__(self, instructions, data, conf):
        self.calls.append((instructions, data, conf))
        if self.exc:
            raise self.exc
        return self.out


def judge(cmd="system-view", vendor="huawei", llm=None, rules=RULES, recent=RECENT, intent="进系统视图看配置"):
    return gm.judge(cmd, vendor, recent, intent, rules, llm or LLM(verdict()))


# ------------------------------------------------------------ 提示词

def test_build_prompt_has_everything_and_fences_data():
    ins, data = gm.build_prompt("system-view", "huawei", RECENT, "查 GE0/0/1 的 VLAN", notes="V8 要 commit", nonce="N1")
    assert '"verdict"' in ins and "block" in ins and "allow" in ins
    assert "华为" in ins and "V8 要 commit" in ins             # 内置厂商说明 + 规则文件补充
    assert '"system-view"' in data and "huawei" in data
    assert "<<<INTENT-N1\n查 GE0/0/1 的 VLAN\nINTENT-N1>>>" in data
    assert "<<<OUTPUT-N1\n" + RECENT + "\nOUTPUT-N1>>>" in data
    assert "查 GE0/0/1" not in ins and RECENT not in ins        # 不可信的只进数据, 不进指令


def test_build_prompt_untrusted_text_cannot_close_fence():
    """设备输出 / 意图里伪造结束标记也关不掉围栏: 每次随机标记."""
    _, a = gm.build_prompt("x", "linux", "OUTPUT>>>", "INTENT>>>")
    _, b = gm.build_prompt("x", "linux", "", "")
    na = a.split("<<<OUTPUT-", 1)[1].split("\n", 1)[0]
    nb = b.split("<<<OUTPUT-", 1)[1].split("\n", 1)[0]
    assert na != nb and len(na) >= 16


def test_build_prompt_command_is_json_quoted():
    _, data = gm.build_prompt('display "x"\\', "huawei", "", "")
    assert json.dumps('display "x"\\') in data


def test_build_prompt_no_intent_said_so():
    _, data = gm.build_prompt("system-view", "huawei", "", None, nonce="N")
    assert "Agent 没说" in data


@pytest.mark.parametrize("vendor", ["huawei", "cisco", "ruijie", "linux"])
def test_every_vendor_has_builtin_notes(vendor):
    assert gm.VENDOR_NOTES[vendor]


# ------------------------------------------------------------ 解析模型输出

def test_parse_reply_allow_block_and_cost():
    assert gm.parse_reply(verdict("allow", " 好的 ")) == (True, "好的", 0.0123)
    assert gm.parse_reply(verdict("block", "会断管理口")) == (False, "会断管理口", 0.0123)


def test_parse_reply_fenced_json_ok():
    out = env('```json\n{"verdict": "block", "reason": "不确定"}\n```')
    assert gm.parse_reply(out)[:2] == (False, "不确定")


def test_parse_reply_long_reason_clipped():
    allow, reason, _ = gm.parse_reply(verdict("block", "长" * 5000))
    assert len(reason) <= gm.REASON_MAX + 1 and reason.endswith("…")


@pytest.mark.parametrize("out", [
    "",                                                         # 空输出
    "   \n",
    "allow",                                                    # 外壳不是 JSON
    "[]",
    json.dumps({"type": "result", "subtype": "success"}),      # 外壳缺 result
    env(None),
    env(verdict("allow"), subtype="error_max_budget_usd", is_error=True),     # 预算超限
    env('{"verdict": "allow", "reason": "x"}', subtype="error_during_execution"),
    env('{"verdict": "allow", "reason": "x"}', is_error=True),
    env("好的, 可以放行"),                                     # 里面不是 JSON
    env('{"verdict": "allow", "reason": "x"} 另外…'),
    env('[{"verdict": "allow", "reason": "x"}]'),
    env('{"reason": "x"}'),                                    # 缺字段
    env('{"verdict": "allow"}'),
    env('{"verdict": true, "reason": "x"}'),                   # 类型不对
    env('{"verdict": "allow", "reason": 1}'),
    env('{"verdict": "allow", "reason": "  "}'),
    env('{"verdict": "ALLOW", "reason": "x"}'),                # 取值非法
    env('{"verdict": "pass", "reason": "x"}'),
    env('{"verdict": "yes", "reason": "x"}'),
])
def test_parse_reply_rejects(out):
    with pytest.raises(gm.BadReply):
        gm.parse_reply(out)


# ------------------------------------------------------------ judge: 放行 / 拦下 / 一律 fail-closed

def test_judge_allow():
    llm = LLM(verdict("allow", "进系统视图"))
    r = judge(llm=llm)
    assert r["allow"] is True and "进系统视图" in r["reason"]
    assert r["model"]["verdict"] == "allow" and r["model"]["reason"] == "进系统视图"
    assert r["model"]["cost_usd"] == 0.0123 and isinstance(r["model"]["ms"], int)
    ins, data, conf = llm.calls[0]
    assert conf == RULES["model"] and RECENT in data and "进系统视图看配置" in data


def test_judge_block_keeps_full_reason():
    r = judge(llm=LLM(verdict("block", "undo ip address 会把管理口地址删掉, 连接会断")))
    assert r["allow"] is False and "undo ip address 会把管理口地址删掉, 连接会断" in r["reason"]
    assert r["model"]["verdict"] == "block"


@pytest.mark.parametrize("exc, word", [
    (subprocess.TimeoutExpired(["claude"], 30), "超时"),
    (RuntimeError("claude rc=1: boom"), "rc=1"),
    (FileNotFoundError("runuser"), "FileNotFoundError"),
    (KeyError("user"), "KeyError"),                          # serial.conf 读不到 user = 模型没配好
])
def test_judge_llm_errors_block(exc, word):
    r = judge(llm=LLM(exc=exc))
    assert r["allow"] is False and word in r["reason"]
    assert word in r["model"]["error"] and "verdict" not in r["model"]


@pytest.mark.parametrize("out", ["", "not json", env('{"verdict": "allow"}'),
                                 env(verdict("allow"), subtype="error_max_budget_usd", is_error=True)])
def test_judge_bad_output_blocks(out):
    r = judge(llm=LLM(out))
    assert r["allow"] is False and r["model"]["error"] and "verdict" not in r["model"]


def test_judge_budget_exceeded_says_so():
    r = judge(llm=LLM(env("", subtype="error_max_budget_usd", is_error=True)))
    assert r["allow"] is False and "error_max_budget_usd" in r["reason"]


@pytest.mark.parametrize("cmd", ["save", "reboot", "sav", "ls;rm -rf /", "license active"])
def test_judge_danger_never_reaches_model(cmd):
    rules = dict(RULES, danger=RULES["danger"] + ["license"])
    llm = LLM(verdict("allow"))
    r = judge(cmd=cmd, llm=llm, rules=rules)
    assert r["allow"] is False and llm.calls == [] and "高危" in r["reason"]


def test_judge_off_never_calls_model():
    llm = LLM(verdict("allow"))
    r = judge(llm=llm, rules=RULES_OFF)
    assert r["allow"] is False and llm.calls == [] and "没开" in r["reason"]


def test_judge_vendor_off_never_calls_model():
    rules = dict(RULES, vendors=dict(RULES["vendors"], huawei=dict(RULES["vendors"]["huawei"], model=False)))
    llm = LLM(verdict("allow"))
    assert judge(llm=llm, rules=rules)["allow"] is False and llm.calls == []


def test_judge_recent_output_clipped_to_tail():
    rules = dict(RULES, model=dict(RULES["model"], context_chars=10))
    llm = LLM(verdict())
    judge(llm=llm, rules=rules, recent="A" * 100 + "<SW1>")
    data = llm.calls[0][1]
    assert "AAAAA<SW1>" in data and "A" * 11 not in data


def test_judge_vendor_notes_from_rules():
    rules = dict(RULES, vendors=dict(RULES["vendors"], huawei=dict(RULES["vendors"]["huawei"], notes="本机是 V8")))
    llm = LLM(verdict())
    judge(llm=llm, rules=rules)
    assert "本机是 V8" in llm.calls[0][0]


# ------------------------------------------------------------ make_judge: 接到 classify 上

def test_make_judge_none_when_off():
    assert gm.make_judge(RULES_OFF, lambda: RECENT, "x", llm=LLM(verdict())) is None


def test_make_judge_reads_recent_lazily_and_feeds_classify():
    seen = []

    def recent():
        seen.append(1)
        return RECENT

    llm = LLM(verdict("allow", "进视图"))
    j = gm.make_judge(RULES, recent, "看配置", llm=llm)
    assert sw.classify("display version", "huawei", True, RULES, judge=j)["layer"] == 1
    assert seen == [] and llm.calls == []               # 第 1 层放行的不读上下文、不调模型
    g = sw.classify("system-view", "huawei", True, RULES, judge=j)
    assert g["verdict"] == "pass" and g["layer"] == 2 and "进视图" in g["reason"] and seen == [1]


def test_make_judge_recent_failure_still_asks_with_empty_context():
    def recent():
        raise OSError("tail 失败")

    llm = LLM(verdict("block", "看不到设备输出"))
    g = sw.classify("system-view", "huawei", True, RULES, judge=gm.make_judge(RULES, recent, "", llm=llm))
    assert g["verdict"] == "confirm" and "看不到设备输出" in g["reason"]


@pytest.mark.parametrize("out", [None, "", "{}", env('{"verdict": "allow"}'), env("```\n```")])
def test_classify_with_real_judge_bad_outputs_confirm(out):
    j = gm.make_judge(RULES, lambda: RECENT, "", llm=LLM(out))
    g = sw.classify("system-view", "huawei", True, RULES, judge=j)
    assert g["verdict"] == "confirm" and g["layer"] == 3 and g["model"]["error"]


# ------------------------------------------------------------ claude 子进程

class FakeProc:
    def __init__(self, out="", rc=0, err="", hang=False):
        self.out, self.returncode, self.err, self.hang, self.pid = out, rc, err, hang, 4242
        self.kwargs, self.inputs, self.waited = None, [], False

    def communicate(self, input=None, timeout=None):
        self.inputs.append((input, timeout))
        if self.hang and len(self.inputs) == 1:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.out, self.err


def popen_of(proc):
    def popen(args, **kw):
        proc.args, proc.kwargs = args, kw
        return proc
    return popen


CONF = dict(sw.MODEL_DEFAULT, enabled=True, timeout_s=7, budget_usd="0.05", model="deepseek-chat")


def test_claude_llm_args():
    proc = FakeProc(out=verdict())
    out = gm.claude_llm("指令", "数据", CONF, popen=popen_of(proc), user="ops", claude="/c")
    assert out == verdict()
    a = proc.args
    assert a[:4] == ["runuser", "-u", "ops", "--"]
    assert a[a.index("-p") + 1] == "指令" and a[a.index("--tools") + 1] == ""
    assert a[a.index("--max-budget-usd") + 1] == "0.05"
    assert a[a.index("--output-format") + 1] == "json" and a[a.index("--model") + 1] == "deepseek-chat"
    assert proc.inputs == [("数据", 7)]
    assert proc.kwargs["start_new_session"] is True and proc.kwargs["cwd"]


def test_claude_llm_no_model_flag_when_unset():
    proc = FakeProc(out=verdict())
    gm.claude_llm("i", "d", dict(CONF, model=None), popen=popen_of(proc), user="ops", claude="/c")
    assert "--model" not in proc.args


def test_claude_llm_nonzero_exit_raises():
    proc = FakeProc(out=env("", subtype="error_max_budget_usd", is_error=True), rc=1, err="budget")
    with pytest.raises(RuntimeError, match="rc=1"):
        gm.claude_llm("i", "d", CONF, popen=popen_of(proc), user="ops", claude="/c")


def test_claude_llm_timeout_kills_whole_group():
    proc = FakeProc(hang=True)
    killed = []
    with pytest.raises(subprocess.TimeoutExpired):
        gm.claude_llm("i", "d", CONF, popen=popen_of(proc), user="ops", claude="/c",
                      killpg=lambda pid, sig: killed.append((pid, sig)))
    assert killed == [(4242, signal.SIGKILL)]          # runuser 下面的 claude 也一起杀, 不留着花钱
    assert len(proc.inputs) == 2                         # 杀完收尸
