"""副屏开机自检 (#26): kiosk 就绪判断 / 每项状态 / 逐行出结果 / 超时琥珀 / 何时切换. 不碰真机."""

import os

import dash_core as dc

NOW = 1793318400.0          # 2026-10-30, 时钟是对的


def _boot_snap(**over):
    """刚开机: 什么都还没采到."""
    s = {"ts": NOW, "rows": [], "ups": None, "slots": [], "kiosk": {}, "svcs": {}, "version": "Ver 1.08.03"}
    s.update(over)
    return s


def _ready_snap(**over):
    """各项都齐了, 只差 kiosk."""
    s = _boot_snap(
        rows=[{"label": "PXE SERVER", "state": "OK", "detail": "RUNNING", "check": "service"},
              {"label": "LAN2", "state": "OK", "detail": "192.0.2.5", "check": "lan"},
              {"label": "WIFI", "state": "OK", "detail": "lab 70% STANDBY", "check": "wifi"},
              {"label": "TAILSCALE", "state": "OK", "detail": "192.0.2.9", "check": "tailscale"},
              {"label": "CH9329", "state": "OK", "detail": "1 ONLINE / 1 BOUND", "check": "ch9329"},
              {"label": "CAPTURE 1", "state": "OK", "detail": "SIGNAL video0", "check": "signal"},
              {"label": "LAN1 / PXE", "state": "--", "detail": "STANDBY", "check": "pxe"}],
        ups={"state": "fully-charged", "percent": 100, "status": "OL"},
        slots=[{"dev": "/dev/con-%d" % i, "present": i in (1, 2), "baud": None, "count": None}
               for i in range(1, 5)],
        svcs={"chronyd": [True, True], "kiosk": [True, True]})
    s.update(over)
    return s


def _items(snap, now=NOW):
    return {it["key"]: it for it in dc.boot_items(snap, now)}


# ---- kiosk 就绪信号: kiosk.json 里带 pid, 副屏用 /proc/<pid> 判活

def test_kiosk_reporter_writes_its_pid(tmp_path):
    p = str(tmp_path / "kiosk.json")
    dc.KioskReporter(p, pid=4242).update(locked=False, kvm=0)
    assert dc.read_kiosk(p) == {"locked": False, "kvm": 0, "pid": 4242}
    dc.KioskReporter(str(tmp_path / "k2.json")).update(locked=False, kvm=0)
    assert dc.read_kiosk(str(tmp_path / "k2.json"))["pid"] == os.getpid()   # 默认就是本进程


def test_kiosk_live_checks_pid(tmp_path):
    p = tmp_path / "kiosk.json"
    assert dc.kiosk_live(str(p)) == {"alive": False}                       # 还没写过 = 没起来
    p.write_text('{"locked": true, "kvm": 2, "pid": 4242}')
    assert dc.kiosk_live(str(p), alive=lambda pid: pid == 4242)["alive"] is True
    live = dc.kiosk_live(str(p), alive=lambda pid: False)                    # 挂了, 文件还留着
    assert live["alive"] is False and live["pid"] == 4242
    p.write_text('{"locked": true, "kvm": 2}')                             # 老 kiosk 没写 pid
    assert dc.kiosk_live(str(p), alive=lambda pid: True)["alive"] is False


def test_kiosk_live_real_proc_for_this_process(tmp_path):
    p = tmp_path / "kiosk.json"
    dc.KioskReporter(str(p)).update(locked=False, kvm=0)
    if os.path.isdir("/proc/self"):                                         # Mac 上没 /proc, 只在 Linux 验
        assert dc.kiosk_live(str(p))["alive"] is True


def test_default_ctx_wires_kiosk_liveness_svcs_and_version(monkeypatch):
    import kiosk_core as kc
    seen = {}
    monkeypatch.setattr(dc, "kiosk_live", lambda *a, **k: {"alive": True})
    monkeypatch.setattr(kc, "services_status", lambda names: seen.setdefault("svcs", tuple(names)) and {})
    monkeypatch.setattr(kc, "version_string", lambda *a: "Ver 9.99.99")
    ctx = dc.default_ctx(lldp=lambda: {})
    assert ctx["kiosk"]() == {"alive": True}
    ctx["svcs"]()
    assert seen["svcs"] == ("chronyd", "kiosk")
    assert ctx["version"]() == "Ver 9.99.99"
    assert "svcs" in dc.PERIODS and "version" in dc.PERIODS        # 慢项, 不是每 200 ms 跑


def test_collect_carries_svcs_and_version():
    s = dc.collect({"now": lambda: NOW, "svcs": lambda: {"chronyd": (True, True)},
                    "version": lambda: "Ver 1.08.03"})
    assert s["svcs"] == {"chronyd": (True, True)} and s["version"] == "Ver 1.08.03"
    s = dc.collect({"now": lambda: NOW})
    assert s["svcs"] == {} and s["version"] == ""


# ---- 每项状态 (纯函数, 只看快照)

def test_boot_items_order_and_labels():
    keys = [it["key"] for it in dc.boot_items(_boot_snap(), NOW)]
    assert keys == ["time", "ups", "net", "tailscale", "pxe", "heads", "kvm", "kiosk"]
    for it in dc.boot_items(_boot_snap(), NOW):
        assert it["label"] and it["state"] in dc.BOOT_STATES


def test_boot_items_all_waiting_right_after_power_on():
    items = _items(_boot_snap())
    assert {k: it["state"] for k, it in items.items()} == {
        "time": "wait", "ups": "wait", "net": "wait", "tailscale": "wait", "pxe": "wait",
        "heads": "wait", "kvm": "wait", "kiosk": "wait"}
    assert items["kiosk"]["status"] == "启动中…"


def test_boot_items_all_ok_except_kiosk():
    items = _items(_ready_snap())
    assert {k: it["state"] for k, it in items.items() if k != "kiosk"} == {
        "time": "ok", "ups": "ok", "net": "ok", "tailscale": "ok", "pxe": "ok", "heads": "ok", "kvm": "ok"}
    assert items["ups"]["detail"] == "市电 100%"
    assert items["net"]["detail"] == "-- / OK / OK"             # LAN1 待机不算问题
    assert items["heads"]["status"] == "2/4"
    assert items["kvm"]["detail"] == "采集 OK · 键鼠 OK"
    assert items["kiosk"]["state"] == "wait"


def test_boot_items_failures_are_red():
    rows = [{"label": "PXE SERVER", "state": "FAIL", "detail": "NOT RUNNING", "check": "service"},
            {"label": "LAN2", "state": "FAIL", "detail": "NO LINK", "check": "lan"},
            {"label": "CH9329", "state": "--", "detail": "NOT CONNECTED", "check": "ch9329"},
            {"label": "CAPTURE 1", "state": "FAIL", "detail": "NOT FOUND", "check": "signal"}]
    items = _items(_ready_snap(rows=rows))
    assert items["pxe"]["state"] == "fail"
    assert items["net"]["state"] == "fail" and "故障" in items["net"]["detail"]
    assert items["kvm"]["state"] == "fail"
    assert items["tailscale"]["state"] == "off"                 # 计划里没这项: 不算等, 也不算坏


def test_boot_items_warn_rows_are_still_pending():
    # WARN (tailscale 没上线 / LAN2 没拿到地址) 开机时多半是还没好: 先算等待, 超时才琥珀
    rows = [{"label": "TAILSCALE", "state": "WARN", "detail": "OFFLINE", "check": "tailscale"},
            {"label": "LAN2", "state": "WARN", "detail": "NO ADDRESS", "check": "lan"}]
    items = _items(_ready_snap(rows=rows))
    assert items["tailscale"]["state"] == "wait" and items["net"]["state"] == "wait"


def test_boot_items_ups_on_battery_is_warn_and_missing_waits():
    items = _items(_ready_snap(ups={"state": "discharging", "percent": 80, "status": "OB DISCHRG"}))
    assert items["ups"]["state"] == "warn" and "电池 80%" in items["ups"]["detail"]
    assert _items(_ready_snap(ups=None))["ups"]["state"] == "wait"


def test_boot_items_heads_none_plugged_is_off():
    slots = [{"dev": "/dev/con-%d" % i, "present": False, "baud": None, "count": None} for i in range(1, 5)]
    it = _items(_ready_snap(slots=slots))["heads"]
    assert it["state"] == "off" and it["status"] == "0/4"


def test_boot_items_time():
    import time
    assert _items(_ready_snap())["time"]["detail"].startswith(time.strftime("%m-%d %H:%M", time.localtime(NOW)))
    assert _items(_ready_snap(), now=86400.0)["time"]["state"] == "fail"          # RTC 掉电回到 1970
    assert _items(_ready_snap(svcs={"chronyd": [True, False]}))["time"]["state"] == "wait"
    assert _items(_ready_snap(svcs={"chronyd": [False, False]}))["time"]["state"] == "off"


def test_boot_items_kiosk_dead_pid_is_red():
    it = _items(_ready_snap(kiosk={"pid": 4242, "alive": False}))["kiosk"]
    assert it["state"] == "fail" and it["status"] == "未运行"
    assert _items(_ready_snap(kiosk={"pid": 1, "alive": True}))["kiosk"]["state"] == "ok"


# ---- 何时切换 / 逐行出结果 / 超时琥珀

def test_tracker_returns_none_once_kiosk_alive():
    t = dc.BootTracker()
    assert t.view(_ready_snap(kiosk={"pid": 1, "alive": True}), NOW) is None
    v = t.view(_ready_snap(), NOW)
    assert v["boot"] is True and v["version"] == "Ver 1.08.03" and len(v["lines"]) == 8


def test_tracker_reveals_settled_lines_one_step_apart():
    t = dc.BootTracker(step=0.5, late=60)
    snap = _ready_snap()
    shown = []
    for i in range(20):
        v = t.view(snap, NOW + 0.25 * i)
        shown.append(sum(1 for ln in v["lines"] if ln["state"] != "wait"))
    assert shown[0] == 0                                   # 第一帧全是"等待", 然后一行行冒出来
    assert shown == sorted(shown)                          # 只增不减
    assert shown[-1] == 7                                  # 除 kiosk 外都出结果了
    assert max(b - a for a, b in zip(shown, shown[1:])) == 1    # 一次只多一行


def test_tracker_reveal_does_not_flicker_when_earlier_line_settles_late():
    t = dc.BootTracker(step=0.5, late=60)
    snap = _ready_snap(ups=None)
    for i in range(20):
        t.view(snap, NOW + 0.25 * i)
    before = [ln["state"] for ln in t.view(snap, NOW + 5)["lines"]]
    after = [ln["state"] for ln in t.view(_ready_snap(), NOW + 5.1)["lines"]]  # UPS 这才连上
    assert before[1] == "wait"
    assert [a for i, a in enumerate(after) if i != 1] == [b for i, b in enumerate(before) if i != 1]


def test_tracker_pending_turns_amber_after_late():
    t = dc.BootTracker(step=0.5, late=30)
    snap = _ready_snap(ups=None)
    assert t.view(snap, NOW)["lines"][1]["state"] == "wait"
    v = t.view(snap, NOW + 31)
    assert v["lines"][1]["state"] == "late"
    assert v["lines"][7]["state"] == "late"                  # kiosk 也超时了


def test_tracker_restarts_when_kiosk_dies_again():
    t = dc.BootTracker(step=0.5, late=30)
    t.view(_ready_snap(), NOW)
    t.view(_ready_snap(), NOW + 40)
    assert t.view(_ready_snap(kiosk={"pid": 1, "alive": True}), NOW + 50) is None
    v = t.view(_ready_snap(kiosk={"pid": 1, "alive": False}), NOW + 100)       # 车机挂了
    assert v["lines"][7]["state"] == "fail"                 # 红的马上出, 不用等动画, 也不算超时
    assert all(ln["state"] in ("wait", "fail") for ln in v["lines"])          # 重新逐行出
    assert t.view(_ready_snap(kiosk={"pid": 1, "alive": False}), NOW + 101)["lines"][0]["state"] == "ok"


def test_tracker_view_is_stable_between_reveals():
    # 版面不变就不推屏 (dash_service 比 _view_key): 动画走完以后每轮版面得一模一样
    t = dc.BootTracker(step=0.5, late=60)
    for i in range(20):
        t.view(_ready_snap(), NOW + 0.25 * i)
    a, b = t.view(_ready_snap(), NOW + 6), t.view(_ready_snap(), NOW + 6.2)
    assert a["lines"] == b["lines"]
