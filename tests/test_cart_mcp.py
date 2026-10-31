"""cart_mcp 的离线测试: 工具 (假的串口/截图/快照) / stdio 分帧 / HTTP 端到端 (只绑 127.0.0.1)."""

import http.client
import io
import json
import threading

import pytest

import cart_mcp as cm
import mcp_core as mc


def _slots(present=(True, True, False, True), baud=("9600", None, None, "115200")):
    return [{"dev": f"/dev/con-{n}", "present": p, "baud": b, "count": 0}
            for n, p, b in zip(range(1, 5), present, baud)]


class Fake:
    def __init__(self):
        self.tails, self.snaps = [], []
        self.slots = _slots()
        self.tail_text = {"/dev/con-1": "display clock\n2026-10-30\n<SW1>", "/dev/con-4": "login:"}
        self.state_doc = {"ts": 1000.0, "net": "on", "ups": {"pct": 90}, "lldp": {}, "slots": [],
                          "kiosk": {"locked": True}, "todo": ["私事"], "dream": "x"}

    def tail(self, dev, n):
        self.tails.append((dev, n))
        if dev not in self.tail_text:
            raise mc.ToolError("conbroker tail 失败")
        return self.tail_text[dev]

    def snap(self, dev):
        self.snaps.append(dev)
        if dev == "/dev/video9":
            raise mc.ToolError("截图失败: 没信号")
        return b"\x89PNG", dev or "/dev/video0"

    def server(self, audit=None):
        tools = cm.build_tools(slots=lambda: self.slots, tail=self.tail, snap=self.snap,
                               state=lambda: self.state_doc, pxe=lambda: {"mode": "rescue"},
                               now=lambda: 1001.0)
        return mc.Server(tools, audit=audit, version="t")


def _call(server, name, args=None):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args or {}}})
    return r["result"]


def _json(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def test_only_read_only_tools():
    names = {t.name for t in cm.build_tools(slots=list, tail=None, snap=None, state=None, pxe=None)}
    assert names == {"list_threads", "read_output", "screenshot", "cart_status", "pxe_status"}


def test_list_threads():
    f = Fake()
    th = _json(_call(f.server(), "list_threads"))["threads"]
    assert [t["slot"] for t in th] == [1, 2, 3, 4]
    assert th[0] == {"slot": 1, "dev": "/dev/con-1", "present": True, "service": True, "baud": "9600",
                     "name": "SW1", "label": "槽位 1 (SW1)", "writer": None}
    assert th[1]["service"] is False and th[1]["name"] is None       # 插着线但没开服务: 不读日志
    assert th[2]["present"] is False and th[2]["name"] is None       # 线拔了: 名字清空
    assert th[3]["name"] is None                                     # 只有 login:, 认不出
    assert [d for d, _ in f.tails] == ["/dev/con-1", "/dev/con-4"]
    assert all(t["writer"] is None for t in th)                      # 写入者等 #21


def test_list_threads_survives_tail_failure():
    f = Fake()
    f.tail_text.pop("/dev/con-1")
    th = _json(_call(f.server(), "list_threads"))["threads"]
    assert th[0]["name"] is None and th[0]["service"] is True


def test_read_output():
    f = Fake()
    res = _call(f.server(), "read_output", {"slot": 1, "lines": 50})
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert text.splitlines()[0].startswith("[槽位 1 (SW1) /dev/con-1")
    assert text.endswith("<SW1>")
    assert f.tails == [("/dev/con-1", 50)]


def test_read_output_default_lines():
    f = Fake()
    _call(f.server(), "read_output", {"slot": 1})
    assert f.tails == [("/dev/con-1", cm.READ_LINES)]


@pytest.mark.parametrize("slot,why", [(3, "没插线"), (2, "没有在跑的 conbroker 服务")])
def test_read_output_refuses_unplugged_or_no_service(slot, why):
    f = Fake()
    res = _call(f.server(), "read_output", {"slot": slot})
    assert res["isError"] is True and why in res["content"][0]["text"]
    assert f"槽位 {slot}" in res["content"][0]["text"]
    assert f.tails == []                                             # 没服务就不碰日志


def test_read_output_bad_slot():
    res = _call(Fake().server(), "read_output", {"slot": 7})
    assert res["isError"] is True


def test_screenshot():
    f = Fake()
    res = _call(f.server(), "screenshot", {"device": "video1"})
    assert res["isError"] is False
    img, note = res["content"]
    assert img == {"type": "image", "data": "iVBORw==", "mimeType": "image/png"}
    assert "/dev/video1" in note["text"]
    assert f.snaps == ["/dev/video1"]


def test_screenshot_default_device_and_errors():
    f = Fake()
    assert _call(f.server(), "screenshot")["isError"] is False
    assert f.snaps == [None]
    res = _call(f.server(), "screenshot", {"device": "/dev/video9"})
    assert res["isError"] is True and "没信号" in res["content"][0]["text"]
    assert _call(f.server(), "screenshot", {"device": "/dev/sda"})["isError"] is True


def test_cart_status():
    d = _json(_call(Fake().server(), "cart_status"))
    assert d["stale"] is False and d["age_s"] == 1.0
    assert d["ups"] == {"pct": 90} and d["kiosk"] == {"locked": True}
    assert "todo" not in d and "dream" not in d                      # 只给运维相关的


def test_cart_status_stale_and_missing():
    f = Fake()
    f.state_doc["ts"] = 900.0
    assert _json(_call(f.server(), "cart_status"))["stale"] is True
    f.state_doc = None
    res = _call(f.server(), "cart_status")
    assert res["isError"] is True and "cart-dash" in res["content"][0]["text"]


def test_pxe_status():
    assert _json(_call(Fake().server(), "pxe_status")) == {"mode": "rescue"}


# ------------------------------------------------------------ 真机读数 (路径注入)

def test_read_pxe_from_files(tmp_path):
    import kiosk_core as kc
    import pxeserver as ps
    mac = "02:00:00:00:00:01"
    mode = tmp_path / "pxe-mode.json"
    mode.write_text(json.dumps({"mac": mac, "since": 0, "until": 2000}))
    (tmp_path / kc.mac_cfg_name(mac)).write_text(ps.grub_menu_winpe("192.0.2.1"))
    guard = tmp_path / "guard.json"
    guard.write_text(json.dumps({"state": "serving", "leases": True, "detail": ""}))
    v = cm.read_pxe(mode_path=str(mode), root=str(tmp_path), guard_path=str(guard), now=1000)
    assert v["mode"] == "install" and v["purpose"] == "winpe" and v["remaining_s"] == 1000
    assert v["guard"]["state"] == "serving"


def test_read_pxe_nothing_armed(tmp_path):
    v = cm.read_pxe(mode_path=str(tmp_path / "none"), root=str(tmp_path), guard_path=str(tmp_path / "g"))
    assert v == {"mode": "rescue", "purpose": None, "guard": {}}


def test_conbroker_tail_command(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "<SW1>\n", ""

    def run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return R()
    monkeypatch.setattr(cm.subprocess, "run", run)
    assert cm.conbroker_tail("/dev/con-2", 30) == "<SW1>"
    assert seen["cmd"][-4:] == ["tail", "/dev/con-2", "-n", "30"]
    assert seen["kw"]["timeout"] > 0


def test_conbroker_tail_failure(monkeypatch):
    class R:
        returncode, stdout, stderr = 1, "", "Traceback...\nFileNotFoundError: x"
    monkeypatch.setattr(cm.subprocess, "run", lambda cmd, **kw: R())
    with pytest.raises(mc.ToolError) as e:
        cm.conbroker_tail("/dev/con-2", 30)
    assert "FileNotFoundError" in str(e.value)


def test_conf_read(tmp_path):
    assert cm.conf_read(str(tmp_path / "nope")) == {"port": 8765, "tokens": {}, "hosts": []}
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"tokens": {"a": "b"}, "port": 9000}))
    assert cm.conf_read(str(p)) == {"port": 9000, "tokens": {"a": "b"}, "hosts": []}
    p.write_text("{bad")
    assert cm.conf_read(str(p))["tokens"] == {}


# ------------------------------------------------------------ stdio

class ListAudit:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(json.loads(line))
        return True


def test_stdio_one_reply_per_request_line():
    a = ListAudit()
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_output",
                                                                          "arguments": {"slot": 1}}}]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n\n")
    out = io.StringIO()
    cm.serve_stdio(Fake().server(a), "stdio:ops", stdin, out)
    lines = out.getvalue().splitlines()
    assert [json.loads(l)["id"] for l in lines] == [1, 2]
    assert a.lines[0]["who"] == "stdio:ops" and a.lines[0]["args"] == {"slot": 1}


def test_stdio_who():
    assert cm.stdio_who({"SUDO_USER": "ops", "SSH_CONNECTION": "192.0.2.9 5000 192.0.2.1 22"}) == \
        "stdio:ops@192.0.2.9"
    assert cm.stdio_who({"USER": "root"}) == "stdio:root"
    assert cm.stdio_who({}) == "stdio:?"


# ------------------------------------------------------------ HTTP 端到端 (127.0.0.1, 随机口)

TOK = "t" * 32


@pytest.fixture
def http_srv():
    a = ListAudit()
    srv = cm.make_http_server("127.0.0.1", 0, Fake().server(a),
                              conf=lambda: {"tokens": {"claude": TOK}, "hosts": []}, audit=a)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], a
    srv.shutdown()
    srv.server_close()


def _post(port, body, token=TOK, path="/mcp", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    c.request("POST", path, body=body if isinstance(body, bytes) else json.dumps(body).encode(), headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, r.getheader("Content-Type"), data


def test_http_tools_list(http_srv):
    port, _ = http_srv
    st, ct, data = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert st == 200 and ct.startswith("application/json")
    assert len(json.loads(data)["result"]["tools"]) == 5


def test_http_call_audited_with_token_name(http_srv):
    port, a = http_srv
    st, _, data = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "list_threads", "arguments": {}}})
    assert st == 200 and json.loads(data)["result"]["isError"] is False
    assert a.lines[-1]["who"] == "claude@127.0.0.1" and a.lines[-1]["tool"] == "list_threads"


def test_http_notification_is_202(http_srv):
    port, _ = http_srv
    st, _, data = _post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202 and data == b""


def test_http_rejects_no_token_and_audits(http_srv):
    port, a = http_srv
    st, _, data = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)
    assert st == 401 and "令牌" in json.loads(data)["error"]
    assert a.lines[-1]["ok"] is False and "401" in a.lines[-1]["error"]


def test_http_rejects_cross_origin(http_srv):
    port, _ = http_srv
    st, _, _ = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "http://evil.example"})
    assert st == 403


def test_http_wrong_path_get_and_big_body(http_srv):
    port, _ = http_srv
    assert _post(port, {}, path="/other")[0] == 404
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/mcp", headers={"Authorization": f"Bearer {TOK}"})
    r = c.getresponse()
    assert r.status == 405 and r.getheader("Allow") == "POST"
    c.close()
    # 只报长度不真发: 服务端看 Content-Length 就拒, 不读体
    assert _post(port, b"{}", headers={"Content-Length": str(cm.MAX_BODY + 1)})[0] == 413


def test_http_parse_error(http_srv):
    port, _ = http_srv
    st, _, data = _post(port, b"{nope")
    assert st == 200 and json.loads(data)["error"]["code"] == -32700


# ------------------------------------------------------------ #21 写路径接进来

def test_list_threads_shows_writer():
    f = Fake()
    tools = cm.build_tools(slots=lambda: f.slots, tail=f.tail, snap=f.snap, state=lambda: f.state_doc,
                           pxe=dict, writer=lambda: {1: "agentA", 2: None, 3: None, 4: None})
    th = _json(_call(mc.Server(tools), "list_threads"))["threads"]
    assert [t["writer"] for t in th] == ["agentA", None, None, None]


def test_real_tools_include_serial_write_tools():
    names = {t.name for t in cm.real_tools()}
    assert {"send_command", "send_key", "lease_acquire", "lease_renew", "lease_release", "pending_list",
            "estop"} <= names
    assert "resume" not in names


def test_real_tools_include_pxe_request_but_no_unlock():
    names = {t.name for t in cm.real_tools()}
    assert {"pxe_request", "pxe_requests", "pxe_retire", "pxe_status"} <= names
    assert not {n for n in names if "arm" in n or "confirm" in n}


def test_main_dispatches_serial_cli(monkeypatch):
    import mcp_serial
    seen = []
    monkeypatch.setattr(mcp_serial, "cli", lambda argv: seen.append(argv) or 0)
    assert cm.main(["confirm", "3"]) == 0
    assert cm.main(["estop"]) == 0
    assert seen == [["confirm", "3"], ["estop"]]
