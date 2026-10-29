#!/usr/bin/env python3
"""dash_core: cart-dash 的可测核心 —— 采集快照 / 原子落盘 / 过期判定 / 版面模型.

分工（issue #6）:
  cart-dash  是唯一采集者，每轮把状态写成 /run/ops-cart/state.json（带 ts/seq）
  kiosk.py   以后只读这份快照（本模块先提供 read/stale，kiosk 改造另一步）
  副屏       dash_render 把 layout_view() 的版面模型画成图，本模块不碰 PIL

所有采集都走注入的 ctx，测试里换成假数据，不打真机。
"""

import json
import time

import atomicfile

STATE_PATH = "/run/ops-cart/state.json"
KIOSK_PATH = "/run/ops-cart/kiosk.json"    # kiosk 回写: 锁屏状态等
TODO_PATH = "/opt/pxe/todo.md"
STALE_S = 3.0                              # 快照超过这么久没更新 = 数据源停了


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
        "kiosk": _pull(ctx, "kiosk", {}),           # {"locked": bool, "panel": str}
        "todo": _pull(ctx, "todo", []),
        "dream": _pull(ctx, "dream", ""),
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


def layout_view(snap, locked=False):
    """快照 + 锁屏状态 → 版面模型. 渲染层只认这个结构.

    锁屏时副屏当教室引导牌: 隐藏待办, 只留时间/灯条/网络/公告.
    """
    snap = snap or {}
    kiosk = snap.get("kiosk") or {}
    locked = bool(locked or kiosk.get("locked"))
    rows = snap.get("rows") or []
    bad = [r for r in rows if r.get("state") not in ("OK", "PASS", "ok")]
    view = {
        "locked": locked,
        "clock": time.strftime("%H:%M", time.localtime(snap.get("ts") or time.time())),
        "net": snap.get("net") or "down",
        "lan2": snap.get("lan2") or "",
        "uptime": snap.get("uptime") or "",
        "temps": snap.get("temps") or [],
        "fans": snap.get("fans") or [],
        "lamps": snap.get("lamps") or {},
        "checks": {"total": len(rows), "bad": len(bad),
                   "first_bad": (bad[0].get("label") if bad else ""),
                   "bad_labels": [r.get("label") for r in bad][:3]},
        "todo": [] if locked else (snap.get("todo") or []),
        "dream": snap.get("dream") or "",
        "stale": stale(snap),
    }
    return view


def default_ctx(ddir="/opt/pxe", kvm_count=0):
    """真机采集 (cart-dash 用). 每一样都兜住, 不抛."""
    import kiosk_core as kc
    import lamps_core

    def safe(f, d=None):
        if f is None:
            return d
        try:
            return f()
        except Exception:
            return d

    return {
        "net": lambda: kc.net_probe()[0],    # net_state 返回 (状态, 说明), 只要状态串
        "rows": lambda: kc.selftest_run(kc.selftest_plan_read(), kc.selftest_context()),
        "temps": lambda: kc.temps_status(smart=lambda: None),
        "lan2": lambda: _lan2_addr(kc),
        "fans": kc.fans_status,
        "uptime": lambda: kc.misc_status().get("uptime", ""),
        "lamps": lambda: lamps_core.sample(ddir, kvm_count)[1],
        "kiosk": lambda: read_kiosk(),
        "todo": lambda: read_todo(),
        "dream": kc.dream_line,
        "safe": safe,
        "now": time.time,
    }


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
