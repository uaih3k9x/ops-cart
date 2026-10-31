"""kvm_broker 测试: 真 Unix socket + 假键鼠, 看代理怎么转发、仲裁、急停、记日志; 客户端退回直开 (#9)."""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time

import pytest

import ch9329
import kvm_broker as kbr

PORT = "/dev/ttyACM0"


class FakeDev:
    """假 CH9329: 记下发了什么. 共享 sent 列表, 看得出先后."""

    def __init__(self, path, sent, gone=None):
        self.path, self.sent, self.gone, self.closed = path, sent, gone, False

    def _w(self, item):
        if self.gone and self.gone.is_set():
            raise ch9329.Ch9329Gone("拔了")
        self.sent.append(item)

    def send_report(self, rep):
        self._w(("report", rep))

    def send_mouse(self, b=0, dx=0, dy=0, w=0):
        self._w(("mouse", b, dx, dy, w))

    def send_mouse_abs(self, b=0, x=0, y=0, w=0):
        self._w(("mouse_abs", b, x, y, w))

    def info(self):
        return "57 ab 00 81"

    def close(self):
        self.closed = True


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="kb")          # pytest 的 tmp_path 在 macOS 上太长, Unix socket 路径有 104 字节上限
    sent, opened, gone = [], [], threading.Event()
    state = {"busy": False}

    def opener(path):
        if state["busy"]:
            raise ch9329.Ch9329Busy("占着")
        gone.clear()
        dev = FakeDev(path, sent, gone)
        opened.append(dev)
        return dev

    sock, log = os.path.join(d, "s"), os.path.join(d, "kvm.log")
    b = kbr.Broker(sock, log, opener=opener, quiet_s=0.3, wait_max_s=2.0,
                   realpath=lambda p: p, group=None)
    t = threading.Thread(target=b.serve_forever, daemon=True)
    t.start()
    yield {"b": b, "sock": sock, "log": log, "sent": sent, "opened": opened, "gone": gone, "state": state}
    b.shutdown()
    shutil.rmtree(d, ignore_errors=True)


def client(env, src="agent", who="cli", port=PORT):
    return kbr.BrokerCh(port, src=src, who=who, sock_path=env["sock"])


def log_recs(env):
    with open(env["log"]) as f:
        return [json.loads(l) for l in f if l.strip()]


def test_socket_is_owner_group_only(env):
    assert stat.S_IMODE(os.stat(env["sock"]).st_mode) == 0o660


def test_human_report_goes_through(env):
    c = client(env, src="human", who="kiosk")
    c.send_report(bytes.fromhex("0000040000000000"))
    c.send_mouse(1, 2, 3, 0)
    c.close()
    assert env["sent"] == [("report", bytes.fromhex("0000040000000000")), ("mouse", 1, 2, 3, 0)]


def test_agent_type_and_keys_logged_with_source(env):
    c = client(env, who="claude")
    c.type_text("ab", press_ms=0)
    c.send_keys(["ctrl", "alt", "delete"], press_ms=0)
    assert c.info() == "57 ab 00 81"
    c.close()
    a, _ = ch9329.key_report(["a"])
    assert env["sent"][:2] == [("report", a), ("report", bytes(8))]
    recs = [r for r in log_recs(env) if r["ev"] == "sent"]
    assert [(r["src"], r["who"], r["op"]) for r in recs] == [("agent", "claude", "type"),
                                                            ("agent", "claude", "keys")]
    assert recs[0]["text"] == "ab" and recs[1]["keys"] == "ctrl+alt+delete"


def test_agent_waits_while_human_active(env):
    h = client(env, src="human", who="kiosk")
    a = client(env, who="claude")
    h.send_report(bytes.fromhex("0000040000000000"))
    t0 = time.monotonic()
    a.send_keys(["b"], press_ms=0)
    assert time.monotonic() - t0 >= 0.25                 # 人刚敲过, agent 排队等人停手
    h.close()
    a.close()


def test_estop_drops_waiting_agent_and_sends_nothing(env):
    h = client(env, src="human", who="kiosk")
    a = client(env, who="claude")
    h.send_report(bytes.fromhex("0000040000000000"))
    res = {}

    def agent():
        try:
            a.type_text("rm -rf", press_ms=0)
        except OSError as e:
            res["e"] = e
    t = threading.Thread(target=agent)
    t.start()
    time.sleep(0.1)
    n = len(env["sent"])
    client(env, src="human", who="kiosk").estop()
    t.join(2)
    assert isinstance(res.get("e"), kbr.KvmStopped)
    time.sleep(0.4)
    assert len(env["sent"]) == n                          # 急停不插任何东西, 也没漏发
    with pytest.raises(kbr.KvmStopped):
        a.send_keys(["a"])
    with pytest.raises(kbr.KvmStopped):                   # 解除要人来
        a.resume()
    h.send_report(bytes(8))                               # 人照常
    h.resume()
    a.send_keys(["a"], press_ms=0)
    evs = [r["ev"] for r in log_recs(env)]
    assert "estop" in evs and "resume" in evs and "dropped" in evs


def test_status_shows_who(env):
    a = client(env, who="claude")
    a.send_keys(["a"], press_ms=0)
    st = a.status()
    assert st[PORT]["last"] == "claude" and st[PORT]["stopped"] is False


def test_busy_on_open_raises_busy(env):
    env["state"]["busy"] = True
    with pytest.raises(ch9329.Ch9329Busy):
        client(env, src="human", who="kiosk")


def test_unplug_raises_gone_then_reopens(env):
    c = client(env, src="human", who="kiosk")
    env["gone"].set()
    with pytest.raises(ch9329.Ch9329Gone):
        c.send_report(bytes(8))
    c.close()
    c = client(env, src="human", who="kiosk")              # 再插上: 代理重新打开
    c.send_report(bytes(8))
    assert len(env["opened"]) == 2 and env["opened"][0].closed


def test_bad_port_and_agent_raw_refused(env):
    b = env["b"]
    b.port_ok = lambda p: p == PORT
    with pytest.raises(ValueError):
        client(env, port="/etc/passwd")
    a = client(env)
    with pytest.raises(ValueError):
        a.send_report(bytes(8))                           # agent 不给原始报告


def test_bad_json_line_answers_error_and_keeps_connection(env):
    s = socket.socket(socket.AF_UNIX)
    s.connect(env["sock"])
    f = s.makefile("rwb")
    f.write(b"nonsense\n")
    f.flush()
    assert json.loads(f.readline())["err"] == "bad"
    f.write(json.dumps({"op": "status"}).encode() + b"\n")
    f.flush()
    assert json.loads(f.readline())["ok"] is True
    s.close()


def test_human_bursts_logged_not_each_key(env):
    h = client(env, src="human", who="web")
    for _ in range(5):
        h.send_report(bytes.fromhex("0000040000000000"))
    time.sleep(0.8)
    evs = [(r["ev"], r.get("who"), r.get("n")) for r in log_recs(env) if r["ev"].startswith("human")]
    assert evs == [("human_start", "web", None), ("human_end", "web", 5)]


def test_second_broker_on_same_socket_refuses(env):
    with pytest.raises(SystemExit):
        kbr.Broker(env["sock"], None, opener=lambda p: None, group=None)


# ------------------------------------------------------------ 客户端退回直开


def test_open_ch_falls_back_to_direct_when_no_broker(monkeypatch):
    got = []
    monkeypatch.setattr(kbr.ch9329, "Ch9329", lambda port, baud=115200, **kw: got.append(port) or "direct")
    assert kbr.open_ch(PORT, src="agent", who="cli", sock_path="/nonexistent/kvm.sock") == "direct"
    assert got == [PORT]


def test_open_ch_prefers_broker(env):
    c = kbr.open_ch(PORT, src="human", who="kiosk", sock_path=env["sock"])
    assert isinstance(c, kbr.BrokerCh)
    c.close()


def test_sock_path_env_override(monkeypatch):
    monkeypatch.setenv("OPS_KVM_SOCK", "/x/y.sock")
    assert kbr.default_sock() == "/x/y.sock"
    monkeypatch.delenv("OPS_KVM_SOCK")
    assert kbr.default_sock() == kbr.SOCK
