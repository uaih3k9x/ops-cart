import wifi_core as wc

SCAN = "\n".join([
    r"*:94\:14\:57\:86\:AE\:37:CAMPUSNET:73:",
    r" :94\:14\:57\:86\:AE\:38:CAMPUSNET:88:",
    r" :AA\:BB\:CC\:00\:00\:01:ExampleCoLtd:92:WPA2 WPA3",
    r" :AA\:BB\:CC\:00\:00\:02::92:WPA2",                      # 隐藏 SSID 不列
    r" :AA\:BB\:CC\:00\:00\:03:a\:b 咖啡:40:WPA1 WPA2",          # SSID 里带冒号
    r" :AA\:BB\:CC\:00\:00\:04:OnlyWPA3:60:WPA3",
])

SAVED = "\n".join([
    "campusnet:6c4d-1:802-11-wireless:yes:0:yes",
    "tailscale0:0d31-2:tun:yes:0:yes",
    "enp3s0:1603-3:802-3-ethernet:yes:0:no",
    "ExampleCo:aaaa-4:802-11-wireless:yes:20:no",
    "Home:bbbb-5:802-11-wireless:no:10:no",
])


def test_split_terse_handles_escaped_colons():
    assert wc.split_terse(r"a\:b:c::d\\e") == ["a:b", "c", "", "d\\e"]


def test_parse_scan_dedups_by_ssid_and_sorts():
    nets = wc.parse_scan(SCAN)
    assert [n["ssid"] for n in nets] == ["CAMPUSNET", "ExampleCoLtd", "OnlyWPA3", "a:b 咖啡"]
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
        ("ExampleCo", 20, False), ("Home", 10, False), ("campusnet", 0, True)]
    assert saved[1]["autoconnect"] is False and saved[0]["uuid"] == "aaaa-4"


def test_move_and_priorities():
    order = ["a", "b", "c"]
    assert wc.move(order, 2, -1) == ["a", "c", "b"]
    assert wc.move(order, 0, -1) == order                     # 到顶不动
    assert wc.move(order, 2, +1) == order
    assert wc.priorities(3) == [30, 20, 10]                   # 排第一的优先级最高


def test_cmd_add_secured_keeps_wired_preferred():
    cmd = wc.cmd_add("ExampleCo", "pw 1", "WPA2 WPA3", priority=40, iface="wlp4s0")
    assert cmd[:4] == ["nmcli", "con", "add", "type"]
    s = " ".join(cmd)
    assert "ssid ExampleCo" in s and "con-name ExampleCo" in s
    assert cmd[cmd.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert cmd[cmd.index("wifi-sec.psk") + 1] == "pw 1"
    assert cmd[cmd.index("ipv4.route-metric") + 1] == str(wc.WIFI_METRIC)
    assert cmd[cmd.index("connection.autoconnect-priority") + 1] == "40"


def test_cmd_add_open_network_has_no_security():
    cmd = wc.cmd_add("CAMPUSNET", "", "", priority=10)
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
    st = wc.status(scan=r"*:CAMPUSNET:89" + "\n" + r" :ExampleCoLtd:97",
                   addr="wlp4s0           UP             10.99.97.104/20 2001:db8::1/64",
                   routes="default via 10.99.96.1 dev wlp4s0 proto dhcp metric 900")
    assert st == {"ssid": "CAMPUSNET", "signal": 89, "ip": "10.99.97.104/20", "egress": True}


def test_status_backup_when_wired_wins():
    st = wc.status(scan=r"*:CAMPUSNET:70", addr="wlp4s0 UP 10.0.0.5/20",
                   routes="default via 192.168.99.1 dev enp3s0 metric 100\n"
                          "default via 10.0.0.1 dev wlp4s0 metric 900")
    assert st["egress"] is False


def test_status_disconnected():
    st = wc.status(scan=r" :CAMPUSNET:70", addr="wlp4s0 DOWN", routes="")
    assert st["ssid"] is None


# ------------------------------------------------------------ 优先级主动生效 (2026-10-22)
# NM 只在断线重连时按优先级挑; 已经连着低优先级的, 高优先级的出现了也不换 -> 我们来换

def _saved(ssid, prio, active=False, auto=True):
    return {"name": ssid, "uuid": f"u-{ssid}", "priority": prio, "active": active, "autoconnect": auto, "ssid": ssid}


def _scan(*pairs):
    return [{"ssid": s, "signal": sig, "security": "WPA2", "in_use": False} for s, sig in pairs]


SAVED_P = [_saved("ExampleCo5G", 20), _saved("ExampleCo", 10), _saved("campusnet", 0, active=True)]


def test_pick_switch_prefers_highest_visible():
    got = wc.pick_switch(SAVED_P, _scan(("ExampleCo5G", 67), ("ExampleCo", 84), ("campusnet", 29)), now=0)
    assert got["ssid"] == "ExampleCo5G"


def test_pick_switch_needs_enough_signal():
    got = wc.pick_switch(SAVED_P, _scan(("ExampleCo5G", 25), ("ExampleCo", 84)), now=0)
    assert got["ssid"] == "ExampleCo"                            # 5G 太弱, 退而求其次


def test_pick_switch_none_when_already_best_or_nothing_better():
    best = [_saved("ExampleCo5G", 20, active=True), _saved("campusnet", 0)]
    assert wc.pick_switch(best, _scan(("ExampleCo5G", 60), ("campusnet", 80)), now=0) is None
    assert wc.pick_switch(SAVED_P, _scan(("campusnet", 29)), now=0) is None


def test_pick_switch_leaves_disconnected_to_nm():
    idle = [_saved("ExampleCo5G", 20), _saved("campusnet", 0)]
    assert wc.pick_switch(idle, _scan(("ExampleCo5G", 90)), now=0) is None   # 没连着 = NM 自己会按优先级连


def test_pick_switch_respects_cooldown_and_autoconnect_off():
    cool = {"ExampleCo5G": 1000}                                 # 冷却到 t=1000
    got = wc.pick_switch(SAVED_P, _scan(("ExampleCo5G", 90), ("ExampleCo", 90)), now=500, cooldown=cool)
    assert got["ssid"] == "ExampleCo"
    assert wc.pick_switch(SAVED_P, _scan(("ExampleCo5G", 90)), now=1001, cooldown=cool)["ssid"] == "ExampleCo5G"
    off = [_saved("Home", 30, auto=False), _saved("campusnet", 0, active=True)]
    assert wc.pick_switch(off, _scan(("Home", 90)), now=0) is None        # 用户关了自动连的不碰


def test_check_wifi_shows_auth_failure():
    import kiosk_core as kc
    st = {"ssid": "CAMPUSNET", "signal": 70, "ip": "10.0.0.2", "egress": False,
          "auth": {"state": "login fail", "ok": False, "message": "密码错误"}}
    level, text = kc.check_wifi({"wifi": lambda: st})
    assert level == "WARN" and "AUTH" in text and "密码错误" in text
    st["auth"] = {"state": "online", "ok": True, "message": ""}
    assert kc.check_wifi({"wifi": lambda: st})[0] == "OK"


def test_cmd_scan_rescan_modes():
    assert wc.cmd_scan(rescan=True)[-1] == "yes"
    assert wc.cmd_scan(rescan=False)[-1] == "no"
    assert wc.cmd_scan(rescan="auto")[-1] == "auto"
