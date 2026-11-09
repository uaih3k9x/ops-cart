"""ime_proc / ime_worker 测试: librime 放子进程, 行 JSON 通信; 子进程崩溃 / 超时 / 坏回复都判不可用, 不拖垮主进程.

子进程用真的 subprocess 跑一个假 worker (不碰 librime); ime_worker 的循环用内存流测.
"""

import io
import json
import os
import sys
import textwrap
import time

import pytest

import ime_core as ic
import ime_proc as ip
import ime_worker as iw
from test_ime_core import FakeEngine

_FAKE = textwrap.dedent('''
    import json, sys, time
    mode = sys.argv[1]
    raw = ""
    def out(d):
        sys.stdout.write(json.dumps(d) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        req = json.loads(line)
        op = req["op"]
        if op == "open":
            if mode == "open_fail":
                out({"ok": False, "why": "中文输入不可用: 没有词库"}); continue
            if mode == "open_hang":
                time.sleep(30)
            if mode == "open_crash":
                sys.exit(5)
            out({"ok": True}); continue
        if op == "close":
            out({"ok": True}); break
        if mode == "crash_on_feed":
            sys.exit(3)
        if mode == "hang_on_feed":
            time.sleep(30)
        if mode == "garbage":
            sys.stdout.write("not json\\n"); sys.stdout.flush(); continue
        if op == "feed":
            raw += req["ch"]
        if op == "clear":
            raw = ""
        commit = None
        if op == "select":
            commit, raw = "你好", ""
        out({"ok": True, "handled": True, "commit": commit,
             "snap": [raw, raw, ["你好"] if raw else [], 0, True, 0]})
''')


@pytest.fixture
def fake(tmp_path):
    p = tmp_path / "fake_worker.py"
    p.write_text(_FAKE, encoding="utf-8")
    return lambda mode: [sys.executable, str(p), mode]


def _open(fake, mode, **kw):
    return ip.open_engine(cmd=fake(mode), **kw)


def test_roundtrip_through_child(fake):
    eng, why = _open(fake, "ok")
    assert eng is not None and why == ""
    try:
        ime = ic.Ime()
        ime.set_engine(eng, zh=True)
        for ch in "nihao":
            assert ime.press(ch, ch, 0).consume
        assert ime.view()["preedit"] == "nihao" and ime.view()["candidates"] == ["你好"]
        assert ime.press("space", " ", 0).commit == "你好"
    finally:
        eng.close()
    assert eng.proc.poll() is not None                     # 关了就真没了


def test_open_failure_reports_reason_and_reaps(fake):
    eng, why = _open(fake, "open_fail")
    assert eng is None and "没有词库" in why


def test_open_crash_and_hang(fake):
    eng, why = _open(fake, "open_crash")
    assert eng is None and "退出" in why
    t = time.time()
    eng, why = _open(fake, "open_hang", open_timeout=0.5)
    assert eng is None and "超时" in why and time.time() - t < 5


def test_child_missing_is_unavailable(tmp_path):
    eng, why = ip.open_engine(cmd=[str(tmp_path / "no-such-python")])
    assert eng is None and "不可用" in why


@pytest.mark.parametrize("mode,word", [("crash_on_feed", "退出"), ("hang_on_feed", "超时"), ("garbage", "回复")])
def test_runtime_failures_degrade_to_english(fake, mode, word):
    eng, why = _open(fake, mode, key_timeout=0.5)
    assert eng is not None, why
    ime = ic.Ime()
    ime.set_engine(eng, zh=True)
    r = ime.press("n", "n", 0)
    assert not r.consume and ime.status == "unavailable" and not ime.zh and word in ime.reason
    assert eng.proc.poll() is not None                     # 坏了的子进程被收掉, 不留僵尸
    ime.press("Shift_L", "", 0)
    assert ime.release("Shift_L").need_engine              # 运行中崩的: 可以重开
    eng2, _ = _open(fake, "ok")
    ime.set_engine(eng2, zh=True)
    assert ime.press("n", "n", 0).consume                  # 重开后照常
    eng2.close()


def test_close_is_idempotent_and_kills(fake):
    eng, _ = _open(fake, "hang_on_feed", key_timeout=0.3)
    eng.close()
    eng.close()
    assert eng.proc.poll() is not None


# ------------------------------------------------------------ 起子进程的命令

def test_worker_cmd_as_root_uses_systemd_run_sandbox(monkeypatch):
    monkeypatch.setattr(ip.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ip.shutil, "which", lambda n: "/usr/bin/systemd-run" if n == "systemd-run" else None)
    cmd = ip.worker_cmd()
    assert cmd[0] == "/usr/bin/systemd-run" and "--pipe" in cmd
    props = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
    for want in ("DynamicUser=yes", "PrivateNetwork=yes", "NoNewPrivileges=yes", "ProtectSystem=strict",
                 "ProtectHome=yes", "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_UNIX",
                 "StateDirectory=ops-cart-ime"):
        assert want in props, want
    assert any(a.startswith("MemoryMax=") for a in props)
    assert "--setenv=OPS_CART_RIME_USER=/var/lib/ops-cart-ime" in cmd
    assert cmd[-1].endswith("ime_worker.py") and cmd[-2] == "-I"


def test_worker_cmd_root_without_systemd_run_refuses(monkeypatch):
    monkeypatch.setattr(ip.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ip.shutil, "which", lambda n: None)
    assert ip.worker_cmd() is None                         # root 下不降权就不起
    eng, why = ip.open_engine()
    assert eng is None and "systemd-run" in why


def test_worker_cmd_non_root_plain_python(monkeypatch):
    monkeypatch.setattr(ip.os, "geteuid", lambda: 1000)
    cmd = ip.worker_cmd()
    assert cmd[:2] == [sys.executable, "-I"] and cmd[-1].endswith("ime_worker.py")


def test_main_process_never_loads_librime():
    """kiosk 进程里只有 ime_tk / ime_proc / ime_core: 都不 import ime_rime, 不 dlopen librime."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("ime_tk.py", "ime_proc.py", "ime_core.py"):
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert "ime_rime" not in src.replace("ime_rime.py", "") and "ctypes" not in src, name


# ------------------------------------------------------------ worker 循环 (内存流, 假引擎)

def _serve(lines, opener):
    out = io.StringIO()
    iw.serve(io.StringIO("".join(json.dumps(x) + "\n" for x in lines)), out, opener=opener)
    return [json.loads(ln) for ln in out.getvalue().splitlines()]


def test_worker_loop_ops():
    eng = FakeEngine()
    rs = _serve([{"op": "open"}, {"op": "feed", "ch": "n"}, {"op": "feed", "ch": "i"}, {"op": "page", "backward": False},
                 {"op": "select", "i": 0}, {"op": "feed", "ch": "a"}, {"op": "backspace"}, {"op": "clear"},
                 {"op": "close"}], lambda **kw: (eng, ""))
    assert rs[0] == {"ok": True}
    assert rs[2]["snap"][1] == "ni" and rs[2]["snap"][2] == ["你", "尼", "泥"] and rs[2]["handled"] is True
    assert rs[4]["commit"] == "你" and rs[4]["snap"][1] == ""
    assert rs[-1] == {"ok": True} and eng.closed


def test_worker_loop_errors():
    rs = _serve([{"op": "feed", "ch": "n"}, {"op": "open"}, {"op": "bogus"}], lambda **kw: (None, "没库"))
    assert rs[0]["ok"] is False and "没打开" in rs[0]["why"]
    assert rs[1] == {"ok": False, "why": "没库"}
    assert rs[2]["ok"] is False


def test_worker_bad_line_answers_error_not_crash():
    out = io.StringIO()
    iw.serve(io.StringIO("{oops\n" + json.dumps({"op": "open"}) + "\n"), out, opener=lambda **kw: (FakeEngine(), ""))
    rs = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert rs[0]["ok"] is False and rs[1] == {"ok": True}


_LIB, _DATA = os.environ.get("OPS_CART_TEST_LIBRIME"), os.environ.get("OPS_CART_TEST_RIME_DATA")


@pytest.mark.skipif(not (_LIB and _DATA), reason="要 OPS_CART_TEST_LIBRIME + OPS_CART_TEST_RIME_DATA 才跑真 librime")
def test_real_worker_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CART_LIBRIME", _LIB)
    monkeypatch.setenv("OPS_CART_RIME_SHARED", _DATA)
    monkeypatch.setenv("OPS_CART_RIME_USER", os.environ.get("OPS_CART_TEST_RIME_USER", str(tmp_path)))
    monkeypatch.setenv("OPS_CART_RIME_SCHEMA", os.environ.get("OPS_CART_TEST_RIME_SCHEMA", "luna_pinyin"))
    eng, why = ip.open_engine(cmd=[sys.executable, os.path.join(os.path.dirname(ip.__file__), "ime_worker.py")])
    assert eng is not None, why
    try:
        ime = ic.Ime()
        ime.set_engine(eng, zh=True)
        for ch in "nihao":
            assert ime.press(ch, ch, 0).consume
        assert "你好" in ime.view()["candidates"]
        assert ime.press("space", " ", 0).commit
    finally:
        eng.close()


# ------------------------------------------------------------ #72 第二轮: 单元名 / 杀单元 / 沙箱 / 清洗回复

def _root_cmd(monkeypatch, cgroup="0::/system.slice/kiosk.service\n"):
    monkeypatch.setattr(ip.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ip.shutil, "which", lambda n: "/usr/bin/" + n if n in ("systemd-run", "systemctl") else None)
    monkeypatch.setattr(ip, "_own_cgroup", lambda: cgroup)
    cmd = ip.worker_cmd()
    return cmd, [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]


def test_worker_cmd_unique_unit_and_lifetime(monkeypatch):
    cmd, props = _root_cmd(monkeypatch)
    units = [a for a in cmd if a.startswith("--unit=")]
    assert len(units) == 1 and units[0].startswith(f"--unit=ops-cart-ime-{os.getpid()}-")
    assert units[0] != [a for a in ip.worker_cmd() if a.startswith("--unit=")][0]   # 每次一个新名字
    assert "RuntimeMaxSec=900" in props
    assert "BindsTo=kiosk.service" in props and "After=kiosk.service" in props


def test_worker_cmd_no_bindsto_outside_kiosk_service(monkeypatch):
    """不在 kiosk.service 里跑 (手工起的车机) 不加 BindsTo: 否则起 worker 会顺手把 kiosk.service 拉起来."""
    _cmd, props = _root_cmd(monkeypatch, cgroup="0::/user.slice/session-1.scope\n")
    assert not any(p.startswith("BindsTo=") for p in props)


def test_worker_cmd_sandbox_matches_cart_ocr(monkeypatch):
    _cmd, props = _root_cmd(monkeypatch)
    for want in ("ProtectKernelLogs=yes", "RestrictRealtime=yes", "MemorySwapMax=0",
                 "SystemCallFilter=@system-service", "ProtectProc=invisible", "ProtectClock=yes",
                 "ProtectHostname=yes", "MemoryDenyWriteExecute=yes", "UMask=0077", "CPUQuota=50%"):
        assert want in props, want
    assert any(p.startswith("TasksMax=") for p in props)
    hidden = next(p for p in props if p.startswith("InaccessiblePaths=")).split("=", 1)[1].split()
    for d in ("-/etc/ops-cart", "-/var/lib/ops-cart", "-/var/log/ops-cart", "-/run/ops-cart", "-/root",
              "-/etc/shadow", "-/opt/pxe/http"):
        assert d in hidden, d


class _Proc:
    """假 Popen: 只看 kill 路径."""
    def __init__(self, alive=True):
        self.alive, self.killed, self.returncode = alive, False, None
        self.stdin = self.stdout = io.BytesIO()

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.killed, self.alive = True, False

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("alive", [True, False])
def test_kill_also_kills_unit_in_background(monkeypatch, alive):
    """root 下 proc 只是 systemd-run 客户端; worker 在另一个单元里, 要 systemctl kill 它. 不阻塞界面线程."""
    seen = []
    monkeypatch.setattr(ip, "_kill_unit", lambda unit: seen.append(unit))
    eng = ip.ProcEngine(_Proc(alive), unit="ops-cart-ime-1-1")
    eng.kill()
    eng.kill()
    assert seen == ["ops-cart-ime-1-1"]                    # 客户端死没死都杀单元, 只杀一次


def test_kill_unit_runs_systemctl_kill_with_timeout_in_thread(monkeypatch):
    calls, threads = [], []
    monkeypatch.setattr(ip.shutil, "which", lambda n: "/usr/bin/systemctl")
    monkeypatch.setattr(ip.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(ip, "_spawn", lambda f: threads.append(f))
    ip._kill_unit("ops-cart-ime-1-1")
    assert calls == []                                     # 不在调用线程里跑
    threads[0]()
    argv, kw = calls[0]
    assert argv == ["/usr/bin/systemctl", "kill", "--signal=KILL", "ops-cart-ime-1-1"]
    assert 0 < kw["timeout"] <= 5


def test_open_engine_passes_unit_from_cmd(fake, monkeypatch):
    seen = []
    monkeypatch.setattr(ip, "_kill_unit", seen.append)
    eng, _ = ip.open_engine(cmd=fake("ok")[:2] + ["--unit=ops-cart-ime-9-9"] + fake("ok")[2:])
    assert eng is not None and eng.unit == "ops-cart-ime-9-9"
    eng.close()
    assert seen == ["ops-cart-ime-9-9"]


def _engine_replying(resp):
    """ProcEngine, call 直接回 resp (不起进程)."""
    eng = ip.ProcEngine(_Proc())
    eng.call = lambda req, timeout=None: resp
    return eng


def test_reply_is_sanitized():
    long = "字" * 200
    eng = _engine_replying({"ok": True, "handled": True, "commit": "你\x00好\x1b[31m\x7f" + long,
                            "snap": ["ni\x07hao", "ni hao\n", ["候\x00选" + long] * 20, 10 ** 9, True, 99]})
    eng.feed("n")
    s = eng.snapshot()
    c = eng.take_commit()
    assert c.startswith("你好[31m") and len(c) == ip.COMMIT_MAX
    assert all(len(x) <= ip.CAND_MAX and "\x00" not in x for x in s.candidates) and len(s.candidates) == 9
    assert s.raw == "nihao" and s.preedit == "ni hao"
    assert 0 <= s.highlighted < len(s.candidates) and 0 <= s.page_no <= ip.PAGE_MAX
    assert ip.LINE_MAX == 64 * 1024


def test_reply_highlight_clamped_when_no_candidates():
    eng = _engine_replying({"ok": True, "handled": True, "commit": None, "snap": ["n", "n", [], -5, True, -3]})
    eng.feed("n")
    assert eng.snapshot().highlighted == 0 and eng.snapshot().page_no == 0


@pytest.mark.parametrize("snap", [["n", "n", [], float("inf"), True, 0], ["n", "n", [], 0, True, float("nan")]])
def test_reply_overflow_is_failure_not_crash(snap):
    eng = _engine_replying({"ok": True, "handled": True, "commit": None, "snap": snap})
    with pytest.raises(OSError):
        eng.feed("n")
    assert eng.proc.killed


def test_call_deeply_nested_reply_is_failure(monkeypatch):
    eng = ip.ProcEngine(_Proc())
    monkeypatch.setattr(eng, "_readline", lambda t: b"[" * 100000 + b"]" * 100000)
    eng.proc.stdin = io.BytesIO()
    with pytest.raises(OSError):
        eng.call({"op": "feed", "ch": "n"})
    assert eng.proc.killed


def test_open_engine_unexpected_exception_kills(monkeypatch, fake):
    killed = []
    real_kill = ip.ProcEngine.kill
    monkeypatch.setattr(ip.ProcEngine, "call", lambda self, req, timeout=None: (_ for _ in ()).throw(RecursionError("deep")))
    monkeypatch.setattr(ip.ProcEngine, "kill", lambda self: (killed.append(1), real_kill(self)))
    eng, why = ip.open_engine(cmd=fake("ok"))
    assert eng is None and "RecursionError" in why and killed


def test_reply_drops_lone_surrogates():
    """json 能解出孤立代理字符 (\\ud800); 插进 Tk 会出错, 清洗时去掉."""
    eng = _engine_replying(json.loads('{"ok": true, "handled": true, "commit": "\\ud800\u4f60", '
                                      '"snap": ["n\\udfff", "n", ["\\ud83d\u597d"], 0, true, 0]}'))
    eng.feed("n")
    assert eng.take_commit() == "你" and eng.snapshot().raw == "n" and eng.snapshot().candidates == ["好"]


def test_alive_follows_child_process(fake):
    eng, why = _open(fake, "ok")
    assert eng is not None, why
    try:
        assert eng.alive() is True
        eng.proc.kill()
        eng.proc.wait(timeout=5)
        assert eng.alive() is False
    finally:
        eng.close()
