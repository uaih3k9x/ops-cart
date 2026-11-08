"""observe_core 测试 (#85 串口观察模式, 半自动): brief 解析 / 前面板排布 / 灯 / 轮询节奏 / 看现场 / 会话编排.

人在 console 上登录好, 推车只发只读命令. 全部用假 send / 假日志, 不碰串口和 conbroker.
样例是手写的, 不含真实地址、MAC、主机名.
"""

import json
import os

import pytest

import observe_core as oc
import survey_core

# ------------------------------------------------------------ display interface brief 解析

BRIEF_S = """display interface brief
PHY: Physical
*down: administratively down
^down: standby
(l): loopback
(s): spoofing
(E): E-Trunk down
(b): BFD down
(e): ETHOAM down
(dl): DLDP down
(d): Dampening Suppressed
InUti/OutUti: input utility/output utility
Interface                   PHY   Protocol  InUti OutUti   inErrors  outErrors
Eth-Trunk1                  up    up           0%     0%          0          0
GigabitEthernet0/0/1        up    up           0%     0%          0          0
GigabitEthernet0/0/2        down  down         0%     0%          0          0
GigabitEthernet0/0/3        *down down         0%     0%          0          0
GigabitEthernet0/0/4(l)     up    up        0.01%  0.01%          0          0
GigabitEthernet0/0/5        ^down down         0%     0%          0          0
GigabitEthernet0/0/6        up    up(s)     12.5%   3.2%          0          0
MEth0/0/1                   down  down         0%     0%          0          0
NULL0                       up    up(s)        0%     0%          0          0
Vlanif1                     up    up           --     --          0          0
XGigabitEthernet0/0/1       up    up          55%   100%          0          0
<SW-TEST>"""

BRIEF_AR = """display interface brief
PHY: Physical
*down: administratively down
^down: standby
(l): loopback
(s): spoofing
(b): BFD down
(e): ETHOAM down
(dl): DLDP down
(d): Dampening Suppressed
InUti/OutUti: input utility/output utility
Interface                   PHY   Protocol InUti OutUti   inErrors  outErrors
Cellular0/0/0               down  down        0%     0%          0          0
GigabitEthernet0/0/0        up    up       0.01%  0.01%          0          0
GigabitEthernet0/0/1        down  down        0%     0%          0          0
GigabitEthernet0/0/2        *down down        0%     0%          0          0
GigabitEthernet0/0/3        up    up          1%     2%          0          0
NULL0                       up    up(s)       0%     0%          0          0
Vlanif1                     up    up          --     --          0          0
<AR-TEST>"""

BRIEF_VRP8 = """Interface                     PHY      Protocol  InUti OutUti   inErrors  outErrors
25GE1/0/1                     up       up         0.10%  0.20%          0          0
25GE1/0/2                     down     down          0%     0%          0          0
100GE1/0/1                    up(d)    up            0%     0%          0          0
MEth0/0/0                     up       up         0.01%  0.01%          0          0
"""


def _by(ports):
    return {p["name"]: p for p in ports}


def test_parse_brief_states():
    p = _by(oc.parse_brief(BRIEF_S))
    assert p["GigabitEthernet0/0/1"]["state"] == "up"
    assert p["GigabitEthernet0/0/2"]["state"] == "down"
    assert p["GigabitEthernet0/0/3"]["state"] == "admin"
    assert p["GigabitEthernet0/0/5"]["state"] == "standby"
    assert p["GigabitEthernet0/0/3"]["phy"] == "*down"


def test_parse_brief_flags_stripped_from_name_and_kept():
    p = _by(oc.parse_brief(BRIEF_S))
    assert p["GigabitEthernet0/0/4"]["flags"] == ["l"]
    assert p["GigabitEthernet0/0/6"]["flags"] == ["s"]
    assert p["GigabitEthernet0/0/6"]["proto"] == "up"


def test_parse_brief_utilization():
    p = _by(oc.parse_brief(BRIEF_S))
    assert p["GigabitEthernet0/0/6"]["in_uti"] == 12.5 and p["GigabitEthernet0/0/6"]["out_uti"] == 3.2
    assert p["GigabitEthernet0/0/4"]["in_uti"] == 0.01
    assert p["XGigabitEthernet0/0/1"]["out_uti"] == 100
    assert p["Vlanif1"]["in_uti"] is None


def test_parse_brief_skips_legend_header_echo_and_prompt():
    names = [x["name"] for x in oc.parse_brief(BRIEF_S)]
    assert names[0] == "Eth-Trunk1"
    assert not any(n.startswith(("PHY", "*down", "Interface", "display", "<", "InUti")) for n in names)
    assert len(names) == 11


def test_parse_brief_ar():
    p = _by(oc.parse_brief(BRIEF_AR))
    assert set(p) >= {"GigabitEthernet0/0/0", "GigabitEthernet0/0/3", "Cellular0/0/0"}
    assert p["GigabitEthernet0/0/2"]["state"] == "admin"
    assert p["GigabitEthernet0/0/3"]["out_uti"] == 2


def test_parse_brief_vrp8_flags_on_phy():
    p = _by(oc.parse_brief(BRIEF_VRP8))
    assert p["100GE1/0/1"]["state"] == "up" and p["100GE1/0/1"]["flags"] == ["d"]
    assert p["25GE1/0/1"]["in_uti"] == 0.1


def test_parse_brief_tolerates_cr_and_garbage():
    text = BRIEF_S.replace("\n", "\r\n") + "\r\nInfo: something\r\n"
    assert len(oc.parse_brief(text)) == 11
    assert oc.parse_brief("") == [] and oc.parse_brief(None) == []


def test_parsers_registry_has_huawei_and_room_for_others():
    assert oc.PARSERS["huawei"] is oc.parse_brief
    assert oc.parser_for("h3c") is None


# ------------------------------------------------------------ 接口名 -> 前面板

def test_port_kind():
    assert oc.port_kind("GigabitEthernet0/0/12") == ("GE", (0, 0), 12, "rj45")
    assert oc.port_kind("XGigabitEthernet0/0/1") == ("XGE", (0, 0), 1, "sfp")
    assert oc.port_kind("25GE1/0/3") == ("25GE", (1, 0), 3, "sfp")
    assert oc.port_kind("40GE1/0/1")[3] == "qsfp"
    assert oc.port_kind("100GE1/0/1")[3] == "qsfp"
    assert oc.port_kind("Ethernet0/0/1")[0] == "FE"
    for name in ("Vlanif1", "NULL0", "Eth-Trunk1", "MEth0/0/1", "LoopBack0", "Cellular0/0/0", "Tunnel0/0/1"):
        assert oc.port_kind(name) is None, name


def test_auto_layout_odd_top_even_bottom():
    names = [f"GigabitEthernet0/0/{i}" for i in range(1, 9)]
    lay = _by(oc.auto_layout(names))
    assert (lay["GigabitEthernet0/0/1"]["x"], lay["GigabitEthernet0/0/1"]["y"]) == (0, 0)
    assert (lay["GigabitEthernet0/0/2"]["x"], lay["GigabitEthernet0/0/2"]["y"]) == (0, 1)
    assert (lay["GigabitEthernet0/0/7"]["x"], lay["GigabitEthernet0/0/7"]["y"]) == (3, 0)
    assert lay["GigabitEthernet0/0/8"]["label"] == "8"
    assert lay["GigabitEthernet0/0/8"]["shape"] == "rj45"


def test_auto_layout_groups_left_to_right_with_gap():
    names = (["XGigabitEthernet0/0/1", "XGigabitEthernet0/0/2"]
             + [f"GigabitEthernet0/0/{i}" for i in range(1, 5)] + ["Vlanif1", "NULL0"])
    lay = _by(oc.auto_layout(names))
    assert set(lay) == set(names) - {"Vlanif1", "NULL0"}
    assert lay["GigabitEthernet0/0/4"]["x"] == 1
    assert lay["XGigabitEthernet0/0/1"]["x"] == 3        # GE 占 0-1, 空一列
    assert lay["XGigabitEthernet0/0/1"]["shape"] == "sfp"
    assert lay["XGigabitEthernet0/0/1"]["group"] == "XGE"


def test_auto_layout_zero_based_ar_ports():
    names = [f"GigabitEthernet0/0/{i}" for i in range(0, 4)]
    lay = _by(oc.auto_layout(names))
    assert lay["GigabitEthernet0/0/0"]["y"] == 0 and lay["GigabitEthernet0/0/1"]["y"] == 1
    assert lay["GigabitEthernet0/0/0"]["label"] == "0"


def test_auto_layout_sorted_numerically_not_lexically():
    names = [f"GigabitEthernet0/0/{i}" for i in (10, 2, 1, 9)]
    lay = _by(oc.auto_layout(names))
    assert lay["GigabitEthernet0/0/1"]["x"] == 0 and lay["GigabitEthernet0/0/2"]["y"] == 1
    assert lay["GigabitEthernet0/0/10"]["x"] == 1 and lay["GigabitEthernet0/0/10"]["y"] == 1


def test_auto_layout_stack_members_separate_groups():
    names = ["GigabitEthernet0/0/1", "GigabitEthernet1/0/1"]
    lay = _by(oc.auto_layout(names))
    assert lay["GigabitEthernet1/0/1"]["x"] == 2


def test_layout_override_by_model_prefix(tmp_path):
    p = tmp_path / "layouts.json"
    p.write_text(json.dumps({"AR651": {"ports": {"GigabitEthernet0/0/0": {"x": 5, "y": 0, "shape": "sfp"}}},
                             "AR651C": {"ports": {"GigabitEthernet0/0/0": {"x": 9, "y": 1}}}}))
    layouts = oc.load_layouts(str(p))
    names = ["GigabitEthernet0/0/0", "GigabitEthernet0/0/1"]
    lay = _by(oc.layout(names, "AR651C", layouts))
    assert (lay["GigabitEthernet0/0/0"]["x"], lay["GigabitEthernet0/0/0"]["y"]) == (9, 1)   # 最长前缀胜
    assert lay["GigabitEthernet0/0/0"]["shape"] == "rj45"          # 覆盖没写形状, 按名字推
    assert lay["GigabitEthernet0/0/1"]["x"] > 9                    # 没写到的口接在右边自动排
    assert _by(oc.layout(names, "S5735", layouts))["GigabitEthernet0/0/0"]["x"] == 0


def test_load_layouts_bad_file_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert oc.load_layouts(str(p)) == {}
    assert oc.load_layouts(str(tmp_path / "missing.json")) == {}


@pytest.mark.parametrize("text,model", [
    ("Huawei Versatile Routing Platform Software\nVRP (R) software, Version 5.170\nHUAWEI S5735-L24T4S-A1 Routing Switch uptime is 1 week, 2 days", "S5735-L24T4S-A1"),
    ("Huawei AR651C Router uptime is 0 week, 0 day, 1 hour", "AR651C"),
    ("HUAWEI S6730S-H24X6C uptime is 3 days, 4 hours", "S6730S-H24X6C"),
    ("nothing here", None),
])
def test_parse_model(text, model):
    assert oc.parse_model(text) == model


# ------------------------------------------------------------ 灯

def test_blink_period_faster_with_more_traffic():
    assert oc.blink_period(None) is None and oc.blink_period(0) is None
    ps = [oc.blink_period(u) for u in (0.01, 1, 10, 100)]
    assert all(a > b for a, b in zip(ps, ps[1:]))
    assert 0.1 <= ps[-1] and ps[0] <= 0.6


def test_lamp_states():
    up = {"state": "up", "in_uti": 0, "out_uti": 0}
    assert oc.lamp(up, 0) == ("up", True)                         # 没流量常亮
    assert oc.lamp({"state": "down"}, 0) == ("down", False)
    assert oc.lamp({"state": "admin"}, 0) == ("admin", True)
    assert oc.lamp({"state": "standby"}, 0) == ("standby", True)
    assert oc.lamp(None, 0) == ("unknown", False)


def test_lamp_blinks_with_traffic_using_lamps_core():
    import lamps_core as lc
    port = {"state": "up", "in_uti": 10, "out_uti": 1}
    period = oc.blink_period(10)
    seen = {oc.lamp(port, t * period)[1] for t in range(4)}
    assert seen == {True, False}
    assert oc.lamp(port, 0.3)[1] == lc.blink_on(0.3, period)


# ------------------------------------------------------------ 轮询节奏

def test_pacer_adapts_to_cost():
    p = oc.Pacer(duty=0.2, min_gap=5, max_gap=300, quiet=10)
    p.done(100, 104, size=0)                    # 一轮 4 秒 -> 再等 16 秒
    assert not p.due(115) and p.due(120)
    p.done(200, 200.1, size=0)                  # 很快 -> 至少隔 min_gap
    assert not p.due(204) and p.due(205.1)
    p.done(300, 400, size=0)                    # 很慢 -> 封顶 max_gap
    assert p.due(700)


def test_pacer_pauses_on_foreign_log_growth_and_resumes_after_quiet():
    p = oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=10, settle=2)
    p.done(0, 1, size=100)
    p.note_log(120, 1.5)                        # 自己那轮的尾巴 (断开标记), 不算人
    assert p.due(3)
    p.note_log(150, 4)                          # 有人在敲 / attach 了
    assert p.paused(5) and not p.due(5)
    assert not p.due(13.9) and p.due(14.1)


def test_pacer_poke_pauses():
    p = oc.Pacer(quiet=8)
    p.poke(50)
    assert not p.due(57) and p.due(58.1)


def test_pacer_unknown_log_size_ignored():
    p = oc.Pacer(min_gap=1, quiet=5)
    p.done(0, 1, size=None)
    p.note_log(None, 3)
    assert not p.paused(3) and p.due(6)


# ------------------------------------------------------------ 共用样例

USER = "\nLogin authentication\n\n\nUsername:"
IDLE = "\n\n  Press any key to get started\n"
ASYNC = ("Nov  7 2026 10:00:00 SW-TEST %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface "
         "GigabitEthernet0/0/1 has entered the UP state.")
ASYNC8 = "2026-11-07 10:00:00.123 +08:00 SW-TEST %%01IFNET/2/linkDown_active(l):CID=0x1; The interface status changes."
LOGGED = "\nInfo: The max number of VTY users is 5.\n\n<SW-TEST>"
RELOGIN = "quit\n" + IDLE + USER + "ops2\nPassword:\n" + LOGGED
SVC = "\n[2026-11-07 09:00:00 服务启动 /dev/con-1 9600]\n"
HUMAN_IN = "\n[2026-11-07 09:00:01 人 接入]\n"
HUMAN_OUT = "\n[2026-11-07 09:00:02 人 断开]\n"
TAKEOVER = ("\n[2026-11-07 10:01:00 人 接入]\n<SW-TEST>quit\n  Configuration console exit, please press any key to log on\n"
            "\nUsername:root\nPassword:\n<SW-TEST>\n[2026-11-07 10:01:20 人 断开]\n")
DISPLAY_USERS = ("<SW-TEST>display users\n  User-Intf    Delay    Type   Network Address     AuthenStatus    AuthorcmdFlag\n"
                 "+ 0   CON 0   00:00:00                                  pass           no\n  Username : admin\n\n<SW-TEST>")


# ------------------------------------------------------------ 纯函数: 看日志 / 分类 / 占用 / 快照

def test_agent_busy_from_write_state():
    states = {1: {"writer": "agent", "estop": False, "sending": False},
              2: {"writer": None, "estop": True, "sending": False},
              3: {"writer": None, "estop": False, "sending": False}}
    same = lambda p: p          # noqa: E731  不解析软链接
    assert "租约" in oc.agent_busy("/dev/con-1", states=lambda: states, realpath=same)
    assert "急停" in oc.agent_busy("/dev/con-2", states=lambda: states, realpath=same)
    assert oc.agent_busy("/dev/con-3", states=lambda: states, realpath=same) is None


def test_agent_busy_unreadable_state_refuses():
    def boom():
        raise ValueError("坏了")
    assert oc.agent_busy("/dev/con-1", states=boom, realpath=lambda p: p)


def test_classify_ignores_conbroker_markers():
    text = IDLE + "\n[2026-11-07 10:00:00 Claude 发送: display interface brief]\ndisplay interface brief\n[conbroker: 60s 内没等到提示符]"
    assert oc.classify(text) == "other"
    assert oc.classify(IDLE + "\n[conbroker: 60s 内没等到提示符]") == "idle"
    assert oc.classify("\n<SW-TEST>\n[2026-11-07 10:00:00 Claude 断开]\n") == "prompt"


def test_prompt_with_space_in_sysname():
    assert oc.classify("\n<Core SW 1>") == "prompt"
    assert oc.prompt_name("<Core SW 1>") == "Core SW 1"


def test_remove_snapshot_only_own(tmp_path):
    p = tmp_path / "observe.json"
    p.write_text(json.dumps({"owner": "a"}))
    assert oc.remove_snapshot("b", str(p)) is False and p.exists()
    assert oc.remove_snapshot("a", str(p)) is True and not p.exists()
    assert oc.remove_snapshot("a", str(p)) is False


def test_read_tail_strips_marks(tmp_path):
    p = tmp_path / "con-1.log"
    p.write_bytes(b"x" * 9000 + b"\r\n<SW-TEST>\r\n[2026-11-07 10:00:00 Claude \xe6\x96\xad\xe5\xbc\x80]\r\n")
    t = oc.read_tail(str(p), 4096)
    assert len(t) <= 4096 and oc.classify(t) == "prompt"
    assert oc.read_tail(str(tmp_path / "nope.log")) is None


@pytest.mark.parametrize("line", [ASYNC, ASYNC8, "#" + ASYNC])
def test_effective_line_skips_async_log_and_marks(line):
    text = "\n<SW-TEST>\n" + line + "\n[2026-11-07 10:00:00 Claude 断开]\n"
    assert oc.last_line(text) == "<SW-TEST>"
    assert oc.classify(text) == "prompt"


def test_device_text_ignores_conbroker_marks():
    a = "x\n<SW>\n[2026-11-07 10:00:00 Claude 断开]\n[2026-11-07 10:00:00 人 接入]\n"
    assert oc.device_text(a) == oc.device_text("x\n<SW>\n")
    assert oc.device_text(a) != oc.device_text("x\n<SW>d\n")


def test_snapshot_path_per_head():
    assert oc.snapshot_path("con-1") != oc.snapshot_path("con-2")
    assert oc.snapshot_path("con-1").startswith(oc.STATE_DIR)


def test_read_snapshots_newest_first(tmp_path):
    for head, ts in (("con-1", 10), ("con-2", 20)):
        (tmp_path / f"{head}.json").write_text(json.dumps({"ts": ts, "head": head}))
    (tmp_path / "junk.json").write_text("{bad")
    docs = oc.read_snapshots(str(tmp_path))
    assert [d["head"] for d in docs] == ["con-2", "con-1"]
    assert oc.read_snapshots(str(tmp_path / "none")) == []


def test_poll_timeout_is_tight():
    assert oc.DEFAULT_PROFILES["huawei"]["timeout"] <= 30
    assert oc.POLL_GRACE_S <= 15


def test_survey_conbroker_send_grace(monkeypatch):
    import survey_core as sv
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "<SW>", ""

    def run(args, **kw):
        seen["timeout"] = kw["timeout"]
        return R()
    monkeypatch.setattr(sv.subprocess, "run", run)
    monkeypatch.setattr(sv, "_conbroker_cmd", lambda: ["conbroker"])
    sv.conbroker_send("con-1", "display version", 20, grace=10)
    assert seen["timeout"] == 30
    sv.conbroker_send("con-1", "display version", 20)
    assert seen["timeout"] == 80                       # 巡检照旧


@pytest.mark.parametrize("ports", [
    {"GigabitEthernet0/0/1": {"x": -1, "y": 0}}, {"GigabitEthernet0/0/1": {"x": 0, "y": 2}},
    {"GigabitEthernet0/0/1": {"x": 500, "y": 0}}, {"GigabitEthernet0/0/1": {"x": "0", "y": 0}},
    {"GigabitEthernet0/0/1": {"x": True, "y": 0}},
])
def test_layout_file_out_of_range_rejected(tmp_path, ports):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"AR651C": {"ports": dict(ports, **{"GigabitEthernet0/0/2": {"x": 1, "y": 0}})}}))
    assert oc.load_layouts(str(p)) == {}


def test_split_port_names():
    assert oc.port_kind("10GE1/0/1:1") == ("XGE", (1, 0), 1, "sfp")
    lay = _by(oc.auto_layout(["100GE1/0/1:2", "100GE1/0/1:1", "100GE1/0/2"]))
    assert lay["100GE1/0/1:1"]["label"] == "1:1" and lay["100GE1/0/1:1"]["y"] == 0
    assert lay["100GE1/0/1:2"]["y"] == 1 and lay["100GE1/0/2"]["x"] == 1


def test_parse_brief_split_and_speed_suffix():
    text = ("XGigabitEthernet0/0/1(10G)   up    up     1%   2%   0   0\n"
            "10GE1/0/1:1                  up    up     0%   0%   0   0\n"
            "GigabitEthernet0/0/2(100M)(l) up   up     0%   0%   0   0\n")
    p = _by(oc.parse_brief(text))
    assert p["XGigabitEthernet0/0/1"]["flags"] == [] and p["XGigabitEthernet0/0/1"]["out_uti"] == 2
    assert "10GE1/0/1:1" in p
    assert p["GigabitEthernet0/0/2"]["flags"] == ["l"]


def test_log_file_read_from_offset(tmp_path):
    p = tmp_path / "con-1.log"
    p.write_bytes(b"abc\n<SW>\n")
    lf = oc.LogFile(str(p))
    off = lf.size()
    assert lf.read_from(off) == ""
    with open(p, "ab") as f:
        f.write(b"[2026-11-07 10:00:00 Claude \xe6\x96\xad\xe5\xbc\x80]\n")
    assert oc.device_text(lf.read_from(off)) == ""
    assert lf.read_from(off + 999) is None
    assert oc.LogFile(str(tmp_path / "none")).size() is None


def test_agent_busy_counts_pending():
    states = {1: {"writer": None, "estop": False, "sending": False, "pending": 2}}
    assert oc.agent_busy("/dev/con-1", states=lambda: states, realpath=lambda p: p)


def test_claim_shared_by_survey_and_observe():
    oc._CLAIMS.clear()
    assert oc.claim("/dev/con-1", "survey")
    assert not oc.claim("/dev/con-1", "observe-w1")
    oc.release("/dev/con-1", "survey")
    assert oc.claim("/dev/con-1", "observe-w1")
    assert oc.holder("/dev/con-1") == "observe-w1"
    oc._CLAIMS.clear()


@pytest.mark.parametrize("text,changed", [
    ("display clock\n2026-11-07 10:00:00\n<SW-TEST>", False),
    (IDLE, True), (USER, True), ("\nPassword:", True), ("<SW-TEST>quit\n", True), ("quit\n", True),
    ("\nLogin authentication\n", True), ("\nInfo: The max number of VTY users is 5.\n", True),
    ("\nThe current login time is 2026-11-07 10:00:00.\n", True),
])
def test_session_changed(text, changed):
    assert oc.session_changed(text) is changed


def test_humans_attached_counts():
    assert oc.humans_attached(SVC + HUMAN_IN) == 1
    assert oc.humans_attached(SVC + HUMAN_IN + HUMAN_OUT) == 0
    assert oc.humans_attached(HUMAN_IN + SVC) == 0                  # 服务重启前的不算
    assert oc.humans_attached(HUMAN_IN + "x" + HUMAN_IN.replace("人", "Claude")) == 1


def test_humans_attached_unknown_when_log_too_big(tmp_path):
    p = tmp_path / "con-1.log"
    p.write_bytes(b"x" * 2048 + HUMAN_OUT.encode())
    assert oc.LogFile(str(p)).humans(limit=1024) is None              # 没读到服务启动, 又不是从头读的
    assert oc.LogFile(str(p)).humans(limit=1 << 20) == 0


@pytest.mark.parametrize("line", [
    "Nov  7 2026 10:00:00 SW-TEST IFNET/4/LINK_STATE:The line protocol on the interface has entered the UP state.",
    "#Nov 7 2026 10:00:00+08:00 SW-TEST SHELL/5/CMDRECORD: Recorded command information.",
])
def test_trap_lines_without_percent_are_async(line):
    assert oc.classify("\n<SW-TEST>\n" + line + "\n") == "prompt"


def test_please_press_enter_is_idle():
    assert oc.classify("\n\nPlease Press ENTER.\n") == "idle"


def _off_writes(tree):
    """(所在函数, 写法) : 赋值 / 增量 / 删除 ._off, setattr / delattr(.., "_off"), __dict__ 访问, "_off" 字符串."""
    import ast
    hits = []

    def scan(node, fn):
        for n in ast.iter_child_nodes(node):
            name = n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(n, ast.Attribute) and n.attr == "_off" and isinstance(n.ctx, (ast.Store, ast.Del)):
                hits.append((name, type(n.ctx).__name__))
            if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                hits.append((name, "__dict__"))
            if isinstance(n, ast.Constant) and n.value == "_off":
                hits.append((name, "字符串"))
            scan(n, name)
    scan(tree, None)
    return hits


def test_baseline_only_moved_in_advance():
    """结构约束: 基线 (_off) 只许在 _advance 里赋值 —— 挪之前必须先扫跳过的日志.
    setattr / delattr / __dict__ / del / "_off" 字符串这些绕法也挡住."""
    import ast
    hits = _off_writes(ast.parse(open(oc.__file__, encoding="utf-8").read()))
    assert hits and all(h == ("_advance", "Store") for h in hits), hits


def test_no_other_module_writes_observer_baseline():
    import ast
    import glob
    root = os.path.dirname(oc.__file__)
    files = glob.glob(os.path.join(root, "*.py")) + glob.glob(os.path.join(root, "scripts", "*.py"))
    for f in files:
        if os.path.basename(f) == "observe_core.py":
            continue
        hits = [h for h in _off_writes(ast.parse(open(f, encoding="utf-8").read())) if h[1] != "__dict__"]
        assert not hits, (f, hits)


def test_off_write_scanner_catches_tricks():
    import ast
    src = ("def f(o):\n    o._off = 1\n"
           "def g(o):\n    setattr(o, '_off', 1)\n"
           "def h(o):\n    o.__dict__['x'] = 1\n"
           "def k(o):\n    del o._off\n")
    kinds = {k for _f, k in _off_writes(ast.parse(src))}
    assert kinds == {"Store", "字符串", "__dict__", "Del"}


def test_humans_reads_back_until_service_start(tmp_path):
    p = tmp_path / "con-1.log"
    with open(p, "wb") as f:
        f.write(SVC.encode() + HUMAN_IN.encode())
        f.write(b"filler line\n" * 300000)                 # 约 3.6MB
        f.write(b"<SW-TEST>\n")
    lf = oc.LogFile(str(p))
    assert lf.humans(limit=64 << 20, chunk=1 << 20) == 1
    assert oc.LogFile(str(p)).humans(limit=1 << 20, chunk=1 << 18) is None   # 新实例 (没缓存): 上限内没读到服务启动


def _logf(tmp_path, data):
    p = tmp_path / "con-1.log"
    p.write_bytes(data)
    return oc.LogFile(str(p))


M = staticmethod(lambda t: f"\r\n[2026-11-07 10:00:00 {t}]\r\n".encode()).__func__


def test_humans_ignores_echoed_service_start_text(tmp_path):
    data = (M("服务启动 loop:// 9600") + M("人 接入")
            + "<SW>display current | include 服务启动\r\n<SW>\r\n".encode())
    assert _logf(tmp_path, data).humans() == 1
    assert _logf(tmp_path, data).humans(chunk=7) == 1


def test_humans_fake_mark_mid_line_not_taken(tmp_path):
    data = M("服务启动 x") + M("人 接入") + "abc[2026-11-07 10:00:00 服务启动 y]\r\n".encode()
    assert _logf(tmp_path, data).humans(chunk=9) == 1


@pytest.mark.parametrize("pad", [0, 1, 5, 23])
@pytest.mark.parametrize("chunk", [4, 11, 22, 23, 40])
def test_humans_boundaries(tmp_path, pad, chunk):
    data = b"z" * pad + M("服务启动 loop:// 9600") + b"x" * 50 + M("人 接入") + b"y" * 30
    assert _logf(tmp_path, data).humans(chunk=chunk) == 1


def test_no_quit_literal_in_observe_modules():
    import ast
    for mod in ("observe_core.py", "observe_panel.py"):
        tree = ast.parse(open(os.path.join(os.path.dirname(oc.__file__), mod), encoding="utf-8").read())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "quit"], mod


def test_display_users_output_not_session_change():
    assert oc.session_changed(DISPLAY_USERS) is False
    assert oc.session_changed("\nUsername:") is True and oc.session_changed("\nUsername : ") is True


def test_human_attached_counts_cached_incrementally(tmp_path):
    p = tmp_path / "con-1.log"
    p.write_bytes(SVC.encode() + HUMAN_IN.encode())
    lf = oc.LogFile(str(p))
    assert lf.humans() == 1
    with open(p, "ab") as f:
        f.write(HUMAN_OUT.encode())
    assert lf.humans() == 0
    with open(p, "ab") as f:
        f.write(SVC.encode() + HUMAN_IN.encode() + HUMAN_IN.encode())
    assert lf.humans() == 2


# ============================================================ 会话编排 (半自动: 人登录, 推车只读)

PROMPT = "\n<SW-TEST>"
VERSION_OUT = ("display version\nHuawei Versatile Routing Platform Software\n"
               "VRP (R) software, Version 5.170\nHUAWEI S5735-L24T4S-A1 Routing Switch uptime is 1 day\n<SW-TEST>")


class FakeDev:
    """假 conbroker 日志 + 假 send. console 是整份日志 (人和设备都往里写); send 像真 conbroker 一样
    先写接入 / 发送标记, 再写设备输出, 最后写断开标记. 像 LogFile 一样给 size / read_from / tail / humans."""

    def __init__(self, console=PROMPT, brief=BRIEF_S, window=4096):
        self.console, self.brief, self.window = console, brief, window
        self.polls, self.unreadable, self.before_send = [], False, None

    def tail(self):
        return None if self.unreadable else self.console[-self.window:]

    def size(self):
        return None if self.unreadable else len(self.console)

    def read_from(self, off):
        if self.unreadable or off is None or off > len(self.console):
            return None
        return self.console[off:]

    def humans(self):
        return None if self.unreadable else oc.humans_attached(self.console)

    def send(self, port, cmd, timeout):
        self.polls.append(cmd)
        if self.before_send:
            self.before_send(self, cmd)
        out = VERSION_OUT if cmd == "display version" else self.brief
        self.console += f"\n[2026-11-07 10:00:00 Claude 接入]\n[2026-11-07 10:00:00 Claude 发送: {cmd}]\n" + out
        self.console += "\n[2026-11-07 10:00:01 Claude 断开]\n"
        return out


def _obs(dev, **kw):
    kw.setdefault("pacer", oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=5))
    kw.setdefault("clock", iter(range(10000)).__next__)
    return oc.Observer("con-1", "huawei", dev.send, log=dev, **kw)


def _started(dev=None, **kw):
    dev = dev or FakeDev()
    ob = _obs(dev, **kw)
    name, why = ob.probe()
    assert why is None, why
    assert ob.start(name) is True
    return dev, ob


# ---- 开始观察: 看现场

def test_probe_accepts_user_view_prompt_and_sends_nothing():
    dev = FakeDev(console="\nInfo: something\n<SW-TEST>\n" + ASYNC + "\n")
    ob = _obs(dev)
    assert ob.probe() == ("SW-TEST", None)
    assert dev.polls == []


def test_probe_prompt_with_space_in_sysname():
    assert _obs(FakeDev(console="\n<Core SW 1>")).probe() == ("Core SW 1", None)


@pytest.mark.parametrize("console,word", [
    ("\n[SW-TEST]", "系统视图"), ("\n[~SW-TEST-GigabitEthernet0/0/1]", "系统视图"),
    (USER, "Username"), ("\nUsername:admin\nPassword:", "Password"), (IDLE, "按任意键"),
    ("\nSave? [Y/N]:", "Y/N"), ("\n<SW-TEST>dis", "半行"), ("\n  ---- More ----", "翻页"),
    ("", "回车"), ("\nRecovering configuration...\n", "回车"), ("\n" + ASYNC + "\n", "回车"),
])
def test_probe_refuses_unless_user_view_prompt(console, word):
    dev = FakeDev(console=console)
    ob = _obs(dev)
    name, why = ob.probe()
    assert name is None and word in why
    assert ob.start("SW-TEST") is False and dev.polls == []


def test_probe_refuses_when_log_unreadable_or_agent_busy():
    dev = FakeDev()
    dev.unreadable = True
    assert _obs(dev).probe()[1]
    dev = FakeDev()
    assert "租约" in _obs(dev, busy=lambda: "Agent 持有这一路的写租约").probe()[1]


def test_probe_refuses_non_readonly_profile():
    dev = FakeDev()
    ob = _obs(dev, profile={"poll": ["display interface brief", "reset counters interface"], "version": None})
    name, why = ob.probe()
    assert name is None and "reset counters interface" in why and dev.polls == []


def test_start_requires_same_name_as_confirmed():
    dev = FakeDev()
    ob = _obs(dev)
    name, _ = ob.probe()
    dev.console += "\n<OTHER-SW>"                    # 人确认的时候 console 换了
    assert ob.start(name) is False and "OTHER-SW" in ob.error and dev.polls == []


def test_start_itself_sends_nothing():
    dev, ob = _started()
    assert dev.polls == [] and ob.state == "polling" and ob.device == "SW-TEST"


# ---- 轮询

def test_first_round_reads_version_then_brief():
    dev, ob = _started()
    assert ob.tick(100) is True
    assert dev.polls == ["display version", "display interface brief"]
    assert ob.model == "S5735-L24T4S-A1" and ob.ports["GigabitEthernet0/0/6"]["in_uti"] == 12.5
    assert ob.tick(200) is True and dev.polls.count("display version") == 1


def test_only_readonly_commands_ever_sent():
    dev, ob = _started()
    for t in range(100, 1000, 50):
        ob.tick(t)
    ob.stop()
    assert dev.polls and all(survey_core.is_readonly(c, "huawei") for c in dev.polls)


def test_no_poll_while_human_attached_until_detached():
    dev = FakeDev(console=SVC + HUMAN_IN + PROMPT)        # 人自己 attach 着登录的
    ob = _obs(dev)
    assert ob.start(ob.probe()[0]) is True
    for t in (100, 200, 300):
        assert ob.tick(t) is False
    assert dev.polls == [] and "attach" in ob.error and "断开后自动开始" in ob.error
    dev.console += HUMAN_OUT
    ob.tick(400)
    assert ob.tick(500) is True and dev.polls


def test_poll_skipped_and_paused_when_agent_takes_lease():
    state = {"busy": None}
    dev, ob = _started(busy=lambda: state["busy"])
    ob.tick(100)
    n = len(dev.polls)
    state["busy"] = "Agent 持有这一路的写租约"
    assert ob.tick(200) is False and len(dev.polls) == n
    assert ob.state == "polling" and "租约" in ob.error and ob.pacer.paused(201)


@pytest.mark.parametrize("add", [
    "\nContinue? [Y/N]:", "\n<SW-TEST>dis", "\n[SW-TEST]", "\n<OTHER-SW>", "\nsome text",
    "\nPING 192.0.2.1: 56  data bytes, press CTRL_C to break", "\nPlease input the password(<6-16>):",
])
def test_poll_only_when_exactly_at_own_prompt(add):
    dev, ob = _started()
    ob.tick(100)
    n = len(dev.polls)
    dev.console += add
    assert ob.tick(200) is False and len(dev.polls) == n and ob.state == "polling"


def test_poll_resumes_after_human_finished():
    dev, ob = _started()
    ob.tick(100)
    dev.console += "display clock\n2026-11-07 10:00:00\n<SW-TEST>"      # 人敲完一条, 回到提示符
    assert ob.tick(200) is False
    assert ob.tick(300) is True


@pytest.mark.parametrize("add", [IDLE, USER])
def test_console_back_at_login_is_lost(add):
    dev, ob = _started()
    ob.tick(100)
    n = len(dev.polls)
    dev.console += add
    assert ob.tick(200) is False and len(dev.polls) == n and ob.state == "lost"


def test_skipped_relogin_marks_lost():
    dev, ob = _started()
    ob.tick(100)
    dev.console += RELOGIN                  # 有人 quit 掉再用别的账号登回 <SW-TEST>
    ob.tick(110)
    assert ob.state == "lost"


def test_session_taken_over_while_poll_queued_is_lost():
    dev, ob = _started()
    ob.tick(100)
    dev.before_send = lambda d, cmd: setattr(d, "console", d.console + TAKEOVER)
    assert ob.tick(200) is False and ob.state == "lost"


def test_takeover_inside_poll_output_is_lost():
    dev, ob = _started()
    ob.tick(100)
    dev.brief = "display interface brief\n" + TAKEOVER + BRIEF_S.split("\n", 1)[1]
    assert ob.tick(200) is False and ob.state == "lost"


def test_display_users_output_does_not_lose_session():
    dev, ob = _started()
    ob.tick(100)
    dev.console += DISPLAY_USERS
    ob.tick(110)
    assert ob.state == "polling"


def test_port_set_change_marks_lost_after_two_rounds():
    dev, ob = _started()
    assert ob.tick(100) is True
    dev.brief = BRIEF_AR.replace("<AR-TEST>", "<SW-TEST>")
    assert ob.tick(200) is False and ob.state == "polling" and "可疑" in ob.warning
    assert ob.tick(300) is False and ob.state == "lost"


def test_port_set_blip_one_round_recovers():
    dev, ob = _started()
    ob.tick(100)
    dev.brief = BRIEF_S.replace("GigabitEthernet0/0/5        ^down down         0%     0%          0          0\n", "")
    assert ob.tick(200) is False and ob.state == "polling"
    dev.brief = BRIEF_S
    assert ob.tick(300) is True and ob.state == "polling" and not ob.warning


def test_prompt_change_in_output_marks_lost():
    dev, ob = _started()
    ob.tick(100)
    dev.brief = BRIEF_S.replace("<SW-TEST>", "<OTHER-SW>")
    assert ob.tick(200) is False and ob.state == "lost"


def test_unreadable_log_after_start_is_lost_not_rebased():
    dev, ob = _started()
    ob.tick(100)
    off = ob._off
    dev.unreadable = True
    ob.tick(200)
    dev.unreadable = False
    assert ob._off == off and ob.state == "lost"


def test_log_truncated_below_baseline_is_lost():
    dev, ob = _started()
    ob.tick(100)
    dev.console = "<SW-TEST>"                # copytruncate: 文件变小
    ob.tick(200)
    assert ob.state == "lost"


def test_advance_to_none_after_baseline_is_lost():
    dev, ob = _started()
    assert ob._advance(None) is False and ob.state == "lost" and ob._off is not None


def test_keeps_last_ports_on_poll_error():
    dev, ob = _started()
    ob.tick(100)

    def boom(*a):
        raise RuntimeError("conbroker 退出码 1")
    ob.send = boom
    assert ob.tick(200) is False
    assert ob.ports and "退出码" in ob.error and ob.state == "polling"


def test_empty_parse_is_error_not_fresh():
    dev, ob = _started(FakeDev(brief="display interface brief\n<SW-TEST>"), wall=lambda: 1000)
    assert ob.tick(100) is False and ob.polled_at is None and ob.error


def test_cancel_stops_between_poll_commands():
    flag = {"stop": False}
    dev = FakeDev()
    ob = _obs(dev, profile={"poll": ["display interface brief", "display interface brief"]})
    assert ob.start(ob.probe()[0], cancelled=lambda: flag["stop"])
    dev.before_send = lambda d, cmd: flag.update(stop=True) if cmd != "display version" else None
    ob.tick(100)
    assert dev.polls == ["display version", "display interface brief"]


def test_unrecognised_phy_warning():
    text = BRIEF_S.replace("GigabitEthernet0/0/2        down  down", "GigabitEthernet0/0/2        #down down")
    rows = _by(oc.parse_brief(text))
    assert rows["GigabitEthernet0/0/2"]["state"] == "unknown"
    assert oc.lamp(rows["GigabitEthernet0/0/2"], 0) == ("unknown", True)
    dev, ob = _started(FakeDev(brief=text))
    assert ob.tick(100) is True and "GigabitEthernet0/0/2" in ob.warning


# ---- 退出: 只停轮询, 任何路径都不发东西

@pytest.mark.parametrize("name", ["polling", "lost", "takeover", "port_change", "failed_start", "attached"])
def test_stop_never_sends_anything(name):
    dev = FakeDev(console=SVC + HUMAN_IN + PROMPT) if name == "attached" else FakeDev()
    ob = _obs(dev)
    if name == "failed_start":
        dev.console += "\n[SW-TEST]"
    ob.start(ob.probe()[0] or "SW-TEST")
    ob.tick(100)
    if name == "lost":
        dev.console += IDLE
    elif name == "takeover":
        dev.console += TAKEOVER
    elif name == "port_change":
        dev.brief = BRIEF_AR.replace("<AR-TEST>", "<SW-TEST>")
    ob.tick(200)
    polls, console = list(dev.polls), dev.console
    ob.stop()
    assert dev.polls == polls and dev.console == console
    assert "quit" not in dev.polls and ob.state in ("stopped", "failed", "lost")


# ---- 副屏快照

def _polled(wall):
    dev, ob = _started(wall=lambda: wall)
    ob.tick(100)
    return ob


def test_front_view_from_snapshot():
    snap = _polled(1000).snapshot(head="con-1", now=1000)
    assert snap["polled_at"] == 1000
    v = oc.front_view(snap, now=1001)
    assert v["title"].startswith("con-1") and "SW-TEST" in v["title"] and not v["stale"]
    states = {p["label"]: p["state"] for p in v["ports"] if p["shape"] == "rj45"}
    assert states["3"] == "admin" and states["2"] == "down"
    assert oc.front_view(None, now=1) is None and oc.front_view({"ts": "x"}, now=1) is None


def test_front_view_gone_when_writer_died():
    snap = _polled(1000).snapshot(head="con-1", now=1000)
    assert oc.front_view(snap, now=1000 + oc.WRITE_STALE_S + 1) is None


def test_front_view_grey_when_poll_old_or_error():
    ob = _polled(1000)
    v = oc.front_view(ob.snapshot(head="con-1", now=1000 + oc.FRONT_STALE_S + 5), now=1000 + oc.FRONT_STALE_S + 6)
    assert v["stale"] and v["note"] and not any(p["lit"] for p in v["ports"])
    ob.error = "没回提示符"
    v = oc.front_view(ob.snapshot(head="con-1", now=1001), now=1001)
    assert v["stale"] and "没回提示符" in v["note"]


@pytest.mark.parametrize("state,error,note", [
    ("idle", None, "准备开始"), ("failed", "console 还没登录", "还没登录"), ("lost", "会话掉了", "会话掉了"),
])
def test_front_view_shows_non_polling_state_without_ports(state, error, note):
    doc = {"ts": 100, "polled_at": None, "head": "con-1", "device": None, "state": state, "error": error, "ports": []}
    v = oc.front_view(doc, now=101)
    assert v is not None and note in v["note"] and v["stale"] and v["ports"] == []


def test_record_event():
    got = []
    oc.record("start", head="con-1", device="SW-TEST", ok=True, event=lambda k, m: got.append((k, m)))
    assert got[0][0] == "observe" and "start" in got[0][1]


# ---- 结构约束: 观察模块里没有登录 / 凭据 / secret 相关代码

def test_no_credential_or_secret_code_in_observe_modules():
    import ast
    import re
    bad = re.compile(r"(?i)secret|credential|passw(or)?d(?!_?prompt)|^pw$|_pw$|^pw_|login(?!_?prompt)|socket|exchange")
    for mod in ("observe_core.py", "observe_panel.py"):
        src = open(os.path.join(os.path.dirname(oc.__file__), mod), encoding="utf-8").read()
        tree = ast.parse(src)
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                names.update(a.name for a in n.names)
        hits = sorted(x for x in names if bad.search(x) and x != "PASS_PROMPT_RE")
        assert not hits, (mod, hits)
        assert 'show="•"' not in src and "HELLO claude" not in src, mod



# ============================================================ 半自动版复审

# ---- 中1: 每条命令前都查 attach; 上一条命令期间有人 attach 过 (哪怕又断开) 也不发

def test_attach_during_previous_command_blocks_next():
    dev, ob = _started()
    orig = dev.send

    def send(port, cmd, timeout):
        out = orig(port, cmd, timeout)
        if cmd == "display version":
            dev.console += HUMAN_IN
        return out
    ob.send = send
    assert ob.tick(100) is False
    assert dev.polls == ["display version"] and "attach" in ob.error


def test_attach_flash_during_previous_command_blocks_next():
    dev, ob = _started()
    orig = dev.send

    def send(port, cmd, timeout):
        out = orig(port, cmd, timeout)
        if cmd == "display version":
            dev.console += HUMAN_IN + HUMAN_OUT
        return out
    ob.send = send
    assert ob.tick(100) is False and dev.polls == ["display version"]
    ob.send = orig
    ob.tick(200)                             # 这一轮让出 (刚才有人来过)
    assert ob.tick(300) is True              # 之后没人再来, 接着轮询


# ---- 低3: 退出时清掉暂时性的错误

def test_stop_clears_transient_error_keeps_lost():
    dev = FakeDev(console=SVC + HUMAN_IN + PROMPT)
    ob = _obs(dev)
    ob.start(ob.probe()[0])
    ob.tick(100)
    assert "attach" in ob.error
    ob.stop()
    assert ob.state == "stopped" and ob.error is None
    v = oc.front_view(ob.snapshot("con-1", now=0), 0)
    assert v["note"] == "已退出"
    dev, ob = _started()
    dev.console += IDLE
    ob.tick(100)
    msg = ob.error
    ob.stop()
    assert ob.state == "lost" and ob.error == msg


def test_stop_takes_no_arguments():
    import inspect
    assert list(inspect.signature(oc.Observer.stop).parameters) == ["self"]


# ---- 存疑 b: 设备路径解析到 con-N 槽位再查租约

def test_slot_of_resolves_alias_byid_and_ttyusb():
    real = {"/dev/con-2": "/dev/ttyUSB1", "/dev/serial/by-id/usb-X-if00": "/dev/ttyUSB1", "/dev/ttyUSB1": "/dev/ttyUSB1",
            "/dev/con-1": "/dev/ttyUSB0", "/dev/con-3": "/dev/con-3", "/dev/con-4": "/dev/con-4"}
    rp = lambda p: real.get(p, p)          # noqa: E731
    assert oc.slot_of("/dev/con-2", realpath=rp) == 2
    assert oc.slot_of("/dev/ttyUSB1", realpath=rp) == 2
    assert oc.slot_of("/dev/serial/by-id/usb-X-if00", realpath=rp) == 2
    assert oc.slot_of("/dev/ttyUSB9", realpath=rp) is None


def test_agent_busy_unresolvable_slot_refuses():
    states = {1: {"writer": None, "estop": False, "sending": False, "pending": 0}}
    why = oc.agent_busy("/dev/ttyUSB9", states=lambda: states, realpath=lambda p: p)
    assert why and "看不清" in why


def test_agent_busy_by_ttyusb_path_finds_lease():
    states = {n: {"writer": None, "estop": False, "sending": False, "pending": 0} for n in (1, 2, 3, 4)}
    states[2]["writer"] = "agent"
    real = {"/dev/con-2": "/dev/ttyUSB1"}
    assert "租约" in oc.agent_busy("/dev/ttyUSB1", states=lambda: states, realpath=lambda p: real.get(p, p))


# ---- 中2: "观察中"登记在模块级, 新建的面板也看得到

def test_observing_registry():
    oc._OBSERVING.clear()
    oc.observing_add("/dev/con-1")
    oc.observing_add("/dev/con-1")
    assert oc.is_observing("/dev/con-1") and not oc.is_observing("/dev/con-2")
    oc.observing_discard("/dev/con-1")
    assert not oc.is_observing("/dev/con-1")


# ---- 低5: 残留清理

def test_no_pager_off_left():
    assert "pager_off" not in oc.DEFAULT_PROFILES["huawei"]
    src = open(oc.__file__, encoding="utf-8").read()
    assert "screen-length" not in src and "pager_off" not in src


# ============================================================ 第十轮: 观察有总时长上限

def test_observation_stops_at_max_duration():
    dev, ob = _started(profile={"max_s": 300})
    assert ob.tick(100) is True
    assert ob.tick(350) is True or ob.state == "polling"
    n = len(dev.polls)
    assert ob.tick(401) is False and ob.state == "stopped" and "时长上限" in ob.reason
    assert ob.tick(500) is False and len(dev.polls) == n
    ob.stop()
    assert ob.state == "stopped" and "时长上限" in ob.reason


def test_default_max_duration_is_30_minutes():
    assert oc.DEFAULT_PROFILES["huawei"]["max_s"] == 1800


def test_profile_can_override_max_duration(tmp_path):
    p = tmp_path / "prof.json"
    p.write_text(json.dumps({"huawei": {"max_s": 600}}))
    assert oc.load_profile("huawei", str(p))["max_s"] == 600
    p.write_text(json.dumps({"huawei": {"max_s": "lots"}}))
    assert oc.load_profile("huawei", str(p))["max_s"] == 1800



# ============================================================ 第十一轮

@pytest.mark.parametrize("raw", ["Infinity", "1e308", "-5", "0", "true", "14401", "NaN"])
def test_max_duration_must_be_finite_and_at_most_4_hours(tmp_path, raw):
    p = tmp_path / "prof.json"
    p.write_text('{"huawei": {"max_s": %s}}' % raw)
    assert oc.load_profile("huawei", str(p))["max_s"] == 1800


def test_max_duration_4_hours_ok(tmp_path):
    p = tmp_path / "prof.json"
    p.write_text('{"huawei": {"max_s": 14400}}')
    assert oc.load_profile("huawei", str(p))["max_s"] == 14400


def test_short_max_duration_shown_in_seconds():
    dev, ob = _started(profile={"max_s": 45})
    ob.tick(100)
    ob.tick(146)
    assert ob.state == "stopped" and "45 秒" in ob.reason


def test_reason_in_snapshot_and_front_view():
    dev, ob = _started(profile={"max_s": 300}, wall=lambda: 1000)
    ob.tick(100)
    ob.tick(500)
    ob.stop()
    snap = ob.snapshot("con-1", now=1000)
    assert "时长上限" in snap["reason"]
    assert "时长上限" in oc.front_view(snap, now=1001)["note"]


def test_idle_lock_keeps_polling_until_max_duration():
    # 闲置锁屏不停观察 (窗口这边不叫停), 轮询照常, 到时长上限才停
    dev, ob = _started(profile={"max_s": 300})
    assert ob.tick(100) is True
    assert ob.tick(200) is True
    assert ob.tick(401) is False and ob.state == "stopped"



# ============================================================ 合入 main 之后: 影子统计 / 只认华为

def test_poll_commands_recorded_in_shadow_stats():
    seen = []
    dev, ob = _started(shadow=lambda cmd: seen.append(cmd))
    ob.tick(100)
    assert seen == ["display version", "display interface brief"] == dev.polls


def test_shadow_failure_does_not_block_polling():
    def boom(cmd):
        raise RuntimeError("影子统计挂了")
    dev, ob = _started(shadow=boom)
    assert ob.tick(100) is True and dev.polls == ["display version", "display interface brief"]


def test_shadow_record_logs_misses_only(monkeypatch):
    got = []
    ev = lambda kind, msg: got.append((kind, msg))          # noqa: E731
    oc.shadow_record("display interface brief", "huawei", (), event=ev)
    hits = list(got)
    oc.shadow_record("display something-not-in-allowlist-xyz", "huawei", (), event=ev)
    assert all(k == "observe" and "allowlist-miss" in m for k, m in got)
    assert len(got) == len(hits) + 1


@pytest.mark.parametrize("ver,word", [
    ("display version\nH3C Comware Platform Software\nComware Software, Version 7.1.064\n<SW-TEST>", "华为"),
    ("display version\nRuijie General Operating System Software\nRGOS 11.4\n<SW-TEST>", "华为"),
    ("display version\nsomething we cannot recognise\n<SW-TEST>", "华为"),
])
def test_only_huawei_confirmed_by_version_output(ver, word):
    dev, ob = _started()
    orig = dev.send

    def send(port, cmd, timeout):
        if cmd == "display version":
            dev.polls.append(cmd)
            dev.console += "\n" + ver
            return ver
        return orig(port, cmd, timeout)
    ob.send = send
    assert ob.tick(100) is False
    assert dev.polls == ["display version"] and ob.state == "failed" and word in ob.error and not ob.ports


def test_profile_without_version_command_refused():
    dev = FakeDev()
    ob = _obs(dev, profile={"version": None})
    name, why = ob.probe()
    assert name is None and "display version" in why and dev.polls == []



# ============================================================ 第十二轮

def test_snapshot_has_end_time_for_locked_countdown():
    dev, ob = _started(profile={"max_s": 600}, wall=lambda: 5000)
    ob.tick(100)
    assert ob.snapshot("con-1", now=5000)["ends_at"] == 5600


@pytest.mark.parametrize("over", [
    {"poll": ["screen-length 0 temporary", "display interface brief"]},
    {"poll": ["display interface brief"], "version": "screen-length 0 temporary"},
    {"poll": ["show interfaces status"]},
])
def test_profile_json_cannot_sneak_session_or_non_display_commands(tmp_path, monkeypatch, over):
    p = tmp_path / "prof.json"
    p.write_text(json.dumps({"huawei": over}))
    monkeypatch.setattr(oc, "PROFILES_PATH", str(p))
    dev = FakeDev()
    ob = oc.Observer("con-1", "huawei", dev.send, profile=oc.load_profile("huawei", str(p)), log=dev,
                     pacer=oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=5))
    name, why = ob.probe()
    assert name is None and why and dev.polls == []


def test_observe_cmd_ok():
    assert oc.observe_cmd_ok("display interface brief", "huawei")
    assert not oc.observe_cmd_ok("screen-length 0 temporary", "huawei")
    assert not oc.observe_cmd_ok("show version", "cisco")
    assert not oc.observe_cmd_ok("display current-configuration | save x", "huawei")
