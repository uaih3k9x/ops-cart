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
    assert not oc.allowed("quit", "huawei")                     # 不自动 quit (9x 定): 闸门里也不放
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


def test_wake_landing_on_password_means_password_only_console():
    # 唤醒 (单个回车) 后直接是 Password: = console 只认密码; 不回空密码 (白白耗一次认证失败), 停下说明
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed("\nPassword:")
    assert st.kind == "fail" and "纯密码" in st.note


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
    """假设备 + 假 conbroker 日志: console 是整份日志 (人和设备都往里写), exchange/send 的输出也追加进去.

    像真 LogFile 一样给 size() / read_from(偏移) / tail() (最后 window 个字符的滑动窗口, 第一行会被截半).
    script: {角色: 输出 或 fn(dev)->输出 (可抛异常)} 覆盖默认应答; later(n, text): 第 n 次读日志时有人插进一段;
    on_sleep(dev): 等人停手那几秒 (>=1 秒) 里发生的事; on_nap(dev): 每次 sleep 都调 (等日志落盘那种短的也算).
    """

    def __init__(self, brief=BRIEF_S, console=IDLE, mark_delay=0, window=4096):
        self.brief, self.console, self.window = brief, console, window
        self.sent, self.polls, self.script, self.naps = [], [], {}, []
        self.on_sleep = self.on_nap = None
        self._later, self.mark_delay, self.unreadable = [], mark_delay, False

    def later(self, n, text):
        self._later.append([n, text])

    def _tick(self):
        for item in self._later:
            item[0] -= 1
            if item[0] == 0:
                self.console += item[1]

    def tail(self):
        self._tick()
        return None if self.unreadable else self.console[-self.window:]

    def size(self):
        self._tick()
        return None if self.unreadable else len(self.console)

    def read_from(self, off):
        self._tick()
        if self.unreadable or off is None or off > len(self.console):
            return None
        return self.console[off:]

    def humans(self):
        return None if self.unreadable else oc.humans_attached(self.console)

    def sleep(self, s):
        self.naps.append(s)
        if self.on_nap:
            self.on_nap(self)
        if self.on_sleep and s >= 1:            # 等人停手的那几秒; 短的是等 conbroker 标记落盘
            self.on_sleep(self)

    def _out(self, text):
        self.console += text
        return text

    def exchange(self, step):
        """像真 conbroker: 先写接入 / 发送标记, 再是设备输出, 断开标记在服务端收到 EOF 后才写 (可延迟)."""
        self.sent.append((step.role, "***" if step.secret else step.text))
        self.console += "\n[2026-11-07 10:00:00 Claude 接入]\n[2026-11-07 10:00:00 Claude 发送: %s]\n" % (
            "******" if step.secret else step.text)
        try:
            if step.role in self.script:
                r = self.script[step.role]
                return self._out(r(self) if callable(r) else r)
            out = {"wake": USER, "user": PASS, "pass": LOGGED, "answer": "N\n<SW-TEST>"}.get(step.role)
            if out is None:
                out = "quit\n" + IDLE if step.text == "quit" else SCREEN_OK
            return self._out(out)
        finally:
            gone = "\n[2026-11-07 10:00:01 Claude 断开]\n"
            if self.mark_delay:
                self.later(self.mark_delay, gone)
            else:
                self.console += gone

    def send(self, port, cmd, timeout):
        self.polls.append(cmd)
        if cmd == "display version":
            return self._out("display version\nHUAWEI S5735-L24T4S-A1 Routing Switch uptime is 1 day\n<SW-TEST>")
        return self._out(self.brief)


def _obs(dev, **kw):
    kw.setdefault("pacer", oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=5))
    kw.setdefault("clock", iter(range(10000)).__next__)
    return oc.Observer("con-1", "huawei", dev.exchange, dev.send, log=dev, sleep=dev.sleep, **kw)


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
    assert "quit" not in [t for _r, t in dev.sent] and "推车登录" in ob.note and "空闲超时" in ob.note
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
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    n = len(dev.polls)
    dev.console += "\n<SW-TEST>"         # 日志在我们没发的时候长了 (有人敲了个回车)
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

# ---- 中3: 登录前看现场

def test_borrowed_session_not_woken_and_not_quit():
    dev = FakeDev(console="\nInfo: something\n<SW-TEST>")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert _roles(dev) == ["cmd"] and dev.sent[0][1] == "screen-length 0 temporary"
    assert ob.borrowed and ob.device == "SW-TEST"
    ob.tick(100)
    ob.stop(now=200)
    assert "quit" not in [t for _r, t in dev.sent] and "借用" in ob.note


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
    dev.unreadable = True
    ob = _obs(dev)
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

    def busy():                         # 每步发送前都会问一次; 唤醒那步刚结束、日志尾已经静止之后, 有人敲了键
        if dev.sent and dev.sent[-1][0] == "wake" and not dev.console.endswith("adm"):
            dev.console += "adm"
        return None
    ob = _obs(dev, busy=busy)
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

def test_pass_step_half_output_no_prompt_seen_never_quits():
    # 第三轮: 登录后没亲眼见到提示符 (没记下设备名) 一律不 quit, 哪怕之后现场冒出一个提示符
    dev = FakeDev()
    dev.script["pass"] = "\n"                       # 超时只拿到半截
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    dev.console += "Info: The max number of VTY users is 5.\n<SW-TEST>"
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent] and "密码可能已发出" in ob.note


def test_pass_step_half_output_no_prompt_no_quit():
    dev = FakeDev()
    dev.script["pass"] = "\n"
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent]


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
    dev.console += "\n<SW-TEST>dis"
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


# ============================================================ 第二轮复审

ASYNC = ("Nov  7 2026 10:00:00 SW-TEST %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP on the interface "
         "GigabitEthernet0/0/1 has entered the UP state.")
ASYNC8 = "2026-11-07 10:00:00.123 +08:00 SW-TEST %%01IFNET/2/linkDown_active(l):CID=0x1; The interface status changes."


# ---- 高1: 唤醒只发回车; 唤醒后看到提示符算借用; 日志尾往回找有效行

def test_wake_is_bare_return():
    st = oc.Login("admin", "s3cret").start()
    assert st.role == "wake" and st.text == ""


def test_wake_landing_on_prompt_is_borrowed():
    lg = oc.Login("admin", "s3cret")
    lg.start()
    st = lg.feed("\n<SW-TEST>")
    assert st.role == "cmd" and lg.borrowed


@pytest.mark.parametrize("line", [ASYNC, ASYNC8, "#" + ASYNC])
def test_effective_line_skips_async_log_and_marks(line):
    text = "\n<SW-TEST>\n" + line + "\n[2026-11-07 10:00:00 Claude 断开]\n"
    assert oc.last_line(text) == "<SW-TEST>"
    assert oc.classify(text) == "prompt"


def test_async_log_after_prompt_borrows_without_wake_and_no_quit():
    dev = FakeDev(console="<SW-TEST>\n" + ASYNC + "\n")
    dev.script["wake"] = "Error: Unrecognized command\n<SW-TEST>"
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert "wake" not in _roles(dev) and ob.borrowed
    ob.tick(100)
    ob.stop(now=200)
    assert "借用" in ob.note and "quit" not in [t for _r, t in dev.sent]


def test_async_log_after_sysview_refuses():
    dev = FakeDev(console="\n[SW-TEST]\n" + ASYNC + "\n")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False and dev.sent == []


@pytest.mark.parametrize("console", [
    "", "\n[2026-11-07 10:00:00 服务启动 /dev/con-1 9600]\n", "\n" + ASYNC + "\n", "\nRecovering configuration...\n",
])
def test_unknown_tail_refuses_start(console):
    dev = FakeDev(console=console)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False and dev.sent == []
    assert "回车" in ob.error


# ---- 中2: conbroker 的断开标记晚到, 不算有人插话

@pytest.mark.parametrize("delay", [1, 2, 3])
def test_late_disconnect_mark_is_not_interjection(delay):
    dev = FakeDev(mark_delay=delay)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True, ob.error


def test_waits_for_tail_to_settle_after_each_step():
    dev = FakeDev(mark_delay=2)
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    assert any(s < 1 for s in dev.naps)


def test_device_text_ignores_conbroker_marks():
    a = "x\n<SW>\n[2026-11-07 10:00:00 Claude 断开]\n[2026-11-07 10:00:00 人 接入]\n"
    assert oc.device_text(a) == oc.device_text("x\n<SW>\n")
    assert oc.device_text(a) != oc.device_text("x\n<SW>d\n")


# ---- 中3: 轮询前查 Agent 租约和日志尾

def test_poll_skipped_and_paused_when_agent_takes_lease():
    dev = FakeDev()
    state = {"busy": None}
    ob = _obs(dev, busy=lambda: state["busy"])
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    state["busy"] = "Agent 持有这一路的写租约"
    assert ob.tick(100) is False and len(dev.polls) == n
    assert ob.state == "polling" and "租约" in ob.error and ob.pacer.paused(101)


def test_poll_sees_password_prompt_as_session_gone():
    # 第四轮: 轮询中途 console 冒出 Password: = 有人登出又在登录, 会话不是我们的了
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.console += "\nPassword:"
    assert ob.tick(100) is False and len(dev.polls) == n and ob.state == "lost"


@pytest.mark.parametrize("add", ["\nContinue? [Y/N]:", "\n<SW-TEST>dis"])
def test_poll_skipped_when_console_mid_operation(add):
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.console += add
    assert ob.tick(100) is False and len(dev.polls) == n and ob.state == "polling"


# ---- 低5 / 低6

def test_stop_after_lost_is_silent():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    dev.brief = "display interface brief\n\nUsername:"
    ob.tick(100)
    msg = ob.error
    ob.stop(now=200)
    assert ob.note is None and ob.error == msg


def test_errors_after_password_carry_no_device_text():
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS])
    st = lg.feed("\nadmin-typed-SECRETX-here")
    assert "SECRETX" not in st.note
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS, LOGGED])
    st = lg.feed("screen-length 0 temporary\nSECRETX")
    assert st.kind == "fail" and "SECRETX" not in st.note


def test_poll_error_carries_no_device_text():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    dev.brief = "display interface brief\nSECRETX"
    ob.tick(100)
    assert ob.error and "SECRETX" not in ob.error


# ---- 低7: 每一路一个快照文件; 非 polling 状态也写

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


@pytest.mark.parametrize("state,error,note", [
    ("idle", None, "登录中"), ("failed", "认证失败 (不重试, 免得锁账号)", "认证失败"), ("lost", "会话掉了", "会话掉了"),
])
def test_front_view_shows_non_polling_state_without_ports(state, error, note):
    doc = {"ts": 100, "polled_at": None, "head": "con-1", "device": None, "state": state, "error": error, "ports": []}
    v = oc.front_view(doc, now=101)
    assert v is not None and note in v["note"] and v["stale"] and v["ports"] == []


def test_snapshot_written_before_login_done():
    dev = FakeDev()
    ob = _obs(dev)
    snap = ob.snapshot(head="con-1", now=5)
    assert snap["state"] == "idle" and oc.front_view(snap, now=5)["note"] == "登录中"


# ---- 低8: 退出不被一轮轮询拖住

def test_cancel_stops_between_poll_commands():
    dev = FakeDev()
    flag = {"stop": False}
    ob = _obs(dev, profile={"poll": ["display interface brief", "display interface brief"], "version": None})
    ob.start("admin", "s3cret", cancelled=lambda: flag["stop"])
    real = dev.send

    def send(port, cmd, timeout):
        flag["stop"] = True
        return real(port, cmd, timeout)
    ob.send = send
    ob.tick(100)
    assert dev.polls == ["display interface brief"]


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


# ---- 低9 / 低10

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


# ============================================================ 第三轮复审: 发送 / quit 都改白名单

# ---- 高1: pw_sent 在密码真正发出后才置; 没记下设备名不 quit

def test_password_interrupted_then_human_logs_in_no_quit():
    dev = FakeDev()

    def human(d):                       # 发密码前的静止期里, 人自己敲密码登录, 停在 <SW-TEST>
        if d.sent and d.sent[-1][0] == "user" and not d.console.endswith("<SW-TEST>"):
            d.console += "\n<SW-TEST>"
    dev.on_sleep = human
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "pass" not in _roles(dev) and ob.login.pw_sent is False and ob.device is None
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent]


def test_pw_sent_means_maybe_sent_once_exchange_starts():
    # 第四轮: pw_sent = "密码可能已发出" (过了所有检查、交给 exchange 那一刻起); 发送出错也算, 但没见到提示符照样不 quit
    dev = FakeDev()

    def boom(d):
        raise OSError("连接断了")
    dev.script["pass"] = boom
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert ob.login.pw_sent is True
    dev.console += "\n<SW-TEST>"
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent] and "密码可能已发出" in ob.note


def test_login_records_prompt_name_after_password():
    lg = oc.Login("admin", "s3cret")
    _drive(lg, [USER, PASS, LOGGED])
    assert lg.device == "SW-TEST" and lg.authed


# ---- 中2: 按字节偏移看新增; 滑动窗口截断不算插话

FILLER = "".join(f"old line {i}\n" for i in range(600))          # 远大于 4096 字节


def test_sliding_window_with_marks_and_async_logs_still_logs_in():
    dev = FakeDev(console=FILLER + IDLE, window=1000, mark_delay=2)
    for n in (3, 9, 15, 21):
        dev.later(n, "\n" + ASYNC + "\n")
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True, ob.error
    assert _roles(dev) == ["wake", "user", "pass", "cmd"]


def test_new_content_detected_by_offset_not_window():
    dev = FakeDev(console=FILLER + IDLE, window=300)
    ob = _obs(dev)

    def busy():                         # 唤醒刚结束、记下偏移之后, 有人敲了一行
        if dev.sent and dev.sent[-1][0] == "wake" and "adm\n" not in dev.console:
            dev.console += "adm\n"
    ob.busy = busy
    assert ob.start("admin", "s3cret") is False and _roles(dev) == ["wake"]


def test_abort_after_username_tells_human_console_waits_at_password():
    dev = FakeDev()
    # 有人在 console 上重新敲了一遍用户名, 设备又停在 Password: -> 提示里写明停在 Password:
    dev.on_sleep = (lambda d: setattr(d, "console", d.console + "\nUsername:other\nPassword:")
                    if d.sent and d.sent[-1][0] == "user" and not d.console.endswith("other\nPassword:") else None)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "pass" not in _roles(dev)
    assert "用户名是我们发的" in ob.error and "Password:" in ob.error


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


# ---- 中3: 轮询前现场白名单

@pytest.mark.parametrize("add", [
    "\nPING 192.0.2.1: 56  data bytes, press CTRL_C to break", "\nContinue?(y/n)[n]:",
    "\nPlease input the password(<6-16>):", "\n<OTHER-SW>", "\nsome text",
])
def test_poll_only_when_exactly_at_own_prompt(add):
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.console += add
    assert ob.tick(100) is False and len(dev.polls) == n and ob.state == "polling"


def test_poll_skipped_when_log_unreadable():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    n = len(dev.polls)
    dev.unreadable = True
    assert ob.tick(100) is False and len(dev.polls) == n


def test_poll_resumes_after_human_finished():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    dev.console += "display clock\n2026-11-07 10:00:00\n<SW-TEST>"      # 人敲完一条, 回到提示符
    assert ob.tick(200) is False                                         # 这轮让出来
    assert ob.tick(300) is True                                          # 之后没人再动, 接着轮询


# ---- 低4 ~ 低8

def test_agent_busy_counts_pending():
    states = {1: {"writer": None, "estop": False, "sending": False, "pending": 2}}
    assert oc.agent_busy("con-1", states=lambda: states)


def test_version_not_sent_unless_scene_ok():
    dev = FakeDev()

    def nap(d):
        if d.sent and d.sent[-1] == ("cmd", "screen-length 0 temporary") and not d.console.endswith("dis"):
            d.console += "\n<SW-TEST>dis"
    dev.on_nap = nap
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True
    assert "display version" not in dev.polls and ob.model is None


def test_empty_parse_is_error_not_fresh():
    dev = FakeDev(brief="display interface brief\n<SW-TEST>")
    ob = _obs(dev, wall=lambda: 1000)
    ob.start("admin", "s3cret")
    assert ob.tick(100) is False
    assert ob.polled_at is None and ob.error


def test_claim_shared_by_survey_and_observe():
    oc._CLAIMS.clear()
    assert oc.claim("/dev/con-1", "survey")
    assert not oc.claim("/dev/con-1", "observe-w1")
    oc.release("/dev/con-1", "survey")
    assert oc.claim("/dev/con-1", "observe-w1")
    assert oc.holder("/dev/con-1") == "observe-w1"
    oc._CLAIMS.clear()


# ---- 存疑: 认不出的 PHY 不静默丢

def test_unrecognised_phy_kept_as_unknown_with_warning():
    text = BRIEF_S.replace("GigabitEthernet0/0/2        down  down", "GigabitEthernet0/0/2        #down down")
    rows = _by(oc.parse_brief(text))
    assert rows["GigabitEthernet0/0/2"]["state"] == "unknown"
    assert oc.lamp(rows["GigabitEthernet0/0/2"], 0) == ("unknown", True)
    dev = FakeDev(brief=text)
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    assert ob.tick(100) is True
    assert "GigabitEthernet0/0/2" in ob.warning



# ============================================================ 第四轮复审

RELOGIN = "quit\n" + IDLE + USER + "ops2\nPassword:\n" + LOGGED
SVC = "\n[2026-11-07 09:00:00 服务启动 /dev/con-1 9600]\n"
HUMAN_IN = "\n[2026-11-07 09:00:01 人 接入]\n"
HUMAN_OUT = "\n[2026-11-07 09:00:02 人 断开]\n"


# ---- 中1: 跳过的日志先扫一遍再丢

def test_skipped_content_with_relogin_marks_lost_and_no_quit():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    dev.console += RELOGIN                  # 有人 quit 掉我们, 用自己的账号重新登录, 回到 <SW-TEST>
    ob.tick(110)                            # 人在用 -> 暂停; 跳过的部分要扫
    assert ob.state == "lost"
    ob.stop(now=500)
    assert "quit" not in [t for _r, t in dev.sent]


def test_interjected_relogin_seen_by_scene_check_marks_lost():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    ob.tick(100)
    dev.console += RELOGIN
    ob.tick(102)                            # 还在自己那轮的 settle 窗口里, 由轮询前的现场检查发现
    assert ob.state == "lost"


@pytest.mark.parametrize("text,changed", [
    ("display clock\n2026-11-07 10:00:00\n<SW-TEST>", False),
    (IDLE, True), (USER, True), ("\nPassword:", True), ("<SW-TEST>quit\n", True), ("quit\n", True),
    ("\nLogin authentication\n", True), ("\nInfo: The max number of VTY users is 5.\n", True),
    ("\nThe current login time is 2026-11-07 10:00:00.\n", True),
])
def test_session_changed(text, changed):
    assert oc.session_changed(text) is changed


# ---- 中2a: 登录全程有人 attach 着就不开始 / 中止

def test_refuses_start_when_human_attached():
    dev = FakeDev(console=SVC + HUMAN_IN + IDLE)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False and dev.sent == []
    assert "attach" in ob.error


def test_starts_when_human_detached():
    dev = FakeDev(console=SVC + HUMAN_IN + HUMAN_OUT + IDLE)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is True, ob.error


def test_aborts_when_human_attaches_mid_login():
    dev = FakeDev()

    def busy():
        if dev.sent and dev.sent[-1][0] == "wake" and HUMAN_IN not in dev.console:
            dev.console += HUMAN_IN
    ob = _obs(dev, busy=busy)
    assert ob.start("admin", "s3cret") is False and _roles(dev) == ["wake"]
    assert "attach" in ob.error


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


# ---- 低4 / 低5

def test_cancel_during_password_quiet_window():
    dev = FakeDev()
    flag = {"c": False}

    def nap(d):
        if d.sent and d.sent[-1][0] == "user":
            flag["c"] = True
    dev.on_sleep = nap
    ob = _obs(dev)
    assert ob.start("admin", "s3cret", cancelled=lambda: flag["c"]) is False
    assert "pass" not in _roles(dev)


def test_auth_failure_reason_kept_by_stop():
    dev = FakeDev()
    dev.script["pass"] = "\nError: Local authentication is rejected.\n\nUsername:"
    ob = _obs(dev)
    assert ob.start("admin", "wrong") is False
    ob.stop(now=100)
    assert "认证失败" in ob.error and "quit" not in [t for _r, t in dev.sent]


def test_username_straight_to_prompt_not_called_password_wait():
    dev = FakeDev()
    dev.script["user"] = "admin\n<SW-TEST>"
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "可能停在 Password" not in ob.error


# ---- 低6 / 低7

@pytest.mark.parametrize("line", [
    "Nov  7 2026 10:00:00 SW-TEST IFNET/4/LINK_STATE:The line protocol on the interface has entered the UP state.",
    "#Nov 7 2026 10:00:00+08:00 SW-TEST SHELL/5/CMDRECORD: Recorded command information.",
])
def test_trap_lines_without_percent_are_async(line):
    assert oc.classify("\n<SW-TEST>\n" + line + "\n") == "prompt"


def test_please_press_enter_is_idle():
    assert oc.classify("\n\nPlease Press ENTER.\n") == "idle"


# ---- 存疑: sysname 一样的两台设备换了线

def test_port_set_change_marks_lost():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    assert ob.tick(100) is True
    dev.brief = BRIEF_AR.replace("<AR-TEST>", "<SW-TEST>")
    assert ob.tick(200) is False and ob.state == "lost"
    ob.stop(now=300)
    assert "quit" not in [t for _r, t in dev.sent]


# ============================================================ 第五轮复审

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


TAKEOVER = ("\n[2026-11-07 10:01:00 人 接入]\n<SW-TEST>quit\n  Configuration console exit, please press any key to log on\n"
            "\nUsername:root\nPassword:\n<SW-TEST>\n[2026-11-07 10:01:20 人 断开]\n")


def test_session_taken_over_while_poll_queued_is_lost():
    dev = FakeDev()
    ob = _obs(dev)
    assert ob.start("admin", "s3cret")
    assert ob.tick(100) is True
    orig = dev.send

    def send(port, cmd, timeout):
        dev.console += TAKEOVER
        return orig(port, cmd, timeout)
    dev.send = send
    ob.send = send
    assert ob.tick(200) is False and ob.state == "lost"
    ob.stop(now=400)
    assert "quit" not in [t for _r, t in dev.sent]


def test_takeover_inside_poll_output_is_lost():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    dev.brief = "display interface brief\n" + TAKEOVER + BRIEF_S.split("\n", 1)[1]
    assert ob.tick(100) is False and ob.state == "lost"


def test_session_taken_over_while_reading_version_is_lost():
    dev = FakeDev()
    orig = dev.send

    def send(port, cmd, timeout):
        if cmd == "display version":
            dev.console += TAKEOVER
        return orig(port, cmd, timeout)
    dev.send = send
    ob = oc.Observer("con-1", "huawei", dev.exchange, send, log=dev, sleep=dev.sleep,
                     pacer=oc.Pacer(duty=0.5, min_gap=1, max_gap=10, quiet=5), clock=iter(range(10000)).__next__)
    ob.start("admin", "s3cret")
    assert ob.state == "lost"
    ob.stop(now=100)
    assert "quit" not in [t for _r, t in dev.sent]


@pytest.mark.parametrize("script", [{"wake": "\nPlease choose (1/2):"}, {"user": "admin\n<SW-TEST>"}])
def test_password_cleared_whatever_way_login_fails(script):
    dev = FakeDev()
    dev.script.update(script)
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "s3cret" not in repr(vars(ob.login))


def test_run_login_clears_password_on_exchange_error():
    lg = oc.Login("admin", "s3cret")

    def exchange(step):
        raise OSError("x")
    oc.run_login(lg, exchange, "huawei")
    assert "s3cret" not in repr(vars(lg))


def test_humans_reads_back_until_service_start(tmp_path):
    p = tmp_path / "con-1.log"
    with open(p, "wb") as f:
        f.write(SVC.encode() + HUMAN_IN.encode())
        f.write(b"filler line\n" * 300000)                 # 约 3.6MB
        f.write(b"<SW-TEST>\n")
    lf = oc.LogFile(str(p))
    assert lf.humans(limit=64 << 20, chunk=1 << 20) == 1
    assert lf.humans(limit=1 << 20, chunk=1 << 18) is None             # 上限内没读到服务启动


# ============================================================ 第六轮复审 (不涉及自动 quit 的部分)

# ---- 中2: 登录阶段跳过的日志里出现过"人 接入"就中止 (哪怕检查那一刻已经断开)

def test_attach_flash_during_password_quiet_aborts():
    dev = FakeDev()

    def flash(d):
        if "user" in _roles(d) and not getattr(d, "_done", False):
            d._done = True
            d.console += HUMAN_IN + HUMAN_OUT
    dev.on_sleep = flash
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "pass" not in _roles(dev) and "attach" in ob.error


def test_attach_flash_inside_checkpoint_aborts():
    dev = FakeDev()

    def nap(d):                             # 用户名那步刚发完、等日志静止的时候, 有人接入又断开
        if _roles(d)[-1:] == ["user"] and not getattr(d, "_done", False):
            d._done = True
            d.console += HUMAN_IN + HUMAN_OUT
    dev.on_nap = nap
    ob = _obs(dev)
    assert ob.start("admin", "s3cret") is False
    assert "pass" not in _roles(dev) and "attach" in ob.error


def test_attach_marks_from_claude_do_not_abort():
    dev = FakeDev()                          # FakeDev 每步都写 "Claude 接入 / 断开"
    assert _obs(dev).start("admin", "s3cret") is True


# ---- 中1 (部分): 读不到日志大小时不挪基线; 建立基线之后再遇到 None 一律 lost

def test_checkpoint_unreadable_size_aborts_without_moving_baseline():
    dev = FakeDev()
    orig = dev.size
    st = {"bad": False}

    def size():
        return None if st["bad"] else orig()
    dev.size = size

    def nap(d):
        if _roles(d)[-1:] == ["user"]:
            st["bad"] = True
    dev.on_nap = nap
    ob = _obs(dev)
    off_before = []
    real = ob._advance

    def spy(to, *a, **k):
        off_before.append(to)
        return real(to, *a, **k)
    ob._advance = spy
    assert ob.start("admin", "s3cret") is False
    assert None not in off_before[1:] and "pass" not in _roles(dev)


def test_advance_to_none_after_baseline_is_lost():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    assert ob._advance(None) is False and ob.state == "lost"
    assert ob._off is not None


def test_poll_with_unreadable_size_is_lost_not_rebased():
    dev = FakeDev()
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    off = ob._off
    orig = dev.size
    dev.size = lambda: None
    ob.tick(100)
    dev.size = orig
    assert ob._off == off and ob.state == "lost"


# ---- 低5: humans() 只认 conbroker 自己的"服务启动"标记行

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


# ---- 存疑: 用户名 / 密码不许带控制字符

@pytest.mark.parametrize("s,bad", [("admin", False), ("p@ss word!", False), ("ad\rmin", True), ("a\nb", True),
                                   ("x\x00", True), ("x\x1b", True), ("x\x7f", True), ("", False)])
def test_bad_credential(s, bad):
    assert oc.bad_credential(s) is bad



# ============================================================ 不自动 quit (9x 定)

def test_quit_refused_by_gate_and_exchange(tmp_path):
    assert "quit" not in oc.SESSION_COMMANDS and not oc.allowed("quit", "huawei")
    with pytest.raises(ValueError):
        oc.broker_exchange(str(tmp_path), "con-1", oc.Step("send", "cmd", "quit", expect=None, timeout=1))


def test_no_quit_literal_in_observe_modules():
    import ast
    for mod in ("observe_core.py", "observe_panel.py"):
        tree = ast.parse(open(os.path.join(os.path.dirname(oc.__file__), mod), encoding="utf-8").read())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "quit"], mod


def _scenario(name):
    dev = FakeDev()
    if name == "borrowed":
        dev.console = "\n<SW-TEST>"
    elif name == "half_output_after_password":
        dev.script["pass"] = "\n"
    elif name == "exchange_error_after_password":
        def boom(d):
            raise OSError("x")
        dev.script["cmd"] = boom
    elif name == "auth_failed":
        dev.script["pass"] = "\nError: Local authentication is rejected.\n\nUsername:"
    ob = _obs(dev)
    ob.start("admin", "s3cret")
    if ob.state == "polling":
        ob.tick(100)
    if name == "lost":
        dev.brief = "display interface brief\n\nUsername:"
        ob.tick(200)
    if name == "takeover":
        dev.console += TAKEOVER
        ob.tick(300)
    return dev, ob


@pytest.mark.parametrize("name", ["normal", "borrowed", "half_output_after_password", "exchange_error_after_password",
                                  "auth_failed", "lost", "takeover"])
def test_stop_never_sends_anything(name):
    dev, ob = _scenario(name)
    sent, console = list(dev.sent), dev.console
    ob.stop(now=1000)
    assert dev.sent == sent and dev.console == console                 # stop 不发任何字节
    assert "quit" not in [t for _r, t in dev.sent]
    assert ob.state in ("stopped", "failed", "lost")


@pytest.mark.parametrize("name,want", [
    ("normal", "推车登录"), ("borrowed", "借用"), ("half_output_after_password", "密码可能已发出"),
    ("exchange_error_after_password", "推车登录"), ("lost", None), ("takeover", None),
])
def test_exit_note_says_what_console_is_left_in(name, want):
    _dev, ob = _scenario(name)
    ob.stop(now=1000)
    if want is None:
        assert ob.note is None
    else:
        assert want in ob.note and "空闲超时" in ob.note and "quit" in ob.note
