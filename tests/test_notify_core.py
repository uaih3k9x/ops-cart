"""notify_core: 当前状态 → 和上次比 → 只在变化时出事件; 防抖、冷却合并、持久化."""

import json

import notify_core as nc

T0 = 1_800_000_000.0
OL = {"percent": 100, "state": "charging", "status": "OL"}
OB = {"percent": 92, "state": "discharging", "status": "OB DISCHRG"}
LOW = {"percent": 12, "state": "discharging", "status": "OB DISCHRG"}


def snap(ups=OL, guard="serving", auth="online", svc=None, boot=("b1", True), ts=T0, **over):
    """拼一份 collect() 的输入: 每个读取函数都是假的."""
    services = {"pxe-server": "active", "kiosk": "active", "cart-dash": "active"}
    services.update(svc or {})
    read = {
        "state": lambda: {"ts": ts, "ups": ups},
        "guard": lambda: None if guard is None else {"state": guard, "detail": "x", "iface": "eno1"},
        "auth": lambda: None if auth is None else {"state": auth, "ok": auth in ("online", "login ok", "skip"),
                                                   "message": "密码不对" if auth == "login fail" else ""},
        "services": lambda: services,
        "boot": lambda: {"id": boot[0], "ok": boot[1]},
        "now": lambda: ts,
    }
    read.update(over)
    return nc.collect(read)


def run(prev, cur, now):
    events, state = nc.diff(prev, cur, now)
    return [e["key"] for e in events], events, state


# ---------------------------------------------------------------- 采集

def test_collect_maps_sources_to_values():
    cur = snap(ups=OB, guard="proxy", auth="login fail", svc={"kiosk": "failed"})
    v = cur["values"]
    assert v["ups.power"] == "OB" and v["ups.low"] is False and cur["info"]["pct"] == 92
    assert v["guard"] == "proxy"
    assert v["auth"] == "fail" and cur["info"]["auth_state"] == "login fail"
    assert v["svc.kiosk"] == "down" and v["svc.pxe-server"] == "up" and v["svc.cart-dash"] == "up"
    assert cur["boot"] == {"id": "b1", "ok": True}


def test_collect_low_battery_uses_ups_core_threshold():
    assert snap(ups=LOW)["values"]["ups.low"] is True


def test_collect_unknowns_are_none_and_never_raise():
    def boom():
        raise OSError("no")
    cur = nc.collect({"state": boom, "guard": boom, "auth": boom, "services": boom, "boot": boom,
                      "now": lambda: T0})
    assert set(cur["values"].values()) == {None}
    assert cur["boot"] == {"id": None, "ok": None}


def test_collect_stale_dash_snapshot_means_ups_unknown():
    cur = snap(ups=OB, ts=T0, now=lambda: T0 + nc.STATE_STALE_S + 1)
    assert cur["values"]["ups.power"] is None and cur["values"]["ups.low"] is None


def test_collect_guard_transient_states_are_unknown_and_stop_states_merge():
    assert snap(guard="probing")["values"]["guard"] is None
    assert snap(guard="starting")["values"]["guard"] is None
    assert snap(guard="no-link")["values"]["guard"] == "stop"
    assert snap(guard="no-ip")["values"]["guard"] == "stop"


def test_collect_guard_ignored_while_pxe_server_down():
    cur = snap(guard="serving", svc={"pxe-server": "inactive"})
    assert cur["values"]["guard"] is None and cur["values"]["svc.pxe-server"] == "down"


def test_collect_service_activating_is_unknown():
    assert snap(svc={"kiosk": "activating"})["values"]["svc.kiosk"] is None


# ---------------------------------------------------------------- diff

def test_first_run_normal_is_silent_and_same_state_is_silent():
    keys, _, st = run(None, snap(), T0)
    assert keys == []
    keys, _, st = run(st, snap(), T0 + 30)
    assert keys == []


def test_first_run_abnormal_reports():
    keys, ev, _ = run(None, snap(ups=OB), T0)
    assert keys == ["ups.power"]


def test_ups_on_battery_then_back():
    _, _, st = run(None, snap(), T0)
    keys, ev, st = run(st, snap(ups=OB), T0 + 30)
    assert keys == ["ups.power"]
    assert ev[0]["text"] == "推车转电池供电 92%，市电断了"
    assert ev[0]["prio"] >= 4
    keys, ev, st = run(st, snap(ups=OB), T0 + 60)
    assert keys == []                                   # 状态不变不推
    t = T0 + 30 + nc.RULES["ups.power"][1]
    keys, ev, st = run(st, snap(ups=OL), t)
    assert keys == ["ups.power"] and ev[0]["text"].startswith("推车市电恢复")


def test_low_battery_reports_once_and_its_recovery_is_quiet():
    _, _, st = run(None, snap(ups=OB), T0)
    keys, ev, st = run(st, snap(ups=LOW), T0 + 30)
    assert keys == ["ups.low"] and "12%" in ev[0]["text"] and ev[0]["prio"] == 5
    keys, _, st = run(st, snap(ups=LOW), T0 + 60)
    assert keys == []
    keys, ev, st = run(st, snap(ups=OL), T0 + 4000)
    assert keys == ["ups.power"]                        # 低电量解除不单独推, 市电恢复那条就够


def test_cooldown_merges_repeats_then_reports_final_state():
    _, _, st = run(None, snap(), T0)
    keys, _, st = run(st, snap(ups=OB), T0 + 30)
    assert keys == ["ups.power"]
    keys, _, st = run(st, snap(ups=OL), T0 + 60)        # 冷却期内: 不推
    assert keys == []
    keys, _, st = run(st, snap(ups=OB), T0 + 90)
    keys2, _, st = run(st, snap(ups=OL), T0 + 120)
    assert keys == keys2 == []
    keys, ev, st = run(st, snap(ups=OL), T0 + 30 + nc.RULES["ups.power"][1])
    assert keys == ["ups.power"] and "市电恢复" in ev[0]["text"]
    assert "冷却期内还变过" in ev[0]["text"]


def test_cooldown_ends_on_same_state_is_silent():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(ups=OB), T0 + 30)
    _, _, st = run(st, snap(ups=OL), T0 + 60)
    _, _, st = run(st, snap(ups=OB), T0 + 90)          # 又回到已经推过的状态
    keys, _, _ = run(st, snap(ups=OB), T0 + 30 + nc.RULES["ups.power"][1] + 1)
    assert keys == []


def test_short_flap_reports_once():
    """守卫 serving → proxy 只闪了一下就回来: 只推一条"短暂…已回到…", 不推两条."""
    _, _, st = run(None, snap(), T0)
    keys, _, st = run(st, snap(guard="proxy"), T0 + 30)
    assert keys == []                                   # 还没稳住 (settle)
    keys, ev, st = run(st, snap(guard="serving"), T0 + 35)
    assert keys == ["guard"] and "短暂" in ev[0]["text"] and ev[0]["prio"] <= 3
    keys, _, _ = run(st, snap(guard="serving"), T0 + 65)
    assert keys == []


def test_settled_change_reports_after_settle():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(guard="proxy"), T0 + 30)
    keys, ev, st = run(st, snap(guard="proxy"), T0 + 30 + nc.RULES["guard"][0])
    assert keys == ["guard"] and ev[0]["text"].startswith("装机口转代理模式")


def test_guard_first_value_is_baseline():
    keys, _, st = run(None, snap(guard="proxy"), T0)
    assert keys == []
    keys, _, _ = run(st, snap(guard=None), T0 + 30)     # 读不到: 不算变化
    assert keys == []


def test_unknown_does_not_clear_state():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(ups=OB), T0 + 30)
    _, _, st = run(st, snap(ups=None), T0 + 400)
    keys, _, _ = run(st, snap(ups=OB), T0 + 430)
    assert keys == []


def test_service_down_and_up():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(svc={"kiosk": "failed"}), T0 + 30)
    keys, ev, st = run(st, snap(svc={"kiosk": "failed"}), T0 + 60)
    assert keys == ["svc.kiosk"]
    assert ev[0]["text"] == "推车服务 kiosk 挂了（failed），看 journalctl -u kiosk"
    t = T0 + 60 + nc.RULES["svc"][1]
    _, _, st = run(st, snap(), t)
    keys, ev, _ = run(st, snap(), t + 30)
    assert keys == ["svc.kiosk"] and ev[0]["text"] == "推车服务 kiosk 恢复运行"


def test_service_restart_blip_reports_once():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(svc={"cart-dash": "failed"}), T0 + 30)
    keys, ev, _ = run(st, snap(), T0 + 60)
    assert keys == ["svc.cart-dash"] and "短暂" in ev[0]["text"]


def test_auth_fail_and_recover_without_secrets():
    _, _, st = run(None, snap(), T0)
    keys, ev, st = run(st, snap(auth="login fail"), T0 + 120)
    keys, ev, st = run(st, snap(auth="login fail"), T0 + 240)
    assert keys == ["auth"]
    assert ev[0]["text"].startswith("校园网认证失败（login fail）")
    t = T0 + 240 + nc.RULES["auth"][1]
    _, _, st = run(st, snap(), t)
    keys, ev, _ = run(st, snap(), t + 200)
    assert keys == ["auth"] and ev[0]["text"].startswith("校园网认证恢复")


def test_auth_untrusted_message_omits_host():
    _, _, st = run(None, snap(), T0)
    cur = snap(auth="untrusted")
    cur["info"]["auth_msg"] = "portal.example.invalid"
    _, _, st = run(st, cur, T0 + 120)
    _, ev, _ = run(st, cur, T0 + 300)
    assert "无线面板" in ev[0]["text"] and "example" not in ev[0]["text"]


def test_auth_skip_counts_ok():
    keys, _, _ = run(None, snap(auth="skip"), T0)
    assert keys == []


def test_boot_bad_previous_boot_reports_once():
    _, _, st = run(None, snap(boot=("b1", False)), T0)       # 这次启动还没走到 boot-success
    keys, ev, st = run(st, snap(boot=("b2", False)), T0 + 100)  # 换了 boot id, 上次一直没成功
    assert keys == ["boot"] and "非正常重启" in ev[0]["text"]
    keys, _, st = run(st, snap(boot=("b2", True)), T0 + 200)
    assert keys == []
    keys, _, _ = run(st, snap(boot=("b3", False)), T0 + 300)  # 上一次成功过
    assert keys == []


def test_boot_ok_is_sticky_within_boot():
    _, _, st = run(None, snap(boot=("b1", True)), T0)
    _, _, st = run(st, snap(boot=("b1", None)), T0 + 30)     # 读不到不覆盖
    keys, _, _ = run(st, snap(boot=("b2", None)), T0 + 60)
    assert keys == []


def test_boot_unknown_source_never_reports():
    _, _, st = run(None, snap(boot=(None, None)), T0)
    keys, _, _ = run(st, snap(boot=(None, None)), T0 + 30)
    assert keys == []


def test_boot_unknown_success_never_reports():
    _, _, st = run(None, snap(boot=("b1", None)), T0)        # 判断不了成没成功
    keys, _, _ = run(st, snap(boot=("b2", None)), T0 + 30)
    assert keys == []


def test_first_ever_boot_record_does_not_report():
    keys, _, _ = run(None, snap(boot=("b9", None)), T0)
    assert keys == []


def test_diff_is_pure():
    prev = run(None, snap(), T0)[2]
    frozen = json.dumps(prev, sort_keys=True)
    nc.diff(prev, snap(ups=OB), T0 + 30)
    assert json.dumps(prev, sort_keys=True) == frozen


def test_messages_are_one_line():
    _, _, st = run(None, snap(), T0)
    cur = snap(auth="login fail")
    cur["info"]["auth_msg"] = "多行\n消息"
    _, _, st = run(st, cur, T0 + 120)
    _, ev, _ = run(st, cur, T0 + 300)
    assert "\n" not in ev[0]["text"]


# ---------------------------------------------------------------- 持久化

def test_state_roundtrip_and_bad_file(tmp_path):
    p = tmp_path / "lib" / "notify.json"
    assert nc.load_state(str(p)) is None
    _, _, st = run(None, snap(), T0)
    nc.save_state(str(p), st)
    assert nc.load_state(str(p)) == st
    p.write_text("{半个")
    assert nc.load_state(str(p)) is None
