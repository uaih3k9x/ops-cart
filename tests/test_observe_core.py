"""observe_core 测试 (#85 串口观察模式): 登录状态机 / brief 解析 / 前面板排布 / 灯 / 轮询节奏 / 会话编排.

全部用假 send / 假 exchange, 不碰串口和 conbroker. 样例是手写的, 不含真实地址、MAC、主机名.
"""

import json
import os
import socket
import threading

import pytest

import observe_core as oc


# ------------------------------------------------------------ 会话命令闸门

def test_session_commands_allowed():
    assert oc.allowed("screen-length 0 temporary", "huawei")
    assert oc.allowed("quit", "huawei")
    assert oc.allowed("display interface brief", "huawei")      # 白名单里的照放


@pytest.mark.parametrize("cmd", [
    "screen-length 0", "screen-length 0 temporary ", " screen-length 0 temporary",
    "screen-length 0 temporary; save", "screen-length 0 temporary | save", "Screen-length 0 temporary",
    "quit ", "QUIT", "quit\r", "quit\nsave", "save", "reboot", "system-view", "return", "undo shutdown",
    "screen-length 24 temporary", "user-interface console 0", "", None,
])
def test_other_commands_rejected(cmd):
    assert not oc.allowed(cmd, "huawei")


def test_session_commands_need_known_vendor():
    assert not oc.allowed("screen-length 0 temporary", "nobody")


# ------------------------------------------------------------ 登录状态机

PRESS_ANY = "\n\n  Press any key to get started\n"
USER = "\nLogin authentication\n\n\nUsername:"
PASS = "\nUsername:admin\nPassword:"
LOGGED = "\nInfo: The max number of VTY users is 5.\n\n<SW-TEST>"


def _drive(login, outputs):
    """依次喂设备输出, 返回状态机发出的每一步 (含开头那一步)."""
    steps = [login.start()]
    for out in outputs:
        steps.append(login.feed(out))
    return steps


def test_login_order_wake_then_username_then_password():
    lg = oc.Login("admin", "s3cret")
    steps = _drive(lg, [USER, PASS, LOGGED, "screen-length 0 temporary\nInfo: The configuration takes effect on the current user terminal interface only.\n<SW-TEST>"])
    wake, user, pw, screen, done = steps
    # 1. 唤醒: 打码 + 强制, 等 Username: (第一下按键只是唤醒)
    assert (wake.kind, wake.role, wake.secret) == ("send", "wake", True)
    assert "s3cret" not in wake.text and "admin" not in wake.text
    assert oc.re.search(wake.expect, "Username:")
    # 2. 看到 Username: 才发用户名, 等 Password:
    assert (user.role, user.text, user.secret) == ("user", "admin", False)
    assert oc.re.search(user.expect, "Password:")
    # 3. 密码只走 secret
    assert (pw.role, pw.text, pw.secret) == ("pass", "s3cret", True)
    assert oc.re.search(pw.expect, "<SW-TEST>")
    # 4. 关翻页, 只对本次会话
    assert (screen.role, screen.text) == ("cmd", "screen-length 0 temporary")
    assert done.kind == "done" and lg.device == "SW-TEST"


def test_username_never_sent_before_username_prompt():
    # 唤醒后设备还停在 Press any key: 再唤醒, 不发用户名
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed(PRESS_ANY)
    assert st.role == "wake"
    st = lg.feed(USER)
    assert st.role == "user"


def test_password_never_sent_unless_password_prompt():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed(USER)
    st = lg.feed("\nUsername:admin\n")              # 没等到 Password:
    assert st.kind == "fail" and "s3cret" not in (st.text or "") + st.note


def test_wake_landing_on_password_bounces_with_empty_secret():
    # 设备本来就停在 Username:, 唤醒键 x 被当成用户名 -> 出 Password:. 回一个空密码弹回去, 绝不发真密码
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed("\nUsername:x\nPassword:")
    assert (st.role, st.text, st.secret) == ("bounce", "", True)
    st = lg.feed("\nError: Local authentication is rejected.\n\nUsername:")
    assert st.role == "user"


def test_bounce_only_once():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed("\nPassword:")
    st = lg.feed("\nPassword:")
    assert st.kind == "fail"


def test_already_logged_in_skips_credentials():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed("\n<SW-TEST>")
    assert (st.role, st.text) == ("cmd", "screen-length 0 temporary")


def test_change_password_prompt_answered_no():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed(USER)
    lg.feed(PASS)
    st = lg.feed("\nWarning: The password will expire in 3 days.\nThe password needs to be changed. Change now? [Y/N]:")
    assert (st.role, st.text, st.secret) == ("answer", "N", False)
    st = lg.feed("N\n<SW-TEST>")
    assert st.role == "cmd"


def test_auth_failure_stops_without_retry():
    lg = oc.Login("admin", "wrong")
    lg.start()
    lg.feed(USER)
    lg.feed(PASS)
    st = lg.feed("\nError: Local authentication is rejected.\n\nUsername:")
    assert st.kind == "fail"
    assert "wrong" not in st.note


def test_unknown_prompt_stops_for_human():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed(USER)
    lg.feed(PASS)
    st = lg.feed("\nPlease choose a language (1/2):")
    assert st.kind == "fail" and "人" in st.note


def test_system_view_prompt_not_accepted():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed("\n[SW-TEST]")
    assert st.kind == "fail"


def test_password_dropped_after_sending():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed(USER)
    lg.feed(PASS)
    assert "s3cret" not in repr(vars(lg))


def test_wake_gives_up_after_retries():
    lg = oc.Login("admin", "s3cret")
    st = lg.start()
    n = 0
    while st.kind == "send":
        st = lg.feed("")
        n += 1
        assert n < 10
    assert st.kind == "fail"


def test_login_step_repr_masks_password():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    lg.feed(USER)
    st = lg.feed(PASS)
    assert "s3cret" not in repr(st) and "s3cret" not in str(st)


def test_pager_off_failure_stops():
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS, LOGGED])
    st = lg.feed("screen-length 0 temporary\n         ^\nError: Unrecognized command found at '^' position.\nUsername:")
    assert st.kind == "fail"


def test_run_login_drives_exchange_and_gates_cmd_steps():
    sent = []
    replies = iter([USER, PASS, LOGGED, "\n<SW-TEST>"])

    def exchange(step):
        sent.append((step.role, step.secret))
        return next(replies)
    res = oc.run_login(oc.Login("admin", "s3cret"), exchange, "huawei")
    assert res.kind == "done"
    assert sent == [("wake", True), ("user", False), ("pass", True), ("cmd", False)]


def test_run_login_refuses_cmd_not_in_gate():
    lg = oc.Login("admin", "s3cret", pager_off="save")
    sent = []

    def exchange(step):
        sent.append(step.role)
        return {"wake": "\n<SW-TEST>"}.get(step.role, "")
    res = oc.run_login(lg, exchange, "huawei")
    assert res.kind == "fail" and "cmd" not in sent


def test_run_login_exchange_error_is_fail():
    def exchange(step):
        raise OSError("连不上")
    res = oc.run_login(oc.Login("admin", "s3cret"), exchange, "huawei")
    assert res.kind == "fail" and "连不上" in res.note


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


# ------------------------------------------------------------ 会话编排 (Observer)

IDLE = "\n\n  Press any key to get started\n"
SCREEN_OK = ("screen-length 0 temporary\nInfo: The configuration takes effect on the current user terminal "
             "interface only.\n<SW-TEST>")


class FakeDev:
    """假设备 + 假 conbroker 日志: console 是日志尾 (人和设备都往里写), exchange/send 的输出也追加进去.

    script: {角色: 输出 或 fn(dev)->输出 (可抛异常)} 覆盖默认应答; later(n, text): 第 n 次读日志尾时有人插进一段;
    on_sleep(dev): 等人停手那几秒里发生的事 (人敲键 / 设备吐日志).
    """

    def __init__(self, brief=BRIEF_S, console=IDLE):
        self.brief, self.console = brief, console
        self.sent, self.polls, self.script = [], [], {}
        self.on_sleep, self._later = None, []

    def later(self, n, text):
        self._later.append([n, text])

    def tail(self):
        for item in self._later:
            item[0] -= 1
            if item[0] == 0:
                self.console += item[1]
        return self.console

    def sleep(self, _s):
        if self.on_sleep:
            self.on_sleep(self)

    def _out(self, text):
        self.console += text
        return text

    def exchange(self, step):
        self.sent.append((step.role, "***" if step.secret else step.text))
        if step.role in self.script:
            r = self.script[step.role]
            return self._out(r(self) if callable(r) else r)
        out = {"wake": USER, "bounce": USER, "user": PASS, "pass": LOGGED, "answer": "N\n<SW-TEST>"}.get(step.role)
        if out is None:
            out = "quit\n" + IDLE if step.text == "quit" else SCREEN_OK
        return self._out(out)

    def send(self, port, cmd, timeout):
        self.polls.append(cmd)
        if cmd == "display version":
            return self._out("display version\nHUAWEI S5735-L24T4S-A1 Routing Switch uptime is 1 day\n<SW-TEST>")
        return self._out(self.brief)


def _obs(dev, **kw):
    kw.setdefault("pacer", oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=5))
    kw.setdefault("clock", iter(range(10000)).__next__)
    return oc.Observer("con-1", "huawei", dev.exchange, dev.send, tail=dev.tail, sleep=dev.sleep, **kw)


def _roles(dev):
    return [r for r, _t in dev.sent]


def test_observer_login_poll_and_quit():
    dev = FakeDev()
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert ob.device == "SW-TEST" and ob.model == "S5735-L24T4S-A1"
    assert ob.tick(100) is True
    assert ob.ports["GigabitEthernet0/0/6"]["in_uti"] == 12.5
    assert any(x["name"] == "XGigabitEthernet0/0/1" for x in ob.panel)
    ob.stop(now=200)
    assert dev.sent[-1] == ("cmd", "quit") and ob.left == "quit"
    assert all(c in ("display version", "display interface brief") for c in dev.polls)
    assert ("pass", "***") in dev.sent and not any("s3cret" in str(x) for x in dev.sent)


def test_observer_refuses_whole_round_if_any_poll_cmd_not_readonly():
    dev = FakeDev()
    ob = _obs(dev, profile={"poll": ["display interface brief", "reset counters interface"], "version": None})
    assert ob.start("admin", "s3cret") is False
    assert dev.sent == [] and dev.polls == []
    assert "reset counters interface" in ob.error


def test_observer_pauses_while_human_active():
    dev = FakeDev()
    sizes = {"v": 0}
    ob = _obs(dev, log_size=lambda: sizes["v"])
    ob.start("admin", "s3cret")
    ob.tick(100)
    n = len(dev.polls)
    sizes["v"] = 999                    # 日志在我们没发的时候长了
    ob.tick(110)
    assert ob.tick(112) is False and len(dev.polls) == n
    assert ob.tick(200) is True


def test_observer_marks_lost_session():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    dev.brief = "display interface brief\n\nUsername:"
    assert ob.tick(100) is False
    assert ob.state == "lost"
    assert ob.tick(200) is False                       # 掉了就不再轮询
    ob.stop(now=300)
    assert dev.sent[-1][1] != "quit"                   # 掉线后没有会话可退, 不往 Username: 上发 quit


def test_observer_keeps_last_ports_on_poll_error():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)

    def boom(*a):
        raise RuntimeError("conbroker 退出码 1")
    ob.send = boom
    assert ob.tick(200) is False
    assert ob.ports and "退出码" in ob.error and ob.state == "polling"


def test_observer_login_fail_does_not_poll():
    dev = FakeDev()
    dev.script["wake"] = "\nPlease choose (1/2):"
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert dev.polls == [] and ob.state == "failed"


def test_observer_snapshot_has_no_secrets():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("opsview", "s3cret")
    ob.tick(100)
    snap = ob.snapshot(head="con-1", now=123)
    text = json.dumps(snap, ensure_ascii=False)
    assert "s3cret" not in text and "opsview" not in text
    assert snap["ts"] == 123 and snap["device"] == "SW-TEST" and snap["ports"]


def test_record_event_has_no_secrets():
    got = []
    oc.record("start", head="con-1", device="SW-TEST", ok=True, event=lambda k, m: got.append((k, m)))
    assert got[0][0] == "observe" and "start" in got[0][1]


def test_step_has_no_force_field():
    assert not hasattr(oc.Step("send", "wake", "x"), "force")


# ---- 中1: 退出时看现场

def test_stop_skips_quit_while_human_active():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    ob.pacer.poke(150)
    ob.stop(now=151)
    assert "quit" not in [t for _r, t in dev.sent]
    assert ob.left == "skipped" and "有人在用" in ob.error


@pytest.mark.parametrize("tail_add", ["dis", "\n[SW-TEST]", "\n<OTHER-SW>", "\nUsername:", "\n  ---- More ----"])
def test_stop_skips_quit_unless_at_own_user_view(tail_add):
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    dev.console += tail_add
    ob.stop(now=200)
    assert "quit" not in [t for _r, t in dev.sent]
    assert ob.left == "skipped" and "未退出登录" in ob.error


def test_stop_skips_quit_when_someone_types_during_quiet_check():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    dev.on_sleep = lambda d: setattr(d, "console", d.console + "d")
    ob.stop(now=200)
    assert "quit" not in [t for _r, t in dev.sent] and ob.left == "skipped"


# ---- 中3: 登录前看现场

def test_borrowed_session_not_woken_and_not_quit():
    dev = FakeDev(console="\nInfo: something\n<SW-TEST>")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert _roles(dev) == ["cmd"] and dev.sent[0][1] == "screen-length 0 temporary"
    assert ob.borrowed and ob.device == "SW-TEST"
    ob.tick(100)
    ob.stop(now=200)
    assert "quit" not in [t for _r, t in dev.sent] and ob.left == "borrowed"


def test_console_already_at_username_skips_wake():
    dev = FakeDev(console=USER)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert _roles(dev)[:2] == ["user", "pass"]


@pytest.mark.parametrize("console", [
    "\nUsername:admin\nPassword:", "\nSave? [Y/N]:", "\n<SW-TEST>dis", "\n[SW-TEST]", "\n[~SW-TEST-GigabitEthernet0/0/1]",
    "\n  ---- More ----",
])
def test_refuses_start_when_console_mid_operation(console):
    dev = FakeDev(console=console)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert dev.sent == [] and ob.error


def test_refuses_start_when_human_typing():
    dev = FakeDev()
    dev.on_sleep = lambda d: setattr(d, "console", d.console + "x")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert dev.sent == [] and "有人在用" in ob.error


def test_refuses_start_when_agent_holds_lease():
    dev = FakeDev()
    ob = _obs(dev, busy=lambda: "Agent 持有写租约")
    assert ob.start("admin", "s3cret") is False
    assert dev.sent == [] and "Agent" in ob.error


def test_refuses_start_when_log_unreadable():
    dev = FakeDev()
    ob = _obs(dev)
    ob.tail = lambda: None
    assert ob.start("admin", "s3cret") is False and dev.sent == []


def test_agent_busy_from_write_state():
    states = {1: {"writer": "agent", "estop": False, "sending": False},
              2: {"writer": None, "estop": True, "sending": False},
              3: {"writer": None, "estop": False, "sending": False}}
    assert "租约" in oc.agent_busy("con-1", states=lambda: states)
    assert "急停" in oc.agent_busy("con-2", states=lambda: states)
    assert oc.agent_busy("con-3", states=lambda: states) is None
    assert oc.agent_busy("ttyUSB0", states=lambda: states) is None


def test_agent_busy_unreadable_state_refuses():
    def boom():
        raise ValueError("坏了")
    assert oc.agent_busy("con-1", states=boom)


# ---- 中2: 每步发送前确认没人插话

def test_aborts_before_username_if_someone_typed_after_wake():
    dev = FakeDev()
    dev.script["wake"] = lambda d: (d.later(2, "adm"), USER)[1]
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert _roles(dev) == ["wake"] and "有人" in ob.error


def test_password_not_sent_if_someone_types_before_it():
    dev = FakeDev()

    def typing(d):
        if d.sent and d.sent[-1][0] == "user":
            d.console += "abc"
    dev.on_sleep = typing
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "pass" not in _roles(dev)
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent]


def test_password_waits_for_quiet_window():
    dev = FakeDev()
    naps = []
    dev.on_sleep = lambda d: naps.append(d.sent[-1][0] if d.sent else None)
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    assert naps == [None, "user"]          # 开始前一次, 发密码前一次


def test_cancel_between_steps():
    dev = FakeDev()
    ob = _obs(dev)
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1
    assert ob.start("admin", "s3cret", cancelled=cancelled) is False
    assert _roles(dev) == ["wake"]


# ---- 中4: 登录成功后的失败路径要退出登录

def test_pass_step_half_output_then_quit_when_prompt_confirmed():
    dev = FakeDev()
    dev.script["pass"] = "\n"                       # 超时只拿到半截
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    dev.console += "Info: The max number of VTY users is 5.\n<SW-TEST>"
    ob.stop(now=100)
    assert dev.sent[-1] == ("cmd", "quit") and ob.left == "quit"


def test_pass_step_half_output_no_prompt_no_quit():
    dev = FakeDev()
    dev.script["pass"] = "\n"
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent]


def test_exchange_raises_after_password_then_quit():
    dev = FakeDev()

    def cmd(d):
        if d.sent[-1][1] == "quit":
            return "quit\n" + IDLE
        raise OSError("超时")
    dev.script["cmd"] = cmd
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    ob.stop(now=100)
    assert dev.sent[-1] == ("cmd", "quit")


def test_pager_off_judged_after_echo_not_by_async_log():
    dev = FakeDev()
    dev.script["cmd"] = ("screen-length 0 temporary\nInfo: The configuration takes effect on the current user "
                         "terminal interface only.\nNov  7 2026 10:00:00 SW-TEST %%01IFNET/4/LINK_STATE(l): "
                         "Error-down recovered.\n<SW-TEST>")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True and ob.pager_ok


def test_pager_off_error_still_logged_in_and_polls():
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS, LOGGED])
    st = lg.feed("screen-length 0 temporary\n                ^\nError: Unrecognized command found at '^' position.\n<SW-TEST>")
    assert st.kind == "done" and lg.pager_ok is False and lg.authed


def test_forced_password_change_reported_precisely():
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS, "\nThe password needs to be changed. Change now? [Y/N]:"])
    st = lg.feed("N\nEnter Password:")
    assert st.kind == "fail" and "改密" in st.note and "认证失败" not in st.note


# ---- 低7: 轮询前看现场; conbroker 的标记行不算设备输出

def test_classify_ignores_conbroker_markers():
    text = IDLE + "\n[2026-11-07 10:00:00 Claude 发送: display interface brief]\ndisplay interface brief\n[conbroker: 60s 内没等到提示符]"
    assert oc.classify(text) == "other"
    assert oc.classify(IDLE + "\n[conbroker: 60s 内没等到提示符]") == "idle"
    assert oc.classify("\n<SW-TEST>\n[2026-11-07 10:00:00 Claude 断开]\n") == "prompt"


def test_poll_not_sent_when_console_back_at_login():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.console += "\nConfiguration console exit, please press any key to log on\n"
    assert ob.tick(100) is False
    assert len(dev.polls) == n and ob.state == "lost"


def test_poll_timeout_marker_with_idle_is_lost():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    dev.brief = "display interface brief\n" + IDLE + "\n[conbroker: 60s 内没等到提示符]"
    assert ob.tick(100) is False and ob.state == "lost"


def test_poll_skipped_while_someone_mid_line():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.console += "dis"
    assert ob.tick(100) is False and len(dev.polls) == n and ob.state == "polling"


def test_prompt_with_space_in_sysname():
    assert oc.classify("\n<Core SW 1>") == "prompt"
    assert oc.prompt_name("<Core SW 1>") == "Core SW 1"


# ------------------------------------------------------------ 副屏视图 (低5)

def _polled(wall):
    dev = FakeDev()
    ob = _obs(dev, wall=lambda: wall)
    ob.start("admin", "s3cret")
    ob.tick(100)
    return ob


def test_front_view_from_snapshot():
    ob = _polled(1000)
    snap = ob.snapshot(head="con-1", now=1000)
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
    snap = ob.snapshot(head="con-1", now=1000 + oc.FRONT_STALE_S + 5)     # 还在写, 但很久没轮询成功
    v = oc.front_view(snap, now=1000 + oc.FRONT_STALE_S + 6)
    assert v["stale"] and v["note"] and not any(p["lit"] for p in v["ports"])
    ob.error = "没回提示符"
    v = oc.front_view(ob.snapshot(head="con-1", now=1001), now=1001)
    assert v["stale"] and "没回提示符" in v["note"]


def test_front_view_never_polled_is_grey():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.panel = oc.auto_layout(["GigabitEthernet0/0/1"])
    v = oc.front_view(ob.snapshot(head="con-1", now=5), now=5)
    assert v["stale"]


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


# ------------------------------------------------------------ 真 exchange: 直接接 conbroker 本地口

class _FakeBroker:
    def __init__(self, reply):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.reply, self.got = reply, b""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        c, _ = self.srv.accept()
        c.settimeout(2)
        try:
            while b"\r" not in self.got.partition(b"\n")[2]:
                d = c.recv(100)
                if not d:
                    break
                self.got += d
            c.sendall(self.reply)
            c.recv(100)
        except OSError:
            pass
        finally:
            c.close()


def test_broker_exchange_secret_over_socket(tmp_path):
    fb = _FakeBroker(b"\r\n\x1b[1D<SW-TEST>")
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "con-1.json").write_text(json.dumps({"port": fb.port}))
    step = oc.Step("send", "pass", "s3cret", secret=True, expect=oc.PROMPT_RE, timeout=3)
    out = oc.broker_exchange(str(tmp_path), "con-1", step)
    assert fb.got.startswith(b"HELLO claude secret\n")
    assert fb.got.endswith(b"s3cret\r")
    assert out.strip().endswith("<SW-TEST>")


def test_broker_exchange_no_service(tmp_path):
    step = oc.Step("send", "wake", "x", secret=True, expect=oc.PROMPT_RE, timeout=1)
    with pytest.raises(OSError):
        oc.broker_exchange(str(tmp_path), "con-9", step)


def test_broker_exchange_refuses_ungated_cmd(tmp_path):
    step = oc.Step("send", "cmd", "save", expect=oc.PROMPT_RE, timeout=1)
    with pytest.raises(ValueError):
        oc.broker_exchange(str(tmp_path), "con-1", step, vendor="huawei")


def test_module_never_writes_password_to_disk(tmp_path, monkeypatch):
    # 整个流程跑一遍, 目录里不出现密码
    monkeypatch.chdir(tmp_path)
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    ob.stop(now=200)
    for root, _d, files in os.walk(tmp_path):
        for f in files:
            assert b"s3cret" not in open(os.path.join(root, f), "rb").read()
