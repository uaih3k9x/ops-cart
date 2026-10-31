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


# ------------------------------------------------------------ JSON-RPC / MCP

def _echo(args):
    return [mc.text_content(f"slot={args['slot']}")]


def _boom(args):
    raise RuntimeError("炸了")


def _refuse(args):
    raise mc.ToolError("槽位 2 没插线")


class FakeAudit:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(json.loads(line))
        return True


def _server(audit=None):
    tools = [mc.Tool("echo", "回显", {"slot": PARAMS["slot"]}, _echo),
             mc.Tool("boom", "抛异常", {}, _boom),
             mc.Tool("refuse", "业务上拒绝", {}, _refuse)]
    return mc.Server(tools, audit=audit or FakeAudit(), version="9.9")


def _req(method, params=None, rid=1):
    d = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        d["params"] = params
    return d


def test_initialize_echoes_supported_version():
    r = _server().handle(_req("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "t", "version": "0"}}))
    assert r["id"] == 1 and r["jsonrpc"] == "2.0"
    res = r["result"]
    assert res["protocolVersion"] == "2025-03-26"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"] == {"name": "ops-cart", "version": "9.9"}
    assert "只读" in res["instructions"]


def test_initialize_unknown_version_gets_latest():
    r = _server().handle(_req("initialize", {"protocolVersion": "1999-01-01"}))
    assert r["result"]["protocolVersion"] == mc.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_response():
    s = _server()
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None


def test_ping():
    assert _server().handle(_req("ping"))["result"] == {}


def test_tools_list():
    r = _server().handle(_req("tools/list"))
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"echo", "boom", "refuse"}
    assert tools["echo"]["description"] == "回显"
    assert tools["echo"]["inputSchema"]["required"] == ["slot"]
    assert tools["echo"]["annotations"]["readOnlyHint"] is True


def test_tools_call_ok_and_audited():
    a = FakeAudit()
    r = _server(a).handle(_req("tools/call", {"name": "echo", "arguments": {"slot": 3}}), who="claude@stdio")
    assert r["result"] == {"content": [{"type": "text", "text": "slot=3"}], "isError": False}
    assert len(a.lines) == 1
    line = a.lines[0]
    assert line["who"] == "claude@stdio" and line["tool"] == "echo"
    assert line["args"] == {"slot": 3} and line["ok"] is True and isinstance(line["ms"], int)


def test_tools_call_bad_args_is_tool_error():
    a = FakeAudit()
    r = _server(a).handle(_req("tools/call", {"name": "echo", "arguments": {"slot": 9}}))
    assert r["result"]["isError"] is True
    assert "slot" in r["result"]["content"][0]["text"]
    assert a.lines[0]["ok"] is False and "slot" in a.lines[0]["error"]


def test_tools_call_tool_error_and_crash_do_not_kill_server():
    s = _server()
    r = s.handle(_req("tools/call", {"name": "refuse"}))
    assert r["result"]["isError"] is True and "没插线" in r["result"]["content"][0]["text"]
    r = s.handle(_req("tools/call", {"name": "boom", "arguments": {}}))
    assert r["result"]["isError"] is True and "RuntimeError" in r["result"]["content"][0]["text"]


def test_tools_call_unknown_tool_is_protocol_error():
    a = FakeAudit()
    r = _server(a).handle(_req("tools/call", {"name": "send_command", "arguments": {}}))
    assert r["error"]["code"] == -32602
    assert a.lines[0]["tool"] == "send_command" and a.lines[0]["ok"] is False


def test_tools_call_without_name():
    r = _server().handle(_req("tools/call", {}))
    assert r["error"]["code"] == -32602


def test_unknown_method():
    r = _server().handle(_req("resources/list"))
    assert r["error"]["code"] == -32601


@pytest.mark.parametrize("msg", [[], "x", {"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1},
                                 {"jsonrpc": "2.0", "id": 1, "method": 5}])
def test_invalid_request(msg):
    r = _server().handle(msg)
    assert r["error"]["code"] == -32600


def test_batch():
    r = _server().handle([_req("ping", rid=1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                          _req("ping", rid=2)])
    assert [x["id"] for x in r] == [1, 2]


def test_handle_text_parse_error():
    out = json.loads(_server().handle_text("{nope"))
    assert out["error"]["code"] == -32700 and out["id"] is None


def test_handle_text_roundtrip_and_notification():
    s = _server()
    assert json.loads(s.handle_text(json.dumps(_req("ping"))))["result"] == {}
    assert s.handle_text(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def test_handle_text_output_is_one_line():
    # stdio 传输按行分帧: 回包里不许有裸换行
    out = _server().handle_text(json.dumps(_req("tools/call", {"name": "refuse"})))
    assert "\n" not in out


# ------------------------------------------------------------ 内容

def test_contents():
    assert mc.text_content("hi") == {"type": "text", "text": "hi"}
    j = mc.json_content({"a": "中"})
    assert j["type"] == "text" and json.loads(j["text"]) == {"a": "中"} and "中" in j["text"]
    assert mc.image_content(b"\x89PNG") == {"type": "image", "data": "iVBORw==", "mimeType": "image/png"}


# ------------------------------------------------------------ 审计

def test_audit_line_format():
    import time
    now = 1793000000.0
    d = json.loads(mc.audit_line("tok:claude", "read_output", {"slot": 2, "lines": 50}, True, ms=12, now=now))
    assert d == {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "who": "tok:claude",
                 "tool": "read_output", "args": {"slot": 2, "lines": 50}, "ok": True, "ms": 12}


def test_audit_line_error_and_clipping():
    d = json.loads(mc.audit_line("x", "t", {"s": "a" * 1000, "o": object()}, False, error="e" * 1000))
    assert len(d["args"]["s"]) <= mc.AUDIT_CLIP + 1 and d["args"]["s"].endswith("…")
    assert isinstance(d["args"]["o"], str)
    assert len(d["error"]) <= mc.AUDIT_CLIP + 1
    assert "\n" not in mc.audit_line("a\nb", "t\n", {"k": "x\ny"}, False)


def test_audit_file_appends_jsonl(tmp_path):
    p = tmp_path / "sub" / "mcp-audit.log"
    a = mc.Audit(str(p))
    assert a.write(mc.audit_line("w", "t", {}, True))
    assert a.write(mc.audit_line("w", "t2", {}, True))
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["t", "t2"]


def test_audit_write_failure_swallowed(tmp_path):
    f = tmp_path / "file"
    f.write_text("")
    assert mc.Audit(str(f / "x.log")).write("{}") is False      # 父目录是个文件


# ------------------------------------------------------------ HTTP: 只绑本机和 tailnet, 要令牌

TS4, TS6 = "100.100.0.5", "fd7a:115c:a1e0::5"       # 编的 tailnet 地址  public-safe-ignore
IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"},
                                   {"family": "inet6", "local": "::1"}]},
    {"ifname": "eno1", "addr_info": [{"family": "inet", "local": "192.0.2.1"},
                                     {"family": "inet6", "local": "fe80::1"}]},
    {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": TS4},
                                           {"family": "inet6", "local": TS6}]},
    {"ifname": "wlp4s0"},                                       # 没地址的口
])


def test_bind_addrs_only_loopback_and_tailnet():
    assert mc.bind_addrs(IP_JSON) == ["127.0.0.1", "::1", TS4, TS6]


@pytest.mark.parametrize("text", ["", "garbage", "{}", "[1, 2]"])
def test_bind_addrs_falls_back_to_loopback(text):
    assert mc.bind_addrs(text) == ["127.0.0.1"]


TOK = "k" * 32
TOKENS = {"claude-mac": TOK, "weak": "short", "blank": ""}


def test_token_who():
    assert mc.token_who(f"Bearer {TOK}", TOKENS) == "claude-mac"
    assert mc.token_who(f"bearer {TOK}", TOKENS) == "claude-mac"
    assert mc.token_who("Bearer short", TOKENS) is None             # 太短的令牌 (样例占位) 不认
    assert mc.token_who("Bearer ", TOKENS) is None
    assert mc.token_who(TOK, TOKENS) is None                        # 没有 Bearer
    assert mc.token_who(None, TOKENS) is None
    assert mc.token_who(f"Bearer {TOK}", None) is None


def _hdr(**kw):
    h = {"Host": f"{TS4}:8765", "Authorization": f"Bearer {TOK}"}
    h.update(kw)
    return {k: v for k, v in h.items() if v is not None}


def test_http_check_passes_tailnet_with_token():
    assert mc.http_check(TS4, _hdr(), TOKENS) == (None, f"claude-mac@{TS4}")
    assert mc.http_check("127.0.0.1", _hdr(Host="localhost:8765"), TOKENS)[0] is None


def test_http_check_rejects_lan_even_with_token():
    code, why = mc.http_check("192.0.2.50", _hdr(), TOKENS)
    assert code == 403 and "tailnet" in why


def test_http_check_rejects_missing_or_wrong_token():
    assert mc.http_check(TS4, _hdr(Authorization=None), TOKENS)[0] == 401
    assert mc.http_check(TS4, _hdr(Authorization="Bearer " + "x" * 32), TOKENS)[0] == 401


def test_http_check_rejects_rebinding_host_and_cross_origin():
    assert mc.http_check(TS4, _hdr(Host="evil.example:8765"), TOKENS)[0] == 403
    assert mc.http_check(TS4, _hdr(Origin="http://evil.example"), TOKENS)[0] == 403
    assert mc.http_check(TS4, _hdr(Origin=f"http://{TS4}:8765"), TOKENS)[0] is None
    assert mc.http_check(TS4, _hdr(Host="cart:8765"), TOKENS, hosts=["cart"])[0] is None


def test_http_check_no_tokens_configured_rejects_all():
    assert mc.http_check("127.0.0.1", _hdr(Host="localhost"), {})[0] == 401


# ------------------------------------------------------------ PXE 状态 (只读)

def test_menu_purpose():
    import pxeserver as ps
    assert mc.menu_purpose(ps.grub_menu("192.0.2.1", install=True)) == "rocky"
    assert mc.menu_purpose(ps.grub_menu_winpe("192.0.2.1")) == "winpe"
    assert mc.menu_purpose(ps.grub_menu_clone("192.0.2.1", "clone-save", ["ocs_live_run=x"])) == "clone-save"
    assert mc.menu_purpose(ps.grub_menu_clone("192.0.2.1", "clone-restore", ["ocs_live_run=x"])) == "clone-restore"
    assert mc.menu_purpose(ps.grub_menu("192.0.2.1")) is None       # 救援菜单不是解锁用途
    assert mc.menu_purpose("") is None


def test_pxe_view_rescue():
    v = mc.pxe_view({"mode": "rescue"}, None, {}, now=100)
    assert v == {"mode": "rescue", "purpose": None, "guard": {}}


def test_pxe_view_armed():
    mode = {"mode": "install", "mac": "02:00:00:00:00:01", "since": 0, "until": 1000}
    guard = {"state": "proxy", "detail": "d", "leases": False, "offered": "192.0.2.77", "ip": "192.0.2.1"}
    v = mc.pxe_view(mode, "inst.ks=http://x/ks.cfg", guard, now=400)
    assert v["mode"] == "install" and v["mac"] == "02:00:00:00:00:01"
    assert v["remaining_s"] == 600 and v["purpose"] == "rocky"
    assert v["guard"] == {"state": "proxy", "detail": "d", "leases": False}


def test_pxe_view_expired_has_no_remaining():
    mode = {"mode": "expired", "mac": "02:00:00:00:00:01", "since": 0, "until": 10}
    v = mc.pxe_view(mode, None, {}, now=400)
    assert v["remaining_s"] == 0 and v["purpose"] is None
