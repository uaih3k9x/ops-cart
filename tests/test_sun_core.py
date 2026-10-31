"""sun_core 测试: 日出日落对公开表 (误差 <= 2 分钟)、极昼极夜、三档 / 缓冲 / 没配置 / 配置坏了 / last 维持.

参考值来源: PyEphem 4.2.1 (XEphem libastro, VSOP87 星历, 和 NOAA 算法互相独立) 按 USNO 口径
(太阳上缘、折射 34'、气压 0、海拔 0) 算出再取整到分钟, 2026-10-31 生成, 生成脚本见文末注释.
伦敦一行另和英国气象局 (Met Office) 公布的 2026 夏至 04:43 / 21:21 对过. timeanddate.com 和
USNO API 在开发机上抓不到 (Cloudflare 拦 / 代理 502), 没直接用.
"""

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import pytest

import sun_core as sc

# (地点, 纬度, 经度, 时区, 当地日期, 日出, 日落) 日出日落写当地完整钟点, 日落可能跨到第二天
REFS = [
    ("伦敦 夏至", 51.508, -0.126, "Europe/London", "2026-06-21", "2026-06-21 04:43", "2026-06-21 21:22"),
    ("悉尼 南半球", -33.87, 151.21, "Australia/Sydney", "2026-12-21", "2026-12-21 05:41", "2026-12-21 20:05"),
    ("乌斯怀亚 南半球高纬", -54.80, -68.30, "America/Argentina/Ushuaia", "2026-12-21",
     "2026-12-21 04:51", "2026-12-21 22:11"),
    ("雷克雅未克 日落过午夜", 64.15, -21.94, "Atlantic/Reykjavik", "2026-06-21",
     "2026-06-21 02:55", "2026-06-22 00:04"),
    ("特罗姆瑟 高纬", 69.65, 18.96, "Europe/Oslo", "2026-05-10", "2026-05-10 02:25", "2026-05-10 23:02"),
    ("北京 UTC 里日出在前一天", 39.91, 116.40, "Asia/Shanghai", "2026-03-20",
     "2026-03-20 06:19", "2026-03-20 18:26"),
    ("檀香山 UTC 里日落在后一天", 21.31, -157.86, "Pacific/Honolulu", "2026-01-15",
     "2026-01-15 07:12", "2026-01-15 18:11"),
    ("基里蒂马蒂 UTC+14 跨日期线", 1.87, -157.43, "Pacific/Kiritimati", "2026-09-01",
     "2026-09-01 06:26", "2026-09-01 18:34"),
    ("阿皮亚 UTC+13 西经", -13.83, -171.77, "Pacific/Apia", "2026-07-01", "2026-07-01 06:51", "2026-07-01 18:11"),
]
TOL_S = 120


def _at(text, tz):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


@pytest.mark.parametrize("name,lat,lon,tzname,day,rise,sset", REFS, ids=[r[0] for r in REFS])
def test_sun_times_match_public_table(name, lat, lon, tzname, day, rise, sset):
    tz = ZoneInfo(tzname)
    st = sc.sun_times(dt.date.fromisoformat(day), lat, lon, tz)
    assert st["polar"] is None
    assert abs((st["rise"] - _at(rise, tz)).total_seconds()) <= TOL_S
    assert abs((st["set"] - _at(sset, tz)).total_seconds()) <= TOL_S
    assert st["rise"].isoformat()[:10] == rise[:10] and st["set"].isoformat()[:10] == sset[:10]


def test_polar_day_and_night_no_raise():           # 朗伊尔城 / 特罗姆瑟: PyEphem 同样报 AlwaysUp / NeverUp
    tz = dt.timezone(dt.timedelta(hours=1))
    summer = sc.sun_times(dt.date(2026, 6, 21), 78.22, 15.65, tz)       # 朗伊尔城
    winter = sc.sun_times(dt.date(2026, 12, 21), 78.22, 15.65, tz)
    assert summer == {"rise": None, "set": None, "polar": "day"}
    assert winter == {"rise": None, "set": None, "polar": "night"}
    tromso = sc.sun_times(dt.date(2026, 12, 10), 69.65, 18.96, tz)      # 北极圈边上的极夜
    assert tromso["polar"] == "night"


def test_sun_times_default_tz_is_utc():
    st = sc.sun_times(dt.date(2026, 3, 20), 0.0, 0.0)
    assert st["rise"].utcoffset() == dt.timedelta(0)
    assert abs(st["rise"].hour * 60 + st["rise"].minute - 6 * 60) <= 10     # 赤道春分 ~06:04 UTC


# ---- 模式选择 ----

LONDON = {"mode": "auto", "lat": 51.508, "lon": -0.126, "buffer_min": 20}
UTC = dt.timezone.utc


def _ev(day=dt.date(2026, 6, 21), conf=LONDON):
    st = sc.sun_times(day, conf["lat"], conf["lon"], UTC)
    return st["rise"].timestamp(), st["set"].timestamp()


def test_auto_day_is_light_night_is_dark():
    rise, sset = _ev()
    assert sc.want_mode((rise + sset) / 2, LONDON) == "light"
    assert sc.want_mode(sset + 3 * 3600, LONDON) == "dark"
    assert sc.want_mode(rise - 2 * 3600, LONDON) == "dark"


def test_auto_without_last_follows_sun_inside_buffer():
    rise, sset = _ev()
    assert sc.want_mode(rise + 60, LONDON) == "light"
    assert sc.want_mode(sset - 60, LONDON) == "light"
    assert sc.want_mode(sset + 60, LONDON) == "dark"


def test_buffer_keeps_last_inside_window():
    rise, sset = _ev()
    for t in (rise - 19 * 60, rise + 19 * 60):
        assert sc.want_mode(t, LONDON, "dark") == "dark"
        assert sc.want_mode(t, LONDON, "light") == "light"
    assert sc.want_mode(rise + 21 * 60, LONDON, "dark") == "light"       # 出了窗口照太阳
    assert sc.want_mode(sset + 19 * 60, LONDON, "light") == "light"
    assert sc.want_mode(sset + 21 * 60, LONDON, "light") == "dark"


def test_buffer_zero_and_custom():
    rise, _ = _ev()
    assert sc.want_mode(rise + 60, dict(LONDON, buffer_min=0), "dark") == "light"
    assert sc.want_mode(rise + 40 * 60, dict(LONDON, buffer_min=45), "dark") == "dark"


def test_fixed_modes_ignore_sun_and_last():
    rise, sset = _ev()
    noon, night = (rise + sset) / 2, sset + 3 * 3600
    assert sc.want_mode(noon, dict(LONDON, mode="dark"), "light") == "dark"
    assert sc.want_mode(night, dict(LONDON, mode="light"), "dark") == "light"
    assert sc.want_mode(noon, {"mode": "light"}) == "light"             # 固定档不要经纬度


def test_auto_without_location_or_bad_conf_is_dark():
    noon = (sum(_ev())) / 2
    assert sc.want_mode(noon, {}) == "dark"
    assert sc.want_mode(noon, {"mode": "auto"}) == "dark"
    assert sc.want_mode(noon, None) == "dark"
    assert sc.want_mode(noon, None, "light") == "dark"


def test_auto_polar():
    svalbard = {"mode": "auto", "lat": 78.22, "lon": 15.65, "buffer_min": 20}
    june = dt.datetime(2026, 6, 21, 23, 0, tzinfo=UTC).timestamp()
    dec = dt.datetime(2026, 12, 21, 11, 0, tzinfo=UTC).timestamp()
    assert sc.want_mode(june, svalbard, "dark") == "light"
    assert sc.want_mode(dec, svalbard, "light") == "dark"
    assert sc.next_switch(june, svalbard, "light") is None              # 两天内没有日落


def test_next_switch():
    rise, sset = _ev()
    noon = (rise + sset) / 2
    assert sc.next_switch(noon, LONDON, "light") == pytest.approx(sset + 20 * 60, abs=1)
    rise2, _ = _ev(dt.date(2026, 6, 22))
    assert sc.next_switch(sset + 3600, LONDON, "dark") == pytest.approx(rise2 + 20 * 60, abs=1)
    assert sc.next_switch(rise + 5 * 60, LONDON, "dark") == pytest.approx(rise + 20 * 60, abs=1)
    assert sc.next_switch(noon, dict(LONDON, mode="dark"), "dark") is None
    assert sc.next_switch(noon, {}, "dark") is None
    assert sc.next_switch(noon, None, "dark") is None


def test_next_switch_agrees_with_want_mode():
    rise, sset = _ev()
    t, mode = rise - 3 * 3600, "dark"
    for _ in range(3):                       # 沿着切换点走: 切换前一秒维持, 切换后一秒翻过去
        nxt = sc.next_switch(t, LONDON, mode)
        assert sc.want_mode(nxt - 1, LONDON, mode) == mode
        flipped = sc.want_mode(nxt + 1, LONDON, mode)
        assert flipped != mode
        t, mode = nxt + 1, flipped


# ---- 配置 ----

def test_read_conf_missing_is_empty(tmp_path):
    assert sc.read_conf(str(tmp_path / "nope.json")) == {}


def test_read_conf_ok_and_defaults(tmp_path):
    p = tmp_path / "theme.json"
    p.write_text(json.dumps({"lat": 51.5, "lon": -0.1}))
    assert sc.read_conf(str(p)) == {"mode": "auto", "lat": 51.5, "lon": -0.1, "buffer_min": 20}
    p.write_text(json.dumps({"mode": "light", "buffer_min": 5}))
    assert sc.read_conf(str(p)) == {"mode": "light", "lat": None, "lon": None, "buffer_min": 5}


@pytest.mark.parametrize("text", [
    "{not json", "[]", '{"mode": "night"}', '{"lat": 95, "lon": 0}', '{"lat": 30}',
    '{"lat": "30", "lon": 120}', '{"lat": true, "lon": 0}', '{"lat": 30, "lon": 200}',
    '{"buffer_min": -1}', '{"buffer_min": 999}',
])
def test_read_conf_bad_is_none(tmp_path, text):
    p = tmp_path / "theme.json"
    p.write_text(text)
    assert sc.read_conf(str(p)) is None


def test_example_conf_is_valid():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "theme.example.json")
    conf = sc.read_conf(path)
    assert conf and conf["mode"] == "auto" and conf["lat"] is not None


# 参考值生成 (开发机上, pip install ephem==4.2.1):
#   o = ephem.Observer(); o.lat, o.lon = "51.508", "-0.126"; o.pressure = 0; o.horizon = "-0:34"
#   o.date = <当地正午的 UTC>; o.previous_rising(ephem.Sun()), o.next_setting(ephem.Sun())
