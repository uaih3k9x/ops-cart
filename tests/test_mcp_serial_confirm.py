"""mcp_serial 给确认 UI 的部分 (#23): 只读 peek、车机确认后发送、网页急停 / 拒绝请求的投递和执行."""

import json
import os

import pytest

import confirm_core as cc
import mcp_serial as ms
from test_mcp_serial import T0, Rig, cli


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _held(rig, slot=1, cmd="save"):
    tok = rig.lease(slot)
    return tok, rig.ok("send_command", {"slot": slot, "command": cmd, "lease": tok})


# ------------------------------------------------------------ peek: 只读, 不加锁不写盘

def test_peek_reads_without_writing(rig):
    _tok, p = _held(rig)
    before = os.stat(rig.store.path).st_mtime_ns
    v = ms.peek(rig.store.path, clock=rig.clock)
    assert v["pending"][0]["id"] == p["id"] and v["slots"][0]["writer"] == rig.who
    assert os.stat(rig.store.path).st_mtime_ns == before
    assert "token" not in json.dumps(v) and "lease" not in json.dumps(v["pending"])


def test_peek_missing_file_is_fresh(tmp_path):
    v = ms.peek(str(tmp_path / "none.json"), clock=lambda: T0)
    assert not any(s["estop"] for s in v["slots"]) and v["pending"] == []
    assert not os.path.exists(tmp_path / "none.json")


def test_peek_expiry_applied_in_memory(rig):
    _held(rig)
    rig.clock.t += 3600                                  # 租约早过期了, 状态文件还没人改
    v = ms.peek(rig.store.path, clock=rig.clock)
    assert v["pending"] == [] and v["slots"][0]["writer"] is None


@pytest.mark.parametrize("content", [b"{oops", b"\xff\xfe", b'{"v": 99}'])
def test_peek_unreadable_means_all_stopped(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_bytes(content)
    v = ms.peek(str(p), clock=lambda: T0)
    assert all(s["estop"] for s in v["slots"])


def test_peek_permission_error_means_all_stopped(tmp_path, monkeypatch):
    import builtins
    real = builtins.open

    def deny(path, *a, **k):
        if str(path).endswith("s.json"):
            raise PermissionError(13, "denied")
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", deny)
    v = ms.peek(str(tmp_path / "s.json"), clock=lambda: T0)
    assert all(s["estop"] for s in v["slots"])


# ------------------------------------------------------------ 车机确认后发送 (和命令行同一条路)

def test_send_confirmed_sends_and_finishes(rig):
    _tok, p = _held(rig)
    it, status, out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send)
    assert status == "sent" and rig.sent == [("/dev/con-1", "save", False)]
    hist = rig.ok("pending_list")["history"][-1]
    assert hist["by"] == cc.BY_KIOSK and hist["status"] == "sent"
    assert [r["ev"] for r in rig.log(1)][-2:] == ["confirm", "send"]


def test_send_confirmed_after_estop_raises_and_sends_nothing(rig):
    _tok, p = _held(rig)
    rig.ok("estop", {"slot": 1})
    with pytest.raises(ms.sw.Refused):
        ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, rig.send)
    assert rig.sent == []


def test_send_confirmed_send_crash_clears_inflight(rig):
    _tok, p = _held(rig)

    def boom(*a):
        raise RuntimeError("x")
    _it, status, _out, note = ms.send_confirmed(rig.store, p["id"], cc.BY_KIOSK, lambda: rig.slots, boom)
    assert status == "error" and "RuntimeError" in note
    assert rig.store.view()["slots"][0]["inflight"] is None


def test_cli_confirm_still_works_after_refactor(rig):
    _tok, p = _held(rig)
    rc, text = cli(rig, "confirm", str(p["id"]))
    assert rc == 0 and "sent" in text and rig.sent


# ------------------------------------------------------------ 网页请求: 投递 (ops-web) -> 执行 (root 单元)

def test_write_request_file_private_and_exclusive(tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    path = ms.write_request(cc.make_request("estop", 2, "web:a", T0), str(d))
    assert os.path.dirname(path) == str(d) and path.endswith(".json")
    assert os.stat(path).st_mode & 0o077 == 0
    assert cc.parse_request(open(path).read(), T0) == {"op": "estop", "slot": 2, "by": "web:a", "t": T0}


def test_process_requests_applies_and_empties_dir(rig, tmp_path):
    d = str(tmp_path / "req")
    os.makedirs(d)
    _tok, p = _held(rig)
    ms.write_request(cc.make_request("reject", p["id"], "web:a", T0), d)
    ms.write_request(cc.make_request("estop", 3, "web:a", T0), d)
    res = ms.process_requests(rig.store, d, clock=rig.clock, sleep=lambda s: None)
    assert os.listdir(d) == [] and len(res) == 2
    v = rig.store.view()
    assert v["pending"] == [] and v["slots"][2]["estop"]["by"] == "web:a"
    assert [r["ev"] for r in rig.log(1)][-1] == "reject" and rig.log(3)[-1]["ev"] == "estop"


@pytest.mark.parametrize("body", [
    {"op": "confirm", "id": 1, "by": "web:a", "t": T0},
    {"op": "resume", "slot": None, "by": "web:a", "t": T0},
    {"op": "estop", "slot": 1, "by": "web:a", "t": T0 - 3600},
])
def test_process_requests_drops_go_direction_and_stale(rig, tmp_path, body):
    rig.ok("estop", {"slot": 1})
    d = tmp_path / "req"
    d.mkdir()
    (d / "x.json").write_text(json.dumps(body))
    os.utime(d / "x.json", (T0 - 10, T0 - 10))
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert os.listdir(d) == [] and "不认" in res[0][1]
    assert rig.store.view()["slots"][0]["estop"]                    # 没被解除
    assert all(s["estop"] is None for s in rig.store.view()["slots"][1:])


def test_process_requests_ignores_symlink_and_dirs(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(cc.make_request("estop", None, "web:a", T0)))
    os.symlink(secret, d / "link.json")
    (d / "sub").mkdir()
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert not any(s["estop"] for s in rig.store.view()["slots"])     # 链接不跟
    assert secret.exists() and not os.path.lexists(d / "link.json")
    assert len(res) == 2


def test_process_requests_waits_for_half_written_then_drops(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    (d / "a.json").write_text("")                    # 刚 O_CREAT 还没 write
    os.utime(d / "a.json", (rig.clock.t, rig.clock.t))
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 1:                           # 第一轮等的时候网页写完了
            (d / "a.json").write_text(json.dumps(cc.make_request("estop", 4, "web:a", T0)))
    ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=sleep)
    assert rig.store.view()["slots"][3]["estop"] and os.listdir(d) == []

    (d / "b.json").write_text("{")
    os.utime(d / "b.json", (rig.clock.t, rig.clock.t))

    def tick(s):
        rig.clock.t += s
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=tick)
    assert os.listdir(d) == [] and "不认" in res[-1][1]   # 一直写不完: 过了等待期就删


def test_process_requests_refused_is_reported_not_raised(rig, tmp_path):
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("reject", 99, "web:a", T0), str(d))
    res = ms.process_requests(rig.store, str(d), clock=rig.clock, sleep=lambda s: None)
    assert "不行" in res[0][1] and os.listdir(d) == []


def test_cli_requests_subcommand(rig, tmp_path, monkeypatch):
    d = tmp_path / "req"
    d.mkdir()
    ms.write_request(cc.make_request("estop", None, "web:a", T0), str(d))
    monkeypatch.setattr(ms, "REQ_DIR", str(d))
    assert "requests" in ms.CLI_CMDS
    rc, text = cli(rig, "requests", local=False)          # 不要求本机: 只有停的方向
    assert rc == 0 and "已急停" in text
    assert all(s["estop"] for s in rig.store.view()["slots"])
