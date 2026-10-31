#!/usr/bin/env python3
"""sun_core: 按日出日落判断界面该用深色还是浅色 (#31). 纯函数, 只用标准库, 不联网.

  sun_times(day, lat, lon, tz)    当地某天的日出 / 日落 (NOAA 太阳位置算法); 极昼极夜不抛, polar 标出来
  want_mode(now, conf, last)      -> "dark" / "light"; 档位 auto / dark / light, auto 下日出日落前后留缓冲
  next_switch(now, conf, mode)    auto 下下次切换的时刻 (时间戳), 算不出 / 固定档 -> None
  read_conf(path)                 读 /etc/ops-cart/theme.json; 没文件 -> {}, 读不懂 -> None (按深色)

只判断"现在该用哪套", 不碰界面. 判断用绝对时间 (时间戳), 不需要配时区; tz 只给 sun_times
出当地钟点 (对表、显示用). 结果由 cart-dash 并进快照 (dash_core.theme_sampler).
"""

import datetime as dt
import json
import math

CONF_PATH = "/etc/ops-cart/theme.json"
MODES = ("auto", "dark", "light")
DEFAULT_BUFFER_MIN = 20
MAX_BUFFER_MIN = 180
ZENITH = 90.833            # 日出日落 = 太阳上缘碰地平线: 半径 16' + 大气折射 34' (NOAA 同口径)
_DAY_S = 86400.0


def _sun(ts):
    """时间戳 -> (时差 equation of time 分钟, 赤纬 弧度). 公式照 NOAA Solar Calculator 表格版."""
    jc = (ts / _DAY_S + 2440587.5 - 2451545.0) / 36525.0        # 儒略世纪 (J2000 起)
    l0 = math.radians((280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360)
    m = math.radians(357.52911 + jc * (35999.05029 - 0.0001537 * jc))
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * m) * (0.019993 - 0.000101 * jc) + math.sin(3 * m) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app = math.radians(math.degrees(l0) + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app))
    y = math.tan(eps / 2) ** 2
    eqt = 4 * math.degrees(y * math.sin(2 * l0) - 2 * e * math.sin(m)
                           + 4 * e * y * math.sin(m) * math.cos(2 * l0)
                           - 0.5 * y * y * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m))
    return eqt, decl


def _hour_angle(lat, decl):
    """日出时角 (度). 太阳整天不落 -> "day", 整天不出 -> "night"."""
    lat = math.radians(lat)
    cos_h = (math.cos(math.radians(ZENITH)) / (math.cos(lat) * math.cos(decl))
             - math.tan(lat) * math.tan(decl))
    if cos_h < -1:
        return "day"
    if cos_h > 1:
        return "night"
    return math.degrees(math.acos(cos_h))


def _noon(t, lon):
    """离 t 最近的那次中天 (时间戳). 真太阳时 = UTC 分钟 + 4*经度 + 时差, 中天时 = 720."""
    for _ in range(2):                       # 时差随时间变, 在中天处再算一遍就够准
        eqt, _decl = _sun(t)
        d = (720 - 4 * lon - eqt - (t % _DAY_S) / 60) % 1440
        t += (d - 1440 if d >= 720 else d) * 60
    return t


def _solar_day(t, lat, lon):
    """离 t 最近那次中天所在的一天 -> (日出, 日落, polar). 时间戳; 极昼极夜时日出日落为 None."""
    noon = _noon(t, lon)
    eqt0, decl = _sun(noon)
    polar = _hour_angle(lat, decl)
    if isinstance(polar, str):
        return None, None, polar
    out = []
    for sign in (-1, 1):                     # 在事件时刻上重算赤纬 / 时差, 迭代两次 < 10 秒
        x = noon + sign * polar * 240
        for _ in range(2):
            eqt, decl = _sun(x)
            h = _hour_angle(lat, decl)
            if isinstance(h, str):           # 贴着极昼极夜边上, 用上一步的估计
                break
            x = noon + ((eqt0 - eqt) + sign * 4 * h) * 60
        out.append(x)
    return out[0], out[1], None


def sun_times(day, lat, lon, tz=dt.timezone.utc):
    """当地日期 day (date) 的日出日落. 经度东正西负, tz 是 tzinfo (ZoneInfo 或固定偏移).
    -> {"rise": datetime|None, "set": datetime|None, "polar": None|"day"|"night"}; 不抛."""
    local_noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=tz).timestamp()
    rise, sset, polar = _solar_day(local_noon, lat, lon)
    if polar:
        return {"rise": None, "set": None, "polar": polar}
    return {"rise": dt.datetime.fromtimestamp(rise, tz), "set": dt.datetime.fromtimestamp(sset, tz),
            "polar": None}


def _located(conf):
    return (isinstance(conf, dict) and conf.get("mode", "auto") == "auto"
            and conf.get("lat") is not None and conf.get("lon") is not None)


def _buffer_s(conf):
    return conf.get("buffer_min", DEFAULT_BUFFER_MIN) * 60


def _events(now, conf):
    """now 前一天到后两天的 [(时间戳, "rise"/"set")], 按时间排."""
    out = []
    for k in (-1, 0, 1, 2):
        rise, sset, _polar = _solar_day(now + k * _DAY_S, conf["lat"], conf["lon"])
        if rise is not None:
            out += [(rise, "rise"), (sset, "set")]
    return sorted(set(out))


def _sun_up(now, conf):
    rise, sset, polar = _solar_day(now, conf["lat"], conf["lon"])
    if polar:
        return polar == "day"
    return rise <= now < sset


def want_mode(now, conf, last=None):
    """now (时间戳) 该用哪套 -> "dark" / "light". 不抛.

    conf 是 read_conf() 的结果: None (读不懂) 或 auto 下没经纬度 -> "dark" (现状).
    auto: 太阳在地平线上浅, 下面深; 离日出日落不到缓冲分钟时, 有 last 就维持 last 不来回跳."""
    if not isinstance(conf, dict):
        return "dark"
    mode = conf.get("mode", "auto")
    if mode in ("dark", "light"):
        return mode
    if not _located(conf):
        return "dark"
    buf = _buffer_s(conf)
    if last in ("dark", "light") and any(abs(t - now) < buf for t, _k in _events(now, conf)):
        return last
    return "light" if _sun_up(now, conf) else "dark"


def next_switch(now, conf, mode):
    """当前是 mode 时下次切换的时刻 (时间戳): 深 -> 下次日出 + 缓冲, 浅 -> 下次日落 + 缓冲.
    固定档 / 没经纬度 / 读不懂 / 两天内没有日出日落 (极昼极夜) -> None."""
    if not _located(conf):
        return None
    buf = _buffer_s(conf)
    want = "rise" if mode == "dark" else "set"
    for t, kind in _events(now, conf):
        if kind == want and t + buf > now:
            return t + buf
    return None


def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def read_conf(path=CONF_PATH):
    """读 theme.json -> {"mode", "lat", "lon", "buffer_min"}. 没文件 -> {} (auto 无经纬度 = 深色);
    读不懂 (坏 json / 档位不认识 / 经纬度越界或只给一个 / 缓冲不在 0~180 分钟) -> None. 不抛."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    conf = {"mode": raw.get("mode", "auto"), "lat": raw.get("lat"), "lon": raw.get("lon"),
            "buffer_min": raw.get("buffer_min", DEFAULT_BUFFER_MIN)}
    if conf["mode"] not in MODES or not _num(conf["buffer_min"], 0, MAX_BUFFER_MIN):
        return None
    if (conf["lat"] is None) != (conf["lon"] is None):
        return None
    if conf["lat"] is not None and not (_num(conf["lat"], -90, 90) and _num(conf["lon"], -180, 180)):
        return None
    return conf
