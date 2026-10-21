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
