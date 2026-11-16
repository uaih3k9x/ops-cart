"""#49: 推车 MCP 按档位启动. 只读档根本不注册会动手的工具; HTTP 两档令牌分开; 审计带档位."""

import http.client
import json
import os
import threading

import pytest

import cart_mcp as cm
import mcp_core as mc

# 会产生副作用的工具 (键鼠、串口写、WOL、PXE 申请 / 上锁、打印、租约、急停). 新加动手工具也要进这张表.
ACT_NAMES = {"lease_acquire", "lease_renew", "lease_release", "send_command", "send_key", "estop",
             "pxe_request", "pxe_retire", "kvm_lease_acquire", "kvm_lease_renew", "kvm_lease_release",
             "kvm_key", "kvm_type", "wol_send", "label_print"}
READONLY_NAMES = {"list_threads", "read_output", "screenshot", "cart_status", "pxe_status", "pending_list",
                  "pxe_clients", "pxe_requests", "kvm_status", "screen_ocr", "label_preview",
                  "label_printer_status", "label_abbrev_propose", "gate_allowlist"}


def _listed(server):
    return {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}


class ListAudit:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(json.loads(line))
        return True


# ------------------------------------------------------------ 标注

def test_every_real_tool_is_explicitly_marked():
    tools = cm.real_tools("act")
    assert all(t.kind in mc.KINDS for t in tools)
    assert {t.name for t in tools if t.kind == mc.ACT} == ACT_NAMES
    assert {t.name for t in tools if t.kind == mc.READONLY} == READONLY_NAMES


def test_tool_kind_is_required_and_checked():
    with pytest.raises(TypeError):
        mc.Tool("x", "d", {}, lambda a: [])                         # 没标注 = 起不来
    with pytest.raises(ValueError):
        mc.Tool("x", "d", {}, lambda a: [], kind="maybe")


def test_read_only_hint_follows_kind():
    specs = {t.name: t.spec() for t in cm.real_tools("act")}
    for name, s in specs.items():
        assert s["annotations"]["readOnlyHint"] is (name in READONLY_NAMES), name


# ------------------------------------------------------------ 只读档注册表

def test_readonly_registry_has_no_act_tool():
    tools = cm.real_tools("readonly")
    assert tools and all(t.kind == mc.READONLY for t in tools)
    assert {t.name for t in tools} == READONLY_NAMES
    server = cm.make_server("readonly", ListAudit())
    assert set(server.tools) == READONLY_NAMES and not set(server.tools) & ACT_NAMES
    assert _listed(server) == READONLY_NAMES


def test_readonly_server_does_not_know_act_tools(monkeypatch):
    monkeypatch.setattr(mc, "RO_BURST", 100)                        # 一口气调十几个工具, 不测限速
    a = ListAudit()
    server = cm.make_server("readonly", a)
    for name in sorted(ACT_NAMES):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": {}}})
        assert r["error"]["code"] == -32602 and "没有这个工具" in r["error"]["message"]
    assert all(line["profile"] == "readonly" and line["ok"] is False for line in a.lines)


def test_select_tools_refuses_unmarked_or_unknown_profile():
    t = mc.Tool("x", "d", {}, lambda a: [], kind=mc.READONLY)
    with pytest.raises(ValueError):
        mc.select_tools([t], "rw")
    t.kind = "weird"                                               # 事后被改坏也不放进来
    with pytest.raises(ValueError):
        mc.select_tools([t], "readonly")


def test_act_profile_has_everything():
    assert {t.name for t in cm.real_tools("act")} == READONLY_NAMES | ACT_NAMES
    assert _listed(cm.make_server("act", ListAudit())) == READONLY_NAMES | ACT_NAMES


# ------------------------------------------------------------ 档位参数

def test_unknown_profile_fails_to_start(capsys):
    with pytest.raises(SystemExit) as e:
        cm.main(["stdio", "--profile", "rw", "--audit", "/dev/null"])
    assert e.value.code != 0
    with pytest.raises(ValueError):
        cm.real_tools("rw")


def test_default_profile_keeps_existing_setup():
    # 现有 `cart_mcp.py stdio` 接法 (Claude Code、开机验收) 依赖全部工具: 默认档先不改, 见 PR 待拍板
    assert mc.DEFAULT_PROFILE == "act"
    assert {t.name for t in cm.real_tools()} == READONLY_NAMES | ACT_NAMES


def test_main_passes_profile_to_stdio(monkeypatch):
    seen = {}
    monkeypatch.setattr(cm, "serve_stdio", lambda server, who, i, o: seen.update(server=server))
    assert cm.main(["stdio", "--profile", "readonly", "--audit", "/dev/null"]) == 0
    assert set(seen["server"].tools) == READONLY_NAMES and seen["server"].profile == "readonly"


def test_main_http_readonly_uses_its_own_port(monkeypatch, tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"port": 9100, "tokens": {}, "readonly": {"port": 9101, "tokens": {}}}))
    seen = {}
    monkeypatch.setattr(cm, "serve_http", lambda server, port, audit, conf: seen.update(port=port, server=server))
    cm.main(["http", "--profile", "readonly", "--conf", str(p), "--audit", "/dev/null"])
    assert seen["port"] == 9101 and seen["server"].profile == "readonly"
    cm.main(["http", "--conf", str(p), "--audit", "/dev/null"])
    assert seen["port"] == 9100 and seen["server"].profile == "act"


def test_profile_port_defaults():
    assert mc.profile_port({"port": 8765}, "act") == 8765
    assert mc.profile_port({"port": 8765}, "readonly") == mc.READONLY_PORT
    assert mc.profile_port({"readonly": {"port": 9001}}, "readonly") == 9001


# ------------------------------------------------------------ 协议 / 审计带档位

def test_initialize_names_profile():
    t = [mc.Tool("x", "d", {}, lambda a: [], kind=mc.READONLY)]
    ro = mc.Server(t, profile="readonly").handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert ro["result"]["serverInfo"]["name"] == "ops-cart-readonly"
    assert "lease_acquire" not in ro["result"]["instructions"] and "只读" in ro["result"]["instructions"]
    act = mc.Server(t, profile="act").handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert act["result"]["serverInfo"]["name"] == "ops-cart"


def test_server_refuses_unknown_profile_and_foreign_tools():
    with pytest.raises(ValueError):
        mc.Server([], profile="rw")
    with pytest.raises(ValueError):                                 # 只读档的 Server 不收动手工具, 绕开 select 也不行
        mc.Server([mc.Tool("x", "d", {}, lambda a: [], kind=mc.ACT)], profile="readonly")


def test_audit_line_has_profile():
    d = json.loads(mc.audit_line("w", "t", {}, True, profile="readonly"))
    assert d["profile"] == "readonly"
    a = ListAudit()
    s = mc.Server([mc.Tool("x", "d", {}, lambda a: [mc.text_content("ok")], kind=mc.READONLY)],
                  audit=a, profile="readonly")
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
    assert a.lines[-1]["profile"] == "readonly" and a.lines[-1]["ok"] is True


# ------------------------------------------------------------ HTTP 令牌按档位分开

RO_TOK, ACT_TOK = "r" * 32, "a" * 32


def test_profile_tokens():
    cfg = {"tokens": {"me": ACT_TOK, "dup": RO_TOK}, "readonly": {"tokens": {"bot": RO_TOK}}}
    assert mc.profile_tokens(cfg, "readonly") == {"bot": RO_TOK}
    assert mc.profile_tokens(cfg, "act") == {"me": ACT_TOK}           # 和只读令牌同值的不认
    assert mc.profile_tokens({"tokens": {"me": ACT_TOK}}, "readonly") == {}   # 没配只读段: 谁都进不来, 不借动手令牌
    assert mc.profile_tokens({"readonly": "bad"}, "readonly") == {}
    with pytest.raises(ValueError):
        mc.profile_tokens(cfg, "rw")


def _serve(profile, cfg, audit):
    tools = [mc.Tool("look", "d", {}, lambda a: [mc.text_content("ok")], kind=mc.READONLY)]
    if profile == "act":
        tools.append(mc.Tool("poke", "d", {}, lambda a: [mc.text_content("poked")], kind=mc.ACT))
    srv = cm.make_http_server("127.0.0.1", 0, mc.Server(tools, audit=audit, profile=profile),
                              conf=lambda: cfg, audit=audit)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(port, token, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/mcp", body=json.dumps(body).encode(),
              headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


@pytest.mark.parametrize("dup", [False, True])
def test_http_readonly_token_cannot_reach_act(dup):
    cfg = {"tokens": {"me": ACT_TOK}, "readonly": {"tokens": {"bot": RO_TOK}}, "hosts": []}
    if dup:
        cfg["tokens"]["oops"] = RO_TOK                               # 手抖把只读令牌也填进动手档
    a = ListAudit()
    ro, act = _serve("readonly", cfg, a), _serve("act", cfg, a)
    try:
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "poke", "arguments": {}}}
        st, _ = _post(act.server_address[1], RO_TOK, call)
        assert st == 401
        assert a.lines[-1]["profile"] == "act" and "401" in a.lines[-1]["error"]
        st, data = _post(act.server_address[1], ACT_TOK, call)
        assert st == 200 and json.loads(data)["result"]["isError"] is False
        assert a.lines[-1]["who"] == "me@127.0.0.1" and a.lines[-1]["profile"] == "act"
        assert _post(ro.server_address[1], ACT_TOK, call)[0] == 401     # 动手令牌也不当只读令牌用
        st, data = _post(ro.server_address[1], RO_TOK, call)
        assert st == 200 and "没有这个工具" in json.loads(data)["error"]["message"]
        assert a.lines[-1]["profile"] == "readonly"
    finally:
        for s in (ro, act):
            s.shutdown()
            s.server_close()


# ------------------------------------------------------------ #60 第三轮: 只读档不收批量, 有整体限速

class _Audit:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(json.loads(line))
        return True


class _Mono:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _ro_server(clock=None, audit=None, calls=None):
    calls = [] if calls is None else calls
    t = mc.Tool("look", "d", {}, lambda a: calls.append(1) or [mc.text_content("ok")], kind=mc.READONLY)
    return mc.Server([t], audit=audit, profile="readonly", clock=clock or _Mono()), calls


def _call_req(rid):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": "look", "arguments": {}}}


def test_readonly_refuses_batch_without_running_any():
    audit = _Audit()
    s, calls = _ro_server(audit=audit)
    r = s.handle([_call_req(i) for i in range(2000)])
    assert isinstance(r, dict) and r["error"]["code"] == -32600 and "批量" in r["error"]["message"]
    assert calls == [] and len(audit.lines) == 1 and audit.lines[0]["ok"] is False


def test_act_batch_unchanged():
    t = mc.Tool("x", "d", {}, lambda a: [mc.text_content("ok")], kind=mc.ACT)
    r = mc.Server([t], profile="act").handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                              {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert [x["id"] for x in r] == [1, 2]


def test_readonly_rate_limit_token_bucket():
    clk = _Mono()
    s, calls = _ro_server(clock=clk)
    outs = [s.handle(_call_req(i)) for i in range(mc.RO_BURST + 3)]
    assert len(calls) == mc.RO_BURST
    limited = [o for o in outs if o["result"]["isError"]]
    assert len(limited) == 3 and "太频繁" in limited[0]["result"]["content"][0]["text"]
    clk.t += 1.0 / mc.RO_RATE + 0.01                              # 过一个补充间隔, 能再来一次
    assert s.handle(_call_req(99))["result"]["isError"] is False
    assert s.handle(_call_req(100))["result"]["isError"] is True


def test_readonly_rate_limited_calls_audited_merged():
    clk, audit = _Mono(), _Audit()
    s, _ = _ro_server(clock=clk, audit=audit)
    for i in range(mc.RO_BURST + 50):
        s.handle(_call_req(i))
    limited = [ln for ln in audit.lines if "太频繁" in (ln.get("error") or "")]
    assert len(limited) == 1                                         # 同一秒内被限的只记一条
    clk.t += 1.5
    for i in range(mc.RO_BURST + 5):
        s.handle(_call_req(i))
    limited = [ln for ln in audit.lines if "太频繁" in (ln.get("error") or "")]
    assert len(limited) == 2 and "另有" in limited[1]["error"]       # 下一条带上之前没逐条记的次数


def test_act_has_no_rate_limit():
    t = mc.Tool("x", "d", {}, lambda a: [mc.text_content("ok")], kind=mc.ACT)
    s = mc.Server([t], profile="act", clock=_Mono())
    for i in range(100):
        r = s.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
        assert r["result"]["isError"] is False


def test_readonly_rate_limit_thread_safe():
    s, calls = _ro_server(clock=_Mono())
    ts = [threading.Thread(target=lambda: [s.handle(_call_req(i)) for i in range(20)]) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == mc.RO_BURST


def test_doc_sshd_match_block_locks_rc_and_shell():
    """#60 第三轮: Match 块关 ~/.ssh/rc, 家目录 root 所有, 登录 shell 用不读 rc 的 /bin/sh."""
    import pathlib
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "DESIGN_agent_mcp.md").read_text(encoding="utf-8")
    block = doc[doc.index("Match User agent-ro"):]
    block = block[:block.index("```")]
    for k in ("PermitUserRC no", "ForceCommand sudo /usr/bin/python3 /opt/pxe/cart_mcp.py stdio --profile readonly",
              "DisableForwarding yes", "PermitTTY no"):
        assert k in block, k
    assert "/bin/sh" in doc and "usermod -s /bin/sh agent-ro" in doc and "chown root:root ~agent-ro" in doc


# ------------------------------------------------------------ #60 第四轮: 审计也受限速; 跨进程槽位

@pytest.fixture(autouse=True)
def _ro_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "RO_SLOT_DIR", str(tmp_path / "ro-slots"))


def _audit_lines_after(s, audit, msgs):
    audit.lines.clear()
    for m in msgs:
        s.handle(m)
    return len(audit.lines)


def test_unknown_tool_and_batch_spam_audit_bounded():
    """不存在的工具名、被拒的批量: 先过令牌桶, 审计跟限速调用一样合并 (中1)."""
    n = 1000
    bound = mc.RO_BURST + 2
    clk, audit = _Mono(), _Audit()
    s, _ = _ro_server(clock=clk, audit=audit)
    unknown = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "nope"}} for i in range(n)]
    assert _audit_lines_after(s, audit, unknown) <= bound
    clk.t += 100
    s2, calls = _ro_server(clock=clk, audit=audit)
    assert _audit_lines_after(s2, audit, [[_call_req(i)] for i in range(n)]) <= bound and calls == []
    r = s2.handle([_call_req(1)])
    assert r["error"]["code"] == -32600


def test_unknown_tool_still_says_no_such_tool_when_tokens_left():
    s, _ = _ro_server()
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}})
    assert "没有这个工具" in r["error"]["message"]


def test_ro_slots_limit_processes(tmp_path):
    d = str(tmp_path / "slots")
    held = [mc.take_ro_slot(d) for _ in range(mc.RO_SLOTS)]
    with pytest.raises(mc.SlotBusy, match="只读档"):
        mc.take_ro_slot(d)
    os.close(held[0])
    os.close(mc.take_ro_slot(d))                                     # 放掉一个就能再进
    for fd in held[1:]:
        os.close(fd)


def test_ro_slots_shared_across_processes(tmp_path):
    """别的进程占满槽位, 这个进程进不来."""
    import subprocess
    import sys
    import time
    d = str(tmp_path / "slots")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys,time; sys.path.insert(0, %r); import mcp_core as mc; "
            "fds=[mc.take_ro_slot(%r) for _ in range(mc.RO_SLOTS)]; print('ok', flush=True); time.sleep(30)" % (root, d))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ok"
        with pytest.raises(mc.SlotBusy):
            mc.take_ro_slot(d)
    finally:
        p.kill()
        p.wait()
    time.sleep(0.05)
    os.close(mc.take_ro_slot(d))


def test_ro_slot_dir_unusable_fails_closed(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(mc.SlotBusy, match="打不开"):
        mc.take_ro_slot(str(f / "sub"))


def test_main_readonly_refuses_when_slots_full(monkeypatch, capsys, tmp_path):
    d = str(tmp_path / "full")
    monkeypatch.setattr(mc, "RO_SLOT_DIR", d)
    held = [mc.take_ro_slot(d) for _ in range(mc.RO_SLOTS)]
    seen = []
    monkeypatch.setattr(cm, "serve_stdio", lambda *a: seen.append(1))
    try:
        assert cm.main(["stdio", "--profile", "readonly", "--audit", "/dev/null"]) == 3
        assert seen == [] and "只读档" in capsys.readouterr().err
        assert cm.main(["stdio", "--profile", "act", "--audit", "/dev/null"]) == 0      # 动手档不占槽位
    finally:
        for fd in held:
            os.close(fd)


def test_doc_match_block_max_sessions():
    import pathlib
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "DESIGN_agent_mcp.md").read_text(encoding="utf-8")
    block = doc[doc.index("Match User agent-ro"):]
    assert "MaxSessions 1" in block[:block.index("```")]
    assert "stdio 只读档不受" in doc


# ------------------------------------------------------------ #60 终审: 认证失败审计合并, 慢连接超时

def _serve_clocked(profile, cfg, audit, clock):
    tools = [mc.Tool("look", "d", {}, lambda a: [mc.text_content("ok")], kind=mc.READONLY)]
    srv = cm.make_http_server("127.0.0.1", 0, mc.Server(tools, audit=audit, profile=profile),
                              conf=lambda: cfg, audit=audit, clock=clock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _raw(port, headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/mcp", body=b"{}", headers=headers)
    st = c.getresponse().status
    c.close()
    return st


@pytest.mark.parametrize("profile", ["readonly", "act"])
def test_http_deny_audit_merged_per_code(profile):
    """1000 个错令牌、1000 个错 Host: 审计每种拒绝每秒最多 1 行, 下一行带上没逐条记的次数 (两档都一样)."""
    cfg = {"tokens": {"me": ACT_TOK}, "readonly": {"tokens": {"bot": RO_TOK}}, "hosts": []}
    a, clk = ListAudit(), _Mono()
    srv = _serve_clocked(profile, cfg, a, clk)
    port = srv.server_address[1]
    try:
        for _ in range(1000):
            assert _raw(port, {"Authorization": "Bearer wrong"}) == 401
        for _ in range(1000):
            assert _raw(port, {"Authorization": f"Bearer {ACT_TOK}", "Host": "evil.example"}) == 403
        assert len(a.lines) <= 2
        assert {ln["error"].split()[0] for ln in a.lines} == {"401", "403"}
        clk.t += 1.5
        assert _raw(port, {"Authorization": "Bearer wrong"}) == 401
        assert len(a.lines) == 3 and "另有 999 次" in a.lines[-1]["error"] and a.lines[-1]["profile"] == profile
    finally:
        srv.shutdown()


def test_http_idle_connection_is_dropped(monkeypatch):
    import socket
    import time
    monkeypatch.setattr(cm, "HTTP_TIMEOUT_S", 0.5)
    cfg = {"tokens": {"me": ACT_TOK}, "hosts": []}
    srv = _serve_clocked("act", cfg, None, _Mono())
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        s.settimeout(3)
        t0 = time.monotonic()
        assert s.recv(100) == b""                                    # 什么都不发: 服务端到时间就断
        assert time.monotonic() - t0 < 2.5
        s.close()
    finally:
        srv.shutdown()


def test_http_timeout_default():
    assert cm.HTTP_TIMEOUT_S == 30


# ------------------------------------------------------------ #60 最后一轮: 慢滴、尾巴计数、403 按原因

def test_http_dribbling_headers_cut_at_deadline(monkeypatch):
    """每 0.2 秒一个字节 (比单次超时短): 读请求头整体超过时限也断开."""
    import socket
    import time
    monkeypatch.setattr(cm, "HTTP_TIMEOUT_S", 5)
    monkeypatch.setattr(cm, "HTTP_HEADER_DEADLINE_S", 0.8)
    srv = _serve_clocked("act", {"tokens": {"me": ACT_TOK}, "hosts": []}, None, _Mono())
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        s.sendall(b"POST /mcp HTTP/1.0\r\nX-A: ")
        t0, alive = time.monotonic(), True
        while time.monotonic() - t0 < 4:
            try:
                s.sendall(b"a")
                s.settimeout(0.2)
                if s.recv(10) == b"":
                    alive = False
                    break
            except socket.timeout:
                continue
            except OSError:
                alive = False
                break
        assert not alive and time.monotonic() - t0 < 2.5
        s.close()
    finally:
        srv.shutdown()


def test_http_normal_request_unaffected_by_deadline(monkeypatch):
    monkeypatch.setattr(cm, "HTTP_HEADER_DEADLINE_S", 0.5)
    cfg = {"tokens": {"me": ACT_TOK}, "hosts": []}
    srv = _serve_clocked("act", cfg, None, _Mono())
    try:
        import time
        st, data = _post(srv.server_address[1], ACT_TOK, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert st == 200
        time.sleep(0.8)                                             # 计时器已经取消, 不会事后乱断
        st, _ = _post(srv.server_address[1], ACT_TOK, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert st == 200
    finally:
        srv.shutdown()


def test_limited_tail_flushed():
    clk, audit = _Mono(), _Audit()
    s, _ = _ro_server(clock=clk, audit=audit)
    for i in range(mc.RO_BURST):
        s.handle(_call_req(i))
    audit.lines.clear()
    for i in range(50):
        s.handle(_call_req(i))                                      # 50 次被限, 只记第 1 次
    assert len(audit.lines) == 1
    s.flush_audit()
    assert len(audit.lines) == 2 and "49" in audit.lines[-1]["error"]
    s.flush_audit()
    assert len(audit.lines) == 2                                     # 刷过就清零, 不重复记


def test_http_deny_tail_flushed_with_distinct_sources():
    cfg = {"tokens": {"me": ACT_TOK}, "hosts": []}
    a, clk = ListAudit(), _Mono()
    srv = _serve_clocked("act", cfg, a, clk)
    try:
        for _ in range(50):
            assert _raw(srv.server_address[1], {"Authorization": "Bearer wrong"}) == 401
        assert len(a.lines) == 1
        srv.flush_audit()
        assert len(a.lines) == 2 and "49" in a.lines[-1]["error"] and "1 个不同来源" in a.lines[-1]["error"]
        assert "127.0.0.1" not in a.lines[-1]["error"]               # 只记来源个数, 不列 IP
    finally:
        srv.shutdown()


def test_audit_merge_counts_distinct_sources_capped():
    m = mc.AuditMerge(lambda: 0.0)
    assert m.note("k", "198.51.100.1") == (True, 0, 0)
    for i in range(5000):
        m.note("k", f"198.51.100.{i % 7}")
    got = []
    m.flush(lambda key, quiet, srcs: got.append((key, quiet, srcs)))
    assert got == [("k", 5000, 7)]
    for i in range(mc.AUDIT_SRC_CAP + 50):
        m.note("k", f"src{i}")
    got.clear()
    m.flush(lambda key, quiet, srcs: got.append(srcs))
    assert got == [mc.AUDIT_SRC_CAP]


def test_http_403_split_by_reason():
    """403 按原因分开合并: 刷 Host 不在白名单的, 不会把 Origin 不同源的那条也压掉."""
    cfg = {"tokens": {"me": ACT_TOK}, "hosts": []}
    a, clk = ListAudit(), _Mono()
    srv = _serve_clocked("act", cfg, a, clk)
    port = srv.server_address[1]
    try:
        for _ in range(20):
            assert _raw(port, {"Authorization": f"Bearer {ACT_TOK}", "Host": "evil.example"}) == 403
        assert _raw(port, {"Authorization": f"Bearer {ACT_TOK}", "Origin": "http://evil.example"}) == 403
        errs = [ln["error"] for ln in a.lines]
        assert len(errs) == 2 and "白名单" in errs[0] and "跨站" in errs[1]
    finally:
        srv.shutdown()


def test_main_registers_audit_flush(monkeypatch):
    seen = []
    monkeypatch.setattr(cm.atexit, "register", lambda f: seen.append(f))
    monkeypatch.setattr(cm, "serve_stdio", lambda *a: None)
    cm.main(["stdio", "--profile", "readonly", "--audit", "/dev/null"])
    assert seen and all(callable(f) for f in seen)
