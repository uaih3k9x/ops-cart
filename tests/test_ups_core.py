"""ups_core 测试: 只测解析/阈值/配色, 不碰真 UPS."""

import ups_core as uc


UPOWER_OUT = """Device: /org/freedesktop/UPower/devices/ups_hiddev2
  native-path:          /sys/devices/.../hiddev2
  vendor:               American Power Conversion
  model:                Back-UPS BX1200CI-CN
  power supply:         yes
  ups
    present:             yes
    state:               discharging
    warning-level:       discharging
    time to empty:       28.5 minutes
    percentage:          92%
"""


def test_parse_fields():
    u = uc.parse(UPOWER_OUT)
    assert u["state"] == "discharging"
    assert u["percent"] == 92
    assert u["warning"] == "discharging"
    assert u["model"] == "Back-UPS BX1200CI-CN"


def test_parse_missing_percent_is_none():
    assert uc.parse("  ups\n    state: charging\n") is None
    assert uc.parse("") is None


def test_on_battery_from_upower_state():
    assert uc.on_battery({"state": "discharging", "percent": 90})
    assert not uc.on_battery({"state": "charging", "percent": 90})
    assert not uc.on_battery(None)


def test_on_battery_trusts_hid_ob_before_upower_catches_up():
    # upower 的 discharging 要滞后一两分钟, hidraw 的 OB 是即时的
    assert uc.on_battery({"state": "fully-charged", "percent": 85, "status": "OB DISCHRG"})
    assert uc.on_battery({"state": "charging", "percent": 85, "status": "OB"})


def test_on_battery_only_whole_word_ob():
    assert not uc.on_battery({"state": "charging", "percent": 85, "status": "OL CHRG"})
    assert not uc.on_battery({"state": "charging", "percent": 85, "status": "OL"})
    # DISCHRG 里没有完整的 OB, 不算
    assert not uc.on_battery({"state": "charging", "percent": 85, "status": "DISCHRG"})
    assert not uc.on_battery({"state": "charging", "percent": 85, "status": "OBX"})
    assert not uc.on_battery({"state": "charging", "percent": 85, "status": None})


def test_hid_ol_wins_over_lagging_upower_discharging():
    # 来电后 upower 还报 discharging, hidraw 已经 OL: 不能当成走电池去关机
    back = {"state": "discharging", "percent": 10, "status": "OL CHRG"}
    assert not uc.on_battery(back)
    assert not uc.low(back)
    assert uc.health(dict(back, warning="none")) == "OK"


def test_no_hid_status_falls_back_to_upower():
    for status in (None, "", "CHRG"):
        u = {"state": "discharging", "percent": 10, "status": status}
        assert uc.on_battery(u)
        assert uc.low(u)
    assert uc.low({"state": "discharging", "percent": 10})
    assert not uc.on_battery({"state": "charging", "percent": 10, "status": ""})


def test_low_and_health_follow_hid_ob():
    ob = {"state": "charging", "percent": 12, "status": "OB DISCHRG"}
    assert uc.low(ob)
    assert uc.health(ob) == "FAIL"
    assert uc.health(dict(ob, percent=60)) == "WARN"
    assert uc.label(dict(ob, percent=60)) == "电池 60% 放电"
    # 在线时电量低也不关机
    assert not uc.low({"state": "charging", "percent": 10, "status": "OL CHRG"})


def test_low_uses_threshold_and_only_on_battery():
    assert uc.low({"state": "discharging", "percent": 15})
    assert uc.low({"state": "discharging", "percent": 9})
    assert not uc.low({"state": "discharging", "percent": 16})
    # 充电时哪怕数字很低也不该关机
    assert not uc.low({"state": "charging", "percent": 10})


def test_label_reads_naturally():
    assert uc.label({"state": "charging", "percent": 93}) == "电池 93% 充电"
    assert uc.label({"state": "discharging", "percent": 40}) == "电池 40% 放电"
    assert uc.label({"state": "", "percent": 50}) == "电池 50%"
    assert uc.label(None) == "电池 --"


def test_health_colors():
    assert uc.health(None) is None
    assert uc.health({"state": "charging", "percent": 90, "warning": "none"}) == "OK"
    assert uc.health({"state": "discharging", "percent": 50}) == "WARN"
    assert uc.health({"state": "discharging", "percent": 12}) == "FAIL"
    assert uc.health({"state": "charging", "percent": 90, "warning": "replace"}) == "WARN"


def test_device_path_picks_ups_line():
    class R:
        returncode = 0
        stdout = b"/org/freedesktop/UPower/devices/line_power_AC\n" \
                 b"/org/freedesktop/UPower/devices/ups_hiddev2\n"
    got = uc.device_path(runner=lambda cmd: R())
    assert got.endswith("ups_hiddev2")


def test_device_path_missing():
    class R:
        returncode = 0
        stdout = b"/org/freedesktop/UPower/devices/line_power_AC\n"
    assert uc.device_path(runner=lambda cmd: R()) is None


def test_sample_returns_none_when_upower_fails():
    class R:
        returncode = 1
        stdout = b""
    assert uc.sample(path="/x", runner=lambda cmd: R()) is None


def test_sample_attaches_path():
    text = UPOWER_OUT.encode()

    class R:
        returncode = 0

    def runner(cmd):
        r = R()
        r.stdout = text
        return r
    u = uc.sample(path="/org/ups", runner=runner)
    assert u["percent"] == 92 and u["path"] == "/org/ups"


# ------------------------------------------------------- hidraw 合并 (issue #4)

def test_merge_prefers_hidraw_values():
    up = {"state": "discharging", "percent": 96, "warning": "none", "path": "/x"}
    hid = {"load": 18, "voltage": 234, "status": "OB", "runtime": 1802,
           "charge": 95, "source": "hidraw", "path": "/dev/hidraw7"}
    m = uc.merge(up, hid)
    assert m["percent"] == 95              # hidraw 更新的电量优先
    assert m["load"] == 18 and m["voltage"] == 234
    assert m["status"] == "OB"
    assert m["state"] == "discharging"     # state 仍然只有 upower 给
    assert m["source"] == "upower+hidraw"


def test_merge_hidraw_only():
    m = uc.merge(None, {"charge": 88, "load": 5, "status": "OL",
                        "voltage": None, "runtime": None, "source": "hidraw"})
    assert m["percent"] == 88 and m["load"] == 5
    assert "voltage" not in m              # None 不塞进来
    assert m["source"] == "hidraw"


def test_merge_nothing_is_none():
    assert uc.merge(None, None) is None


def test_sample_all_survives_broken_hidraw(monkeypatch):
    monkeypatch.setattr(uc, "sample", lambda path=None, runner=None: {"state": "charging", "percent": 100})

    def boom():
        raise OSError("no permission")
    got = uc.sample_all(hid_reader=boom)
    assert got["percent"] == 100 and got["source"] == "upower"


def test_label_includes_load_and_voltage():
    ups = {"state": "fully-charged", "percent": 100, "load": 18, "voltage": 234.0}
    assert uc.label(ups) == "电池 100% · 18% 负载 · 234V"
    assert uc.detail({"load": None}) == ""
    assert uc.detail(None) == ""


def test_detail_partial():
    assert uc.detail({"voltage": 232.0}) == "232V"
    assert uc.detail({"load": 7}) == "7% 负载"


# ---- hidraw 找 APC: 内核 uevent 的 HID_ID 是补零 8 位 (0003:0000051D:00000002)

def _hidraw_tree(tmp_path, devs):
    for name, hid_id in devs.items():
        d = tmp_path / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text("DRIVER=hid-generic\nHID_ID=%s\nHID_NAME=x\n" % hid_id)
    return str(tmp_path / "hidraw*")


def test_hidraw_nodes_matches_real_padded_hid_id(tmp_path):
    import ups_core as uc
    root = _hidraw_tree(tmp_path, {
        "hidraw0": "0003:00002109:00008888",      # USB hub 的 HID, 不是 UPS
        "hidraw7": "0003:0000051D:00000002",      # 推车上实测的 APC Back-UPS
    })
    assert uc.hidraw_nodes(root) == ["/dev/hidraw7"]


def test_hidraw_nodes_ignores_other_vendors(tmp_path):
    import ups_core as uc
    root = _hidraw_tree(tmp_path, {"hidraw1": "0003:00001164:0000B29D"})
    assert uc.hidraw_nodes(root) == []


# ---- 两段式低电量关机 (#4, 10/31 放电演练后定): 预告 -> 30 秒倒计时 -> 关机

OB = {"state": "discharging", "percent": 60, "status": "OB DISCHRG"}
T = 1_000_000.0


def _ob(**kw):
    return dict(OB, **kw)


def test_stage_normal_on_mains_or_unknown():
    assert uc.shutdown_stage({"state": "charging", "percent": 10, "status": "OL CHRG"}, None, T) == ("normal", None)
    assert uc.shutdown_stage(None, None, T) == ("normal", None)
    assert uc.shutdown_stage(_ob(percent=60, runtime=1500), None, T) == ("normal", None)


def test_stage_warn_by_percent_or_runtime():
    assert uc.shutdown_stage(_ob(percent=uc.WARN_PCT), None, T) == ("warn", None)
    assert uc.shutdown_stage(_ob(percent=uc.WARN_PCT + 1), None, T) == ("normal", None)
    # 电量还高但 UPS 估计剩不到 8 分钟 (开着装机负载大) 也预告
    assert uc.shutdown_stage(_ob(percent=60, runtime=uc.WARN_RUNTIME_S - 1), None, T) == ("warn", None)
    assert uc.shutdown_stage(_ob(percent=60, runtime=uc.WARN_RUNTIME_S), None, T) == ("normal", None)


def test_stage_countdown_starts_at_low_pct_and_counts_by_injected_time():
    assert uc.shutdown_stage(_ob(percent=uc.LOW_PCT), None, T) == ("countdown", uc.COUNTDOWN_S)
    assert uc.shutdown_stage(_ob(percent=14), T, T + 10.2) == ("countdown", 20)
    assert uc.shutdown_stage(_ob(percent=14), T, T + 29.5) == ("countdown", 1)
    assert uc.shutdown_stage(_ob(percent=14), T, T + uc.COUNTDOWN_S) == ("poweroff", 0)
    assert uc.shutdown_stage(_ob(percent=14), T, T + 999) == ("poweroff", 0)


def test_countdown_keeps_going_if_battery_bounces_back_above_threshold():
    # 只有插回市电才取消; 电池电压回弹到 17% 不算
    assert uc.shutdown_stage(_ob(percent=17), T, T + 5) == ("countdown", 25)


def test_countdown_cancelled_only_by_mains():
    assert uc.shutdown_stage({"state": "discharging", "percent": 15, "status": "OL CHRG"}, T, T + 5) == ("normal", None)
    assert uc.shutdown_stage({"state": "charging", "percent": 15}, T, T + 5) == ("normal", None)
    # 插回市电时电量还低: 回到"在市电"的正常态, 不是预告
    assert uc.shutdown_stage({"state": "fully-charged", "percent": 15}, T, T + 5) == ("normal", None)


def test_countdown_fail_safe_when_ups_unreadable():
    # 倒计时中读不到 / 缺字段 = 当仍在电池, 继续关机
    assert uc.shutdown_stage(None, T, T + 5) == ("countdown", 25)
    assert uc.shutdown_stage({}, T, T + 31) == ("poweroff", 0)
    assert uc.shutdown_stage({"state": "", "percent": 15}, T, T + 5) == ("countdown", 25)
    assert uc.shutdown_stage({"state": "unknown", "status": ""}, T, T + 5) == ("countdown", 25)


def test_alert_texts():
    assert uc.alert(OB, "normal", None) is None
    a = uc.alert(_ob(percent=22, runtime=400), "warn", None)
    assert a["level"] == "warn"
    assert a["text"] == "电池约剩 6 分钟，到 15% 自动关机，请插回市电"
    assert a["short"] == "电池约剩 6 分钟 · 15% 自动关机"
    # 没有 hidraw 的剩余时间: 说百分比, 不编分钟数
    a = uc.alert(_ob(percent=22), "warn", None)
    assert a["text"] == "电池剩 22%，到 15% 自动关机，请插回市电"
    assert a["short"] == "电池剩 22% · 15% 自动关机"
    assert uc.alert(_ob(percent=22, runtime=30), "warn", None)["text"].startswith("电池约剩不到 1 分钟")
    c = uc.alert(_ob(percent=15), "countdown", 27)
    assert c["level"] == "bad"
    assert c["text"] == "电量 15%，27 秒后自动关机 · 插回市电可取消"
    assert c["short"] == "27 秒后关机 · 插回市电取消"
    assert uc.alert(None, "countdown", 3)["text"] == "电量 --，3 秒后自动关机 · 插回市电可取消"
    assert uc.alert(_ob(percent=15), "countdown", None)["text"] == "电量 15%，即将自动关机 · 插回市电可取消"
    assert uc.alert(_ob(percent=14), "poweroff", 0)["text"] == "电量 14%，正在关机"


def test_stage_log_only_on_transitions():
    ups = _ob(percent=22, runtime=400)
    assert uc.stage_log(None, "normal", ups) is None
    assert uc.stage_log("normal", "normal", ups) is None
    assert uc.stage_log("warn", "warn", ups) is None
    assert "预告" in uc.stage_log("normal", "warn", ups)
    assert "30 秒" in uc.stage_log("warn", "countdown", _ob(percent=15))
    assert uc.stage_log("countdown", "countdown", _ob(percent=15)) is None
    msg = uc.stage_log("countdown", "normal", {"state": "charging", "percent": 15, "status": "OL"})
    assert "市电恢复" in msg and "取消" in msg
    assert "关机" in uc.stage_log("countdown", "poweroff", _ob(percent=14))
    assert "解除" in uc.stage_log("warn", "normal", {"state": "charging", "percent": 22})


def test_log_goes_to_ops_cart_dir_and_events(tmp_path, monkeypatch):
    assert uc.UPS_LOG == "/var/log/ops-cart/ups.log"
    import cartlog
    monkeypatch.setattr(cartlog, "EVENTS", str(tmp_path / "events.log"))
    uc.log("倒计时开始", path=str(tmp_path / "ups.log"))
    assert "[ups] 倒计时开始" in (tmp_path / "ups.log").read_text(encoding="utf-8")
    assert "[ups] 倒计时开始" in (tmp_path / "events.log").read_text(encoding="utf-8")
