"""mcp_serial 的离线测试: 状态文件 + 执行绪日志 / MCP 写工具 (假 conbroker) / 车机命令行 (确认、急停、解除).
不碰任何真串口: send 和 tail 全是假的."""

import io
import json
import os

import pytest

import mcp_core as mc
import mcp_serial as ms
import serial_write_core as sw

T0 = 1_700_000_000.0
PROMPT = "display clock\n2026-10-30\n<SW1>"


def _slots(present=(True, True, False, True), baud=("9600", "9600", None, None)):
    return [{"dev": f"/dev/con-{n}", "present": p, "baud": b, "count": 0}
            for n, p, b in zip(range(1, 5), present, baud)]


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Rig:
    """一套假的: 状态文件和日志在 tmp, conbroker send/tail 记下调用."""

    def __init__(self, tmp_path):
        self.clock = Clock()
        self.events = []
        self.store = ms.Store(str(tmp_path / "run" / "serial-write.json"), str(tmp_path / "log"),
                              clock=self.clock, event=lambda kind, msg: self.events.append((kind, msg)))
        self.log_dir = str(tmp_path / "log")
        self.slots = _slots()
        self.tail_text = {"/dev/con-1": PROMPT, "/dev/con-2": PROMPT}
        self.sent = []
        self.reply = ("sent", "display version\nHuawei VRP\n<SW1>", None)
        self.rules = sw.load_rules(None)
        self.who = "stdio:ops@198.51.100.7"

    def tail(self, dev, n):
        if dev not in self.tail_text:
            raise mc.ToolError("conbroker tail 失败")
        return self.tail_text[dev]

    def send(self, dev, text, timeout, raw=False):
        self.sent.append((dev, text, raw))
        return self.reply

    def tools(self):
        ts = ms.build_tools(self.store, slots=lambda: self.slots, tail=self.tail, send=self.send,
                            rules=lambda: self.rules, who=lambda: self.who)
        return ms.Server(ts, version="t")

    def call(self, name, args=None, who=None):
        if who:
            self.who = who
        r = self.tools().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args or {}}})
        return r["result"]

    def ok(self, name, args=None, who=None):
        res = self.call(name, args, who)
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def err(self, name, args=None, who=None):
        res = self.call(name, args, who)
        assert res["isError"] is True, res
        return res["content"][0]["text"]

    def log(self, slot):
        path = os.path.join(self.log_dir, f"serial-slot{slot}.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f]

    def lease(self, slot=1, who=None):
        return self.ok("lease_acquire", {"slot": slot}, who=who)["lease"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ------------------------------------------------------------ 工具清单

def test_tool_names_and_hints(rig):
    specs = {t["name"]: t for t in rig.tools().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert set(specs) == {"send_command", "send_key", "lease_acquire", "lease_renew", "lease_release",
                          "pending_list", "estop"}
    assert specs["pending_list"]["annotations"]["readOnlyHint"] is True
    assert specs["send_command"]["annotations"]["readOnlyHint"] is False
    assert specs["send_command"]["annotations"]["destructiveHint"] is True
    assert specs["estop"]["annotations"]["destructiveHint"] is False
    assert "resume" not in json.dumps(list(specs))                   # 解除急停不给 agent
    assert set(specs["send_key"]["inputSchema"]["properties"]["key"]["enum"]) == {"enter", "space", "ctrl_c"}
    assert "slot" not in specs["estop"]["inputSchema"].get("required", [])


# ------------------------------------------------------------ 租约

def test_lease_flow_and_single_writer(rig):
    tok = rig.lease(1)
    assert len(tok) >= 16
    assert "agentB" in rig.err("lease_acquire", {"slot": 1}, who="agentB") or "手里" in rig.err(
        "lease_acquire", {"slot": 1}, who="agentB")
    rig.clock.t += 30
    assert rig.ok("lease_renew", {"slot": 1, "lease": tok})["expires_in"] == sw.LEASE_TTL_S
    rig.ok("lease_release", {"slot": 1, "lease": tok})
    rig.lease(1, who="agentB")
    evs = [r["ev"] for r in rig.log(1)]
    assert evs == ["lease_acquire", "lease_release", "lease_acquire"]


def test_lease_needs_live_service(rig):
    assert "没插线" in rig.err("lease_acquire", {"slot": 3})
    assert "conbroker" in rig.err("lease_acquire", {"slot": 4})


def test_lease_expires_without_renew(rig):
    tok = rig.lease(1)
    rig.clock.t += sw.LEASE_TTL_S + 1
    assert "租约" in rig.err("send_command", {"slot": 1, "command": "display version", "lease": tok})
    assert rig.sent == []
    assert any(r["ev"] == "lease_expired" for r in rig.log(1))


# ------------------------------------------------------------ 发命令: 只读直发 / 其余挂起

def test_readonly_command_goes_straight_through(rig):
    tok = rig.lease(1)
    out = rig.ok("send_command", {"slot": 1, "command": "display version", "lease": tok})
    assert out["status"] == "sent" and "Huawei VRP" in out["output"] and out["gate"]["verdict"] == "pass"
    assert rig.sent == [("/dev/con-1", "display version", False)]
    rec = [r for r in rig.log(1) if r["ev"] == "send"][-1]
    assert rec["who"] == "stdio:ops@198.51.100.7" and rec["src"] == "agent" and rec["name"] == "SW1"
    assert rec["cmd"] == "display version" and "Huawei VRP" in rec["echo"] and rec["status"] == "sent"


def test_non_readonly_is_held_for_human(rig):
    tok = rig.lease(1)
    out = rig.ok("send_command", {"slot": 1, "command": "interface GigabitEthernet0/0/1", "lease": tok})
    assert out["status"] == "pending" and isinstance(out["id"], int)
    assert rig.sent == []                                             # 一个字节都没发
    pend = rig.ok("pending_list")["pending"]
    assert [p["cmd"] for p in pend] == ["interface GigabitEthernet0/0/1"]
    assert tok not in json.dumps(rig.ok("pending_list"))              # 令牌不外露
    assert [r["ev"] for r in rig.log(1)][-1] == "pending"


def test_danger_word_held_even_if_display(rig):
    tok = rig.lease(1)
    out = rig.ok("send_command", {"slot": 1, "command": "display current-configuration | include save",
                                  "lease": tok})
    assert out["status"] == "pending" and out["danger"] == ["save"]
    assert rig.sent == []


def test_not_at_prompt_is_held(rig):
    rig.tail_text["/dev/con-1"] = "Username:"
    tok = rig.lease(1)
    assert rig.ok("send_command", {"slot": 1, "command": "display version", "lease": tok})["status"] == "pending"
    rig.tail_text.pop("/dev/con-1")                                   # 读不到输出: 一样挂起
    rig.clock.t += 1
    assert rig.ok("send_command", {"slot": 1, "command": "display clock", "lease": tok})["status"] == "pending"
    assert rig.sent == []


@pytest.mark.parametrize("cmd", ["display version\rsave", "save\x08", "\x03", "display\tversion", "-h"])
def test_bad_lines_refused_and_logged(rig, cmd):
    tok = rig.lease(1)
    assert rig.err("send_command", {"slot": 1, "command": cmd, "lease": tok})
    assert rig.sent == []
    assert rig.log(1)[-1]["ev"] == "refused"


def test_send_needs_own_lease(rig):
    rig.lease(1, who="agentA")
    assert rig.err("send_command", {"slot": 1, "command": "display version", "lease": "x" * 22}, who="agentB")
    assert rig.sent == []
    assert rig.log(1)[-1]["ev"] == "refused"


def test_conbroker_refusal_is_reported(rig):
    rig.reply = ("refused", "[conbroker: 拒绝发送, 设备在等 [Y/N] 回答]", "[conbroker: 拒绝发送]")
    tok = rig.lease(1)
    out = rig.ok("send_command", {"slot": 1, "command": "display version", "lease": tok})
    assert out["status"] == "refused"
    assert rig.ok("pending_list")["history"][-1]["status"] == "refused"


# ------------------------------------------------------------ 特殊键

def test_keys(rig):
    tok = rig.lease(1)
    assert rig.ok("send_key", {"slot": 1, "key": "enter", "lease": tok})["status"] == "sent"
    assert rig.ok("send_key", {"slot": 1, "key": "space", "lease": tok})["status"] == "sent"
    assert rig.sent == [("/dev/con-1", "", False), ("/dev/con-1", " ", True)]
    out = rig.ok("send_key", {"slot": 1, "key": "ctrl_c", "lease": tok})
    assert out["status"] == "pending"                                 # 打断键要人确认
    assert len(rig.sent) == 2
    keys = [r for r in rig.log(1) if r.get("kind") == "key"]
    assert [r["cmd"] for r in keys] == ["enter", "space", "ctrl_c"]


# ------------------------------------------------------------ 急停

def test_agent_estop_one_slot(rig):
    tok = rig.lease(1)
    p = rig.ok("send_command", {"slot": 1, "command": "save", "lease": tok})
    out = rig.ok("estop", {"slot": 1})
    assert out["stopped"] == [1] and out["dropped"] == [p["id"]]
    assert "急停" in rig.err("send_command", {"slot": 1, "command": "display version", "lease": tok})
    assert "急停" in rig.err("lease_acquire", {"slot": 1})
    rig.lease(2)                                                       # 别的槽位照常
    assert rig.sent == []
    assert ("serial-write" in [k for k, _ in rig.events])              # 摘要进 events.log


def test_agent_estop_all(rig):
    out = rig.ok("estop")
    assert out["stopped"] == [1, 2, 3, 4]
    assert all(s["estop"] for s in rig.ok("pending_list")["slots"])


def test_estop_state_survives_new_store(rig, tmp_path):
    rig.ok("estop", {"slot": 2})
    again = ms.Store(rig.store.path, rig.log_dir, clock=rig.clock)
    assert again.view()["slots"][1]["estop"]["by"] == rig.who


def test_writer_view_for_list_threads(rig):
    rig.lease(2, who="agentB")
    assert ms.writers(rig.store) == {1: None, 2: "agentB", 3: None, 4: None}


def test_writers_never_raise(tmp_path):
    st = ms.Store(str(tmp_path / "nodir" / "x" / "s.json"), str(tmp_path / "log"), clock=lambda: T0)
    os.makedirs(str(tmp_path / "nodir"))
    os.chmod(str(tmp_path / "nodir"), 0o500)
    try:
        assert ms.writers(st) == {1: None, 2: None, 3: None, 4: None}
    finally:
        os.chmod(str(tmp_path / "nodir"), 0o700)


# ------------------------------------------------------------ 本机判定

def _stat(table):
    return lambda pid: table.get(pid)


def test_local_human_ok():
    table = {100: ("python3", 90), 90: ("sudo", 80), 80: ("bash", 70), 70: ("login", 1), 1: ("systemd", 0)}
    ok, why = ms.local_human({}, 100, _stat(table), tty=True)
    assert ok, why


@pytest.mark.parametrize("comm", ["sshd", "sshd-session", "tailscaled"])
def test_local_human_rejects_remote_ancestor(comm):
    table = {100: ("python3", 90), 90: ("bash", 80), 80: (comm, 1), 1: ("systemd", 0)}
    ok, why = ms.local_human({}, 100, _stat(table), tty=True)
    assert not ok and comm in why


def test_local_human_rejects_ssh_env_no_tty_unknown_proc():
    table = {100: ("bash", 1), 1: ("systemd", 0)}
    assert not ms.local_human({"SSH_CONNECTION": "x"}, 100, _stat(table), tty=True)[0]
    assert not ms.local_human({}, 100, _stat(table), tty=False)[0]
    assert not ms.local_human({}, 100, _stat({}), tty=True)[0]           # 读不到 /proc: 拿不准就不认


def test_read_stat_parses_comm_with_spaces(tmp_path):
    p = tmp_path / "stat"
    p.write_text("1234 (tmux: server) S 77 1234 1234 0 -1")
    assert ms.read_stat(1234, path_fmt=str(tmp_path / "stat")) == ("tmux: server", 77)
    assert ms.read_stat(1, path_fmt=str(tmp_path / "nope")) is None


# ------------------------------------------------------------ 车机命令行

def cli(rig, *argv, stdin="yes\n", local=True):
    out = io.StringIO()
    rc = ms.cli(list(argv), store=rig.store, send=rig.send, slots=lambda: rig.slots,
                local=lambda: (local, "测试" if local else "经 sshd 进来的"), stdin=io.StringIO(stdin), out=out,
                env={"SUDO_USER": "ops"})
    return rc, out.getvalue()


def test_cli_confirm_sends_and_records(rig):
    tok = rig.lease(1)
    p = rig.ok("send_command", {"slot": 1, "command": "save", "lease": tok})
    rc, text = cli(rig, "pending")
    assert rc == 0 and "save" in text and str(p["id"]) in text
    rc, text = cli(rig, "confirm", str(p["id"]))
    assert rc == 0, text
    assert rig.sent == [("/dev/con-1", "save", False)]
    hist = rig.ok("pending_list")["history"][-1]
    assert hist["status"] == "sent" and hist["by"] == "human:ops@local"
    evs = [r["ev"] for r in rig.log(1)]
    assert evs[-2:] == ["confirm", "send"]


def test_cli_confirm_key(rig):
    tok = rig.lease(1)
    p = rig.ok("send_key", {"slot": 1, "key": "ctrl_c", "lease": tok})
    assert cli(rig, "confirm", str(p["id"]))[0] == 0
    assert rig.sent == [("/dev/con-1", "\\x03", True)]


def test_cli_confirm_needs_yes_and_local(rig):
    tok = rig.lease(1)
    p = rig.ok("send_command", {"slot": 1, "command": "save", "lease": tok})
    rc, text = cli(rig, "confirm", str(p["id"]), stdin="y\n")
    assert rc != 0 and rig.sent == []
    rc, text = cli(rig, "confirm", str(p["id"]), local=False)
    assert rc != 0 and "sshd" in text and rig.sent == []
    assert rig.ok("pending_list")["pending"][0]["id"] == p["id"]      # 还挂着


def test_cli_confirm_after_estop_sends_nothing(rig):
    tok = rig.lease(1)
    p = rig.ok("send_command", {"slot": 1, "command": "save", "lease": tok})
    assert cli(rig, "estop", "1")[0] == 0
    rc, text = cli(rig, "confirm", str(p["id"]))
    assert rc != 0 and rig.sent == []                                 # 急停后任何排队命令都不会发出


def test_cli_reject_allowed_remote(rig):
    tok = rig.lease(1)
    p = rig.ok("send_command", {"slot": 1, "command": "reboot", "lease": tok})
    assert cli(rig, "reject", str(p["id"]), local=False)[0] == 0
    assert rig.ok("pending_list")["history"][-1]["status"] == "rejected"


def test_cli_estop_remote_ok_resume_local_only(rig):
    assert cli(rig, "estop", local=False)[0] == 0                      # 停永远可以
    assert all(s["estop"] for s in rig.store.view()["slots"])
    rc, text = cli(rig, "resume", "all", local=False)
    assert rc != 0 and all(s["estop"] for s in rig.store.view()["slots"])
    rc, _ = cli(rig, "resume", "2")
    assert rc == 0
    view = rig.store.view()["slots"]
    assert view[1]["estop"] is None and view[0]["estop"] is not None
    assert cli(rig, "resume", "all")[0] == 0
    assert not any(s["estop"] for s in rig.store.view()["slots"])
    assert rig.log(2)[-1]["ev"] == "resume" and rig.log(2)[-1]["src"] == "human"


def test_cli_resume_needs_explicit_target(rig):
    rc, _ = cli(rig, "resume")
    assert rc != 0


# ------------------------------------------------------------ 状态文件

def test_state_file_is_private(rig):
    rig.lease(1)
    assert os.stat(rig.store.path).st_mode & 0o077 == 0


def test_corrupt_state_file_stops_everything(rig):
    os.makedirs(os.path.dirname(rig.store.path), exist_ok=True)
    with open(rig.store.path, "w") as f:
        f.write("{oops")
    assert "急停" in rig.err("lease_acquire", {"slot": 1})
    assert all(s["estop"] for s in rig.store.view()["slots"])


# ------------------------------------------------------------ 真 conbroker send 的封装 (假 subprocess)

class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_conbroker_send_args_and_statuses(monkeypatch):
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        return R(out="display version\nVRP\n<SW1>\n")

    monkeypatch.setattr(ms.serial_core, "_conbroker_cmd", lambda: ["cb"])
    assert ms.conbroker_send("/dev/con-1", "display version", 30, run=run)[0] == "sent"
    assert calls[0][0] == ["cb", "send", "/dev/con-1", "display version", "-t", "30"]
    ms.conbroker_send("/dev/con-1", "\\x03", 5, raw=True, run=run)
    assert calls[1][0] == ["cb", "send", "/dev/con-1", "\\x03", "-t", "5", "--raw"]
    st, _out, note = ms.conbroker_send("/dev/con-1", "x", 5, run=lambda a, **k: R(out="[conbroker: 拒绝发送, 密码]"))
    assert st == "refused"
    st, _out, _n = ms.conbroker_send("/dev/con-1", "x", 5,
                                     run=lambda a, **k: R(out="x\n[conbroker: 5s 内没等到提示符]"))
    assert st == "timeout"
    st, _out, note = ms.conbroker_send("/dev/con-1", "x", 5, run=lambda a, **k: R(rc=1, err="boom"))
    assert st == "error" and "boom" in note


def test_conbroker_send_refuses_bad_line_without_subprocess():
    def run(*a, **k):
        raise AssertionError("不该起子进程")
    with pytest.raises(sw.Refused):
        ms.conbroker_send("/dev/con-1", "save\rreboot", 5, run=run)
    with pytest.raises(sw.Refused):
        ms.conbroker_send("/dev/con-1", "\x03", 5, raw=True, run=run)   # raw 只收固定键
