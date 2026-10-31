"""pxe_log_core: 网页只读显示 PXE 日志, 按 MAC 分组 (#24 后续). 纯逻辑 + 读文件尾, 不写任何东西.

两份来源: pxe-req.log (mcp_pxe 写的 JSONL: 申请 / 确认 / 拒绝 / 作废…) + events.log 里的 [pxe] 行 (解锁 / 上锁 /
菜单被取走, 自由文本, MAC 从正文里认). [pxe-req] 行是 JSONL 的复写, 不重复收.
输出字段白名单 (FIELDS): 日志里就算混进了 code / digest 之类的字段也不出. 网页不显示在等的申请, 不读状态文件.
读不到 / 坏行一律跳过, 不抛.
"""

import json
import os
import re

import cartlog
from pxeserver import norm_mac

REQ_LOG = os.path.join(cartlog.LOG_DIR, "pxe-req.log")
EVENTS_LOG = cartlog.EVENTS
REQ_TAIL_BYTES = 256 * 1024
EVENTS_TAIL_BYTES = 512 * 1024      # events.log 是大家共用的, [pxe] 只占一部分
MAX_EVENTS = 300                    # 两份合起来最多看最近几条
TEXT_MAX = 200
FIELDS = ("t", "src", "ev", "what", "id", "who", "by", "purpose", "why", "msg", "macs")
EV_LABEL = {"request": "申请", "bad_code": "确认码不对", "confirm": "已确认解锁", "reject": "拒绝",
            "refused": "推车拒绝", "failed": "解锁失败", "expire": "过期", "drop": "作废", "retire": "提前上锁",
            "corrupt": "状态读不懂", "pxe": "装机口"}

_EVENT_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[pxe\] (.+)$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}$")
# 6 段 (或 grub.cfg-01- 带 ARP 类型 01 的 7 段) 两位十六进制; 前后再接十六进制段的不算 (太长不是 MAC)
_MAC = re.compile(r"(?<![0-9A-Za-z:])((?:[0-9A-Fa-f]{2}[:-]){5,6}[0-9A-Fa-f]{2})(?![0-9A-Za-z:]|-[0-9A-Fa-f])")


def tail_lines(path, max_bytes=REQ_TAIL_BYTES):
    """文件最后 max_bytes 字节的整行 (截断处的半行丢掉); 没有 / 读不了 -> []."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def _mac(s):
    try:
        return norm_mac(s) if isinstance(s, str) else None
    except ValueError:
        return None


def macs_in(text):
    """正文里出现的 MAC (规范成小写冒号, 去重保序)."""
    out = []
    for m in _MAC.findall(text):
        parts = re.split(r"[:-]", m)
        if len(parts) == 7:
            if parts[0] != "01":
                continue
            parts = parts[1:]
        mac = _mac(":".join(parts))
        if mac and mac not in out:
            out.append(mac)
    return out


def _text(v):
    return " ".join(v.split())[:TEXT_MAX] if isinstance(v, str) else None


def _from_req(line):
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("t"), str) or not _STAMP.match(rec["t"]):
        return None
    ev = _text(rec.get("ev"))
    if not ev:
        return None
    e = {"t": rec["t"].replace("T", " "), "src": "req", "ev": ev, "what": EV_LABEL.get(ev, ev)}
    if isinstance(rec.get("id"), int) and not isinstance(rec["id"], bool):
        e["id"] = rec["id"]
    for k in ("who", "by", "purpose", "why"):
        v = _text(rec.get(k))
        if v:
            e[k] = v
    macs = rec.get("macs") if isinstance(rec.get("macs"), list) else []
    e["macs"] = list(dict.fromkeys(m for m in map(_mac, macs) if m))
    return e


def _from_events(line):
    m = _EVENT_LINE.match(line)
    if not m or not m.group(2).strip():
        return None
    msg = _text(m.group(2))
    return {"t": m.group(1), "src": "pxe", "ev": "pxe", "what": EV_LABEL["pxe"], "msg": msg, "macs": macs_in(msg)}


def events(req_lines, event_lines):
    """两份日志的行 -> 事件 (新的在前, 最多 MAX_EVENTS). 没带 MAC 的申请类事件 (bad_code) 按编号借同一申请的 MAC."""
    # [pxe] 放前面: 同一秒里解锁先写 events.log, 确认后才记进 pxe-req.log
    evs = [e for e in map(_from_events, event_lines) if e] + [e for e in map(_from_req, req_lines) if e]
    by_id = {}
    for e in evs:
        if e["src"] == "req" and "id" in e and e["macs"]:
            by_id.setdefault(e["id"], e["macs"])
    for e in evs:
        if not e["macs"] and e["src"] == "req" and "id" in e:
            e["macs"] = list(by_id.get(e["id"], []))
    order = sorted(range(len(evs)), key=lambda i: (evs[i]["t"], i), reverse=True)   # 同一秒: 后写的 (下标大的) 在前
    return [evs[i] for i in order[:MAX_EVENTS]]


def group(evs):
    """事件 (新的在前) -> {groups: [{mac, last, events}], unlinked: [事件]}. 一条涉及几台就进几组; 组按最近事件倒序."""
    groups, unlinked = {}, []
    for e in evs:
        e = {k: e[k] for k in FIELDS if k in e and e[k] not in (None, "")}
        e.setdefault("macs", [])
        if not e["macs"]:
            unlinked.append(e)
        for mac in e["macs"]:
            groups.setdefault(mac, []).append(e)
    out = [{"mac": mac, "last": es[0]["t"], "events": es} for mac, es in groups.items()]
    out.sort(key=lambda g: g["mac"])
    out.sort(key=lambda g: g["last"], reverse=True)
    return {"groups": out, "unlinked": unlinked}


def load(req_path=REQ_LOG, events_path=EVENTS_LOG):
    return group(events(tail_lines(req_path, REQ_TAIL_BYTES), tail_lines(events_path, EVENTS_TAIL_BYTES)))
