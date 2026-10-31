"""dash_core 测试: 采集兜底 / 原子落盘 / 过期判定 / 锁屏版面. 不碰真机."""

import json
import time

import pytest

import confirm_core
import dash_core as dc


def _ctx(**over):
    ctx = {"safe": lambda f, d=None: (f() if f else d), "now": lambda: 1000.0, "seq": 7,
           "net": lambda: "on", "rows": lambda: [{"label": "LAN2", "state": "OK"},
                                                 {"label": "FAN", "state": "FAIL"}],
           "temps": lambda: [("CPU", 45)], "fans": lambda: [], "uptime": lambda: "1d",
           "lamps": lambda: {"con-1": 3, "kvm": 0}, "kiosk": lambda: {"locked": False},
           "todo": lambda: ["换线"], "dream": lambda: "昨夜整理 10-28 · 修了 PXE"}
    ctx.update(over)
    return ctx


def test_collect_shape():
    s = dc.collect(_ctx())
    assert s["ts"] == 1000.0 and s["seq"] == 7 and s["net"] == "on"
    assert s["lamps"]["con-1"] == 3
    assert json.dumps(s, ensure_ascii=False)      # 可序列化


def test_collect_survives_broken_collector():
    def boom():
        raise OSError("device gone")
    s = dc.collect(_ctx(**{"net": boom, "todo": boom}))
    assert s["net"] == "down" and s["todo"] == []


def test_write_is_atomic_and_readable(tmp_path):
    p = tmp_path / "state.json"
    dc.write(str(p), dc.collect(_ctx()))
    assert dc.read(str(p))["seq"] == 7
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]   # 不留临时文件


def test_read_missing_or_broken_is_none(tmp_path):
    assert dc.read(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert dc.read(str(bad)) is None


def test_stale_rules():
    assert dc.stale(None)
    assert dc.stale({})
    assert dc.stale({"ts": "x"})
    assert not dc.stale({"ts": 1000.0}, now=1002.5)
    assert dc.stale({"ts": 1000.0}, now=1004.0)          # > 3 s


def test_read_todo_skips_done_and_headers(tmp_path):
    p = tmp_path / "todo.md"
    p.write_text("# 待办\n\n- [ ] 换 LAN2 的线\n- [x] 打标签\n* [ ] 补 CART-LOG\n普通一行\n")
    assert dc.read_todo(str(p)) == ["换 LAN2 的线", "补 CART-LOG"]
    assert dc.read_todo(str(tmp_path / "nope.md")) == []


def test_read_todo_limit(tmp_path):
    p = tmp_path / "todo.md"
    p.write_text("\n".join(f"- [ ] 第{i}件" for i in range(10)))
    assert len(dc.read_todo(str(p), limit=3)) == 3


def test_layout_locked_hides_todo():
    snap = dc.collect(_ctx())
    v = dc.layout_view(snap, locked=True)
    assert v["locked"] and v["todo"] == []
    c = v["checks"]
    assert (c["ok"], c["warn"], c["fail"]) == (1, 0, 1)
    assert [p["label"] for p in c["problems"]] == ["FAN"]


def test_layout_unlocked_keeps_todo_and_reads_kiosk_flag():
    snap = dc.collect(_ctx(**{"kiosk": lambda: {"locked": True}}))
    v = dc.layout_view(snap)
    assert v["locked"] is True and v["todo"] == []
    v2 = dc.layout_view(dc.collect(_ctx()))
    assert v2["locked"] is False and v2["todo"] == ["换线"]


def test_layout_marks_stale_snapshot():
    snap = dict(dc.collect(_ctx()), ts=time.time())      # ts 用"现在", 不然按 1970 算
    old = dict(snap, ts=time.time() - 10)
    assert dc.layout_view(old)["stale"]


# ---- 采集节流: 快照照样每 200 ms 写, 但慢的采集项按自己的节奏跑 (ping/自检别每轮都跑)

class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_every_caches_within_period():
    clk, calls = _Clock(), []
    get = dc.every(10, lambda: calls.append(1) or len(calls), clock=clk)
    assert get() == 1
    clk.t += 9.9
    assert get() == 1 and len(calls) == 1


def test_every_refreshes_after_period():
    clk, calls = _Clock(), []
    get = dc.every(10, lambda: calls.append(1) or len(calls), clock=clk)
    get()
    clk.t += 10
    assert get() == 2


def test_every_failure_waits_a_period_before_retry():
    clk, calls = _Clock(), []

    def boom():
        calls.append(1)
        raise OSError("ping timeout")
    get = dc.every(5, boom, clock=clk)
    for _ in range(3):
        try:
            get()
        except OSError:
            pass
    assert len(calls) == 1            # 坏了也别每 200 ms 再撞一次超时
    clk.t += 5
    try:
        get()
    except OSError:
        pass
    assert len(calls) == 2


def test_default_ctx_throttles_selftest_and_net(monkeypatch):
    import kiosk_core as kc
    n = {"rows": 0, "net": 0}
    monkeypatch.setattr(kc, "selftest_plan_read", lambda *a, **k: [])
    monkeypatch.setattr(kc, "selftest_context", lambda: {})
    monkeypatch.setattr(kc, "selftest_run", lambda *a: n.__setitem__("rows", n["rows"] + 1) or [])
    monkeypatch.setattr(kc, "net_probe", lambda: n.__setitem__("net", n["net"] + 1) or ("on", ""))
    clk = _Clock()
    ctx = dc.default_ctx(clock=clk)
    for _ in range(15):               # 3 秒, 200 ms 一轮
        ctx["rows"]()
        ctx["net"]()
        clk.t += 0.2
    assert n == {"rows": 1, "net": 1}
    assert dc.PERIODS["rows"] >= 5 and dc.PERIODS["net"] >= 2


# ---- 副屏版面 v2: 执行器两排 / 自检只算 WARN FAIL / 锁屏公告 / 开机时长

def _snap(**over):
    s = {"ts": 1000.0, "net": "on", "lan2": "192.0.2.5", "uptime": "up 1 day, 3 hours, 4 minutes",
         "rows": [{"label": "LAN2", "state": "OK", "detail": "192.0.2.5"},
                  {"label": "WIFI", "state": "WARN", "detail": "lab 78% IN USE"},
                  {"label": "CAPTURE 1", "state": "--", "detail": "NO SIGNAL video0"},
                  {"label": "CH9329", "state": "OK", "detail": "1 ONLINE"},
                  {"label": "PXE SERVER", "state": "FAIL", "detail": "DEAD"}],
         "slots": [{"dev": "/dev/con-1", "present": True, "baud": 9600, "count": 10},
                   {"dev": "/dev/con-2", "present": True, "baud": None, "count": None},
                   {"dev": "/dev/con-3", "present": False, "baud": None, "count": None},
                   {"dev": "/dev/con-4", "present": True, "baud": None, "count": None}],
         "lamps": {"con-1": 10, "kvm": 0, "pxe": 5, "net:eno1": 1, "net:enp3s0": 2, "net:wlp4s0": 0},
         "notice": "请前往 B101 上课", "todo": ["换线"], "dream": "昨夜整理 10-30 · 修了 PXE"}
    s.update(over)
    return s


def test_checks_count_only_warn_and_fail_as_problems():
    c = dc.layout_view(_snap())["checks"]
    assert (c["ok"], c["warn"], c["fail"]) == (2, 1, 1)     # "--" 是待机, 不算异常
    assert [p["label"] for p in c["problems"]] == ["PXE SERVER", "WIFI"]   # 故障排前面
    assert c["problems"][1]["detail"] == "lab 78% IN USE"


def test_heads_show_baud_idle_or_unplugged():
    heads = dc.layout_view(_snap(), active={"con-1"})["heads"]
    assert [(h["n"], h["text"], h["health"]) for h in heads] == [
        (1, "9600", "OK"), (2, "空闲", "OK"), (3, "未插", None), (4, "空闲", "OK")]
    assert heads[0]["active"] and not heads[1]["active"]


def test_lower_row_follows_selftest_health_and_activity():
    lower = dc.layout_view(_snap(), active={"pxe"})["lower"]
    assert [x["name"] for x in lower] == ["键鼠", "PXE", "LAN1", "LAN2", "Wi-Fi"]
    by = {x["name"]: x for x in lower}
    assert by["PXE"]["health"] == "FAIL" and by["PXE"]["active"]
    assert by["Wi-Fi"]["health"] == "WARN" and by["LAN2"]["health"] == "OK"


def test_locked_view_is_a_guide_board():
    v = dc.layout_view(_snap(), locked=True)
    assert v["notice"] == "请前往 B101 上课"
    assert v["todo"] == [] and v["dream"] == ""            # 锁屏只留时间/灯/公告


def test_unlocked_view_hides_notice_and_keeps_todo():
    v = dc.layout_view(_snap())
    assert v["notice"] == "" and v["todo"] == ["换线"] and v["dream"]


def test_clock_is_wall_time_even_when_snapshot_is_stale():
    import time as _t
    now = _t.mktime((2026, 10, 30, 13, 5, 0, 0, 0, -1))
    v = dc.layout_view(_snap(ts=1.0), now=now)        # 快照停了, 钟照样走
    assert v["clock"] == "13:05" and v["date"] == "10月30日 周五" and v["stale"]


def test_uptime_short_form():
    assert dc.short_uptime("up 1 day, 3 hours, 4 minutes") == "1天3小时"
    assert dc.short_uptime("up 2 hours, 5 minutes") == "2小时5分"
    assert dc.short_uptime("up 7 minutes") == "7分"
    assert dc.short_uptime("") == ""


# ---- 和 kiosk 同步: conbroker 目录 / 键鼠计数 / 锁屏 (kiosk.json)

def _stub_kc(monkeypatch):
    import kiosk_core as kc
    for name in ("selftest_plan_read", "selftest_context", "selftest_run", "net_probe"):
        monkeypatch.setattr(kc, name, lambda *a, **k: [])


def test_default_ctx_reads_conbroker_dir_like_kiosk(monkeypatch):
    import lamps_core
    import serial_core as sc
    _stub_kc(monkeypatch)
    seen = {}
    monkeypatch.setattr(sc, "serial_conf_read", lambda *a: {"user": "ops", "data_dir": "/tmp/cb"})
    monkeypatch.setattr(lamps_core, "slot_status", lambda ddir, *a, **k: seen.setdefault("slots", ddir))
    monkeypatch.setattr(lamps_core, "sample",
                        lambda ddir, kvm, **k: seen.update(lamps=ddir, kvm=kvm) or ([], {}))
    monkeypatch.setattr(dc, "read_kiosk", lambda *a: {"locked": False, "kvm": 5})
    ctx = dc.default_ctx()
    ctx["slots"]()
    ctx["lamps"]()
    assert seen == {"slots": "/tmp/cb", "lamps": "/tmp/cb", "kvm": 5}   # 键鼠计数从 kiosk 回写里读


def test_kiosk_reporter_writes_only_on_change(tmp_path):
    p = str(tmp_path / "kiosk.json")
    rep = dc.KioskReporter(p, pid=42)
    assert rep.update(locked=True, kvm=3) is True
    assert dc.read_kiosk(p) == {"locked": True, "kvm": 3, "pid": 42}
    assert rep.update(locked=True, kvm=3) is False         # 没变不写盘 (200 ms 一次的采样线程里调)
    assert rep.update(locked=False, kvm=3) is True
    assert dc.read_kiosk(p)["locked"] is False


def test_kiosk_reporter_swallows_write_errors(tmp_path):
    (tmp_path / "f").write_text("")                         # 父路径是个文件, 怎么都写不进去
    rep = dc.KioskReporter(str(tmp_path / "f" / "kiosk.json"))
    assert rep.update(locked=True, kvm=0) is False         # /run/ops-cart 没建好也不能拖垮 kiosk


# ---- 副屏 UPS 一行

def test_ups_line_on_mains():
    u = dc.layout_view(_snap(ups={"state": "fully-charged", "percent": 100, "load": 18,
                                  "status": "OL"}))["ups"]
    assert u == {"text": "电池 100% · 负载 18%", "level": "ok"}


def test_ups_line_on_battery_uses_hid_status_before_upower_catches_up():
    # upower 的 discharging 要滞后一两分钟; hidraw 的 OB 是即时的
    u = dc.layout_view(_snap(ups={"state": "fully-charged", "percent": 85, "status": "OB DISCHRG",
                                  "runtime": 1630}))["ups"]
    assert u == {"text": "市电断开 · 电池 85% · 剩 27 分", "short": "断电 · 85% · 剩 27 分", "level": "warn"}


def test_ups_line_back_on_mains_trusts_hid_ol_over_lagging_upower():
    u = dc.layout_view(_snap(ups={"state": "discharging", "percent": 10, "status": "OL CHRG"}))["ups"]
    assert u == {"text": "电池 10%", "level": "ok"}


def test_ups_line_low_battery_is_bad_and_missing_ups_is_dim():
    u = dc.layout_view(_snap(ups={"state": "discharging", "percent": 12}))["ups"]
    assert u["level"] == "bad" and u["text"].startswith("市电断开 · 电池 12%")
    assert dc.layout_view(_snap(ups=None))["ups"] == {"text": "UPS 未连接", "level": "dim"}


# ---- 两段式低电量关机 (#4): 副屏跟车机同一个 ups_core.shutdown_stage, 倒计时起点由 kiosk 回写

OB22 = {"state": "discharging", "percent": 22, "status": "OB DISCHRG", "runtime": 400}


def test_kiosk_reporter_writes_ups_countdown_start(tmp_path):
    p = str(tmp_path / "kiosk.json")
    rep = dc.KioskReporter(p, pid=4242)
    assert rep.update(locked=False, kvm=0, ups_since=1000.0) is True
    assert dc.read_kiosk(p) == {"locked": False, "kvm": 0, "pid": 4242, "ups_since": 1000.0}
    assert rep.update(locked=False, kvm=0, ups_since=1000.0) is False
    assert rep.update(locked=False, kvm=0) is True                 # 取消倒计时: 键去掉
    assert dc.read_kiosk(p) == {"locked": False, "kvm": 0, "pid": 4242}


def test_ups_warn_is_amber_band_not_red():
    v = dc.layout_view(_snap(ups=OB22), now=1000.0)
    assert v["ups"]["level"] == "warn"                             # 20% 以下不再直接变红
    assert v["ups_alert"] == {"text": "电池约剩 6 分钟，到 15% 自动关机，请插回市电",
                              "short": "电池约剩 6 分钟 · 15% 自动关机", "level": "warn"}


def test_ups_no_alert_on_mains_or_plenty_left():
    assert dc.layout_view(_snap(ups=dict(OB22, percent=80, runtime=3000)))["ups_alert"] is None
    assert dc.layout_view(_snap(ups=dict(OB22, status="OL CHRG")))["ups_alert"] is None
    assert dc.layout_view(_snap(ups=None))["ups_alert"] is None


def test_ups_countdown_uses_kiosk_start_and_wall_clock():
    low = dict(OB22, percent=15)
    v = dc.layout_view(_snap(ups=low, kiosk={"locked": True, "ups_since": 1000.0}), now=1012.0)
    assert v["ups_alert"]["level"] == "bad"
    assert v["ups_alert"]["text"] == "电量 15%，18 秒后自动关机 · 插回市电可取消"
    assert v["ups"]["level"] == "bad"
    # kiosk 没回写起点 (老 kiosk / 还没收到): 不编秒数
    v = dc.layout_view(_snap(ups=low), now=1012.0)
    assert v["ups_alert"]["text"] == "电量 15%，即将自动关机 · 插回市电可取消"
    # 倒计时中插回市电: 副屏也立刻恢复
    v = dc.layout_view(_snap(ups=dict(low, status="OL CHRG"), kiosk={"ups_since": 1000.0}), now=1012.0)
    assert v["ups_alert"] is None and v["ups"]["level"] == "ok"


# ---- LLDP 对端 (#14): 监听在 dash_service, 这里只管采集项和版面

def _lldp(**states):
    nb = {"proto": "lldp", "sys_name": "SW-A", "chassis_id": "02:00:00:00:00:aa", "port_id": "Gi0/1",
          "port_subtype": 5, "port_desc": None, "vlan": 10, "ttl": 120, "mgmt": []}
    out = {}
    for name, st in states.items():
        out[name] = {"iface": "x", "state": st, "age": 400 if st == "stale" else 3,
                     "neighbor": nb if st in ("fresh", "stale") else None,
                     "error": "没有权限 (要 root)" if st == "off" else ""}
    return out


def test_lldp_ifaces_come_from_lower_row_config():
    import lamps_core
    want = [(n, key[4:]) for n, _l, key in lamps_core.LOWER if n in ("LAN1", "LAN2")]
    assert dc.LLDP_IFACES == want and len(want) == 2


def test_collect_includes_lldp_and_survives_failure():
    s = dc.collect(_ctx(lldp=lambda: _lldp(LAN1="fresh")))
    assert s["lldp"]["LAN1"]["neighbor"]["sys_name"] == "SW-A"

    def boom():
        raise RuntimeError("listener gone")
    assert dc.collect(_ctx(lldp=boom))["lldp"] == {}
    assert dc.collect(_ctx())["lldp"] == {}
    assert "lldp" in dc.PERIODS


def test_default_ctx_wires_lldp_source(monkeypatch):
    _stub_kc(monkeypatch)
    assert dc.default_ctx()["lldp"]() == {}                       # 没监听器: 空, 版面不显示
    assert dc.default_ctx(lldp=lambda: {"LAN1": 1})["lldp"]() == {"LAN1": 1}


def test_layout_peers_follow_lan_order_and_levels():
    v = dc.layout_view(_snap(lldp=_lldp(LAN2="off", LAN1="fresh")))
    assert v["peers"] == [{"name": "LAN1", "text": "对端 SW-A Gi0/1 · VLAN 10", "level": "ok"},
                          {"name": "LAN2", "text": "LLDP 不可用: 没有权限 (要 root)", "level": "dim"}]
    stale = dc.layout_view(_snap(lldp=_lldp(LAN1="stale", LAN2="none")))["peers"]
    assert stale[0]["level"] == "dim" and stale[0]["text"].endswith("6 分钟前")
    assert stale[1]["text"] == "没收到 LLDP（交换机可能关了）"


def test_layout_peers_hidden_when_locked_or_missing():
    assert dc.layout_view(_snap(lldp=_lldp(LAN1="fresh")), locked=True)["peers"] == []
    assert dc.layout_view(_snap())["peers"] == []                  # 老 cart-dash 的快照没这项
    assert dc.layout_view(_snap(lldp="garbage"))["peers"] == []


def test_peers_for_kiosk_board():
    assert dc.peers(None) == [] and dc.peers({"lldp": _lldp(LAN1="none")})[0]["name"] == "LAN1"


# ---- #11: 推车自己给整个网段发地址时副屏也醒目显示 (读 pxe-guard.json, 不再自己采)

def test_collect_includes_pxe_guard_and_survives_failure():
    g = {"state": "serving", "until": 1754.0, "window": 1800}
    assert dc.collect(_ctx(pxe=lambda: g))["pxe"] == g

    def boom():
        raise OSError("gone")
    assert dc.collect(_ctx(pxe=boom))["pxe"] == {}
    assert "pxe" in dc.PERIODS and dc.PERIODS["pxe"] <= 1


def test_layout_serve_banner_counts_down_on_wall_clock():
    snap = {"ts": 1000.0, "pxe": {"state": "serving", "until": 1754.0, "window": 1800}}
    for locked in (False, True):                  # 锁屏当引导牌时也要看得见
        v = dc.layout_view(snap, locked=locked, now=1000.0)
        assert v["serve"] == {"text": "正在给整个网段发地址 剩 12:34", "level": "bad"}
    assert dc.layout_view(snap, now=1100.0)["serve"]["text"].endswith("10:54")


def test_layout_serve_banner_expired_and_hidden():
    v = dc.layout_view({"ts": 0, "pxe": {"state": "expired", "until": 1, "window": 1800}}, now=10)
    assert v["serve"]["level"] == "warn" and "到时" in v["serve"]["text"] and "续期" in v["serve"]["text"]
    for pxe in ({}, {"state": "proxy"}, None):
        assert dc.layout_view({"ts": 0, "pxe": pxe}, now=10)["serve"] is None
    assert dc.layout_view({"ts": 0}, now=10)["serve"] is None      # 老快照没这项


def test_default_ctx_reads_live_guard(monkeypatch):
    import kiosk_core as kc
    _stub_kc(monkeypatch)
    monkeypatch.setattr(kc, "pxe_guard_live", lambda *a, **k: {"state": "expired"})
    assert dc.default_ctx()["pxe"]() == {"state": "expired"}


# ---- #23 Agent 写串口: 副屏同步挂起数和急停 (状态读不懂 = 全部急停)

def _agent(**over):
    st = [{"estop": False, "pending": 0, "writer": False, "sending": False} for _ in range(4)]
    for n, d in over.items():
        st[int(n[1:]) - 1].update(d)
    return st


def test_heads_follow_agent_state_and_summary():
    v = dc.layout_view(dict(_snap(), agent=_agent(s1={"pending": 2, "writer": True}, s3={"estop": True})))
    h = v["heads"]
    assert h[0]["text"] == "待确认 2" and h[0]["look"]["bg"] == confirm_core.PENDING_COLOR
    assert h[2]["text"] == "急停" and h[2]["look"]["bg"] == confirm_core.ESTOP_COLOR
    assert h[1]["look"] is None or h[1]["look"]["bar"] != confirm_core.AGENT_COLOR
    assert v["agent"]["level"] == "bad" and "待确认 2" in v["agent"]["text"]


def test_no_agent_key_keeps_old_heads():
    v = dc.layout_view(_snap())
    assert v["agent"] is None and all(h["look"] is None for h in v["heads"])


@pytest.mark.parametrize("bad", ["x", [1, 2], [{"estop": False}] * 4, {"1": {}}])
def test_unreadable_agent_state_shows_all_stopped(bad):
    v = dc.layout_view(dict(_snap(), agent=bad))
    assert all(h["text"] == "急停" for h in v["heads"]) and v["agent"]["text"] == "Agent 全部急停"


def test_agent_sample_reads_state_and_hides_names(tmp_path):
    import serial_write_core as sw
    b = sw.Book.from_text(None, 1_700_000_000.0)
    b.acquire(2, "stdio:ops@198.51.100.9", 1_700_000_000.0)
    p = tmp_path / "s.json"
    p.write_text(b.dumps())
    st = dc.agent_sample(str(p), clock=lambda: 1_700_000_001.0)
    assert st[1] == {"estop": False, "pending": 0, "writer": True, "sending": False}
    assert "ops@" not in json.dumps(st)                              # 快照是 0644, 不带 Agent 名字
    (tmp_path / "bad.json").write_text("{")
    assert all(s["estop"] for s in dc.agent_sample(str(tmp_path / "bad.json")))


def test_collect_carries_agent():
    snap = dc.collect({"agent": lambda: _agent(s2={"estop": True})})
    assert snap["agent"][1]["estop"]
    assert dc.collect({})["agent"] is None


# ---- 深浅模式 (#31): cart-dash 按日出日落判断, 并进快照, 车机和副屏都读这一份

THEME_DARK = {"mode": "dark", "setting": None, "next_switch": None}


def test_collect_theme_default_is_dark():
    assert dc.collect(_ctx())["theme"] == THEME_DARK               # 没接采集: 深色 (现状)

    def boom():
        raise OSError("conf gone")
    assert dc.collect(_ctx(theme=boom))["theme"] == THEME_DARK
    assert dc.collect(_ctx(theme=lambda: {"mode": "light"}))["theme"] == {"mode": "light"}


def _theme_conf(tmp_path, **conf):
    p = tmp_path / "theme.json"
    p.write_text(json.dumps(conf))
    return str(p)


# 伦敦 2026-06-21: 日出 ~03:43 UTC, 日落 ~20:21 UTC
LONDON_NOON = 1782043200.0          # 2026-06-21 12:00 UTC
LONDON_SUNSET = 1782073294.0        # 2026-06-21 20:21:34 UTC


def test_theme_sampler_auto(tmp_path):
    conf = _theme_conf(tmp_path, lat=51.508, lon=-0.126)
    t = {"now": LONDON_NOON}
    get = dc.theme_sampler(conf, str(tmp_path / "state.json"), clock=lambda: t["now"])
    th = get()
    assert th["mode"] == "light" and th["setting"] == "auto"
    assert th["next_switch"] == pytest.approx(LONDON_SUNSET + 20 * 60, abs=60)
    t["now"] = LONDON_SUNSET + 10 * 60                             # 缓冲里: 维持上一轮的浅色
    assert get()["mode"] == "light"
    t["now"] = LONDON_SUNSET + 21 * 60
    assert get()["mode"] == "dark"


def test_theme_sampler_last_from_previous_snapshot(tmp_path):
    conf = _theme_conf(tmp_path, lat=51.508, lon=-0.126)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"theme": {"mode": "dark"}}))       # cart-dash 重启前是深色
    get = dc.theme_sampler(conf, str(state), clock=lambda: LONDON_SUNSET - 5 * 60)
    assert get()["mode"] == "dark"                                 # 缓冲里接着用上一份快照的
    state.write_text("{broken")
    assert dc.theme_sampler(conf, str(state), clock=lambda: LONDON_SUNSET - 5 * 60)()["mode"] == "light"


def test_theme_sampler_fixed_missing_and_bad_conf(tmp_path):
    state = str(tmp_path / "state.json")
    fixed = dc.theme_sampler(_theme_conf(tmp_path, mode="light"), state, clock=lambda: 0.0)()
    assert fixed == {"mode": "light", "setting": "light", "next_switch": None}
    missing = dc.theme_sampler(str(tmp_path / "nope.json"), state, clock=lambda: LONDON_NOON)()
    assert missing == {"mode": "dark", "setting": "auto", "next_switch": None}
    bad = tmp_path / "bad.json"
    bad.write_text('{"mode": "night"}')
    assert dc.theme_sampler(str(bad), state, clock=lambda: LONDON_NOON)() == THEME_DARK


def test_default_ctx_wires_theme(monkeypatch):
    _stub_kc(monkeypatch)
    monkeypatch.setattr(dc, "theme_sampler", lambda *a, **k: (lambda: {"mode": "light"}))
    assert dc.default_ctx()["theme"]() == {"mode": "light"}
    assert "theme" in dc.PERIODS                                   # 不必每 200 ms 读一次配置
