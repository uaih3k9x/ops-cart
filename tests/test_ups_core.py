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


def test_on_battery_only_when_discharging():
    assert uc.on_battery({"state": "discharging", "percent": 90})
    assert not uc.on_battery({"state": "charging", "percent": 90})
    assert not uc.on_battery(None)


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
