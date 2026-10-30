"""survey_core 测试: 只读白名单 (fail-closed) + 巡检执行/存档/比对. conbroker 用假 send, 不碰串口."""

import json

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
