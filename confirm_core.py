"""确认 UI 的纯逻辑 (#23): 执行绪方块怎么画、确认框里写什么、确认 / 拒绝 / 急停 / 解除的前置校验、
网页急停 / 拒绝请求的格式. 不碰文件和 Tk; 输入是 serial_write_core.Book.view() 的结果 (mcp_serial.peek 读来).

规矩:
- 视图读不懂 (None / 缺字段) = 全部急停显示, 不显示旧数据.
- 方块优先级: 急停 (实心红) > 待确认 (黄闪) > 响铃 (红闪) > Agent 输入 (在途, 紫闪) > Agent 持写租约 (紫色带) > 原样 (#2).
  颜色就是 #2 指示灯那套 (OK 绿 / WARN 黄 / FAIL 红), 只多一个 Agent 紫.
- 前置校验只为了界面早点说"不行"; 真正的规则还在 Book 里 (确认时租约、急停、在途都会再查一遍).
- 网页只走"停"的方向 (急停 / 拒绝): ops-web 写不了状态文件, 请求落到 /run/ops-cart/serial-req/, 由 root 的
  cart-serial-req 单元执行. 确认发送和解除急停只在车机上 (只认本机人), 网页请求里出现就当坏请求.
"""

import json
import re

import lamps_core as lc
import serial_write_core as sw
import theme as th

PENDING_COLOR, ESTOP_COLOR, AGENT_COLOR = th.follow(
    globals(), PENDING_COLOR="LAMP_WARN", ESTOP_COLOR="LAMP_FAIL", AGENT_COLOR="AGENT")
KEY_LABEL = {"ctrl_c": "Ctrl+C", "enter": "回车", "space": "空格"}
BY_KIOSK = "human:kiosk@local"

WEB_OPS = ("estop", "reject", "kvm_reject")
REQ_MAX = 4096              # 一个请求文件最多多少字节
REQ_MAX_AGE_S = 60          # 请求超过这么久还没人处理 (单元没起来) 就作废, 别过后突然生效
REQ_FUTURE_S = 5
BY_MAX = 64


# ------------------------------------------------------------ 每槽位状态

def stopped_view(by="state-unreadable"):
    """读不懂状态时给界面的视图: 全部急停, 没有挂起."""
    return {"slots": [{"slot": n, "estop": {"by": by, "src": "system", "t": ""}, "writer": None,
                       "expires_in": None, "inflight": None, "pending": 0} for n in sw.SLOTS],
            "pending": [], "history": []}


def slot_states(view):
    """视图 -> {槽位: {estop, pending, writer, sending}}. 读不懂 = 全部急停."""
    try:
        out = {}
        for s in view["slots"]:
            out[s["slot"]] = {"estop": bool(s["estop"]), "pending": int(s["pending"]), "writer": s["writer"],
                              "sending": bool(s["inflight"])}
        if sorted(out) == list(sw.SLOTS):
            return out
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return {n: {"estop": True, "pending": 0, "writer": None, "sending": False} for n in sw.SLOTS}


# ------------------------------------------------------------ 执行绪方块

def _look(color, lit):
    if lit:
        return {"bg": color, "fg": lc.LIT_FG, "bar": color}
    return {"bg": lc.BODY, "fg": lc.FG, "bar": color}


def head_view(n, slot, st, active, on, bell=False):
    """一个操作头方块 -> {text, bg, fg, bar, blink}. slot = lamps_core.slot_status 的一项, st = slot_states 的一项."""
    text, health = lc.slot_view(n, slot)
    if st["estop"]:
        return dict(_look(ESTOP_COLOR, True), text=f"{n}\n急停", blink=False)
    if st["pending"]:
        return dict(_look(PENDING_COLOR, on), text=f"{n}\n待确认 {st['pending']}", blink=True)
    if bell:
        return dict(lc.lamp_view(health, active, on, bell=True), text=text, blink=False)
    if st["sending"]:
        return dict(_look(AGENT_COLOR, on), text=f"{n}\nAgent 输入", blink=True)
    if st["writer"]:
        base = text.split("\n", 1)[1]
        return dict(_look(AGENT_COLOR, active and on), text=f"{n} Agent\n{base}", blink=False)
    return dict(lc.lamp_view(health, active, on), text=text, blink=False)


def summary(states):
    """副屏一行: 待确认几条、哪几路急停. 都没有 None; 有急停 bad, 只有待确认 warn."""
    stopped = [n for n, s in sorted(states.items()) if s["estop"]]
    pend = sum(s["pending"] for s in states.values())
    if len(stopped) == len(sw.SLOTS):
        return {"text": "Agent 全部急停" + (f" · 待确认 {pend}" if pend else ""), "level": "bad"}
    bits = [f"待确认 {pend}"] if pend else []
    if stopped:
        bits.append("急停 " + "、".join(str(n) for n in stopped))
    if not bits:
        return None
    return {"text": "Agent " + " · ".join(bits), "level": "bad" if stopped else "warn"}


# ------------------------------------------------------------ 确认框

UNTRUSTED_MAX = 200         # Agent 自述 / 模型理由显示多少字


def one_line(text, limit=UNTRUSTED_MAX):
    """不可信文字 -> 一行: 删 ANSI 转义, 所有空白 (含换行、U+2028) 压成一个空格, 控制 / 格式字符换成 ?, 截短.
    防它在审批界面上伪造出推车自己的块 (#65 第六轮)."""
    s = sw._show(" ".join(sw._ANSI.sub("", str(text or "")).split())).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _intent(item):
    it = (item.get("gate") or {}).get("intent")
    return f"Agent 自述（不可信）：（{one_line(it)}）" if it else ""


def _reasons(item):
    """闸门理由 -> (推车自己的部分, 模型理由行). 第 2 层模型的理由单独标不可信、压成一行."""
    reason = str(item.get("reason") or "-")
    model = (item.get("gate") or {}).get("model") or {}
    for key, label in (("reason", "模型理由（不可信）："), ("error", "模型出错（不可信）：")):
        m = model.get(key)
        if isinstance(m, str) and m.strip() and m not in ("off", "danger"):
            own = reason[:-len(m)].rstrip(" :：,，") if reason.endswith(m) else reason   # 只切后缀, 不改推车自己的话
            return own or "-", label + one_line(m)
    return reason, ""


def _name(item):
    """设备自报的名字 (提示符里认的): 显示前再洗一遍 (旧状态文件里的没洗过), 放条目末尾, 标不可信 (#65 第八轮)."""
    name = sw.clean_name(item.get("name"))
    return f"设备自报名（不可信）：{name}" if name else ""


def _want(item):
    if item.get("kind") == "key":
        return f"Agent 想按：{KEY_LABEL.get(item['cmd'], item['cmd'])}"
    return f"Agent 想执行：{item['cmd']}"


def _context(item):
    """挂起时设备最近输出 (#65): 来自被测设备, 不可信, 只给审批的人参考."""
    lines = ((item.get("context") or {}).get("lines")) or []
    return ("设备最近输出（来自设备，不可信，仅供参考）：\n" + "\n".join("| " + ln for ln in lines)) if lines else ""




def _note(item):
    """只有规则文件里登记为华为的槽位省掉这句 (华为提示符带视图名). 自动认出来的华为不省: 自动认只看最后一行像
    不像 <名>/[名], Linux 的 PS1 也能伪装成这个样子 (#65 第四轮)."""
    gate = item.get("gate") or {}
    if gate.get("vendor") == "huawei" and gate.get("vendor_registered") is True:
        return ""
    return "注意：这类设备的提示符不带对象名，提示符不能证明作用对象；先看「可信记录」一块再确认"


TRUST_CMD_MAX = 60          # 可信记录里每条命令显示多少字
UNSURE = {"timeout": "超时", "unknown": "发送进程没回来", "error": "出错"}


def _entry(e):
    """一条可信记录 -> 一行: 连续空白压成一个、超长截断、命令用「」括起来 (防止折行 / 补空格伪造成多条)."""
    if e.get("kind") == "key":
        cmd = KEY_LABEL.get(e["cmd"], e["cmd"]) + (f" ×{e['count']}" if e.get("count", 1) > 1 else "")
    else:
        cmd = " ".join(str(e.get("cmd", "")).split())
    if len(cmd) > TRUST_CMD_MAX:
        cmd = cmd[:TRUST_CMD_MAX] + "…"
    st = e.get("status")
    mark = (f"［{UNSURE[st]}：可能已发/可能未发］" if st in UNSURE else "［发送中］" if st == "sending"
            else "［conbroker 拒发，没发出］" if st == "refused" else "")
    by = f"（人确认 {e['by']}）" if e.get("by") else ""
    return f"  #{e['id']} 「{cmd}」{by}{mark}"


LOST_WARN = ("  ！！更早的可能改变视图 / 对象的记录已被挤掉，无法确认作用对象 —— 建议拒绝，"
             "让 Agent 重新进入目标视图后再提交")


def _trusted(slot_view):
    """推车自己记的本槽位实际发出的命令 (#65): 可信, 和设备输出分开显示; 没有记录也明说.
    留着的可能改视图的命令全显示 (最多 CHANGE_KEEP 条). 不替人下"现在在哪个对象上"的结论: 缩写、视图内命令、
    exit / telnet 推车认不准 (第六轮删掉了那一行)."""
    tr = slot_view.get("trusted") or {}
    changes, recent = tr.get("changes") or [], tr.get("recent") or []
    head = "可信记录（推车自己记的本槽位实际发出的命令；不含人在车机 / 网页直接敲的键）："
    lost = "  （更早的记录已被挤掉，无法确认作用对象）"
    if not changes and not recent:
        return head + ("\n" + lost + "\n" + LOST_WARN if tr.get("lost") else "无记录")
    rows = [head]
    if tr.get("lost"):
        rows += [lost, LOST_WARN]
    rows += [_entry(e) for e in changes] if changes else \
        ([] if tr.get("lost") else ["  （无可能改变视图 / 对象的命令记录）"])
    if tr.get("after"):
        rows.append(f"之后还有 {tr['after']} 条只读命令 / 按键" + ("，最近的：" if recent else ""))
        rows += [_entry(e) for e in recent]
    return "\n".join(rows)


def card(view, n):
    """槽位 n 的确认框 -> {title, status, stopped, items}. items 每条 {id, head, meta, reason, danger}."""
    s = next(x for x in view["slots"] if x["slot"] == n)
    items = [p for p in view["pending"] if p["slot"] == n]
    status = []
    if s["estop"]:
        e = s["estop"]
        status.append(f"急停中 (by {e.get('by')} {e.get('t') or ''})".replace(" )", ")"))
    if s["writer"]:
        status.append(f"写入者 {s['writer']} 剩 {s['expires_in']} 秒")
    if s["inflight"]:
        status.append(f"在途 #{s['inflight']['id']} {s['inflight']['cmd']}")
    if not status:
        status.append("没有 Agent 在写")
    out = []
    for p in items:
        out.append({"id": p["id"], "head": _want(p),
                    "meta": f"#{p['id']} · 来自 {p['who']} · {p.get('age_s', 0)} 秒前",
                    "reason": f"闸门理由：{one_line(_reasons(p)[0], 300)}", "model": _reasons(p)[1],
                    "intent": _intent(p),
                    "danger": ("高危词：" + "、".join(p["danger"])) if p.get("danger") else "",
                    "context": _context(p), "note": _note(p), "name": _name(p)})
    return {"title": f"操作头 {n}", "status": status,
            "stopped": bool(s["estop"]), "items": out, "trusted": _trusted(s) if out else ""}


# ------------------------------------------------------------ 键鼠 (#42 #43): 同一套配色, 灯在下排

def kvm_summary(view):
    """mcp_kvm.peek 的结果 -> 只有几个数 (副屏快照 0644, 不带名字和内容). 读不懂 = 急停."""
    try:
        return {"stopped": bool(view["stopped"]), "pending": len(view["pending"]),
                "writer": bool(view["writer"]), "sending": bool(view["sending"])}
    except (KeyError, TypeError):
        return {"stopped": True, "pending": 0, "writer": False, "sending": False}


def kvm_lamp(st, on):
    """键鼠灯: 急停红 > 待确认黄闪 > 正在发紫闪 > 持租约紫; 没 Agent 的事 None (走原来的健康色)."""
    if not st:
        return None
    if st["stopped"]:
        return dict(_look(ESTOP_COLOR, True), text="键鼠\n急停", blink=False)
    if st["pending"]:
        return dict(_look(PENDING_COLOR, on), text=f"键鼠\n待确认 {st['pending']}", blink=True)
    if st["sending"]:
        return dict(_look(AGENT_COLOR, on), text="键鼠\nAgent 输入", blink=True)
    if st["writer"]:
        return dict(_look(AGENT_COLOR, False), text="键鼠\nAgent", blink=False)
    return None


def kvm_card(view):
    """键鼠确认框 -> {title, status, stopped, items}, 同 card. 读不懂当急停画."""
    try:
        status = []
        if view["stopped"]:
            status.append("全部急停中: Agent 不能动键鼠, 人不受影响")
        if view["writer"]:
            status.append(f"写入者 {view['writer']} 剩 {view['expires_in']} 秒")
        if not status:
            status.append("没有 Agent 在动键鼠")
        items = [{"id": p["id"], "head": f"Agent 想输入：{p['what']}", "meta": f"#{p['id']} · 来自 {p['who']}",
                  "reason": f"闸门理由：{p['reason']}" + (f"（整行：{p['line']}）" if p.get("line") else ""),
                  "danger": ("高危：" + "、".join(p["danger"])) if p.get("danger") else ""} for p in view["pending"]]
        return {"title": "键鼠", "status": status, "stopped": bool(view["stopped"]), "items": items}
    except (KeyError, TypeError):
        return {"title": "键鼠", "status": ["读不到键鼠状态, 按急停算"], "stopped": True, "items": []}


# ------------------------------------------------------------ 前置校验

def check(view, op, arg):
    """界面动作前先看一眼 -> None (可以) 或 一句为什么不行. arg: confirm/reject 是编号, estop/resume 是槽位或 None."""
    states = slot_states(view)
    if op in ("confirm", "reject"):
        try:
            item = next((p for p in view["pending"] if p["id"] == arg), None)
        except (KeyError, TypeError):
            item = None
        if item is None:
            return f"#{arg} 已经不在挂起了 (发了、拒了或作废了)"
        if op == "reject":
            return None
        st = states.get(item["slot"])
        if st is None or st["estop"]:
            return f"槽位 {item['slot']} 急停中, 先解除"
        if st["sending"]:
            return f"槽位 {item['slot']} 上一条还没回来, 等它回来再确认"
        if not st["writer"]:
            return "提交它的 Agent 租约已失效"
        return None
    if op in ("estop", "resume"):
        if arg is not None and arg not in sw.SLOTS:
            return f"槽位只有 {sw.SLOTS[0]}-{sw.SLOTS[-1]}"
        if op == "estop":
            return None
        if arg is None:
            return None if any(s["estop"] for s in states.values()) else "没有急停的槽位"
        return None if states[arg]["estop"] else f"槽位 {arg} 没在急停"
    return f"不认识的动作 {op}"


# ------------------------------------------------------------ 网页请求 (只有停的方向)

def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _by_ok(by):
    return isinstance(by, str) and 0 < len(by) <= BY_MAX and by.isprintable()


def make_request(op, arg, by, now):
    """网页 -> 请求 dict. op 只收 estop (arg = 槽位或 None = 全部) / reject (arg = 编号); 不合格抛 ValueError."""
    if op == "estop" and (arg is None or arg in sw.SLOTS) and (arg is None or _int(arg)):
        req = {"op": op, "slot": arg}
    elif op in ("reject", "kvm_reject") and _int(arg) and arg > 0:
        req = {"op": op, "id": arg}
    else:
        raise ValueError(f"网页只能急停或拒绝 ({op!r} {arg!r} 不行)")
    if not _by_ok(by):
        raise ValueError("by 不合格")
    req.update(by=by, t=now)
    return req


def parse_request(text, now):
    """请求文件内容 -> 请求 dict; 不合格 / 太旧 / 来自未来 抛 ValueError (调用方删掉这个文件)."""
    if not isinstance(text, str) or len(text) > REQ_MAX:
        raise ValueError("请求太大")
    try:
        d = json.loads(text)
    except ValueError:
        raise ValueError("不是 JSON") from None
    if not isinstance(d, dict) or not isinstance(d.get("t"), (int, float)) or isinstance(d.get("t"), bool):
        raise ValueError("缺时间")
    if now - d["t"] > REQ_MAX_AGE_S or d["t"] - now > REQ_FUTURE_S:
        raise ValueError("请求太旧或时间不对")
    req = make_request(d.get("op"), d.get("slot") if d.get("op") == "estop" else d.get("id"), d.get("by"), d["t"])
    if set(d) != set(req):
        raise ValueError("多余字段")
    return req


def apply_request(book, req, now):
    """在 Store.run 里执行一个网页请求 -> 一句结果. Book 不让做抛 sw.Refused."""
    if req["op"] == "estop":
        dropped = book.estop(req["slot"], req["by"], "human", now)
        where = "全部槽位" if req["slot"] is None else f"槽位 {req['slot']}"
        return f"已急停 {where}; 丢弃挂起 {[d['id'] for d in dropped]}"
    book.reject(req["id"], req["by"], now, why="网页上拒绝")
    return f"已拒绝 #{req['id']}"

