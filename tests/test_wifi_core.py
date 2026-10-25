import wifi_core as wc

SCAN = "\n".join([
    r"*:02\:00\:00\:00\:00\:AB:CAMPUS-NET:73:",
    r" :02\:00\:00\:00\:00\:AA:CAMPUS-NET:88:",
    r" :AA\:BB\:CC\:00\:00\:01:LabNet:92:WPA2 WPA3",
    r" :AA\:BB\:CC\:00\:00\:02::92:WPA2",                      # 隐藏 SSID 不列
    r" :AA\:BB\:CC\:00\:00\:03:a\:b 咖啡:40:WPA1 WPA2",          # SSID 里带冒号
    r" :AA\:BB\:CC\:00\:00\:04:OnlyWPA3:60:WPA3",
])

SAVED = "\n".join([
    "campus-net:6c4d-1:802-11-wireless:yes:0:yes",
    "tailscale0:0d31-2:tun:yes:0:yes",
    "enp3s0:1603-3:802-3-ethernet:yes:0:no",
    "LabNet:aaaa-4:802-11-wireless:yes:20:no",
    "Home:bbbb-5:802-11-wireless:no:10:no",
])


def test_split_terse_handles_escaped_colons():
    assert wc.split_terse(r"a\:b:c::d\\e") == ["a:b", "c", "", "d\\e"]


def test_parse_scan_dedups_by_ssid_and_sorts():
    nets = wc.parse_scan(SCAN)
    assert [n["ssid"] for n in nets] == ["CAMPUS-NET", "LabNet", "OnlyWPA3", "a:b 咖啡"]
    sc = nets[0]
    assert sc["in_use"] is True and sc["signal"] == 88      # 同名取最强信号, 在用标记保留
    assert sc["security"] == ""


def test_key_mgmt():
    assert wc.key_mgmt("") is None and wc.key_mgmt("--") is None
    assert wc.key_mgmt("WPA2 WPA3") == "wpa-psk"             # 混合模式用 PSK 兼容性最好
    assert wc.key_mgmt("WPA1 WPA2") == "wpa-psk"
    assert wc.key_mgmt("WPA3") == "sae"


def test_parse_saved_only_wifi_sorted_by_priority():
    saved = wc.parse_saved(SAVED)
    assert [(s["name"], s["priority"], s["active"]) for s in saved] == [
        ("LabNet", 20, False), ("Home", 10, False), ("campus-net", 0, True)]
    assert saved[1]["autoconnect"] is False and saved[0]["uuid"] == "aaaa-4"


def test_move_and_priorities():
    order = ["a", "b", "c"]
    assert wc.move(order, 2, -1) == ["a", "c", "b"]
    assert wc.move(order, 0, -1) == order                     # 到顶不动
    assert wc.move(order, 2, +1) == order
    assert wc.priorities(3) == [30, 20, 10]                   # 排第一的优先级最高


def test_cmd_add_secured_keeps_wired_preferred():
    cmd = wc.cmd_add("LabNet", "pw 1", "WPA2 WPA3", priority=40, iface="wlp4s0")
    assert cmd[:4] == ["nmcli", "con", "add", "type"]
    s = " ".join(cmd)
    assert "ssid LabNet" in s and "con-name LabNet" in s
    assert cmd[cmd.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert cmd[cmd.index("wifi-sec.psk") + 1] == "pw 1"
    assert cmd[cmd.index("ipv4.route-metric") + 1] == str(wc.WIFI_METRIC)
    assert cmd[cmd.index("connection.autoconnect-priority") + 1] == "40"


def test_cmd_add_open_network_has_no_security():
    cmd = wc.cmd_add("CAMPUS-NET", "", "", priority=10)
    assert "wifi-sec.key-mgmt" not in cmd and "wifi-sec.psk" not in cmd


def test_simple_cmds():
    assert wc.cmd_up("u1") == ["nmcli", "con", "up", "uuid", "u1"]
    assert wc.cmd_delete("u1") == ["nmcli", "con", "delete", "uuid", "u1"]
    assert wc.cmd_priority("u1", 30) == ["nmcli", "con", "mod", "uuid", "u1",
                                         "connection.autoconnect-priority", "30"]
    assert wc.cmd_show_psk("u1") == ["nmcli", "-s", "-g", "802-11-wireless-security.psk",
                                     "con", "show", "uuid", "u1"]
    assert wc.cmd_set_psk("u1", "x") == ["nmcli", "con", "mod", "uuid", "u1",
                                         "802-11-wireless-security.psk", "x"]


def test_next_priority_goes_on_top():
    assert wc.next_priority([]) == 10
    assert wc.next_priority([{"priority": 20}, {"priority": 0}]) == 30


# 状态板一行 (2026-10-21): SSID / 信号 / IP, 网线拔了时标出"当前出口"

def test_status_connected_and_is_default_route():
    st = wc.status(scan=r"*:CAMPUS-NET:89" + "\n" + r" :LabNet:97",
                   addr="wlp4s0           UP             198.51.100.104/20 2001:db8::1/64",
                   routes="default via 198.51.100.1 dev wlp4s0 proto dhcp metric 900")
    assert st == {"ssid": "CAMPUS-NET", "signal": 89, "ip": "198.51.100.104/20", "egress": True}


def test_status_backup_when_wired_wins():
    st = wc.status(scan=r"*:CAMPUS-NET:70", addr="wlp4s0 UP 10.0.0.5/20",
                   routes="default via 192.0.2.1 dev enp3s0 metric 100\n"
                          "default via 10.0.0.1 dev wlp4s0 metric 900")
    assert st["egress"] is False


def test_status_disconnected():
    st = wc.status(scan=r" :CAMPUS-NET:70", addr="wlp4s0 DOWN", routes="")
    assert st["ssid"] is None


# ------------------------------------------------------------ 优先级主动生效 (2026-10-22)
# NM 只在断线重连时按优先级挑; 已经连着低优先级的, 高优先级的出现了也不换 -> 我们来换

def _saved(ssid, prio, active=False, auto=True):
    return {"name": ssid, "uuid": f"u-{ssid}", "priority": prio, "active": active, "autoconnect": auto, "ssid": ssid}


def _scan(*pairs):
    return [{"ssid": s, "signal": sig, "security": "WPA2", "in_use": False} for s, sig in pairs]


SAVED_P = [_saved("LabNet5G", 20), _saved("LabNet", 10), _saved("campus-net", 0, active=True)]


def test_pick_switch_prefers_highest_visible():
    got = wc.pick_switch(SAVED_P, _scan(("LabNet5G", 67), ("LabNet", 84), ("campus-net", 29)), now=0)
    assert got["ssid"] == "LabNet5G"


def test_pick_switch_needs_enough_signal():
    got = wc.pick_switch(SAVED_P, _scan(("LabNet5G", 25), ("LabNet", 84)), now=0)
    assert got["ssid"] == "LabNet"                            # 5G 太弱, 退而求其次


def test_pick_switch_none_when_already_best_or_nothing_better():
    best = [_saved("LabNet5G", 20, active=True), _saved("campus-net", 0)]
    assert wc.pick_switch(best, _scan(("LabNet5G", 60), ("campus-net", 80)), now=0) is None
    assert wc.pick_switch(SAVED_P, _scan(("campus-net", 29)), now=0) is None


def test_pick_switch_leaves_disconnected_to_nm():
    idle = [_saved("LabNet5G", 20), _saved("campus-net", 0)]
    assert wc.pick_switch(idle, _scan(("LabNet5G", 90)), now=0) is None   # 没连着 = NM 自己会按优先级连


def test_pick_switch_respects_cooldown_and_autoconnect_off():
    cool = {"LabNet5G": 1000}                                 # 冷却到 t=1000
    got = wc.pick_switch(SAVED_P, _scan(("LabNet5G", 90), ("LabNet", 90)), now=500, cooldown=cool)
    assert got["ssid"] == "LabNet"
    assert wc.pick_switch(SAVED_P, _scan(("LabNet5G", 90)), now=1001, cooldown=cool)["ssid"] == "LabNet5G"
    off = [_saved("Home", 30, auto=False), _saved("campus-net", 0, active=True)]
    assert wc.pick_switch(off, _scan(("Home", 90)), now=0) is None        # 用户关了自动连的不碰


def test_check_wifi_shows_auth_failure():
    import kiosk_core as kc
    st = {"ssid": "CAMPUS-NET", "signal": 70, "ip": "10.0.0.2", "egress": False,
          "auth": {"state": "login fail", "ok": False, "message": "密码错误"}}
    level, text = kc.check_wifi({"wifi": lambda: st})
    assert level == "WARN" and "AUTH" in text and "密码错误" in text
    st["auth"] = {"state": "online", "ok": True, "message": ""}
    assert kc.check_wifi({"wifi": lambda: st})[0] == "OK"


def test_cmd_scan_rescan_modes():
    assert wc.cmd_scan(rescan=True)[-1] == "yes"
    assert wc.cmd_scan(rescan=False)[-1] == "no"
    assert wc.cmd_scan(rescan="auto")[-1] == "auto"


# ------------------------------------------------------------ 不切断唯一出口 (2026-10-25)
# 重输密码 = 先建新连接并连上, 成功了才删旧的; 失败按回滚序列回到原样

def _runner(fail_on=()):
    calls = []

    def run(cmd):
        calls.append(cmd)
        return (1, "boom") if any(cmd[:len(f)] == f for f in fail_on) else (0, "")
    return run, calls


OLD = [{"name": "LabNet", "uuid": "old-1", "ssid": "LabNet", "active": True, "priority": 20, "autoconnect": True},
       {"name": "Other", "uuid": "o-2", "ssid": "Other", "active": False, "priority": 10, "autoconnect": True}]


def test_cmd_add_can_pin_uuid():
    cmd = wc.cmd_add("LabNet", "pw123456", "WPA2", 30, uuid="new-9")
    assert cmd[cmd.index("connection.uuid") + 1] == "new-9"
    assert "connection.uuid" not in wc.cmd_add("LabNet", "pw123456", "WPA2", 30)


def test_plan_replace_builds_before_deleting():
    p = wc.plan_replace(OLD, "LabNet", "pw123456", "WPA2", 30, new_uuid="new-9")
    assert p["do"][0][:3] == ["nmcli", "con", "add"] and p["do"][1] == wc.cmd_up("new-9")
    assert p["then"] == [wc.cmd_delete("old-1")]                         # 别的网络不动
    assert p["rollback"] == [wc.cmd_delete("new-9"), wc.cmd_up("old-1")]


def test_plan_replace_new_network_has_nothing_to_delete():
    p = wc.plan_replace(OLD, "Fresh", "", "", 30, new_uuid="n")
    assert p["then"] == [] and p["rollback"] == [wc.cmd_delete("n")]


def test_run_plan_success_runs_then():
    run, calls = _runner()
    p = wc.plan_replace(OLD, "LabNet", "pw123456", "WPA2", 30, new_uuid="new-9")
    assert wc.run_plan(p, run) == (True, "", False)
    assert calls == p["do"] + p["then"]


def test_run_plan_up_fails_rolls_back_and_keeps_old():
    run, calls = _runner(fail_on=[wc.cmd_up("new-9")])
    p = wc.plan_replace(OLD, "LabNet", "pw123456", "WPA2", 30, new_uuid="new-9")
    ok, out, rolled = wc.run_plan(p, run)
    assert not ok and rolled and "boom" in out
    assert wc.cmd_delete("old-1") not in calls                           # 旧的从没删
    assert calls[-2:] == p["rollback"]


def test_run_plan_first_step_fails_no_rollback():
    run, calls = _runner(fail_on=[["nmcli", "con", "add"]])
    p = wc.plan_replace(OLD, "LabNet", "pw123456", "WPA2", 30, new_uuid="new-9")
    assert wc.run_plan(p, run)[:2] == (False, "boom") and len(calls) == 1


def test_plan_change_psk_writes_back_old_on_failure():
    p = wc.plan_change_psk("u1", old="oldpass1", new="newpass1")
    assert p["do"] == [wc.cmd_set_psk("u1", "newpass1"), wc.cmd_up("u1")]
    assert p["rollback"] == [wc.cmd_set_psk("u1", "oldpass1"), wc.cmd_up("u1")]
    run, calls = _runner(fail_on=[wc.cmd_up("u1")])
    ok, _out, rolled = wc.run_plan(p, run)
    assert not ok and rolled and wc.cmd_set_psk("u1", "oldpass1") in calls


def test_is_egress_only_when_active_and_default_route_on_wifi():
    wifi_first = "default via 198.51.100.1 dev wlp4s0 metric 900"
    wired_first = "default via 192.0.2.1 dev enp3s0 metric 100\n" + wifi_first
    assert wc.is_egress(OLD[0], wifi_first) is True
    assert wc.is_egress(OLD[0], wired_first) is False
    assert wc.is_egress(OLD[1], wifi_first) is False                    # 没连着的不是出口
    assert wc.is_egress(OLD[0], "") is False
