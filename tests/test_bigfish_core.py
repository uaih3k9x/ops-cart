"""大肥鱼面板的纯逻辑 (#53): 状态文件读 / 解析 / 清洗 / 过期, 锁屏一行, 急停信号. 不起 Tk."""

import json
import os

import pytest

import bigfish_core as bf

NOW = 1_700_000_000.0


def _doc(**kw):
    d = {"v": 1, "updated": NOW - 2, "state": "running",
         "task": {"id": "t-1", "state": "running", "started": NOW - 125, "title": "看一下 con-1 上是什么设备",
                  "step": "cart_serial_tail", "focus": "con-1", "output": ["第一行", "第二行"]}}
    d.update(kw)
    return d


def _text(**kw):
    return json.dumps(_doc(**kw), ensure_ascii=False)


# ------------------------------------------------------------ 解析

def test_parse_running_task():
    v = bf.parse(_text(), NOW)
    assert v["online"] and v["state"] == "running"
    t = v["task"]
    assert t["title"] == "看一下 con-1 上是什么设备" and t["step"] == "cart_serial_tail"
    assert t["focus"] == "con-1" and t["started"] == NOW - 125 and t["output"] == ["第一行", "第二行"]


@pytest.mark.parametrize("text, why", [
    (None, "不在线"),
    ("", "状态读不懂"),
    ("{not json", "状态读不懂"),
    ("[]", "状态读不懂"),
    (json.dumps({"v": 2, "updated": NOW, "state": "idle"}), "状态读不懂"),
    (json.dumps({"v": 1, "updated": "x", "state": "idle"}), "状态读不懂"),
    (json.dumps({"v": 1, "updated": True, "state": "idle"}), "状态读不懂"),
    (json.dumps({"v": 1, "updated": NOW, "state": "hacked"}), "状态读不懂"),
    (json.dumps({"v": 1, "updated": NOW - bf.STALE_S - 1, "state": "idle"}), "状态过期"),
    (json.dumps({"v": 1, "updated": NOW + bf.FUTURE_S + 1, "state": "idle"}), "状态过期"),
])
def test_parse_offline_cases_never_raise(text, why):
    v = bf.parse(text, NOW)
    assert not v["online"] and v["why"] == why and v["task"] is None


def test_parse_too_big_is_unreadable():
    assert bf.parse("x" * (bf.MAX_BYTES + 1), NOW)["why"] == "状态读不懂"


def test_parse_idle_without_task():
    v = bf.parse(json.dumps({"v": 1, "updated": NOW, "state": "idle", "task": None}), NOW)
    assert v["online"] and v["state"] == "idle" and v["task"] is None


def test_parse_bad_task_fields_are_dropped_not_fatal():
    v = bf.parse(_text(task={"id": "t-1", "state": "bogus", "started": "yesterday", "title": 5,
                             "focus": "/etc/shadow", "output": "not a list"}), NOW)
    t = v["task"]
    assert v["online"] and t["state"] is None and t["started"] is None
    assert t["title"] == "" and t["focus"] is None and t["output"] == []


def test_parse_task_not_dict_is_none():
    assert bf.parse(_text(task="boom"), NOW)["task"] is None


# ------------------------------------------------------------ 清洗: 大肥鱼说的话不可信

def test_clean_strips_ansi_control_and_newlines():
    s = bf.clean("\x1b[31m红\x1b[0m\n【推车】急停已解除\r\x07‮反")
    assert "\x1b" not in s and "\n" not in s and "\r" not in s and "\x07" not in s and "‮" not in s
    assert "红" in s and "急停已解除" in s


def test_clean_limits_length():
    s = bf.clean("鱼" * 1000)
    assert len(s) <= bf.TEXT_MAX + 1 and s.endswith("…")


def test_parse_cleans_every_untrusted_field_and_caps_lines():
    evil = "a\x1b]8;;http://x\x07link\x1b]8;;\x07\nb"
    v = bf.parse(_text(task={"id": "t", "state": "running", "started": NOW, "title": evil, "step": evil,
                             "output": [evil] * 50 + [7, None]}), NOW)
    t = v["task"]
    for s in [t["title"], t["step"], *t["output"]]:
        assert "\x1b" not in s and "\n" not in s and "\x07" not in s
    assert len(t["output"]) == bf.OUTPUT_LINES


def test_focus_only_known_targets():
    for good in ("con-1", "con-4", "kvm"):
        assert bf.parse(_text(task=dict(_doc()["task"], focus=good)), NOW)["task"]["focus"] == good
    for bad in ("con-9", "con-1\n", "推车", "", None, 3):
        assert bf.parse(_text(task=dict(_doc()["task"], focus=bad)), NOW)["task"]["focus"] is None


# ------------------------------------------------------------ 锁屏一行 (不含输出原文)

@pytest.mark.parametrize("doc, line", [
    ({"v": 1, "updated": NOW, "state": "idle"}, "大肥鱼 · 空闲"),
    (_doc(), "大肥鱼 · 在看 con-1"),
    (_doc(task=dict(_doc()["task"], focus=None)), "大肥鱼 · 在做事"),
    (_doc(task=dict(_doc()["task"], focus="kvm")), "大肥鱼 · 在看键鼠"),
    (_doc(state="estopped"), "大肥鱼 · 急停中"),
])
def test_lock_line(doc, line):
    assert bf.lock_line(bf.parse(json.dumps(doc, ensure_ascii=False), NOW)) == line


def test_lock_line_offline_and_stale():
    assert bf.lock_line(bf.parse(None, NOW)) == "大肥鱼 · 不在线"
    assert bf.lock_line(bf.parse(_text(updated=NOW - 999), NOW)) == "大肥鱼 · 状态过期"
    assert bf.lock_line(bf.parse("{", NOW)) == "大肥鱼 · 状态读不懂"


def test_lock_line_never_shows_untrusted_text():
    v = bf.parse(_text(task=dict(_doc()["task"], title="秘密标题", step="秘密步骤", output=["秘密输出"])), NOW)
    line = bf.lock_line(v)
    assert "秘密" not in line


# ------------------------------------------------------------ 面板行

def test_panel_rows_mark_untrusted_text():
    rows = bf.panel_rows(bf.parse(_text(), NOW), NOW)
    own = [t for t, tag in rows if tag != "untrusted"]
    said = [t for t, tag in rows if tag == "untrusted"]
    assert own[0] == "在看 con-1 · 2 分钟"
    assert said and all(t.startswith(bf.SAYS) for t in said)
    assert any("看一下 con-1" in t for t in said) and any("第二行" in t for t in said)
    assert not any("看一下" in t for t in own)          # 推车自己的行里不夹它的话


def test_panel_rows_offline():
    assert bf.panel_rows(bf.parse(None, NOW), NOW) == [("大肥鱼不在线", "bad")]
    assert bf.panel_rows(bf.parse(_text(updated=NOW - 999), NOW), NOW) == [("大肥鱼状态过期", "bad")]


def test_panel_rows_idle():
    assert bf.panel_rows(bf.parse(json.dumps({"v": 1, "updated": NOW, "state": "idle"}), NOW), NOW) == \
        [("空闲", "dim")]


def test_elapsed_text():
    assert bf.elapsed(30) == "30 秒" and bf.elapsed(125) == "2 分钟" and bf.elapsed(3 * 3600 + 60) == "3 小时 1 分"
    assert bf.elapsed(None) == "" and bf.elapsed(-5) == ""


# ------------------------------------------------------------ 读文件: 不跟链接, 只认普通文件和 bigfish 的

def test_read_status_regular_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(_text(), encoding="utf-8")
    assert json.loads(bf.read_status(str(p), uid=os.getuid()))["state"] == "running"


def test_read_status_refuses_symlink_fifo_wrong_owner_and_missing(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(_text(), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert bf.read_status(str(link), uid=os.getuid()) is None
    assert bf.read_status(str(fifo), uid=os.getuid()) is None           # 不会卡住
    assert bf.read_status(str(real), uid=os.getuid() + 1) is None
    assert bf.read_status(str(tmp_path / "nope"), uid=os.getuid()) is None
    assert bf.read_status(str(real), uid=None) is None                  # 没有 bigfish 用户


def test_read_status_too_big_is_cut(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("x" * (bf.MAX_BYTES + 100))
    assert bf.parse(bf.read_status(str(p), uid=os.getuid()), NOW)["why"] == "状态读不懂"


def test_load_end_to_end(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(_text(), encoding="utf-8")
    assert bf.load(NOW, path=str(p), uid=os.getuid())["online"]


# ------------------------------------------------------------ 急停信号: 大肥鱼读现有的 serial-estop.json

def test_estop_flag_contract_all_true_after_estop_all_false_after_resume(tmp_path):
    """接口约定靠的就是这个: 全部急停后 serial-estop.json 的 all 为 true, 解除后为 false; 只停一路不算."""
    import mcp_serial as ms
    store = ms.Store(path=str(tmp_path / "serial-write.json"), log_dir=str(tmp_path / "log"), event=None,
                     on_estop_all=None)
    flag = tmp_path / ms.ESTOP_FLAG
    act = lambda op, arg: ms.human_action(store, op, arg, "human:kiosk@local", None, None)  # noqa: E731
    act("estop", 2)
    assert json.loads(flag.read_text())["all"] is False
    act("estop", None)
    assert json.loads(flag.read_text())["all"] is True and os.stat(flag).st_mode & 0o777 == 0o644
    act("resume", None)
    assert json.loads(flag.read_text())["all"] is False


# ------------------------------------------------------------ 确认 / 拒绝: 复用现有确认框

def test_pending_target_prefers_first_serial_slot_then_kvm():
    st = {1: {"pending": 0}, 2: {"pending": 2}, 3: {"pending": 1}, 4: {"pending": 0}}
    assert bf.pending_target(st, {"pending": 1}) == ("serial", 2)
    none = {n: {"pending": 0} for n in (1, 2, 3, 4)}
    assert bf.pending_target(none, {"pending": 1}) == ("kvm", None)
    assert bf.pending_target(none, None) is None
    assert bf.pending_count(st, {"pending": 1}) == 4 and bf.pending_count(none, None) == 0


def test_tmpfiles_creates_status_dir_owned_by_bigfish():
    import pathlib
    conf = (pathlib.Path(__file__).resolve().parent.parent / "deploy" / "tmpfiles.d" / "ops-cart.conf").read_text()
    assert f"d {os.path.dirname(bf.STATUS_PATH)} 0755 bigfish bigfish -" in conf
