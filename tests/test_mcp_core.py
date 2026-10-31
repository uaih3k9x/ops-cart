"""mcp_core 的离线测试: 执行绪命名 / 参数校验 / JSON-RPC / 审计行 / HTTP 来源与令牌."""

import json

import pytest

import mcp_core as mc


# ------------------------------------------------------------ 执行绪命名 (华为提示符)

@pytest.mark.parametrize("text,name", [
    ("<SW-CORE-1>", "SW-CORE-1"),
    ("display version\n...\n<SW1>", "SW1"),
    ("<SW1>display vlan\nVLAN ID ...\n", "SW1"),           # 提示符后面跟着敲过的命令
    ("[SW1]", "SW1"),
    ("[~SW1]", "SW1"),                                      # VRP8 两阶段提交的视图
    ("[*SW1]", "SW1"),
    ("<SW1>\r\n", "SW1"),
    ("<old>\nsysname new\n[new]", "new"),                   # 改了 sysname, 以最后一个为准
])
def test_thread_name_from_huawei_prompt(text, name):
    assert mc.thread_name(text) == name


def test_thread_name_strips_config_view_suffix():
    # 系统视图进了接口: [名-GigabitEthernet0/0/1], 名字本身也可能带横杠
    assert mc.thread_name("<SW-A>\n[SW-A]interface GigabitEthernet0/0/1\n[SW-A-GigabitEthernet0/0/1]") == "SW-A"
    assert mc.thread_name("[SW-A-GigabitEthernet0/0/1]") == "SW-A"
    assert mc.thread_name("[AR1-Vlanif10]") == "AR1"
    assert mc.thread_name("[AR1-ui-console0]") == "AR1"
    assert mc.thread_name("[AR1-aaa]") == "AR1"


@pytest.mark.parametrize("text", [
    "", "login:", "Password:", "  <cr>", "  <1-4094>  VLAN ID", "[Y/N]:",
    "Press any key to get started", "<cr>", "<a b>", "x" * 100,
])
def test_thread_name_none_when_no_prompt(text):
    # ? 帮助里的 <cr> / <1-4094> 是缩进的; [Y/N] 是确认提示, 都不是名字
    assert mc.thread_name(text) is None


def test_thread_name_only_looks_at_recent_lines():
    assert mc.thread_name("<OLD>\n" + "x\n" * (mc.NAME_SCAN_LINES + 5)) is None


def test_slot_label_always_has_slot():
    assert mc.slot_label(2, "SW1") == "槽位 2 (SW1)"
    assert mc.slot_label(3, None) == "槽位 3"


# ------------------------------------------------------------ 参数校验

PARAMS = {
    "slot": {"type": "integer", "minimum": 1, "maximum": 4, "required": True, "description": "槽位"},
    "lines": {"type": "integer", "minimum": 1, "maximum": 500, "default": 80},
    "device": {"type": "string", "pattern": r"(/dev/)?video[0-9]{1,2}", "maxLength": 16},
}


def test_check_args_fills_defaults():
    assert mc.check_args(PARAMS, {"slot": 2}) == {"slot": 2, "lines": 80}


def test_check_args_keeps_given_values():
    assert mc.check_args(PARAMS, {"slot": 4, "lines": 1, "device": "video1"}) == \
        {"slot": 4, "lines": 1, "device": "video1"}


def test_check_args_accepts_integral_float():
    # 有的客户端把 JSON 数字一律当 float 发
    assert mc.check_args(PARAMS, {"slot": 2.0})["slot"] == 2


@pytest.mark.parametrize("args,why", [
    ({}, "slot"),
    ({"slot": 0}, "slot"),
    ({"slot": 5}, "slot"),
    ({"slot": True}, "slot"),                   # bool 不算整数
    ({"slot": "2"}, "slot"),
    ({"slot": 2.5}, "slot"),
    ({"slot": 1, "lines": 501}, "lines"),
    ({"slot": 1, "device": "/dev/sda"}, "device"),
    ({"slot": 1, "device": "video1; rm"}, "device"),
    ({"slot": 1, "device": 3}, "device"),
    ({"slot": 1, "bogus": 1}, "bogus"),         # 不认识的参数拒掉, 免得 Agent 以为它生效了
])
def test_check_args_rejects(args, why):
    with pytest.raises(mc.ArgError) as e:
        mc.check_args(PARAMS, args)
    assert why in str(e.value)


def test_check_args_rejects_non_object():
    with pytest.raises(mc.ArgError):
        mc.check_args(PARAMS, [1])


def test_input_schema_is_json_schema():
    s = mc.input_schema(PARAMS)
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert s["required"] == ["slot"]
    assert s["properties"]["slot"] == {"type": "integer", "minimum": 1, "maximum": 4, "description": "槽位"}
    assert s["properties"]["lines"]["default"] == 80
    assert s["properties"]["device"]["pattern"] == r"^(/dev/)?video[0-9]{1,2}$"
    json.dumps(s)


def test_input_schema_empty():
    assert mc.input_schema({}) == {"type": "object", "properties": {}, "additionalProperties": False}
