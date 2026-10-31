#!/usr/bin/env python3
"""dash_core: cart-dash 的可测核心 —— 采集快照 / 原子落盘 / 过期判定 / 版面模型.

分工（issue #6）:
  cart-dash  是唯一采集者，每轮把状态写成 /run/ops-cart/state.json（带 ts/seq）
  kiosk.py   以后只读这份快照（本模块先提供 read/stale，kiosk 改造另一步）
  副屏       dash_render 把 layout_view() 的版面模型画成图，本模块不碰 PIL

所有采集都走注入的 ctx，测试里换成假数据，不打真机。
"""

import json
import os
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
           "todo": 30, "dream": 60, "notice": 5, "ups": 5, "lldp": 2, "pxe": 1,
           "svcs": 5, "version": 300}
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
        "svcs": _pull(ctx, "svcs", {}),             # {"chronyd": (enabled, active), "kiosk": ...} 开机自检用 #26
        "version": _pull(ctx, "version", ""),       # "Ver 1.08.03   Build ..." 开机自检抬头
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


def _pid_alive(pid):
    return os.path.exists("/proc/%d" % pid)


def kiosk_live(path=KIOSK_PATH, alive=_pid_alive):
    """kiosk.json + "alive": 写它的 kiosk 进程还在不在 (#26).

    kiosk.json 只在值变了才写, 可能半天不动, 不能按 mtime 判活; 改看里面的 pid.
    没文件 = 还没起来 (/run 开机清空); 有文件但 pid 没了 = 挂了/在重启; 没 pid = 老版 kiosk, 也算没就绪."""
    doc = read_kiosk(path)
    pid = doc.get("pid")
    return dict(doc, alive=isinstance(pid, int) and bool(alive(pid)))


class KioskReporter:
    """kiosk 回写 kiosk.json: 锁屏状态 + 键鼠计数 + pid. 在 kiosk 的采样线程里调,
    只在值变了才落盘; 写失败吞掉 (/run 没建好也不能拖垮车机界面).

    采样线程在界面建好后才跑, 所以第一次写出来 = kiosk 初始化完了; 副屏拿 pid 判活 (#26)."""

    def __init__(self, path=KIOSK_PATH, pid=None):
        self.path, self._last = path, None
        self.pid = os.getpid() if pid is None else pid

    def update(self, locked, kvm, ups_since=None):
        state = {"locked": bool(locked), "kvm": int(kvm), "pid": self.pid}
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


# ---- 开机自检 (#26): kiosk 没就绪前副屏不画工作页/锁屏 (那些是旧的或残缺的), 改画逐行自检

BOOT_UNITS = ("chronyd", "kiosk")       # 只多查这两个服务的 is-active, 别的全用快照里已有的
BOOT_LATE_S = 45                        # 开机这么久还没好的项标琥珀
BOOT_STEP_S = 0.5                       # 逐行出结果的间隔; 跟副屏推送上限 (2 fps) 一致, 不为动画加帧
BOOT_MIN_S = 4                          # 进了自检至少停这么久: kiosk 重启不到 1 秒就回写, 不然自检页只闪一下 (10/31)
BOOT_HOLD_S = 1.5                       # 最后一行 / "车机 kiosk OK" 亮出后再停这么久才切状态板
BOOT_STATES = ("ok", "warn", "fail", "off", "wait", "late")
BOOT_STATUS = {"ok": "OK", "warn": "注意", "fail": "故障", "off": "--", "wait": "等待", "late": "未就绪"}
TIME_SANE = 1735689600                  # 2025-01-01: 早于这个 = RTC 掉电回到 1970 了
_ROW_STATE = {"OK": "ok", "FAIL": "fail", "WARN": "wait", "--": "off"}   # 开机时 WARN 多半是还没好
_PART = {"OK": "OK", "WARN": "注意", "FAIL": "故障"}


def _item(key, label, state, detail="", status=None):
    return {"key": key, "label": label, "state": state, "detail": detail,
            "status": status or BOOT_STATUS[state]}


def _svc(snap, unit):
    """(enabled, active); 快照里没有 (还没采到) 给 None."""
    v = (snap.get("svcs") or {}).get(unit)
    return (bool(v[0]), bool(v[1])) if isinstance(v, (list, tuple)) and len(v) == 2 else None


def _row(rows, check=None, label=None):
    return next((r for r in rows if (check and r.get("check") == check)
                 or (label and r.get("label") == label)), None)


def _row_item(key, label, rows, row):
    if not rows:
        return _item(key, label, "wait")                 # 自检还没跑完第一轮
    if row is None:
        return _item(key, label, "off", "不在自检计划里")
    return _item(key, label, _ROW_STATE.get(row.get("state"), "wait"), row.get("detail", ""))


def _worst(states):
    for s in ("fail", "wait", "ok"):
        if s in states:
            return s
    return "off"


def _boot_time(snap, now):
    label = "系统时间 / 时钟同步"
    t = time.strftime("%m-%d %H:%M", time.localtime(now))
    if now < TIME_SANE:
        return _item("time", label, "fail", "时钟不对 (%s)" % time.strftime("%Y", time.localtime(now)))
    svc = _svc(snap, "chronyd")
    if svc is None:
        return _item("time", label, "wait", t)
    if svc[1]:
        return _item("time", label, "ok", t + " · chronyd")
    return _item("time", label, "wait" if svc[0] else "off", t + (" · 等 chronyd" if svc[0] else " · 没开对时"))


def _boot_ups(snap):
    ups = snap.get("ups")
    if not ups:
        return _item("ups", "UPS", "wait", "未连接")
    if ups_core.on_battery(ups):
        return _item("ups", "UPS", "warn", "市电断开 · 电池 %s%%" % ups.get("percent"))
    return _item("ups", "UPS", "ok", "市电 %s%%" % ups.get("percent"))


def _boot_net(rows):
    label = "网口 LAN1 / LAN2 / Wi-Fi"
    if not rows:
        return _item("net", label, "wait")
    healths = [lamps_core.health_of(rows, lab) for name, lab, _ in lamps_core.LOWER
               if name in ("LAN1", "LAN2", "Wi-Fi")]
    states = [{"OK": "ok", "WARN": "wait", "FAIL": "fail"}.get(h, "off") for h in healths]
    return _item("net", label, _worst(states), " / ".join(_PART.get(h, "--") for h in healths))


def _boot_heads(slots):
    label = "串口操作头 1–4"
    if not slots:
        return _item("heads", label, "wait")
    n = sum(1 for s in slots if s.get("present"))
    return _item("heads", label, "ok" if n else "off", "" if n else "一根没插",
                 status="%d/%d" % (n, len(slots)))


def _boot_kvm(rows):
    """采集卡 (selftest.json 里 check=signal 的行, 可能好几张) + 键鼠 CH9329 合一行."""
    label = "采集卡 / 键鼠"
    if not rows:
        return _item("kvm", label, "wait")
    caps = [r.get("state") for r in rows if r.get("check") == "signal"]
    cap = "FAIL" if "FAIL" in caps else "OK" if "OK" in caps else "--"
    kb = (_row(rows, check="ch9329", label="CH9329") or {}).get("state", "--")
    parts = [cap, kb] if caps else [kb]
    states = [{"OK": "ok", "FAIL": "fail"}.get(p, "off") for p in parts]
    state = "fail" if "fail" in states else "ok" if "ok" in states else "off"
    detail = ("采集 %s · " % _PART.get(cap, "--") if caps else "") + "键鼠 " + _PART.get(kb, "--")
    return _item("kvm", label, state, detail)


def _boot_kiosk(snap):
    label = "车机 kiosk"
    k = snap.get("kiosk") or {}
    if k.get("alive"):
        return _item("kiosk", label, "ok", "就绪")
    if isinstance(k.get("pid"), int):                    # 写过 kiosk.json, 进程没了: 挂了或在重启
        return _item("kiosk", label, "fail", "进程 %d 已退出" % k["pid"], status="未运行")
    svc = _svc(snap, "kiosk")
    detail = "" if svc is None else "服务已起, 等界面" if svc[1] else "等服务拉起"
    return _item("kiosk", label, "wait", detail, status="启动中…")


def boot_items(snap, now):
    """开机自检各项 [{key, label, state, status, detail}], 只看快照. state 见 BOOT_STATES:
    ok 绿 / warn 琥珀 (真有事, 如走电池) / fail 红 / off 没装没插 (灰, 不算问题) / wait 还没好."""
    snap = snap or {}
    rows = snap.get("rows") or []
    return [_boot_time(snap, now),
            _boot_ups(snap),
            _boot_net(rows),
            _row_item("tailscale", "tailscale", rows, _row(rows, check="tailscale", label="TAILSCALE")),
            _row_item("pxe", "PXE 服务", rows, _row(rows, label="PXE SERVER")),
            _boot_heads(snap.get("slots") or []),
            _boot_kvm(rows),
            _boot_kiosk(snap)]


class BootTracker:
    """副屏开机自检的状态机: kiosk 就绪 → view() 返回 None (照常画); 否则返回自检版面.

    "逐行出结果": 每项第一次有结果时排一个亮相时刻, 和上一个亮相至少隔 step 秒, 排定就不再改
    (后面的项先好、前面的项后好也不会让已出的行缩回去). 没到亮相时刻的行显示"等待".
    红的 (fail) 不排队, 马上出. 自检开始 late 秒后还在等的标琥珀 (late).
    kiosk 就绪后再掉线, 下次进自检从头再来一遍.
    """

    def __init__(self, late=BOOT_LATE_S, step=BOOT_STEP_S):
        self.late, self.step = late, step
        self.start = None

    def view(self, snap, now):
        snap = snap or {}
        alive = bool((snap.get("kiosk") or {}).get("alive"))
        if self.start is None:
            if alive:                       # 没在自检 (比如只重启了 cart-dash): 照常画
                return None
            self.start, self._reveal, self._last, self._alive_at = now, {}, now, None
        if not alive:
            self._alive_at = None
        elif self._alive_at is None:
            self._alive_at = now
        lines, pending = [], False
        for it in boot_items(snap, now):
            st, key = it["state"], it["key"]
            if st != "wait" and key not in self._reveal:
                at = now if st == "fail" else max(now, self._last + self.step)
                self._reveal[key] = at
                self._last = max(self._last, at)
            shown = st != "wait" and (st == "fail" or now >= self._reveal[key])
            pending = pending or not shown
            if not shown and now - self.start >= self.late:
                it = dict(it, state="late", status=BOOT_STATUS["late"])
            elif not shown and st != "wait":               # 有结果了, 还没轮到它亮相
                it = dict(it, state="wait", status=BOOT_STATUS["wait"], detail="")
            lines.append({k: it[k] for k in ("label", "state", "status", "detail")})
        if alive and now >= self._alive_at + BOOT_HOLD_S and (
                now - self.start >= self.late or not pending and now >= max(self.start + BOOT_MIN_S,
                                                                            self._last + BOOT_HOLD_S)):
            self.start = None               # 每行都亮完、kiosk OK 也让人看见了 (或者等太久了): 切状态板
            return None
        t = time.localtime(now)
        return {"boot": True, "title": "OPS-CART", "version": snap.get("version") or "",
                "clock": time.strftime("%H:%M", t), "lines": lines, "stale": stale(snap, now)}


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
        "kiosk": lambda: kiosk_live(),
        "svcs": lambda: kc.services_status(BOOT_UNITS),
        "version": lambda: kc.version_string(),
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
