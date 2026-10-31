"""survey_core 测试: 只读白名单 (fail-closed) + 巡检执行/存档/比对. conbroker 用假 send, 不碰串口."""

import json
import os
import time

import pytest
import survey_core as sv


# ------------------------------------------------------------ 白名单: 放行

@pytest.mark.parametrize("cmd", [
    "display version", "display clock", "display ip interface brief", "display interface brief",
    "display lldp neighbor brief", "display mac-address", "display arp", "display current-configuration",
    "display interface GigabitEthernet0/0/1",
    "display current-configuration | include sysname",
    "display current-configuration | exclude undo",
    "display current-configuration | begin interface",
    "display current-configuration | count",
    "display current-configuration | no-more",
    "display current-configuration | include vlan | count",
])
def test_huawei_readonly_allowed(cmd):
    assert sv.is_readonly(cmd, "huawei")


@pytest.mark.parametrize("cmd", [
    "show version", "show clock", "show ip interface brief", "show running-config",
    "show running-config | include hostname", "show mac address-table", "show cdp neighbors",
])
def test_cisco_readonly_allowed(cmd):
    assert sv.is_readonly(cmd, "cisco")


def test_default_profiles_all_pass():
    for vendor, prof in sv.load_profiles(path="/nonexistent").items():
        for c in prof["commands"]:
            assert sv.is_readonly(c["cmd"], vendor), (vendor, c)


# ------------------------------------------------------------ 白名单: 拦下 (变形大量覆盖)

_BAD_HUAWEI = [
    # 根本不是只读
    "save", "reboot", "reset saved-configuration", "system-view", "undo info-center enable",
    "quit", "return", "format flash:", "delete flash:/vrpcfg.zip", "ping 192.0.2.1", "",
    # 大小写变形: 设备照认, 但不是规范写法, 一律不放
    "DISPLAY version", "Display version", "dIsPlAy version", "display VERSION | SAVE",
    # 前后空白 / 空白变形
    " display version", "display version ", "  display version", "display  version",
    "display\tversion", "\tdisplay version", "display version ",
    # 缩写 (见 is_readonly 的说明)
    "dis version", "disp version", "dis cur", "d version", "displ version",
    # 只有动词
    "display", "display ",
    # 多命令拼接 / 换行 / 控制字符
    "display version; save", "display version;save", "display version ;reboot",
    "display version\nsave", "display version\rsave", "display version\r", "display version\n",
    "display version\r\nsave", "display version\x00", "display\x08\x08\x08\x08\x08\x08\x08save",
    "display version\x03", "display version\x1b[A", "display version\x7f",
    "display version && save", "display version & save", "display version || save",
    "display version `save`", "display version $(save)",
    # 管道到写操作 / 重定向
    "display current-configuration | save", "display current-configuration |save",
    "display current-configuration | append flash:/x.txt", "display current-configuration | redirect flash:/x",
    "display current-configuration | tee flash:/x", "display current-configuration > flash:/x.txt",
    "display current-configuration >> flash:/x.txt", "display current-configuration < x",
    "display current-configuration | include vlan | save", "display current-configuration |",
    "display current-configuration | ", "display current-configuration || include x",
    "display current-configuration | inc vlan",   # 过滤词也不许缩写
    "display current-configuration | INCLUDE vlan",
    # display 里会写文件的
    "display diagnostic-information", "display diagnostic-information flash:/diag.txt",
    # 非 ASCII / 看起来像空格或字母的字符
    "display　version", "ｄisplay version", "display version​", "dіsplay version",
    "display version save",
    # 注释符号 / 其他厂商动词
    "display version # x", "display version ! x", "show version",
]


@pytest.mark.parametrize("cmd", _BAD_HUAWEI)
def test_huawei_not_readonly(cmd):
    assert not sv.is_readonly(cmd, "huawei")


@pytest.mark.parametrize("cmd", [
    "sh version", "SHOW version", " show version", "show version ", "show running-config | append flash:x",
    "show running-config | redirect tftp://192.0.2.1/x", "show running-config | tee flash:x",
    "show running-config > flash:x", "show version; reload", "show version\nreload", "copy run start",
    "write memory", "reload", "show", "display version", "show running-config | i hostname",
])
def test_cisco_not_readonly(cmd):
    assert not sv.is_readonly(cmd, "cisco")


@pytest.mark.parametrize("cmd", [None, 123, b"display version", ["display", "version"]])
def test_non_string_rejected(cmd):
    assert not sv.is_readonly(cmd, "huawei")


@pytest.mark.parametrize("vendor", ["", "unknown", "HUAWEI", None, "ruijie"])
def test_unknown_vendor_rejected(vendor):
    assert not sv.is_readonly("display version", vendor)
    assert not sv.is_readonly("show version", vendor)


def test_extra_verbs_only_add_verbs_keep_other_rules():
    """#21 闸门第 1 层: 规则文件可以给内置厂商加只读动词 (ping), 其余规则 (禁字符、管道、全称) 照旧."""
    assert not sv.is_readonly("ping 192.0.2.1", "huawei")
    assert sv.is_readonly("ping 192.0.2.1", "huawei", extra_verbs=["ping"])
    assert sv.is_readonly("display version", "huawei", extra_verbs=["ping"])
    assert not sv.is_readonly("ping 192.0.2.1 ; save", "huawei", extra_verbs=["ping"])
    assert not sv.is_readonly("ping", "huawei", extra_verbs=["ping"])
    assert not sv.is_readonly("ping 192.0.2.1 | save x", "huawei", extra_verbs=["ping"])
    assert not sv.is_readonly("ls -l", "linux", extra_verbs=["ls"])        # 没有内置规则的厂商照样不放


def test_check_commands_reports_all_bad():
    bad = sv.check_commands(["display version", "save", "dis cur"], "huawei")
    assert bad == ["save", "dis cur"]
    assert sv.check_commands(["display version"], "huawei") == []


# ------------------------------------------------------------ 清单: 默认 + JSON 覆盖

def test_default_huawei_profile_is_the_eight():
    cmds = [c["cmd"] for c in sv.load_profiles(path="/nonexistent")["huawei"]["commands"]]
    assert cmds == ["display version", "display clock", "display ip interface brief",
                    "display interface brief", "display lldp neighbor brief", "display mac-address",
                    "display arp", "display current-configuration"]


def test_json_overrides_commands_but_not_rules(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"huawei": {"commands": ["display version", {"cmd": "display arp", "timeout": 5}],
                                        "verbs": ["save"], "pipes": ["save"]},
                             "ruijie": {"commands": ["show version"]}}))
    prof = sv.load_profiles(path=str(p))
    assert prof["huawei"]["commands"] == [{"cmd": "display version", "timeout": sv.DEFAULT_TIMEOUT},
                                          {"cmd": "display arp", "timeout": 5}]
    # 规则不能经 JSON 改: 动词/管道词写在代码里
    assert not sv.is_readonly("save", "huawei")
    assert not sv.is_readonly("display arp | save", "huawei")
    # JSON 新加的厂商没有内置规则 -> 一条都不放
    assert "ruijie" in prof and not sv.is_readonly("show version", "ruijie")


def test_bad_json_falls_back_to_default(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{not json")
    assert sv.load_profiles(path=str(p)) == sv.load_profiles(path="/nonexistent")


def test_example_profiles_file_loads_and_passes():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "survey_profiles.json")
    assert os.path.exists(path)
    prof = sv.load_profiles(path=path)
    for vendor in ("huawei", "cisco"):
        assert sv.check_commands([c["cmd"] for c in prof[vendor]["commands"]], vendor) == []


# ------------------------------------------------------------ 设备名 (从提示符)

@pytest.mark.parametrize("text,name", [
    ("display version\nxx\n<SW-A>", "SW-A"),
    ("<SW-A>\n", "SW-A"),
    ("[SW-A]", "SW-A"),
    ("[~SW-A]", "SW-A"),
    ("[*SW-A]", "SW-A"),
    ("SW-A#", "SW-A"),
    ("SW-A>", "SW-A"),
    ("SW-A(config)#", "SW-A"),
    ("show version\nfoo\nSW-B# ", "SW-B"),
    ("Username:", None),
    ("Password:", None),
    ("", None),
    ("<SW-A>\nmore text after", None),
])
def test_parse_device(text, name):
    assert sv.parse_device(text) == name


def test_safe_name_has_no_separator_or_slash():
    assert sv.safe_name("a_b/c d") == "a-b-c-d"
    assert sv.safe_name("/dev/con-1") == "-dev-con-1"


# ------------------------------------------------------------ 执行

T0 = time.mktime((2026, 10, 30, 14, 3, 0, 0, 0, -1))
T1 = time.mktime((2026, 10, 31, 9, 0, 0, 0, 0, -1))


def _body(cmd, n=0):
    return {
        "display version": "VRP (R) software, Version 5.170\nSW-A uptime is 1 week, %d days" % n,
        "display clock": "2026-10-%02d 14:03:00\nFriday" % (n + 1),
        "display arp": "192.0.2.1  0200-0000-0001  20  D-0  GE0/0/1\n192.0.2.%d  0200-0000-0002  20  D-0" % (n + 2),
        "display current-configuration": "#\nsysname SW-A\n#\ninterface Vlanif1\n ip address 192.0.2.254 255.255.255.0\n#",
    }.get(cmd, "ok %s" % cmd)


def fake_send(log, prompt="<SW-A>", n=0, misbehave=None):
    """像 conbroker send: 回显命令 + 正文 + 提示符. misbehave={cmd: 'timeout'|'raise'|'refuse'|'noprompt'}."""
    misbehave = misbehave or {}

    def send(port, cmd, timeout):
        log.append((port, cmd, timeout))
        how = misbehave.get(cmd)
        if how == "raise":
            raise RuntimeError("没有在跑的服务")
        if how == "refuse":
            return "[conbroker: 拒绝发送, 设备停在密码/用户名提示上; 真要发密码加 --secret, 确认无误加 --force]"
        if how == "timeout":
            return "%s\npartial line\n[conbroker: %ss 内没等到提示符]" % (cmd, timeout)
        if how == "noprompt":
            return "%s\nUsername:" % cmd
        return "%s\n%s\n%s" % (cmd, _body(cmd, n), prompt)
    return send


def _run(tmp_path, log, now=T0, **kw):
    return sv.run_survey("/dev/con-1", "huawei", send=fake_send(log, **kw), now=lambda: now,
                         survey_dir=str(tmp_path), profiles=sv.load_profiles(path="/nonexistent"))


def test_run_survey_ok_writes_archive(tmp_path):
    log = []
    r = _run(tmp_path, log)
    assert r["ok"] and r["device"] == "SW-A" and r["head"] == "con-1"
    assert [c for _p, c, _t in log] == [c["cmd"] for c in sv.load_profiles(path="/x")["huawei"]["commands"]]
    assert all(p == "/dev/con-1" for p, _c, _t in log)
    assert dict((c, t) for _p, c, t in log)["display current-configuration"] == 120
    assert r["path"] == str(tmp_path / "con-1_SW-A_20261030-1403.txt")
    text = open(r["path"], encoding="utf-8").read()
    assert "===== [8/8] display current-configuration =====" in text
    assert "sysname SW-A" in text
    assert "<SW-A>" not in text                         # 提示符和回显不进正文
    assert "首次" in r["summary"] and r["path"] in r["summary"]


def test_refused_profile_sends_nothing(tmp_path):
    log = []
    prof = sv.load_profiles(path="/x")
    prof["huawei"]["commands"].insert(3, {"cmd": "dis cur", "timeout": 5})
    prof["huawei"]["commands"].append({"cmd": "display version | save", "timeout": 5})
    r = sv.run_survey("/dev/con-1", "huawei", send=fake_send(log), now=lambda: T0,
                      survey_dir=str(tmp_path), profiles=prof)
    assert log == [] and not r["ok"] and r["path"] is None
    assert r["refused"] == ["dis cur", "display version | save"]
    assert "拒绝" in r["summary"] and "dis cur" in r["summary"]
    assert list(tmp_path.iterdir()) == []


def test_unknown_vendor_refused(tmp_path):
    log = []
    r = sv.run_survey("/dev/con-1", "ruijie", send=fake_send(log), now=lambda: T0, survey_dir=str(tmp_path),
                      profiles=sv.load_profiles(path="/x"))
    assert log == [] and not r["ok"] and "ruijie" in r["summary"]


@pytest.mark.parametrize("how,word", [("timeout", "超时"), ("raise", "没有在跑的服务"),
                                      ("refuse", "拒绝发送"), ("noprompt", "提示符")])
def test_abort_midway_reports_and_archives(tmp_path, how, word):
    log = []
    r = _run(tmp_path, log, misbehave={"display mac-address": how})
    assert not r["ok"]
    sent = [c for _p, c, _t in log]
    assert sent[-1] == "display mac-address" and "display arp" not in sent      # 出事就停, 后面不发
    assert word in r["summary"] and "display mac-address" in r["summary"] and "5/8" in r["summary"]
    text = open(r["path"], encoding="utf-8").read()
    assert "===== [6/8] display mac-address =====" in text
    assert "未执行" in text and "display arp" in text
    assert "中止" in text


def test_first_command_fails_archive_unknown_device(tmp_path):
    r = _run(tmp_path, [], misbehave={"display version": "noprompt"})
    assert not r["ok"] and r["device"] is None
    assert os.path.basename(r["path"]).startswith("con-1_unknown_")
    assert r["diff"] is None


def test_prompt_changes_midway_aborts(tmp_path):
    log = []
    send0 = fake_send(log)

    def send(port, cmd, timeout):
        out = send0(port, cmd, timeout)
        return out.replace("<SW-A>", "<SW-B>") if cmd == "display arp" else out
    r = sv.run_survey("/dev/con-1", "huawei", send=send, now=lambda: T0, survey_dir=str(tmp_path),
                      profiles=sv.load_profiles(path="/x"))
    assert not r["ok"] and "SW-B" in r["summary"]
    assert "display current-configuration" not in [c for _p, c, _t in log]


def test_resend_check_blocks_even_if_list_mutates(tmp_path, monkeypatch):
    # 发之前每条再过一次白名单: 预检后清单被改也发不出去
    calls = []
    real = sv.is_readonly
    monkeypatch.setattr(sv, "is_readonly", lambda c, v: (calls.append(c), real(c, v))[1])
    _run(tmp_path, [])
    assert calls.count("display arp") >= 2


# ------------------------------------------------------------ 存档解析 / 比对

def test_archive_roundtrip(tmp_path):
    r = _run(tmp_path, [], misbehave={"display arp": "timeout"})
    secs = sv.parse_archive(open(r["path"], encoding="utf-8").read())
    assert secs["display version"][0] == "ok"
    assert secs["display arp"][0] == "timeout"
    assert secs["display current-configuration"][0] == "skipped"
    assert "sysname SW-A" not in "\n".join(secs["display current-configuration"][1])


def test_second_run_diff_ignores_clock_and_uptime(tmp_path):
    _run(tmp_path, [])
    r = _run(tmp_path, [], now=T1, n=0)
    assert r["diff"] == []
    assert "无变化" in r["summary"] and "con-1_SW-A_20261030-1403.txt" in r["summary"]
    r = _run(tmp_path, [], now=T1 + 60, n=3)            # 时钟/uptime 变了, arp 多换了一行
    assert [d[0] for d in r["diff"]] == ["display arp"]
    assert r["diff"][0][1:] == (1, 1)
    assert "display arp (+1 -1)" in r["summary"]
    assert "display clock" not in r["summary"] and "display version" not in r["summary"]


def test_diff_compares_with_latest_same_device_only(tmp_path):
    (tmp_path / "con-2_SW-B_20261031-0000.txt").write_text("x")
    (tmp_path / "con-2_SW-A_20261029-0000.txt").write_text("old")
    _run(tmp_path, [])                                   # 20261030-1403
    r = _run(tmp_path, [], now=T1)
    assert r["previous"].endswith("con-1_SW-A_20261030-1403.txt")


def test_same_minute_does_not_overwrite(tmp_path):
    a = _run(tmp_path, [])["path"]
    b = _run(tmp_path, [])["path"]
    assert a != b and os.path.exists(a) and os.path.exists(b)
    assert sv.previous_archive(str(tmp_path), "SW-A", exclude=b) == a


def test_diff_marks_sections_not_comparable(tmp_path):
    _run(tmp_path, [])
    r = _run(tmp_path, [], now=T1, misbehave={"display arp": "timeout"})
    assert "无法比对" in r["summary"] and "display arp" in r["summary"]


def test_diff_sections_counts_and_added_removed_sections():
    prof = sv.load_profiles(path="/x")["huawei"]
    old = {"display arp": ("ok", ["a", "b", "c"]), "display version": ("ok", ["v1"]),
           "display gone": ("ok", ["x"])}
    new = {"display arp": ("ok", ["a", "c", "d", "e"]), "display version": ("ok", ["v1"]),
           "display new": ("ok", ["y"])}
    d = sv.diff_sections(old, new, prof)
    assert ("display arp", 2, 1) in d
    assert ("display new", 1, 0) in d and ("display gone", 0, 1) in d
    assert all(x[0] != "display version" for x in d)


def test_volatile_lines_masked():
    prof = sv.load_profiles(path="/x")["huawei"]
    old = {"display current-configuration": ("ok", ["!Last configuration was updated at 2026-10-30 14:00:00",
                                                     "sysname SW-A"])}
    new = {"display current-configuration": ("ok", ["!Last configuration was updated at 2026-10-31 09:00:00",
                                                     "sysname SW-A"])}
    assert sv.diff_sections(old, new, prof) == []


# ------------------------------------------------------------ 记一笔 / 真 send / 命令行

def test_record_writes_cartlog_event(tmp_path):
    r = _run(tmp_path, [])
    got = []
    sv.record(r, event=lambda kind, msg: got.append((kind, msg)))
    assert got[0][0] == "survey"
    assert "device=SW-A" in got[0][1] and "ok=true" in got[0][1] and "done=8/8" in got[0][1]
    assert r["path"] in got[0][1]


def test_conbroker_send_builds_command(monkeypatch):
    import serial_core as sc
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "display version\nx\n<SW-A>\n", ""
    monkeypatch.setattr(sv.subprocess, "run", lambda cmd, **kw: (seen.update(cmd=cmd, kw=kw), R())[1])
    out = sv.conbroker_send("/dev/con-1", "display version", 30)
    assert seen["cmd"] == sc._conbroker_cmd() + ["send", "/dev/con-1", "display version", "-t", "30"]
    assert seen["kw"]["timeout"] > 30 and out.startswith("display version")


def test_conbroker_send_refuses_non_readonly(monkeypatch):
    monkeypatch.setattr(sv.subprocess, "run", lambda *a, **k: pytest.fail("不该真发"))
    with pytest.raises(ValueError):
        sv.conbroker_send("/dev/con-1", "save", 30)


def test_conbroker_send_raises_on_failure(monkeypatch):
    class R:
        returncode, stdout, stderr = 1, "", "/dev/con-1 没有在跑的服务, 先 serve 或 console\n"
    monkeypatch.setattr(sv.subprocess, "run", lambda cmd, **kw: R())
    with pytest.raises(RuntimeError, match="没有在跑的服务"):
        sv.conbroker_send("/dev/con-1", "display version", 30)


def test_main_no_service(monkeypatch, capsys):
    monkeypatch.setattr(sv, "_list_services", lambda: {})
    monkeypatch.setattr(sv, "run_survey", lambda *a, **k: pytest.fail("没服务不该跑"))
    assert sv.main(["con-1"]) == 2
    assert "没有在跑的" in capsys.readouterr().out


def test_main_runs_with_service(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sv, "_list_services", lambda: {"/dev/con-1": "9600"})
    seen = {}

    def fake_run(port, vendor, **kw):
        seen.update(port=port, vendor=vendor, **kw)
        return {"ok": True, "summary": "SW-A 巡检完成", "head": "con-1", "device": "SW-A", "path": None,
                "sections": [], "diff": None, "previous": None, "refused": [], "error": None}
    monkeypatch.setattr(sv, "run_survey", fake_run)
    monkeypatch.setattr(sv, "record", lambda r: None)
    assert sv.main(["con-1", "--dir", str(tmp_path)]) == 0
    assert seen["port"] == "/dev/con-1" and seen["vendor"] == "huawei" and seen["survey_dir"] == str(tmp_path)
    assert "巡检完成" in capsys.readouterr().out
