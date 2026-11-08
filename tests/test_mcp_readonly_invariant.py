"""#49 审查: 只读档的工具不加锁、不写盘、不写日志. 用真的 Store / LeaseStore / PXE Store (临时目录),
逐个调只读档每个工具, 覆盖坏状态文件、过期租约、读不懂的 PXE 状态; 断言目录里文件内容和 mtime 都不变."""

import fcntl
import json
import os

import pytest

import cart_mcp as cm
import mcp_core as mc
import mcp_kvm
import mcp_label
import mcp_pxe
import mcp_serial as ms
import pxe_req_core as pr
import serial_write_core as sw

MAC = "02:00:00:00:00:01"
T0 = 1_000_000.0


def _snapshot(d):
    out = {}
    for root, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                out[p] = (fh.read(), os.stat(p).st_mtime_ns)
    return out


def _serial_expired(d):
    """槽位 1 的租约在 T0 拿, 60 秒后过期."""
    st = ms.Store(path=os.path.join(d, "serial-write.json"), log_dir=d, clock=lambda: T0, event=None,
                  on_estop_all=None)
    st.run(lambda b, now: b.acquire(1, "agentA", now, ttl=60))


def _kvm_expired_with_pending(d):
    json.dump({"next": 2, "lease": {"token": "t" * 22, "who": "x", "until": T0 - 1, "ttl": 60},
               "pending": [{"id": 1, "kind": "type", "keys": [], "text": "diskpart", "enter": True, "who": "x",
                            "t": T0 - 5, "reason": "r", "danger": ["diskpart"], "line": "diskpart"}],
               "line": "", "sending_until": 0, "history": []}, open(os.path.join(d, "kvm-lease.json"), "w"))


def _pxe_expired(d):
    st = mcp_pxe.Store(path=os.path.join(d, "pxe-req.json"), log_path=os.path.join(d, "pxe-req.log"),
                       clock=lambda: T0 - pr.REQ_TTL_S - 5, event=None, stopped=lambda: False)
    st.run(lambda b, now, s: b.submit("agentA", "rocky", MAC, None, pr.simple_plan("rocky", MAC), now, s))


SCENARIOS = {
    "corrupt": lambda d: [open(os.path.join(d, n), "w").write("{garbage")
                          for n in ("serial-write.json", "kvm-lease.json", "pxe-req.json")],
    "expired": lambda d: (_serial_expired(d), _kvm_expired_with_pending(d), _pxe_expired(d)),
    "empty": lambda d: None,
}


class FakeOcr:
    def __call__(self, dev, lang):
        return [], "卡 (/dev/video0) 当前画面", "fake"


def _readonly_tools(d, events):
    clock = lambda: T0                                              # noqa: E731
    sp = os.path.join(d, "serial-write.json")
    serial = ms.Store(path=sp, log_dir=d, clock=clock, event=lambda *a: events.append(a),
                      on_estop_all=lambda by: events.append(("estop_all", by)))
    kvm = mcp_kvm.LeaseStore(path=os.path.join(d, "kvm-lease.json"), clock=clock, stopped=lambda: False,
                             log_path=os.path.join(d, "kvm.log"), event=lambda *a: events.append(a))
    pxe = mcp_pxe.Store(path=os.path.join(d, "pxe-req.json"), log_path=os.path.join(d, "pxe-req.log"), clock=clock,
                        event=lambda *a: events.append(a), stopped=lambda: mcp_pxe.serial_stopped(sp))
    slots = [{"dev": f"/dev/con-{n}", "present": True, "baud": "9600", "count": 0} for n in range(1, 5)]
    tools = cm.build_tools(slots=lambda: slots, tail=lambda dev, n: "<SW1>", snap=lambda dev: (b"\x89PNG", "卡"),
                           state=lambda: {"ts": T0}, pxe=lambda: {"mode": "rescue"}, now=clock,
                           writer=lambda: ms.writers(serial, readonly=True))
    tools += ms.build_tools(serial, slots=lambda: slots, tail=lambda dev, n: "", send=None, readonly=True)
    tools += mcp_pxe.build_tools(pxe, clients=lambda: [], readonly=True)
    tools += mcp_kvm.build_tools(kvm, ports=lambda: {"SN1": "/dev/kvm-x"}, opener=None, ocr=FakeOcr(), wol=None,
                                 broker_up=lambda p: False, readonly=True)
    tools += mcp_label.build_label_tools(write=None, status=lambda: 0, abbrev=lambda: ({}, "x"), readonly=True)
    return tools


ARGS = {"read_output": {"slot": 1}, "gate_allowlist": {"slot": 1}, "label_preview": {"kind": "text", "labels": [{"big": "A", "small": "B"}]},
        "label_abbrev_propose": {"category": "vendor", "entries": {"x": "X"}}}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_readonly_tools_never_lock_or_write(tmp_path, monkeypatch, scenario):
    d = str(tmp_path)
    SCENARIOS[scenario](d)
    before = _snapshot(d)
    events, locks = [], []
    real_flock = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: locks.append(op) or real_flock(fd, op))
    tools = _readonly_tools(d, events)
    assert {t.name for t in tools} == {
        "list_threads", "read_output", "screenshot", "cart_status", "pxe_status", "pending_list", "pxe_clients",
        "pxe_requests", "kvm_status", "screen_ocr", "label_preview", "label_printer_status", "label_abbrev_propose", "gate_allowlist"}
    server = ms.Server(tools, profile=mc.READONLY)
    for t in tools:
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": t.name, "arguments": ARGS.get(t.name, {})}})
        assert "result" in r, (t.name, r)
    assert locks == [], "只读档不该拿锁"
    assert events == [], "只读档不该写事件"
    assert _snapshot(d) == before, "只读档不该改任何文件"


def test_readonly_views_still_report_state(tmp_path):
    d = str(tmp_path)
    SCENARIOS["corrupt"](d)
    tools = {t.name: t for t in _readonly_tools(d, [])}
    serial = json.loads(tools["pending_list"].fn({})[0]["text"])
    assert all(s["estop"] for s in serial["slots"])                   # 读不懂 = 全部急停 (视图里), 但不落盘
    assert json.loads(tools["pxe_requests"].fn({})[0]["text"]) == {"pending": [], "history": []}


def test_readonly_pxe_view_never_has_code(tmp_path):
    d = str(tmp_path)
    st = mcp_pxe.Store(path=os.path.join(d, "pxe-req.json"), log_path=os.path.join(d, "l"), clock=lambda: T0,
                       event=None, stopped=lambda: False)
    st.run(lambda b, now, s: b.submit("agentA", "rocky", MAC, None, pr.simple_plan("rocky", MAC), now, s))
    raw = open(os.path.join(d, "pxe-req.json")).read()
    code = json.loads(raw)["reqs"][0]["code"]
    tool = next(t for t in mcp_pxe.build_tools(st, clients=lambda: [], readonly=True) if t.name == "pxe_requests")
    text = tool.fn({})[0]["text"]
    view = json.loads(text)
    assert len(view["pending"]) == 1
    assert code not in text and "code" not in view["pending"][0] and "digest" not in view["pending"][0]


def test_readonly_serial_view_expires_lease_in_memory(tmp_path):
    d = str(tmp_path)
    _serial_expired(d)
    serial = ms.Store(path=os.path.join(d, "serial-write.json"), log_dir=d, clock=lambda: T0 + 120, event=None,
                      on_estop_all=None)
    assert ms.writers(serial, readonly=True)[1] is None
    serial0 = ms.Store(path=os.path.join(d, "serial-write.json"), log_dir=d, clock=lambda: T0, event=None,
                       on_estop_all=None)
    assert ms.writers(serial0, readonly=True)[1] == "agentA"


def test_readonly_builders_have_no_act_tools(tmp_path):
    d = str(tmp_path)
    tools = _readonly_tools(d, [])
    assert all(t.kind == mc.READONLY for t in tools)


def test_real_tools_readonly_wires_readonly_builders(monkeypatch):
    seen = []
    for mod, fn in ((ms, "real_tools"), (mcp_pxe, "real_tools"), (mcp_kvm, "real_tools")):
        orig = getattr(mod, fn)
        monkeypatch.setattr(mod, fn, lambda *a, _o=orig, _m=mod, **k: seen.append((_m.__name__, k.get("readonly")))
                            or _o(*a, **k))
    orig_label = mcp_label.real_label_tools
    monkeypatch.setattr(mcp_label, "real_label_tools",
                        lambda **k: seen.append(("mcp_label", k.get("readonly"))) or orig_label(**k))
    cm.real_tools("readonly")
    assert sorted(seen) == [("mcp_kvm", True), ("mcp_label", True), ("mcp_pxe", True), ("mcp_serial", True)]


# ------------------------------------------------------------ destructiveHint 别默认 False

def test_act_tools_destructive_hint_is_honest():
    specs = {t.name: t.spec()["annotations"] for t in cm.real_tools("act")}
    for name in ("send_command", "send_key", "kvm_key", "kvm_type", "pxe_request", "pxe_retire"):
        assert specs[name]["destructiveHint"] is True, name
    assert "destructiveHint" not in specs["wol_send"]                   # 不写 = 客户端按可能有破坏处理
    # 明确不算破坏: 急停只停不动设备 (标破坏客户端会先问人, 急停不能等); 打印最坏废一张; 租约只记账
    for name in ("estop", "label_print", "lease_acquire", "lease_renew", "lease_release",
                 "kvm_lease_acquire", "kvm_lease_renew", "kvm_lease_release"):
        assert specs[name]["destructiveHint"] is False, name
    assert {n for n, a in specs.items() if a.get("destructiveHint") is False and not a["readOnlyHint"]} == {
        "estop", "label_print", "lease_acquire", "lease_renew", "lease_release",
        "kvm_lease_acquire", "kvm_lease_renew", "kvm_lease_release"}
    for t in cm.real_tools("act"):
        if t.kind == mc.READONLY:
            assert specs[t.name]["destructiveHint"] is False, t.name


# ------------------------------------------------------------ 键鼠状态不让代理 open 设备

def test_broker_probe_uses_status_op_not_open():
    import shutil
    import socket
    import tempfile
    import threading
    tmp = tempfile.mkdtemp()                                        # AF_UNIX 路径有长度上限, 不用 pytest 的长目录
    sock = os.path.join(tmp, "k.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sock)
    srv.listen(1)
    got = []

    def serve():
        c, _ = srv.accept()
        got.append(json.loads(c.makefile("rb").readline()))
        c.sendall(b'{"ok": true, "status": {}}\n')
        c.close()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    assert mcp_kvm.broker_probe(sock_path=sock) is True
    th.join(5)
    srv.close()
    assert got[0]["op"] == "status" and "port" not in got[0]
    assert mcp_kvm.broker_probe(sock_path=os.path.join(tmp, "nope.sock")) is False
    shutil.rmtree(tmp)


# ------------------------------------------------------------ 标签机: 查状态和打印进程间串行

def test_printer_lock_serializes_across_processes(tmp_path):
    p = str(tmp_path / "printer.lock")
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                                  # 模拟别的进程正在打印
    try:
        with pytest.raises(mc.ToolError, match="忙"):
            with mcp_label.printer_lock(p, wait_s=0.2):
                pass
    finally:
        os.close(fd)
    with mcp_label.printer_lock(p, wait_s=0.2):
        pass


def test_label_status_and_print_take_printer_lock():
    held, log = [], []

    class XL:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            held.pop()

    tools = {t.name: t for t in mcp_label.build_label_tools(
        write=lambda data: log.append(("write", bool(held))), status=lambda: log.append(("status", bool(held))) or 0,
        abbrev=lambda: ({}, "x"), xlock=XL)}
    tools["label_printer_status"].fn({})
    tools["label_print"].fn({"kind": "text", "labels": [{"big": "A", "small": "B"}]})
    assert log == [("status", True), ("status", True), ("write", True)]


# ------------------------------------------------------------ 第二轮审查: 接线层 + 设备锁白名单 (#49)
#
# 只读档允许的写 (白名单, 文档第十六节同列): mcp-audit.log (审计, 本测试不接); 设备互斥锁 printer.lock / snap.lock
# (flock + snap.lock 里记上次抓帧时刻); /run/ops-cart 下的临时 PNG (抓完即删); 往打印机写 ESC !? (这里用假件).
# 除此之外: 不碰 Store.run / LeaseStore._tx / PXE Store.run, 不写事件, 不改任何状态文件.

ALLOWED_LOCKS = {"printer.lock", "snap.lock"}
ALLOWED_WRITES = ALLOWED_LOCKS | {"printer-status.json"}     # 只读档查标签机状态的跨进程缓存 (#60 第三轮, root 0600)


def _wire_real_tools(d, monkeypatch, events, forbidden):
    """把 cm.real_tools 用到的真机读数换成 tmp 下的真 Store 和假设备; 写路径一律打桩记账并抛异常."""
    import cart_ocr
    import cart_snap
    import dash_core
    import kvm_broker
    import kvm_core
    clock = lambda: T0                                              # noqa: E731
    sp = os.path.join(d, "serial-write.json")
    RS, RP, RL = ms.Store, mcp_pxe.Store, mcp_kvm.LeaseStore
    monkeypatch.setattr(ms, "Store", lambda: RS(path=sp, log_dir=d, clock=clock, event=lambda *a: events.append(a),
                                                on_estop_all=lambda by: events.append(("estop_all", by))))
    monkeypatch.setattr(mcp_pxe, "Store", lambda: RP(path=os.path.join(d, "pxe-req.json"),
                                                     log_path=os.path.join(d, "pxe-req.log"), clock=clock,
                                                     event=lambda *a: events.append(a),
                                                     stopped=lambda: mcp_pxe.serial_stopped(sp)))
    monkeypatch.setattr(mcp_kvm, "LeaseStore", lambda: RL(path=os.path.join(d, "kvm-lease.json"), clock=clock,
                                                          stopped=lambda: False, log_path=os.path.join(d, "kvm.log"),
                                                          event=lambda *a: events.append(a)))

    def spy(name):
        def f(*a, **k):
            forbidden.append(name)
            raise RuntimeError(f"只读档不该走 {name}")
        return f
    monkeypatch.setattr(RS, "run", spy("serial Store.run"))
    monkeypatch.setattr(RP, "run", spy("pxe Store.run"))
    monkeypatch.setattr(RL, "_tx", spy("LeaseStore._tx"))
    monkeypatch.setattr(kvm_broker, "BrokerCh", spy("BrokerCh (open 设备)"))

    slots = [{"dev": f"/dev/con-{n}", "present": True, "baud": "9600", "count": 0} for n in range(1, 5)]
    monkeypatch.setattr(cm, "real_slots", lambda: slots)
    monkeypatch.setattr(ms, "real_slots", lambda: slots)
    monkeypatch.setattr(cm, "conbroker_tail", lambda dev, n: "<SW1>")
    monkeypatch.setattr(cm, "read_pxe", lambda: {"mode": "rescue"})
    monkeypatch.setattr(dash_core, "read", lambda: {"ts": T0})
    monkeypatch.setattr(kvm_core, "ports", lambda: {"SN1": "/dev/kvm-x"})
    monkeypatch.setenv("OPS_KVM_SOCK", os.path.join(d, "no-broker.sock"))
    monkeypatch.setattr(mcp_label, "real_status", lambda: 0)
    monkeypatch.setattr(mcp_label, "PRINTER_LOCK", os.path.join(d, "printer.lock"))
    monkeypatch.setattr(mcp_label, "STATUS_CACHE", os.path.join(d, "printer-status.json"))
    monkeypatch.setattr(cart_snap, "SNAP_LOCK", os.path.join(d, "snap.lock"))
    monkeypatch.setattr(cart_snap, "RUN_DIR", d)
    monkeypatch.setattr(cart_snap, "running_mpv_device", lambda: None)
    monkeypatch.setattr(cart_snap, "default_device", lambda: "/dev/video0")

    def fake_ffmpeg(dev, out):
        with open(out, "wb") as f:
            f.write(b"\x89PNG")
        return True, ""
    monkeypatch.setattr(cart_snap, "via_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(cart_ocr, "ocr_best", lambda path, lang: ([], "fake"))


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_real_tools_readonly_end_to_end(tmp_path, monkeypatch, scenario):
    d = str(tmp_path)
    SCENARIOS[scenario](d)
    events, forbidden, locked = [], [], []
    _wire_real_tools(d, monkeypatch, events, forbidden)
    before = _snapshot(d)
    real_flock = fcntl.flock

    def flock(fd, op):
        if op & (fcntl.LOCK_EX | fcntl.LOCK_SH):
            locked.append(os.fstat(fd).st_ino)
        return real_flock(fd, op)
    monkeypatch.setattr(fcntl, "flock", flock)

    monkeypatch.setattr(mc, "RO_BURST", 100)                        # 这里一口气调 15 次, 不测限速 (test_mcp_profiles 测)
    server = cm.make_server("readonly", None)
    assert set(server.tools) == {
        "list_threads", "read_output", "screenshot", "cart_status", "pxe_status", "pending_list", "pxe_clients",
        "pxe_requests", "kvm_status", "screen_ocr", "label_preview", "label_printer_status", "label_abbrev_propose", "gate_allowlist"}

    def call(name, args=None):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args or ARGS.get(name, {})}})["result"]

    results = {name: call(name) for name in sorted(server.tools)}
    again = call("screenshot")                                      # 只读档冷却 (比动手档长): 紧接着再截就拒
    again_ocr = call("screen_ocr")                                  # OCR 也走同一条限速
    assert forbidden == [], forbidden
    assert events == []
    for name in ("list_threads", "pending_list", "kvm_status", "pxe_requests", "label_printer_status"):
        assert results[name]["isError"] is False, (name, results[name])
    assert json.loads(results["kvm_status"]["content"][0]["text"])["broker"] is False
    assert again["isError"] is True and "刚抓过" in again["content"][0]["text"]
    assert again_ocr["isError"] is True and "刚抓过" in again_ocr["content"][0]["text"]
    after = _snapshot(d)
    inodes = {os.stat(p).st_ino: os.path.basename(p) for p in after}
    assert {inodes.get(i, "?") for i in locked} == ALLOWED_LOCKS      # 只拿这两把设备锁, 别的锁一把都不拿
    changed = {os.path.basename(p) for p in set(after) | set(before) if after.get(p) != before.get(p)}
    assert changed <= ALLOWED_WRITES, changed                       # 临时 PNG 抓完就删, 不留
    assert "printer-status.json" in changed                         # 缓存确实写到了打桩的路径 (不是推车真路径)
    assert not [p for p in after if p.endswith(".png")]


def test_readonly_pxe_requests_shows_expiry_in_history(tmp_path):
    d = str(tmp_path)
    _pxe_expired(d)
    st = mcp_pxe.Store(path=os.path.join(d, "pxe-req.json"), log_path=os.path.join(d, "l"), clock=lambda: T0,
                       event=None, stopped=lambda: False)
    before = _snapshot(d)
    view = st.public_view()
    assert view["pending"] == [] and [h["status"] for h in view["history"]] == ["expired"]
    assert _snapshot(d) == before


# ------------------------------------------------------------ 标签机锁打不开: 不拒打, 退回进程内锁并记一条

def test_printer_lock_unopenable_falls_back(tmp_path):
    notes = []
    with mcp_label.printer_lock(str(tmp_path / "no" / "such" / "printer.lock"), note=notes.append):
        pass
    assert notes and "printer.lock" in notes[0]
    log = []
    bad = str(tmp_path / "missing" / "printer.lock")
    tools = {t.name: t for t in mcp_label.build_label_tools(
        write=lambda data: log.append("write"), status=lambda: 0, abbrev=lambda: ({}, "x"),
        xlock=lambda: mcp_label.printer_lock(bad, note=notes.append))}
    tools["label_print"].fn({"kind": "text", "labels": [{"big": "A", "small": "B"}]})
    assert log == ["write"]
