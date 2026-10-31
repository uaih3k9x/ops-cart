#!/usr/bin/env python3
"""dash_core: cart-dash 的可测核心 —— 采集快照 / 原子落盘 / 过期判定 / 版面模型.

分工（issue #6）:
  cart-dash  是唯一采集者，每轮把状态写成 /run/ops-cart/state.json（带 ts/seq）
  kiosk.py   以后只读这份快照（本模块先提供 read/stale，kiosk 改造另一步）
  副屏       dash_render 把 layout_view() 的版面模型画成图，本模块不碰 PIL

所有采集都走注入的 ctx，测试里换成假数据，不打真机。
"""

import json
import re
import time

import atomicfile
import lamps_core
import lldp_core
import pxeserver
import ups_core

STATE_PATH = "/run/ops-cart/state.json"
KIOSK_PATH = "/run/ops-cart/kiosk.json"    # kiosk 回写: 锁屏状态等
TODO_PATH = "/opt/pxe/todo.md"
NOTICE_PATH = "/opt/pxe/notice.txt"        # 锁屏公告 (kiosk 锁屏也读它)
STALE_S = 3.0                              # 快照超过这么久没更新 = 数据源停了

# 慢采集项各自的节奏(秒). 快照照样每轮写, 这些项在周期内复用上次结果:
# 自检要 ping/systemctl/v4l2-ctl, 每 200 ms 跑一遍既浪费又会在断网时拖过 STALE_S.
# 没列出来的 (lamps / kiosk) 每轮都取, 灯条闪烁靠它.
PERIODS = {"net": 5, "rows": 10, "temps": 2, "fans": 2, "lan2": 5, "uptime": 30,
           "todo": 30, "dream": 60, "notice": 5, "ups": 5, "lldp": 2, "pxe": 1}
# LLDP 监听哪些口 (#14): 跟下排 LAN1 / LAN2 灯同一份配置, 网口名不另写
LLDP_IFACES = [(name, key[4:]) for name, _label, key in lamps_core.LOWER
               if name in ("LAN1", "LAN2") and key.startswith("net:")]


def _pull(ctx, key, default):
    """取一项采集结果: 没给 / 抛异常都退默认值, 单项坏不拖垮整份快照."""
    f = ctx.get(key)
    if f is None:
        return default
    try:
        return f()
    except Exception:
        return default


def collect(ctx):
    """一轮采集 → 快照 dict. 缺项或单项抛异常都退默认, 本函数不抛."""
    now = ctx.get("now") or time.time
    snap = {
        "ts": now(),
        "seq": ctx.get("seq", 0),
        "net": _pull(ctx, "net", "down"),           # "on"/"warn"/"off"
        "rows": _pull(ctx, "rows", []),             # 自检行 [{label,state,detail}]
        "lan2": _pull(ctx, "lan2", ""),             # LAN2 管理口地址
        "temps": _pull(ctx, "temps", []),
        "fans": _pull(ctx, "fans", []),
        "uptime": _pull(ctx, "uptime", ""),
        "lamps": _pull(ctx, "lamps", {}),           # {"con-1": n, ..., "kvm": n, "pxe": bytes}
        "slots": _pull(ctx, "slots", []),           # 操作头 [{dev, present, baud, count}]
        "notice": _pull(ctx, "notice", ""),
        "ups": _pull(ctx, "ups", None),             # ups_core.sample_all(): upower + hidraw
        "kiosk": _pull(ctx, "kiosk", {}),           # {"locked": bool, "panel": str}
        "todo": _pull(ctx, "todo", []),
        "dream": _pull(ctx, "dream", ""),
        "lldp": _pull(ctx, "lldp", {}),             # {"LAN1": {state, age, neighbor, error}} 见 lldp_core
        "pxe": _pull(ctx, "pxe", {}),               # pxe-guard.json (守卫在跑才有), 自己发地址限时 #11
    }
    return snap


def write(path, snap):
    """原子写快照 (推车会断电, 不能留半个 json)."""
    atomicfile.write_json(path, snap)
    return snap


def read(path=STATE_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stale(snap, now=None, limit=STALE_S):
    """快照过期 = 采集停了. 没快照/没 ts 也算过期."""
    if not isinstance(snap, dict) or not isinstance(snap.get("ts"), (int, float)):
        return True
    return (now or time.time()) - snap["ts"] > limit


def read_kiosk(path=KIOSK_PATH):
    d = read(path)
    return d if isinstance(d, dict) else {}


class KioskReporter:
    """kiosk 回写 kiosk.json: 锁屏状态 + 键鼠计数. 在 kiosk 的采样线程里调,
    只在值变了才落盘; 写失败吞掉 (/run 没建好也不能拖垮车机界面)."""

    def __init__(self, path=KIOSK_PATH):
        self.path, self._last = path, None

    def update(self, locked, kvm, ups_since=None):
        state = {"locked": bool(locked), "kvm": int(kvm)}
        if ups_since is not None:           # 低电量关机倒计时的起点 (#4), 副屏据此算剩几秒
            state["ups_since"] = ups_since
        if state == self._last:
            return False
        try:
            atomicfile.write_json(self.path, state)
        except OSError:
            return False
        self._last = state
        return True


def read_todo(path=TODO_PATH, limit=6):
    """/opt/pxe/todo.md 里没勾掉的条目, 最多 limit 条."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("- [ ]") or t.startswith("* [ ]"):
            out.append(t[5:].strip())
        elif t.startswith("- ") and "[x]" not in t.lower():
            out.append(t[2:].strip())
        if len(out) >= limit:
            break
    return out


WEEKDAYS = "一二三四五六日"
PROBLEM_ORDER = {"FAIL": 0, "WARN": 1}


def short_uptime(text):
    """"up 1 day, 3 hours, 4 minutes" -> "1天3小时"; 只留最大的两级."""
    units = [("week", "周"), ("day", "天"), ("hour", "小时"), ("minute", "分")]
    parts = []
    for en, zh in units:
        m = re.search(r"(\d+) %ss?\b" % en, text or "")
        if m:
            parts.append(m.group(1) + zh)
    return "".join(parts[:2])


def _checks(rows):
    """自检汇总. "--" (没插/待机) 不算异常, 只有 WARN/FAIL 进问题列表, 故障排前."""
    states = [r.get("state") for r in rows]
    problems = sorted((r for r in rows if r.get("state") in PROBLEM_ORDER),
                      key=lambda r: PROBLEM_ORDER[r["state"]])
    return {"ok": states.count("OK"), "warn": states.count("WARN"), "fail": states.count("FAIL"),
            "problems": [{"label": r.get("label", ""), "state": r["state"],
                          "detail": r.get("detail", "")} for r in problems]}


def _ups_stage(ups, kiosk, now):
    """低电量关机阶段 (#4): 跟车机同一个 ups_core.shutdown_stage. 倒计时起点只有 kiosk 知道 (它真关机),
    从 kiosk.json 读; 没回写起点时倒计时不编秒数 (left=None -> "即将自动关机")."""
    since = kiosk.get("ups_since")
    since = since if isinstance(since, (int, float)) else None
    stage, left = ups_core.shutdown_stage(ups, since, now)
    if stage == "countdown" and since is None:
        left = None
    return stage, left


def _ups_line(ups, stage="normal"):
    """副屏头部的 UPS 一行. 走不走电池跟车机同一个判断 (ups_core.on_battery).
    走电池是琥珀; 只有进了关机倒计时才红 (预告阶段不用红, 10/31 演练定的)."""
    if not ups:
        return {"text": "UPS 未连接", "level": "dim"}
    pct = ups.get("percent")
    if ups_core.on_battery(ups):
        left = " · 剩 %d 分" % (int(ups["runtime"]) // 60) if ups.get("runtime") else ""
        # short: 头部右边放不下全文时用 (大时钟占了左边大半), 见 dash_render.ups_fit
        return {"text": "市电断开 · 电池 %s%%" % pct + left, "short": "断电 · %s%%" % pct + left,
                "level": "bad" if stage in ("countdown", "poweroff") else "warn"}
    text = "电池 %s%%" % pct
    if ups.get("load") is not None:
        text += " · 负载 %s%%" % ups["load"]
    return {"text": text, "level": "ok"}


def _heads(slots, active):
    """上排 4 个操作头: 文字同主屏执行器条 (波特率 / 空闲 / 未插)."""
    out = []
    for n, slot in enumerate(slots or [], 1):
        text, health = lamps_core.slot_view(n, slot)
        out.append({"n": n, "text": text.split("\n", 1)[1], "health": health,
                    "active": "con-%d" % n in active})
    return out


def _lower(rows, active):
    """下排: 键鼠 / PXE / LAN1 / LAN2 / Wi-Fi, 健康看自检那一行, 闪看计数器."""
    return [{"name": name, "health": lamps_core.health_of(rows, label), "active": counter in active}
            for name, label, counter in lamps_core.LOWER]


def peers(snap):
    """LAN1 / LAN2 的 LLDP 对端行 [{name, text, level}]; 快照里没这项 (老 cart-dash) 就空."""
    lldp = (snap or {}).get("lldp")
    if not isinstance(lldp, dict):
        return []
    out = []
    for name, _iface in LLDP_IFACES:
        if name in lldp:
            text, level = lldp_core.peer_line(lldp[name])
            out.append({"name": name, "text": text, "level": level})
    return out


def _serve(guard, now):
    """推车自己给整个网段发地址 (#11): 头部一整条, 倒计时按墙上时间走; 别的状态 None."""
    n = pxeserver.serve_notice(guard, now)
    if n is None:
        return None
    if n["level"] == "expired":
        return {"text": n["text"] + " · 车机上续期", "level": "warn"}
    return {"text": n["text"], "level": "bad"}


def layout_view(snap, locked=False, active=(), now=None):
    """快照 + 锁屏状态 + 正在动的计数器 → 版面模型. 渲染层只认这个结构.

    锁屏时副屏当教室引导牌: 只留时间/灯条/网络/公告, 待办和昨夜整理都藏.
    active: 最近变过的计数器名 (lamps_core.Activity 算, dash_service 传进来).
    时钟走真实时间而不是快照时间: 采集停了钟也照走, "数据源停了" 另外提示.
    """
    snap = snap or {}
    kiosk = snap.get("kiosk") or {}
    locked = bool(locked or kiosk.get("locked"))
    rows = snap.get("rows") or []
    active = set(active or ())
    now = time.time() if now is None else now
    t = time.localtime(now)
    ups_stage, ups_left = _ups_stage(snap.get("ups"), kiosk, now)
    view = {
        "locked": locked,
        "clock": time.strftime("%H:%M", t),
        "date": "%d月%d日 周%s" % (t.tm_mon, t.tm_mday, WEEKDAYS[t.tm_wday]),
        "net": snap.get("net") or "down",
        "lan2": snap.get("lan2") or "",
        "ups": _ups_line(snap.get("ups"), ups_stage),
        "ups_alert": ups_core.alert(snap.get("ups"), ups_stage, ups_left),   # 预告/倒计时横条 (#4)
        "uptime": short_uptime(snap.get("uptime") or ""),
        "temps": snap.get("temps") or [],
        "fans": snap.get("fans") or [],
        "heads": _heads(snap.get("slots"), active),
        "lower": _lower(rows, active),
        "checks": _checks(rows),
        "todo": [] if locked else (snap.get("todo") or []),
        "dream": "" if locked else (snap.get("dream") or ""),
        "notice": (snap.get("notice") or "") if locked else "",
        "peers": [] if locked else peers(snap),      # 锁屏是教室引导牌, 不露交换机名
        "serve": _serve(snap.get("pxe"), now),       # 锁屏也显示: 发地址影响整个网段
        "stale": stale(snap, now),
    }
    return view


def read_notice(path=NOTICE_PATH):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def every(period, f, clock=time.monotonic):
    """包一层: period 秒内复用上次结果. 出错也记住, 一个周期后再试 (别每轮都撞超时)."""
    st = {"at": None, "val": None, "err": None}

    def get():
        t = clock()
        if st["at"] is None or t - st["at"] >= period:
            st["at"] = t
            try:
                st["val"], st["err"] = f(), None
            except Exception as e:
                st["err"] = e
        if st["err"] is not None:
            raise st["err"]
        return st["val"]
    return get


def default_ctx(ddir=None, clock=time.monotonic, lldp=None):
    """真机采集 (cart-dash 用). 每一样都兜住, 不抛.

    ddir: conbroker 数据目录, 默认和车机一样取 serial_core.data_dir (跑服务那个用户的
    ~/.conbroker), 不是 /opt/pxe. 键鼠计数在 kiosk 进程里, 从它回写的 kiosk.json 读.
    lldp: 监听器的 snapshot (dash_service 起线程后传进来); 不给就是空.
    """
    import kiosk_core as kc
    import serial_core as sc
    if ddir is None:
        ddir = sc.data_dir(sc.serial_conf_read())

    def safe(f, d=None):
        if f is None:
            return d
        try:
            return f()
        except Exception:
            return d

    ctx = {
        "net": lambda: kc.net_probe()[0],    # net_state 返回 (状态, 说明), 只要状态串
        "rows": lambda: kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context()),
        "temps": lambda: kc.temps_status(smart=lambda: None),
        "lan2": lambda: _lan2_addr(kc),
        "fans": lambda: kc.fans_status(),
        "uptime": lambda: kc.misc_status().get("uptime", ""),
        "lamps": lambda: lamps_core.sample(ddir, read_kiosk().get("kvm", 0))[1],
        "slots": lambda: lamps_core.slot_status(ddir),
        "notice": lambda: read_notice(),
        "kiosk": lambda: read_kiosk(),
        "todo": lambda: read_todo(),
        "dream": lambda: kc.dream_line(),
        "ups": lambda: _ups_sample(),
        "lldp": lldp or (lambda: {}),
        "pxe": lambda: kc.pxe_guard_live(),
    }
    for key, period in PERIODS.items():
        ctx[key] = every(period, ctx[key], clock)
    ctx.update(safe=safe, now=time.time)
    return ctx


def _ups_sample():
    return ups_core.sample_all()


def _lan2_addr(kc):
    """LAN2(管理口)上那个 IPv4 地址; 找不到返回空串."""
    try:
        out = kc.run(["ip", "-4", "-br", "addr", "show", kc.LAN2_CON])[1]
    except Exception:
        return ""
    for tok in out.split():
        if tok.count(".") == 3 and "/" in tok:
            return tok.split("/")[0]
    return ""


def main():                                  # 手动跑一次, 打印快照
    import argparse
    ap = argparse.ArgumentParser(description="cart-dash 采集一轮并落盘")
    ap.add_argument("--path", default=STATE_PATH)
    ap.add_argument("--print", action="store_true", dest="show")
    a = ap.parse_args()
    snap = write(a.path, collect(default_ctx()))
    if a.show:
        print(json.dumps(snap, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
