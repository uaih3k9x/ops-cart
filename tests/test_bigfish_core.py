"""大肥鱼面板的纯逻辑 (#53, 接口 v1 最终版): 状态文件读 / 解析 / 清洗 / 过期, 锁屏一行, 急停信号和硬急停兜底."""

import json
import os
import pathlib

import pytest

import bigfish_core as bf

NOW = 1_700_000_000.0


def _doc(**kw):
    d = {"schema": 1, "writer": "bigfish-mcp", "seq": 7, "written_at": NOW - 2,
         "estop": {"seen": False, "since": None},
         "current": {"task_id": "t-1", "state": "running", "started_at": NOW - 125, "elapsed_s": 125,
                     "steps_total": 5, "step_current": 2, "stop_reason": None, "source": "mcp"},
         "running_count": 1,
         "untrusted": {"title": "看一下 con-1 上是什么设备", "output_tail": "第一行\n第二行\n",
                       "steps": [{"title": "读串口", "status": "completed"}, {"title": "截屏", "status": "running"}]}}
    d.update(kw)
    return d


def _text(**kw):
    return json.dumps(_doc(**kw), ensure_ascii=False)


def _cur(**kw):
    return dict(_doc()["current"], **kw)


# ------------------------------------------------------------ 解析

def test_parse_running_task():
    v = bf.parse(_text(), NOW)
    assert v["online"] and not v["estop_seen"] and v["running_count"] == 1
    c = v["current"]
    assert c["state"] == "running" and c["elapsed_s"] == 125 and (c["step_current"], c["steps_total"]) == (2, 5)
    s = v["said"]
    assert s["title"] == "看一下 con-1 上是什么设备" and s["output"] == ["第一行", "第二行"]
    assert s["steps"] == [("completed", "读串口"), ("running", "截屏")]


@pytest.mark.parametrize("text, why", [
    (None, "不在线"),
    ("", "状态读不懂"),
    ("{not json", "状态读不懂"),
    ("[]", "状态读不懂"),
    (json.dumps({"schema": 2, "written_at": NOW}), "状态读不懂"),
    (json.dumps({"schema": 1, "written_at": "x"}), "状态读不懂"),
    (json.dumps({"schema": 1, "written_at": True}), "状态读不懂"),
    (json.dumps({"schema": 1, "written_at": NOW - bf.STALE_S - 1}), "离线"),
    (json.dumps({"schema": 1, "written_at": NOW + bf.FUTURE_S + 1}), "离线"),
])
def test_parse_offline_cases_never_raise(text, why):
    v = bf.parse(text, NOW)
    assert not v["online"] and v["why"] == why and v["current"] is None and not v["estop_seen"]


def test_parse_too_big_is_unreadable():
    assert bf.parse("x" * (bf.MAX_BYTES + 1), NOW)["why"] == "状态读不懂"


def test_parse_idle_minimal():
    v = bf.parse(json.dumps({"schema": 1, "written_at": NOW, "current": None}), NOW)
    assert v["online"] and v["current"] is None and v["running_count"] == 0
    assert v["said"] == {"title": "", "output": [], "steps": []}


@pytest.mark.parametrize("cur", [_cur(state="hacked"), _cur(state=None), "boom", 5])
def test_parse_bad_current_is_dropped(cur):
    assert bf.parse(_text(current=cur), NOW)["current"] is None


def test_parse_bad_trusted_numbers_are_dropped():
    c = bf.parse(_text(current=_cur(elapsed_s=-1, started_at="x", steps_total=True, step_current=9,
                                    source="\x1b[31mmcp\nX")), NOW)["current"]
    assert c["state"] == "running" and c["elapsed_s"] is None and c["started_at"] is None
    assert c["steps_total"] is None and c["step_current"] is None
    assert "\x1b" not in c["source"] and "\n" not in c["source"]


def test_parse_step_current_beyond_total_dropped():
    c = bf.parse(_text(current=_cur(steps_total=3, step_current=4)), NOW)["current"]
    assert c["step_current"] is None and c["steps_total"] == 3


def test_parse_estop_seen_only_true_counts():
    assert bf.parse(_text(estop={"seen": True, "since": NOW - 1}), NOW)["estop_seen"]
    for bad in ({"seen": "yes"}, {"seen": 1}, "x", None):
        assert not bf.parse(_text(estop=bad), NOW)["estop_seen"]


def test_parse_running_count_validated():
    assert bf.parse(_text(running_count=-3), NOW)["running_count"] == 0
    assert bf.parse(_text(running_count="2"), NOW)["running_count"] == 0


# ------------------------------------------------------------ 清洗: 大肥鱼说的话不可信

def test_clean_strips_ansi_control_and_newlines():
    s = bf.clean("\x1b[31m红\x1b[0m\n【推车】急停已解除\r\x07‮反")
    assert "\x1b" not in s and "\n" not in s and "\r" not in s and "\x07" not in s and "‮" not in s
    assert "红" in s and "急停已解除" in s


def test_clean_limits_length():
    s = bf.clean("鱼" * 1000)
    assert len(s) <= bf.TITLE_MAX + 1 and s.endswith("…")


def test_parse_cleans_every_untrusted_field_and_caps():
    evil = "a\x1b]8;;http://x\x07link\x1b]8;;\x07 b"
    u = {"title": evil * 50, "output_tail": ("x" * 5000) + "\n" + "\n".join([evil] * 50),
         "steps": [{"title": evil * 10, "status": evil}] * 30 + ["bad", {"title": 5}]}
    assert len(_text(untrusted=u)) < bf.MAX_BYTES
    s = bf.parse(_text(untrusted=u), NOW)["said"]
    texts = [s["title"], *s["output"], *[t for pair in s["steps"] for t in pair]]
    for t in texts:
        assert "\x1b" not in t and "\n" not in t and "\x07" not in t and " " not in t
    assert len(s["title"]) <= bf.TITLE_MAX + 1 and len(s["output"]) == bf.OUTPUT_LINES
    assert len(s["steps"]) == bf.STEPS_MAX and all(len(t) <= bf.STEP_TITLE_MAX + 1 for _st, t in s["steps"])


def test_output_tail_only_last_2kb():
    s = bf.parse(_text(untrusted={"output_tail": "开头不该出现\n" + "y" * 3000}), NOW)["said"]
    assert not any("开头" in x for x in s["output"])


def test_untrusted_not_dict_is_empty():
    assert bf.parse(_text(untrusted="boom"), NOW)["said"] == {"title": "", "output": [], "steps": []}


# ------------------------------------------------------------ 锁屏一行 (不含输出原文)

@pytest.mark.parametrize("doc, line", [
    (_doc(current=None, running_count=0), "大肥鱼 · 空闲"),
    (_doc(), "大肥鱼 · 在做事 · 第 2/5 步"),
    (_doc(current=_cur(steps_total=None, step_current=None)), "大肥鱼 · 在做事"),
    (_doc(current=_cur(state="starting")), "大肥鱼 · 启动中"),
    (_doc(current=_cur(state="done")), "大肥鱼 · 做完了"),
    (_doc(current=_cur(state="failed")), "大肥鱼 · 失败"),
    (_doc(current=_cur(state="cancelled")), "大肥鱼 · 已取消"),
    (_doc(current=_cur(state="estopped")), "大肥鱼 · 急停中"),
    (_doc(estop={"seen": True, "since": NOW}), "大肥鱼 · 急停中"),
])
def test_lock_line(doc, line):
    assert bf.lock_line(bf.parse(json.dumps(doc, ensure_ascii=False), NOW)) == line


def test_lock_line_offline_and_stale():
    assert bf.lock_line(bf.parse(None, NOW)) == "大肥鱼 · 不在线"
    assert bf.lock_line(bf.parse(_text(written_at=NOW - 999), NOW)) == "大肥鱼 · 离线"
    assert bf.lock_line(bf.parse("{", NOW)) == "大肥鱼 · 状态读不懂"


def test_lock_line_never_shows_untrusted_text():
    u = {"title": "秘密标题", "output_tail": "秘密输出", "steps": [{"title": "秘密步骤", "status": "x"}]}
    assert "秘密" not in bf.lock_line(bf.parse(_text(untrusted=u), NOW))


# ------------------------------------------------------------ 面板行

def test_panel_rows_mark_untrusted_text():
    rows = bf.panel_rows(bf.parse(_text(), NOW), NOW)
    own = [t for t, tag in rows if tag != "untrusted"]
    said = [t for t, tag in rows if tag == "untrusted"]
    assert own[0] == "在做事 · 第 2/5 步 · 2 分钟"
    assert said and all(t.startswith(bf.SAYS) for t in said)
    assert any("看一下 con-1" in t for t in said) and any("第二行" in t for t in said)
    assert any("截屏" in t for t in said)
    assert not any("看一下" in t for t in own)


def test_panel_rows_offline():
    assert bf.panel_rows(bf.parse(None, NOW), NOW) == [("大肥鱼不在线", "bad")]
    assert bf.panel_rows(bf.parse(_text(written_at=NOW - 999), NOW), NOW) == [("大肥鱼离线", "bad")]


def test_panel_rows_idle():
    assert bf.panel_rows(bf.parse(json.dumps({"schema": 1, "written_at": NOW}), NOW), NOW) == [("空闲", "dim")]


def test_panel_rows_note_and_channel_first():
    v = bf.parse(json.dumps({"schema": 1, "written_at": NOW}), NOW)
    assert bf.panel_rows(v, NOW, note="已硬停，需人工恢复")[0] == ("已硬停，需人工恢复", "bad")
    assert bf.panel_rows(v, NOW, ready=False)[0] == ("急停通道未就绪", "bad")
    assert bf.panel_rows(v, NOW, note="x", ready=False)[0] == ("x", "bad")   # 有急停进展先说进展


def test_elapsed_text():
    assert bf.elapsed(30) == "30 秒" and bf.elapsed(125) == "2 分钟" and bf.elapsed(3 * 3600 + 60) == "3 小时 1 分"
    assert bf.elapsed(None) == "" and bf.elapsed(-5) == ""


# ------------------------------------------------------------ 读文件: 不跟链接, 只认普通文件和 bigfish 的

def test_read_status_regular_file(tmp_path):
    p = tmp_path / "bigfish.json"
    p.write_text(_text(), encoding="utf-8")
    assert json.loads(bf.read_status(str(p), uid=os.getuid()))["schema"] == 1


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


def test_read_status_too_big_is_refused(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("x" * (bf.MAX_BYTES + 100))
    assert bf.read_status(str(p), uid=os.getuid()) is None


def test_load_end_to_end(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text(_text(), encoding="utf-8")
    monkeypatch.setattr(bf, "STATUS_PATH", str(p))
    monkeypatch.setattr(bf, "bigfish_uid", lambda user=bf.USER: os.getuid())
    assert bf.load(NOW)["online"]


def test_paths_are_the_v1_contract():
    assert bf.STATUS_PATH == "/home/bigfish/status/bigfish.json"
    src = pathlib.Path(bf.__file__).read_text()                 # conftest 改过模块里的值, 看源码里的默认
    assert 'ESTOP_DIR = "/run/bigfish-ctl"' in src and 'ESTOP_PATH = ESTOP_DIR + "/estop.json"' in src


# ------------------------------------------------------------ 软急停: 写 estop.json, seq 递增, 解除删掉

def test_estop_write_seq_increments_and_mode(tmp_path):
    p = str(tmp_path / "estop.json")
    assert bf.estop_write(NOW, path=p, gid=os.getgid()) == 1
    d = json.loads(open(p).read())
    assert d == {"seq": 1, "at": NOW, "by": "kiosk"}
    st = os.stat(p)
    assert st.st_mode & 0o777 == 0o640 and st.st_gid == os.getgid()
    assert bf.estop_write(NOW + 1, path=p, gid=os.getgid()) == 2
    assert json.loads(open(p).read())["seq"] == 2
    assert sorted(os.listdir(tmp_path)) == ["estop.json"]          # 临时文件不留


def test_estop_write_bad_old_file_restarts_seq(tmp_path):
    p = tmp_path / "estop.json"
    p.write_text("{broken")
    assert bf.estop_write(NOW, path=str(p)) == 1


def test_estop_write_dir_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bf.estop_write(NOW, path=str(tmp_path / "nope" / "estop.json"))


def test_estop_clear(tmp_path):
    p = str(tmp_path / "estop.json")
    bf.estop_write(NOW, path=p)
    bf.estop_clear(path=p)
    assert not os.path.exists(p)
    bf.estop_clear(path=p)                       # 没有也不报错


def test_channel_ready(tmp_path):
    assert bf.channel_ready(str(tmp_path)) and not bf.channel_ready(str(tmp_path / "nope"))


def test_default_estop_path_redirected_in_tests():
    """conftest 把 ESTOP_DIR / ESTOP_PATH 改到临时目录: 走急停的用例不碰 /run."""
    assert not bf.ESTOP_PATH.startswith("/run/")


# ------------------------------------------------------------ 硬急停兜底: 10 秒内没确认就 systemctl stop

def _seen(seen, online=True):
    return bf.parse(_text(estop={"seen": seen, "since": NOW}), NOW) if online else bf.parse(None, NOW)


def test_watch_seen_within_deadline():
    w = bf.EstopWatch(NOW)
    assert w.step(NOW + 1, _seen(False)) is None
    assert w.step(NOW + 3, _seen(True)) == "seen"


def test_watch_escalates_after_deadline_if_not_seen_or_offline():
    assert bf.EstopWatch(NOW).step(NOW + bf.HARD_AFTER_S, _seen(False)) == "hard"
    w = bf.EstopWatch(NOW)
    assert w.step(NOW + 5, _seen(True, online=False)) is None
    assert w.step(NOW + bf.HARD_AFTER_S + 0.1, _seen(True, online=False)) == "hard"


def test_hard_stop_terminates_user_then_stops_manager_and_logs():
    ran, logged = [], []
    ok, msg = bf.hard_stop(lambda cmd: ran.append(cmd) or (0, ""), 1001, "10 秒内没确认",
                           event=lambda kind, m: logged.append((kind, m)))
    assert ok and ran == [["loginctl", "terminate-user", "bigfish"], ["systemctl", "stop", "user@1001.service"]]
    assert logged and logged[0][0] == "bigfish" and "user@1001.service" in logged[0][1] and "10 秒内没确认" in logged[0][1]
    assert "terminate-user" in logged[0][1]


def test_hard_stop_ok_follows_systemctl_even_if_terminate_fails():
    rcs = {"loginctl": 1, "systemctl": 0}
    ok, _ = bf.hard_stop(lambda cmd: (rcs[cmd[0]], ""), 1001, "x", event=lambda k, m: None)
    assert ok


def test_hard_stop_failure_and_no_user():
    logged = []
    ok, _ = bf.hard_stop(lambda cmd: (5, ""), 1001, "x", event=lambda k, m: logged.append(m))
    assert not ok and "rc=5" in logged[-1]
    ran = []
    ok, msg = bf.hard_stop(lambda cmd: ran.append(cmd) or (0, ""), None, "x", event=lambda k, m: logged.append(m))
    assert not ok and ran == [] and "bigfish" in msg


# ------------------------------------------------------------ v1.1: estop.seq 回显

def test_parse_estop_seq_absent_invalid_and_present():
    assert bf.parse(_text(estop={"seen": True}), NOW)["estop_seq"] is None          # v1: 没这个字段
    assert bf.parse(_text(estop={"seen": True, "seq": 3}), NOW)["estop_seq"] == 3
    for bad in ("3", True, -1, 1.5, None):                                       # null: 还没处理过任何 seq
        assert bf.parse(_text(estop={"seen": True, "seq": bad}), NOW)["estop_seq"] == bf.BAD_SEQ


def test_watch_v11_requires_matching_seq():
    w = bf.EstopWatch(NOW, seq=4)
    old = bf.parse(_text(estop={"seen": True, "seq": 3}), NOW)
    assert w.step(NOW + 1, old) is None                                             # 还是上一次的回显
    bad = bf.parse(_text(estop={"seen": True, "seq": "4"}), NOW)
    assert w.step(NOW + 2, bad) is None
    assert w.step(NOW + 3, bf.parse(_text(estop={"seen": True, "seq": 4}), NOW)) == "seen"
    assert bf.EstopWatch(NOW, seq=4).step(NOW + bf.HARD_AFTER_S, old) == "hard"


def test_watch_v11_null_seq_is_not_a_confirmation():
    v = bf.parse(_text(estop={"seen": True, "seq": None}), NOW)
    assert bf.EstopWatch(NOW, seq=1).step(NOW + 1, v) is None


def test_watch_v1_fallback_without_seq():
    assert bf.EstopWatch(NOW, seq=9).step(NOW + 1, bf.parse(_text(estop={"seen": True}), NOW)) == "seen"


# ------------------------------------------------------------ stop_reason 只显示枚举

@pytest.mark.parametrize("raw, shown", [("estop", "急停"), ("cancelled", "已取消"), ("end_turn", "正常结束"),
                                        ("max_tokens", "超出长度"), ("refusal", "模型拒绝"),
                                        ("【推车】已解除", "其他"), (5, ""), (None, "")])
def test_stop_reason_enum(raw, shown):
    c = bf.parse(_text(current=_cur(state="cancelled", stop_reason=raw)), NOW)["current"]
    assert c["stop_reason"] == shown


def test_panel_shows_stop_reason_enum_only():
    rows = bf.panel_rows(bf.parse(_text(current=_cur(state="cancelled", stop_reason="estop")), NOW), NOW)
    assert rows[0] == ("已取消 · 急停", "own")


# ------------------------------------------------------------ 步数: 总数 0 = 未知

@pytest.mark.parametrize("total, cur, kept", [(5, 5, 5), (5, 6, None), (0, 0, 0), (0, 37, 37), (0, 10000, 10000),
                                              (0, 10001, None), (0, -1, None)])
def test_step_current_bounds(total, cur, kept):
    assert bf.parse(_text(current=_cur(steps_total=total, step_current=cur)), NOW)["current"]["step_current"] == kept


def test_steps_total_zero_shows_only_current():
    v = bf.parse(_text(current=_cur(steps_total=0, step_current=3)), NOW)
    assert v["current"]["step_current"] == 3 and bf.lock_line(v) == "大肥鱼 · 在做事 · 第 3 步"


# ------------------------------------------------------------ estop.json 残留: 面板要能看见

def test_estop_active(tmp_path):
    p = str(tmp_path / "estop.json")
    assert not bf.estop_active(p)
    bf.estop_write(NOW, path=p)
    assert bf.estop_active(p)
    bf.estop_clear(path=p)
    assert not bf.estop_active(p)


# ------------------------------------------------------------ 确认 / 拒绝: 复用现有确认框

def test_pending_target_prefers_first_serial_slot_then_kvm():
    st = {1: {"pending": 0}, 2: {"pending": 2}, 3: {"pending": 1}, 4: {"pending": 0}}
    assert bf.pending_target(st, {"pending": 1}) == ("serial", 2)
    none = {n: {"pending": 0} for n in (1, 2, 3, 4)}
    assert bf.pending_target(none, {"pending": 1}) == ("kvm", None)
    assert bf.pending_target(none, None) is None
    assert bf.pending_count(st, {"pending": 1}) == 4 and bf.pending_count(none, None) == 0


def test_no_tmpfiles_for_bigfish_dirs():
    """两个目录都由大肥鱼那边的 setup-root.sh 建, 本仓库 tmpfiles 不碰."""
    conf = (pathlib.Path(__file__).resolve().parent.parent / "deploy" / "tmpfiles.d" / "ops-cart.conf").read_text()
    assert "bigfish" not in conf


# ------------------------------------------------------------ #101 非有限数和超大整数

@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan"), 10 ** 400], ids=lambda v: str(v)[:8])
def test_num_rejects_non_finite_and_huge(v):
    assert bf._num(v) is None and bf._num(v, 0) is None


def test_num_upper_bound():
    assert bf._num(5, 0, 5) == 5 and bf._num(6, 0, 5) is None


@pytest.mark.parametrize("raw", ["NaN", "1e999", "-Infinity", "1" + "0" * 400], ids=lambda v: str(v)[:8])
def test_parse_non_finite_written_at_is_not_online(raw):
    text = '{"schema": 1, "written_at": %s}' % raw
    assert bf.parse(text, NOW)["online"] is False


@pytest.mark.parametrize("raw", ["NaN", "1e999", "1" + "0" * 400, "1e12"], ids=lambda v: str(v)[:8])
def test_parse_bad_elapsed_in_status_file_dropped(raw):
    text = '{"schema": 1, "written_at": %f, "current": {"state": "running", "elapsed_s": %s}}' % (NOW, raw)
    view = bf.parse(text, NOW)
    assert view["current"]["elapsed_s"] is None
    assert bf.panel_rows(view, NOW)


@pytest.mark.parametrize("v", [float("inf"), float("nan")])
def test_elapsed_non_finite_is_blank(v):
    assert bf.elapsed(v) == ""
