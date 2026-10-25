"""wifi_prefer: 定时让无线优先级生效. nmcli / 时间 / 冷却文件 / 事件日志都注入, 不碰真机."""

import json

import wifi_core as wc
import wifi_prefer as wp

SAVED = "\n".join([
    "LabNet5G:u-5g:802-11-wireless:yes:20:no",
    "campus-net:u-cn:802-11-wireless:yes:0:yes",
])
SCAN = "\n".join([
    r" :02\:00\:00\:00\:00\:01:LabNet5G:80:WPA2",
    r"*:02\:00\:00\:00\:00\:02:CAMPUS-NET:60:",
])
SSIDS = {"u-5g": "LabNet5G", "u-cn": "CAMPUS-NET"}


def _run(up_rc=0, scan=SCAN):
    calls = []

    def run(cmd, timeout=60):
        calls.append(cmd)
        if cmd == wc.CMD_SAVED:
            return 0, SAVED
        if cmd[:3] == ["nmcli", "-g", "802-11-wireless.ssid"]:
            return 0, SSIDS[cmd[-1]]
        if cmd[:4] == ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SIGNAL,SECURITY"]:
            return 0, scan
        if cmd[:3] == ["nmcli", "con", "up"]:
            return (up_rc, "" if up_rc == 0 else "Error: 密码不对") if cmd[-1] == "u-5g" else (0, "")
        raise AssertionError(cmd)
    return run, calls


def test_switches_to_higher_priority(tmp_path):
    run, calls = _run()
    events = []
    pick = wp.main(run=run, now=lambda: 100.0, cooldown_path=str(tmp_path / "cool.json"),
                   log=lambda *a: events.append(a))
    assert pick["ssid"] == "LabNet5G" and wc.cmd_up("u-5g") in calls
    assert events and "LabNet5G" in events[0][1]
    assert not (tmp_path / "cool.json").exists()


def test_failed_switch_cools_down_and_goes_back(tmp_path):
    run, calls = _run(up_rc=4)
    cool = tmp_path / "cool.json"
    events = []
    wp.main(run=run, now=lambda: 100.0, cooldown_path=str(cool), log=lambda *a: events.append(a))
    assert json.loads(cool.read_text()) == {"LabNet5G": 100.0 + wc.COOLDOWN_S}
    assert calls[-1] == wc.cmd_up("u-cn")                       # 切回原来的
    assert "失败" in events[0][1]
    # 冷却期间不再试
    run2, calls2 = _run()
    assert wp.main(run=run2, now=lambda: 200.0, cooldown_path=str(cool), log=lambda *a: None) is None
    assert wc.cmd_up("u-5g") not in calls2


def test_nothing_to_do_when_better_not_visible(tmp_path):
    run, calls = _run(scan=r"*:02\:00\:00\:00\:00\:02:CAMPUS-NET:60:")
    assert wp.main(run=run, now=lambda: 1.0, cooldown_path=str(tmp_path / "c"), log=lambda *a: None) is None
    assert not any(c[:3] == ["nmcli", "con", "up"] for c in calls)
