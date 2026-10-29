import json

import lamps_core as lc


def test_activity_needs_a_change_after_first_sample():
    a = lc.Activity(hold=1.0)
    assert a.update({"con-1": 100}, now=0.0) == set()          # 第一次只是基线
    assert a.update({"con-1": 100}, now=0.2) == set()
    assert a.update({"con-1": 140}, now=0.4) == {"con-1"}
    assert a.update({"con-1": 140}, now=1.3) == {"con-1"}      # 1 s 内还算活动
    assert a.update({"con-1": 140}, now=1.5) == set()


def test_activity_ignores_missing_counters():
    a = lc.Activity(hold=1.0)
    a.update({"lan1": 5, "kvm": None}, now=0.0)
    assert a.update({"lan1": None, "kvm": 3}, now=0.2) == set()
    assert a.update({"lan1": 9, "kvm": 4}, now=0.4) == {"lan1", "kvm"}


def test_blink_phase():
    assert lc.blink_on(0.0, period=0.2) and not lc.blink_on(0.25, period=0.2) and lc.blink_on(0.45, period=0.2)


def test_lamp_view_colors():
    idle = lc.lamp_view("OK", active=False, on=True)
    lit = lc.lamp_view("OK", active=True, on=True)
    dark = lc.lamp_view("OK", active=True, on=False)
    assert idle["bar"] == lit["bg"] == lc.HEALTH_COLOR["OK"]
    assert dark == idle and idle["bg"] == lc.BODY
    assert lc.lamp_view("FAIL", False, True)["bar"] == lc.HEALTH_COLOR["FAIL"]
    assert lc.lamp_view(None, True, True)["bg"] == lc.EMPTY_BODY   # 没东西的槽不闪


def test_health_of_maps_selftest_rows_by_label():
    rows = [{"label": "LAN2", "state": "OK"}, {"label": "WIFI", "state": "--"},
            {"label": "CH9329", "state": "WARN"}]
    assert lc.health_of(rows, "LAN2") == "OK"
    assert lc.health_of(rows, "WIFI") is None          # "--" = 没在用, 灰
    assert lc.health_of(rows, "CH9329") == "WARN"
    assert lc.health_of(rows, "NOPE") is None
    assert lc.health_of(None, "LAN2") is None          # 第一轮自检还没回来


def test_net_bytes_parses_proc_net_dev(tmp_path):
    p = tmp_path / "dev"
    p.write_text("Inter-|   Receive  |  Transmit\n"
                 " face |bytes packets|bytes packets\n"
                 "  eno1: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n"
                 "wlp4s0:20 1 0 0 0 0 0 0 30 1 0 0 0 0 0 0\n")
    assert lc.net_bytes(str(p)) == {"eno1": 1500, "wlp4s0": 50}
    assert lc.net_bytes(str(tmp_path / "none")) == {}


def _broker(tmp_path, name, pid, baud=9600):
    safe = name.replace("/", "_")
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "run" / f"{safe}.json").write_text(json.dumps({"name": name, "baud": baud, "pid": pid}))
    (tmp_path / "logs" / f"{safe}.log").write_bytes(b"x" * 42)


def test_slot_status(tmp_path):
    _broker(tmp_path, "/dev/con-2", pid=111, baud=115200)
    _broker(tmp_path, "/dev/con-3", pid=222)
    exists = {"/dev/con-1", "/dev/con-2", "/dev/con-3"}.__contains__
    alive = {111}.__contains__
    s = lc.slot_status(str(tmp_path), ["/dev/con-1", "/dev/con-2", "/dev/con-3", "/dev/con-4"],
                       exists=exists, alive=alive)
    assert s[0] == {"dev": "/dev/con-1", "present": True, "baud": None, "count": None}
    assert s[1] == {"dev": "/dev/con-2", "present": True, "baud": 115200, "count": 42}
    assert s[2]["baud"] is None and s[2]["count"] == 42     # 残留状态文件, 进程已死
    assert s[3]["present"] is False


def test_slot_view_text_and_health():
    assert lc.slot_view(1, {"present": False, "baud": None}) == ("1\n未插", None)
    assert lc.slot_view(2, {"present": True, "baud": None}) == ("2\n空闲", "OK")
    assert lc.slot_view(3, {"present": True, "baud": 9600}) == ("3\n9600", "OK")


# ------------------------------------------------------------ 响铃: 设备回 ^G -> 操作头亮红 (可选主板蜂鸣)

def test_bell_tail_counts_new_bel_bytes_only(tmp_path):
    log = tmp_path / "c.log"
    log.write_bytes(b"old\x07\x07")                 # 第一次见: 只记位置, 不数旧的
    t = lc.BellTail()
    assert t.read("con-1", str(log)) == 0
    with open(log, "ab") as f:
        f.write(b"Username:abc\x07\x1b[1D \x07")
    assert t.read("con-1", str(log)) == 2
    assert t.read("con-1", str(log)) == 2          # 没新内容
    log.write_bytes(b"\x07")                        # 被截断/轮转: 重新从末尾记
    assert t.read("con-1", str(log)) == 2
    assert t.read("con-1", str(tmp_path / "none")) == 2


def test_lamp_view_bell_is_solid_red():
    v = lc.lamp_view("OK", active=True, on=False, bell=True)
    assert v["bg"] == lc.HEALTH_COLOR["FAIL"] and v["fg"] == lc.LIT_FG


def test_tone_events_on_then_off():
    on, off = lc.tone_events(1000, now=12.5)
    assert len(on) == len(off) == 24                 # x86_64 struct input_event
    import struct
    assert struct.unpack("llHHi", on)[2:] == (0x12, 0x02, 1000)
    assert struct.unpack("llHHi", off)[2:] == (0x12, 0x02, 0)


def test_beeper_rate_limited():
    played = []
    b = lc.Beeper(play=played.append, gap=0.3)
    b.ring(now=1.0); b.ring(now=1.1); b.ring(now=1.35)
    assert played == [1.0, 1.35]


def test_sample_reports_bell_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "net_bytes", lambda: {})
    monkeypatch.setattr(lc, "SLOTS", ["/dev/con-1"])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "_dev_con-1.log").write_bytes(b"x")
    bells, rung = lc.BellTail(), []
    lc.sample(str(tmp_path), 0, bells=bells, on_bell=lambda: rung.append(1))
    with open(tmp_path / "logs" / "_dev_con-1.log", "ab") as f:
        f.write(b"\x07")
    _slots, counters = lc.sample(str(tmp_path), 0, bells=bells, on_bell=lambda: rung.append(1))
    assert counters["bell:con-1"] == 1 and rung == [1]


def test_slot_for_lamp_gives_device_path():
    """执行器条的键是 con-3, 面板要的是 /dev/con-3 (issue #5 的回归)."""
    assert lc.slot_for_lamp("con-1") == "/dev/con-1"
    assert lc.slot_for_lamp("con-4") == "/dev/con-4"
    # 面板真正能用它选中
    import serial_panel as sp
    assert sp.select_port(["/dev/con-1", "/dev/con-3"], lc.slot_for_lamp("con-3")) == ("/dev/con-3", True)
