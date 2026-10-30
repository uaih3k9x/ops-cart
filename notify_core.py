#!/usr/bin/env python3
"""notify_core: 推车出了要紧的事推到手机 (issue #16) —— 读现有状态文件, 和上次比, 只在变化时出事件.

状态源 (只读, 不和 cart-dash 抢采集):
  /run/ops-cart/state.json             cart-dash 的快照, 取 UPS (OB/OL、电量、低电量阈值)
  /run/ops-cart/pxe-guard.json         装机口守卫 serving / proxy / 停 (no-link、no-ip)
  /run/ops-cart/campus-auth-status.json 校园网认证结果 (ok 字段, 见 ruijie_portal.OK_STATES)
  systemctl is-active                  pxe-server / kiosk / cart-dash
  boot_id + cart-boot-success          上一次启动有没有走到 boot-success (开机 60 s kiosk 还活着)

每个监视项一个值, None = 这次读不到, 不算变化. diff 的规则 (RULES: 稳定秒数, 冷却秒数):
  - 新值要连续保持 settle 秒才算数; 没稳住就回到原值 = 抖了一下, 只推一条"短暂…已回到…"
  - 推过一条后 cooldown 秒内的变化先压着, 冷却结束时仍和上次推的不同才推最终状态, 附带变过几次
上次状态、冷却记录、没发出去的消息都在 /var/lib/ops-cart/notify.json (原子写).
"""

import copy
import json

import atomicfile
import ups_core

STATE_PATH = "/var/lib/ops-cart/notify.json"
DASH_STATE = "/run/ops-cart/state.json"
GUARD_STATE = "/run/ops-cart/pxe-guard.json"
AUTH_STATUS = "/run/ops-cart/campus-auth-status.json"
SERVICES = ("pxe-server", "kiosk", "cart-dash")
STATE_STALE_S = 90          # cart-dash 快照这么久没更新, UPS 当读不到 (cart-dash 挂了另有服务事件)

# 监视项 -> (settle 秒, cooldown 秒). timer 每 30 s 跑一次: settle 20 = 第二次看到还这样才推.
# UPS 断电、低电量要立刻知道, 不等稳定; 认证 2 分钟才跑一次, 等下一次结果确认.
RULES = {
    "ups.power": (0, 300),
    "ups.low": (0, 1800),
    "guard": (20, 300),
    "auth": (100, 600),
    "svc": (20, 300),
}
# 第一次见到时当作"上次是这样"的正常值; 没列出的 (守卫) 第一次见到就是基线, 不推
NORMAL = {"ups.power": "OL", "ups.low": False, "auth": "ok", "svc": "up"}

_GUARD = {"serving": "serving", "proxy": "proxy", "no-link": "stop", "no-ip": "stop"}
_GUARD_WHY = {"no-link": "没插线", "no-ip": "网口上没有装机地址"}
_LABEL = {
    "ups.power": {"OB": "电池供电", "OL": "市电"},
    "guard": {"serving": "自己发地址", "proxy": "代理模式", "stop": "停发"},
    "auth": {"ok": "在线", "fail": "认证失败"},
    "svc": {"up": "运行", "down": "停了"},
}
_SUBJECT = {"ups.power": "推车供电", "guard": "装机口守卫", "auth": "校园网认证"}


def _rule(key):
    return RULES[key.split(".")[0] if key.startswith("svc.") else key]


def _kind(key):
    return "svc" if key.startswith("svc.") else key


def _one_line(s):
    return " ".join(str(s).split())


# ---------------------------------------------------------------- 采集

def _pull(read, key):
    f = read.get(key)
    if f is None:
        return None
    try:
        return f()
    except Exception:            # noqa: BLE001  单项坏了当读不到, 不拖垮别的
        return None


def collect(read):
    """读取函数 dict -> {"values": {监视项: 值或 None}, "info": 写消息用的细节, "boot": {id, ok}}. 不抛."""
    now = _pull(read, "now")
    snap = _pull(read, "state")
    guard = _pull(read, "guard")
    auth = _pull(read, "auth")
    svcs = _pull(read, "services")
    boot = _pull(read, "boot")
    values, info = {}, {}

    ups = snap.get("ups") if isinstance(snap, dict) else None
    ts = snap.get("ts") if isinstance(snap, dict) else None
    if ups and isinstance(ts, (int, float)) and now is not None and now - ts > STATE_STALE_S:
        ups = None
    if isinstance(ups, dict) and isinstance(ups.get("percent"), (int, float)):
        values["ups.power"] = "OB" if ups_core.on_battery(ups) else "OL"
        values["ups.low"] = bool(ups_core.low(ups))
        info["pct"] = int(ups["percent"])
    else:
        values["ups.power"] = values["ups.low"] = None

    svcs = svcs if isinstance(svcs, dict) else {}
    for name in SERVICES:
        raw = svcs.get(name)
        info[f"svc_state.{name}"] = raw
        if raw == "active":
            values[f"svc.{name}"] = "up"
        elif raw in ("inactive", "failed", "deactivating"):
            values[f"svc.{name}"] = "down"
        else:                       # activating (正在重启)、读不到
            values[f"svc.{name}"] = None

    g = guard.get("state") if isinstance(guard, dict) else None
    values["guard"] = _GUARD.get(g) if values.get("svc.pxe-server") == "up" else None
    info["guard_state"] = g

    if isinstance(auth, dict) and auth.get("state"):
        values["auth"] = "ok" if auth.get("ok") else "fail"
        info["auth_state"] = str(auth["state"])
        info["auth_msg"] = str(auth.get("message") or "")
    else:
        values["auth"] = None

    boot = boot if isinstance(boot, dict) else {}
    return {"values": values, "info": info, "boot": {"id": boot.get("id"), "ok": boot.get("ok")}}


# ---------------------------------------------------------------- 消息: 一行, 先写要做什么/出了什么

def _text(key, old, new, info):
    """监视项从 old 变成 new 的消息; None = 这种变化不推."""
    pct = info.get("pct")
    if key == "ups.power":
        return f"推车转电池供电 {pct}%，市电断了" if new == "OB" else f"推车市电恢复，电池 {pct}%"
    if key == "ups.low":
        return f"推车电池只剩 {pct}%，到关机阈值 {ups_core.LOW_PCT}%，赶紧接市电" if new else None
    if key == "guard":
        if new == "proxy":
            return "装机口转代理模式：线上有别的 DHCP，推车不发地址"
        if new == "serving":
            return "装机口恢复自己发地址"
        return f"装机口停发：{_GUARD_WHY.get(info.get('guard_state'), '守卫没在服务')}"
    if key == "auth":
        st = info.get("auth_state", "")
        if new == "ok":
            return f"校园网认证恢复（{st}）"
        if st == "untrusted":       # message 是认证页主机, 不往外推
            return "校园网认证页换了主机，去无线面板确认要不要信任"
        msg = _one_line(info.get("auth_msg", ""))[:40]
        return f"校园网认证失败（{st}），去无线面板看看" + (f"：{msg}" if msg else "")
    name = key[4:]
    if new == "down":
        return f"推车服务 {name} 挂了（{info.get('svc_state.' + name)}），看 journalctl -u {name}"
    return f"推车服务 {name} 恢复运行"


def _flap_text(key, was, back):
    lab = _LABEL[_kind(key)]
    subject = _SUBJECT.get(key) or f"推车服务 {key[4:]}"
    return f"{subject}短暂{lab.get(was, was)}，已回到{lab.get(back, back)}"


def _prio(key, new, flap=False):
    """ntfy 优先级 1-5."""
    if flap:
        return 2
    if key == "ups.low":
        return 5
    if new in ("OB", "down") or key == "boot":
        return 4
    return 3


def _event(key, text, prio, now):
    return {"key": key, "text": _one_line(text), "prio": prio, "ts": now}


# ---------------------------------------------------------------- diff

def diff(prev, cur, now):
    """(上次的状态记录, collect() 结果, 现在) -> (事件列表, 新的状态记录). 纯函数, 不改 prev."""
    st = copy.deepcopy(prev) if isinstance(prev, dict) else {}
    keys = st.setdefault("keys", {})
    events = []
    for key, v in cur["values"].items():
        if v is None:
            continue
        settle, cool = _rule(key)
        s = keys.get(key)
        if s is None:
            s = keys[key] = {"sent": NORMAL.get(_kind(key), v), "last": 0, "pend": None, "since": 0, "held": 0}
        cooling = now - s["last"] < cool
        if v == s["sent"]:
            if s["pend"] is not None:           # 没稳住就回来了: 抖了一下
                if cooling:
                    s["held"] += 1
                else:
                    events.append(_event(key, _flap_text(key, s["pend"], v), _prio(key, v, True), now))
                    s["last"], s["held"] = now, 0
                s["pend"] = None
            elif not cooling:
                s["held"] = 0
            continue
        if s["pend"] != v:
            s["pend"], s["since"] = v, now
            if cooling:
                s["held"] += 1
        if now - s["since"] < settle or cooling:
            continue
        text = _text(key, s["sent"], v, cur["info"])
        if text:
            if s["held"] > 1:
                text += f"（冷却期内还变过 {s['held'] - 1} 次）"
            events.append(_event(key, text, _prio(key, v), now))
            s["last"] = now
        s["sent"], s["pend"], s["held"] = v, None, 0

    events += _boot(st, cur.get("boot") or {}, now)
    return events, st


def _boot(st, boot, now):
    """换了 boot id 且上一次启动明确没走到 boot-success -> 一条事件. ok 一次为真就一直为真."""
    bid, ok = boot.get("id"), boot.get("ok")
    if not bid:
        return []
    old = st.get("boot") or {}
    out = []
    if old.get("id") and old["id"] != bid and old.get("ok") is False:
        out.append(_event("boot", "推车刚才非正常重启：上一次启动没走到 boot-success，看 journalctl -b -1",
                          _prio("boot", None), now))
    if old.get("id") == bid:
        ok = True if old.get("ok") is True or ok is True else (old.get("ok") if ok is None else ok)
    st["boot"] = {"id": bid, "ok": ok}
    return out


# ---------------------------------------------------------------- 持久化

def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def save_state(path, st):
    atomicfile.write_json(path, st)
