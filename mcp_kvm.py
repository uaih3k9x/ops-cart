"""MCP 工具: 看和动被测机 (#41). 键鼠 kvm_* / OCR screen_ocr / 网络唤醒 wol_send.

键鼠: 一把写租约 (/run/ops-cart/kvm-lease.json, root 0600), 有期限要续, 同时一个写入者, 认令牌不认名字; 全部急停 (kvm_broker
读的公开标记) 时收回, 解除后要重新申请. 发键只经键鼠代理、来源固定 agent, 代理不在就报错, 不退回直开串口.
只给整键和整行, 不给原始报告 / 鼠标 (同 kvm_broker_core.AGENT_OPS).
闸门 (#42, #64): 打字按"从上次回车起的整行"查高危词, text 里每个换行切出的整行都查 (第一行跟缓冲拼起来), kvm_key 的
可打印单键也计入这一行; 高危组合键也算, 命中就挂起等车机上的人确认; Agent 没有确认工具. 推车看不全这一行时
(新租约 / 发键失败 / 挪了光标 / 补全 / 调出历史 / 前面是 secret) 接着打的字要人确认, 推不出这一行或带历史展开时
回车也要 (见 press / advance). 发前对着芯片键表预检 (sendable), 不行就拒绝、状态不动.
secret=true 的内容照样计入行检查, 但不进审计、日志和状态文件 (行缓冲里只记 <secret>), 命中高危词直接拒绝, 带回车碰上看不全的行也拒绝 (不带回车放行, 这一行变看不清). 这只是减速带: 推车看不懂屏幕, 鼠标 / 图形界面 / 脚本 / 编码都拦不住,
真正的兜底是人盯和急停.
日志 (#43): /var/log/ops-cart/kvm-thread.log, JSONL; 确认 / 拒绝 / 急停收回另在 events.log 记摘要.
这里没有一个工具能解锁 PXE, 也不碰串口写路径.
"""

import contextlib
import fcntl
import hmac
import json
import math
import os
import re
import secrets
import socket
import subprocess
import time

import atomicfile
import cart_ocr
import ch9329
import cartlog
import cart_snap
import kvm_broker
import kvm_broker_core as kbc
import kiosk_core as kc
import kvm_core
import mcp_core as mc
import mcp_serial as ms
import serial_write_core as sw
import wol_core

LEASE_PATH = kvm_broker.LEASE_PATH
LOG_PATH = os.path.join(cartlog.LOG_DIR, "kvm-thread.log")
TTL_S, TTL_MIN_S, TTL_MAX_S = 60, 10, 300
LAN1 = "eno1"                   # 魔术包只从装机口发: LAN2 / 无线上都是别人的机器
TEXT_MAX = 1000
HISTORY_MAX = 10
ESTOP_SETTLE_S = kbc.QUIET_S + 1     # 急停解除后这段时间不给租约: 粗略兜底; 准的是 kvm_lease_acquire 看代理 queued
SUMMARY_EVS = {"confirm", "reject", "revoked"}
OCR_MIN_LINE = 40.0             # 低于这个置信度的行当噪声扔掉
OCR_MIN_MEAN = 60.0             # 平均低于这个: 明说读不出, 叫 Agent 看图
OCR_HINT = "OCR 读不出 (没信号、纯图形画面都这样; 退回 Tesseract 时彩色桌面也读不出), 用 screenshot 看图"
# 串口闸门的词表之外, 操作系统上常见的清盘 / 引导命令 (子串匹配: mkfs.ext4、Format-Volume 都算)
DANGER_SUB = ("diskpart", "bcdedit", "mkfs", "wipefs", "fdisk", "sgdisk", "parted", "shred", "dd if=", "cipher /w",
              "format-volume", "clear-disk", "remove-item", "reg delete")
# 高危组合键: 按下的键里包含其中一组就算 (多带 shift 等也算). SysRq 在 HID 上就是 alt+printscreen (+字母, Linux 魔术键).
DANGER_KEYS = {frozenset(k) for k in (("ctrl", "alt", "delete"), ("ctrl", "alt", "del"), ("alt", "f4"),
                                      ("alt", "printscreen"), ("ctrl", "alt", "backspace"))}
_PATH_SEP = re.compile(r"[\\/]")
_EXE = re.compile(r"\.(?:exe|com|bat|cmd|ps1|sh)\b")


def danger_of(text):
    """这行里的高危词. 往多拦的方向错: 比如普通文字里带 write 也挂起.
    命令名带路径 / 扩展名 (/sbin/reboot、C:\\...\\shutdown.exe) 时把路径分隔符换成空格、去掉扩展名再查一遍."""
    low = (text or "").lower()
    out = sw.danger_words(text) + sw.danger_words(_EXE.sub("", _PATH_SEP.sub(" ", low))) + \
        [w for w in DANGER_SUB if w in low]
    return list(dict.fromkeys(out))


def danger_keys(keys):
    pressed = {k.lower() for k in keys}
    return any(combo <= pressed for combo in DANGER_KEYS)


# ---- 行缓冲 (#64): 推车对"当前这一行"知道多少
# blind = ""       整行都看得见 (缓冲 line 就是这一行)
#         PARTIAL  这一行前头有看不见的内容 (secret 只记成 SECRET_MARK): 接着打字要人确认, 回车照发
#         UNKNOWN  这一行变成什么推不出来 (新租约、发键失败、挪了光标、补全、调历史、组合键、空行退格...):
#                  接着打字和回车都要人确认, 直到人确认过的回车
# 行里有 bash 历史展开 (! 或行首 ^) 时回车也要人确认: 真正执行的不是这一行.
# 回车 (含 text 里的换行) 之后新的一行重新可知. 往多拦的方向错.
PARTIAL, UNKNOWN = "partial", "unknown"
SECRET_MARK = "<secret>"
NEUTRAL_KEYS = {"capslock", "numlock", "scrolllock", "printscreen", "pause"}
NEUTRAL_KEYS |= {f"f{i}" for i in range(10, 25)}               # BIOS 启动菜单常用; 不碰 shell 的这一行
RECALL_KEYS = {"up", "down", "pgup", "pgdn", "esc"} | {f"f{i}" for i in range(1, 10)}    # cmd.exe 的历史键是 F1–F9
BLIND_WHY = ("推车看不全这一行 (新租约 / 发键失败过 / 前面有 secret / 挪过光标 / 调出过历史 / 历史展开), "
             "接着打字或回车要人确认")


def _opaque(s):
    """bash 历史展开 (!!、!-1、!foo、^a^b): 回车后真正执行的不是推车看到的这一行. 往多拦的方向错."""
    return "!" in s or s.lstrip().startswith("^")


_RANK = {"": 0, PARTIAL: 1, UNKNOWN: 2}


def _worse(a, b):
    return a if _RANK[a] >= _RANK[b] else b


def _clip(s, blind):
    """行缓冲只留最后 TEXT_MAX 个字符; 丢了头就至少看不全, 丢掉的头里有历史展开就看不清."""
    if len(s) <= TEXT_MAX:
        return s, blind
    return s[-TEXT_MAX:], _worse(blind, UNKNOWN if _opaque(s[:-TEXT_MAX]) else PARTIAL)


def sendable(kind, keys=(), text="", secret=False):
    """发之前对着芯片键表预检, 不行抛 ToolError, 闸门状态一点不动. 键名大小写要跟键表一致."""
    if kind == "key":
        try:
            _report, unknown = ch9329.key_report(list(keys))
        except ValueError as e:
            raise mc.ToolError(f"发不出这组键: {e}; 一个键都没发") from None
        if unknown or not keys:
            raise mc.ToolError(f"不认识的键 {unknown or '(空)'} (键名小写, 如 enter / ctrl+alt+delete); 一个键都没发")
        return
    if "\t" in text:
        raise mc.ToolError("text 里不许 Tab (补全后推车推不出这一行); 要补全用 kvm_key tab; 一个键都没发")
    if secret and "\n" in text:
        raise mc.ToolError("secret 里不许换行 (多行命令会无痕执行); 要回车给 enter=true 或另按 kvm_key enter; 一个键都没发")
    bad = ch9329.untypable(text)
    if bad:
        raise mc.ToolError("有打不出的字符 (只能 ASCII)" + ("" if secret else f": {''.join(bad)!r}") + "; 一个键都没发")


def key_effect(keys):
    """单次按键对行缓冲的作用: ("char", 字符) / ("enter",) / ("backspace",) / ("recall",) / ("neutral",) / ("unknown",)."""
    if len(keys) == 1:
        k, low = keys[0], keys[0].lower()
        if low == "enter":
            return ("enter",)
        if low == "space":
            return ("char", " ")
        if len(k) == 1 and k.isascii() and k.isprintable():
            return ("char", k)
        if low == "backspace":
            return ("backspace",)
        if low in RECALL_KEYS:
            return ("recall",)
        if low in NEUTRAL_KEYS:
            return ("neutral",)
    return ("unknown",)


def _hits(lines):
    out = []
    for ln in lines:
        out += [w for w in danger_of(ln) if w not in out]
    return out


def advance(line, blind, text, secret=False):
    """打一段字 -> (高危词, 是否因看不全要人确认, 新 line, 新 blind). text 里每个换行切出的整行都查,
    第一行跟缓冲拼起来查. secret 的内容照样查, 但新 line 里只留 SECRET_MARK."""
    segs = text.split("\n")
    done = [line + segs[0]] + segs[1:-1]           # 打了换行的整行
    blocked = bool(blind) and bool(segs[0]) or (blind == UNKNOWN and len(segs) > 1) or \
        (len(segs) > 1 and any(_opaque(s) for s in done))
    hits = _hits([line + segs[0]] + segs[1:])
    if secret:                                      # 不带换行 (sendable 保证)
        if not text:
            return hits, blocked, line, blind
        mark = UNKNOWN if _opaque(line + text) or blind == UNKNOWN else PARTIAL
        return (hits, blocked) + _clip(line + SECRET_MARK, mark)
    tail = segs[-1] if len(segs) > 1 else line + segs[0]
    return (hits, blocked) + _clip(tail, "" if len(segs) > 1 else blind)


def press(line, blind, keys):
    """按一次键 -> (高危词, 是否因看不全要人确认, 新 line, 新 blind)."""
    if danger_keys(keys):                           # 确认发出后焦点可能换了窗口 (alt+f4)
        return ["+".join(keys)], False, "", UNKNOWN
    eff = key_effect(keys)
    if eff[0] == "char":
        return advance(line, blind, eff[1])
    if eff[0] == "enter":
        return [], blind == UNKNOWN or _opaque(line), "", ""
    if eff[0] == "backspace":
        return ([], False, line[:-1], blind) if line and not blind else ([], False, "", UNKNOWN)
    if eff[0] == "neutral":
        return [], False, line, blind
    return [], False, "", UNKNOWN


def _shown(text, secret):
    return f"<secret {len(text)} 字符>" if secret else text


class LeaseStore:
    """键鼠状态: {next, lease, pending, line, sending_until, history}. 每次操作加锁、读、算过期和急停、存、写日志.
    文件读不懂 = 没有租约 (lease_allows 同理)."""

    def __init__(self, path=LEASE_PATH, clock=time.time, stopped=kvm_broker.serial_all_stopped,
                 log_path=None, event=None):
        self.path, self.clock, self.stopped = path, clock, stopped
        real = path == LEASE_PATH
        self.log_path = log_path or (LOG_PATH if real else None)
        self.event = event if event is not None else (cartlog.event if real else None)

    @contextlib.contextmanager
    def _tx(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = self.clock()
            st, evs = settle(read_state(self.path), now, self.stopped()), []
            evs.extend(st.pop("_evs"))
            try:
                yield st, now, evs
            finally:
                atomicfile.write_text(self.path, json.dumps(st), mode=0o600)
                self._log(evs, now)
        finally:
            os.close(fd)

    def _log(self, evs, now):
        for rec in evs:
            rec = dict(t=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), **rec)
            if self.log_path:
                try:
                    with open(self.log_path, "a", encoding="utf-8") as f:     # 每行开一次: logrotate copytruncate 后照样写对
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            if rec["ev"] in SUMMARY_EVS and self.event is not None:
                self.event("kvm", cartlog.fields(ev=rec["ev"], who=rec.get("who"), id=rec.get("id"), log=self.log_path))

    def _need(self, st, token):
        if self.stopped():
            raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除, 之后重新申请租约")
        lease = st["lease"]
        if not lease or not hmac.compare_digest(str(lease.get("token", "")), str(token)):
            raise mc.ToolError("没有有效的键鼠写租约 (过期了? 急停收回了?), 先 kvm_lease_acquire")
        return lease

    def acquire(self, who, ttl, seen=None):
        """seen: {键鼠口: 代理记的人工输入次数}, 之后 gate 拿来比, 租约期间人动过就算看不清."""
        ttl = int(max(TTL_MIN_S, min(TTL_MAX_S, ttl)))
        with self._tx() as (st, now, evs):
            if self.stopped():
                raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除")
            if st["lease"]:             # 认令牌不认名字: stdio 下两个 Agent 可能是同一个用户
                raise mc.ToolError(f"键鼠写租约在 {st['lease']['who']} 手里, 还剩 {int(st['lease']['until'] - now)} 秒")
            if st["sending_until"] > now:
                raise mc.ToolError("上一个租约的键还在发, 等几秒再申请")
            if st["estop_seen"] is not None and now - st["estop_seen"] < ESTOP_SETTLE_S:   # 粗略兜底, 见 ESTOP_SETTLE_S
                raise mc.ToolError(f"全部急停刚解除, {ESTOP_SETTLE_S:g} 秒后再申请")
            st["lease"] = {"token": secrets.token_urlsafe(16), "who": who, "until": now + ttl, "ttl": ttl}
            _forget_line(st)            # 租约之间人可能留了半截命令: 第一个回车要人确认
            st["sending_id"], st["line_port"] = None, None
            st["human_seen"] = dict(seen or {})
            evs.append({"ev": "lease_acquire", "src": "agent", "who": who, "ttl": ttl})
            return {"lease": st["lease"]["token"], "ttl_s": ttl, "hint": "用完 kvm_lease_release; 令牌别给别人."}

    def renew(self, token):
        with self._tx() as (st, now, _evs):
            lease = self._need(st, token)
            lease["until"] = now + lease.get("ttl", TTL_S)
            return {"expires_in": int(lease["until"] - now)}

    def release(self, token):
        with self._tx() as (st, _now, evs):
            lease = self._need(st, token)
            evs.append({"ev": "lease_release", "src": "agent", "who": lease["who"]})
            _drop_all(st, evs, "租约交还")
            st["lease"] = None
            _forget_line(st)
            return {"released": True}

    def gate(self, token, kind, keys=(), text="", enter=False, secret=False, serial=None, port=None, human=None):
        """-> 发送编号 (str, 直接发, 已标在发; 发完拿它 finish); 挂起时 -> 回给 Agent 的说明 (dict).
        secret 命中高危词抛 ToolError. port / human: 要发的键鼠口和代理的 [启动 id, 人工输入次数];
        跟上次看到的不一样 (或拿不到) = 人在车机 / 网页口动过, 这一行看不清; 换了一块键鼠也看不清."""
        with self._tx() as (st, now, evs):
            lease = self._need(st, token)
            who = lease["who"]
            if st["pending"]:           # 键盘是一条流: 接着发的话, 人确认后顺序就乱了
                raise mc.ToolError(f"有 {len(st['pending'])} 条在等确认, 处理完才能接着发; 一个键都没发. 用 kvm_status 看结果")
            if st["sending_until"] > now:   # 同一令牌并发两路: 键会交错
                raise mc.ToolError("上一条还在发, 等它发完; 一个键都没发")
            sendable(kind, keys, text, secret)
            if port is not None:
                cur = list(human) if isinstance(human, (list, tuple)) else None
                if cur is None or st["human_seen"].get(port) != cur or st["line_port"] not in (None, port):
                    st["line"], st["blind"] = "", UNKNOWN
                st["human_seen"][port], st["line_port"] = cur, port
            shown = "+".join(keys) if kind == "key" else _shown(text, secret)
            line, blind = st["line"], st["blind"]
            if kind == "key":
                hits, blocked, after, after_blind = press(line, blind, keys)
                full = line + (key_effect(keys)[1] if key_effect(keys)[0] == "char" else "")
            else:
                hits, blocked, after, after_blind = advance(line, blind, text, secret)
                if enter:
                    blocked = blocked or after_blind == UNKNOWN or _opaque(after)
                    after, after_blind = "", ""
                full = line + ("" if secret else text)
            if hits and kind == "key" and danger_keys(keys):
                why = "高危组合键, 要人确认"
            else:
                why = f"这一行带高危词 {'、'.join(hits)}, 要人确认" if hits else None
            if secret and blocked and not enter and not hits:
                blocked, after_blind = False, UNKNOWN     # 密码框 (新租约 / tab 过来): 放行, 回车要人确认
            if blocked:
                why = f"{why}; {BLIND_WHY}" if why else BLIND_WHY
            if why and secret:
                evs.append({"ev": "refused", "src": "agent", "who": who, "text": shown})   # 命中哪个词也不记, 算泄露
                if hits:
                    raise mc.ToolError("secret 内容带高危词, 不能挂起给人看, 已拒绝; 一个键都没发")
                raise mc.ToolError("这一行推车看不全 (新租约 / 前面有 secret / 挪过光标 / 带 ! 等历史展开), secret 不能挂起"
                                   "给人看, 已拒绝; 一个键都没发. 先回车换到新的一行再打; 密码带 ! 的不给 enter, "
                                   "打完另按 kvm_key enter (回车要人确认)")
            if why:
                item = {"id": st["next"], "kind": kind, "keys": list(keys), "text": text, "enter": bool(enter),
                        "who": who, "t": now, "reason": why, "danger": hits, "line": full[-TEXT_MAX:],
                        "serial": serial, "port": port, "after": after, "after_blind": after_blind}
                st["next"] += 1
                st["pending"].append(item)
                evs.append({"ev": "pending", "src": "agent", "who": who, "id": item["id"], "text": shown,
                            "danger": hits})
                return {"status": "pending", "id": item["id"], "reason": why, "danger": hits,
                        "hint": "已挂起, 一个键都没发. 要人在推车上确认才发; 用 kvm_status 看结果. 等的时候继续 "
                                "kvm_lease_renew, 租约过期这条就作废."}
            sid = _start_send(st, now, send_grace(text if kind == "type" else "", enter, kind), lease["token"],
                              line, st["blind"])
            st["line"], st["blind"] = after, after_blind
            return sid

    def finish(self, who, kind, shown, ok, why=None, started=True, send=None):
        """发完 (send = gate / take 给的发送编号). 失败: 进了发送就可能发了一半, 这一行看不清;
        一个键都没发出去, 且租约和发送编号都没换过, 才退回发之前. 编号对不上 (过时的回报) 按看不清, 也不清别人的"在发"."""
        with self._tx() as (st, _now, evs):
            mine = isinstance(send, str) and send == st["sending_id"]
            before = st.pop("before", None) if mine else None
            if mine:
                st["sending_until"], st["sending_id"] = 0, None
            token = st["lease"]["token"] if st["lease"] else None
            if not mine:                # 过时的回报: 连 before 一起丢, 当前这条失败也只能按看不清
                _forget_line(st)
            elif not ok and not started and before and token and before["send"] == send and before["token"] == token:
                st["line"], st["blind"] = before["line"], before["blind"]
            elif not ok:
                _forget_line(st)
            evs.append({"ev": "send", "src": "agent", "who": who, "kind": kind, "text": shown, "ok": ok, "why": why})

    def take(self, iid, by):
        """人确认 -> 取出那条, 不在 / 急停 / 租约没了都抛 ValueError, 一个键都不发."""
        with self._tx() as (st, _now, evs):
            if self.stopped():
                raise ValueError("全部急停中, 先解除")
            if st["sending_until"] > _now:
                raise ValueError("Agent 那边还在发, 等它发完再确认")
            item = next((p for p in st["pending"] if p["id"] == iid), None)
            if item is None:
                raise ValueError(f"#{iid} 不在等确认 (租约过期或急停时作废)")
            st["pending"].remove(item)
            token = st["lease"]["token"] if st["lease"] else None
            sid = _start_send(st, _now, send_grace(item.get("text") or "", bool(item.get("enter")), item.get("kind")),
                              token, st["line"], st["blind"])
            if isinstance(item.get("port"), str):
                st["line_port"] = item["port"]
            if isinstance(item.get("after"), str) and item.get("after_blind") in ("", PARTIAL, UNKNOWN):
                st["line"], st["blind"] = item["after"], item["after_blind"]
            else:                       # 旧版本挂起的条目: 没有 after, 推不出这一行, 按看不见算
                st["line"], st["blind"] = "", ("" if item.get("enter") else UNKNOWN)
            evs.append({"ev": "confirm", "src": "human", "who": by, "id": iid, "text": _item_text(item)})
            _remember(st, item, "confirmed", by)
            return dict(item, send=sid)

    def reject(self, iid, by, why="人拒绝"):
        with self._tx() as (st, _now, evs):
            item = next((p for p in st["pending"] if p["id"] == iid), None)
            if item is None:
                raise ValueError(f"#{iid} 不在等确认")
            st["pending"].remove(item)
            evs.append({"ev": "reject", "src": "human", "who": by, "id": iid, "text": _item_text(item), "why": why})
            _remember(st, item, "rejected", by)

    def view(self):
        with self._tx() as (st, now, _evs):
            return public_view(st, now, self.stopped())

    def peek(self):
        """只读档用 (#49): 不加锁不写盘不写日志; 过期 / 急停只在视图里算."""
        return peek(self.path, self.clock, self.stopped)


def send_grace(text, enter, kind="type"):
    """"在发"的兜底时长: 不能短于代理正常最长耗时. 每个代理请求 (打字一次、回车一次) 一份客户端超时
    (含人在操作时 agent 排队的时间), 再加每字符 0.2 秒."""
    n = 1 if kind == "key" else max(1, bool(text) + bool(enter))
    return n * kvm_broker.CLIENT_TIMEOUT_S + len(text) * 0.2


SEND_GRACE_MAX = send_grace("x" * TEXT_MAX, True)


def _start_send(st, now, grace, token, line, blind):
    """标"在发" -> 发送编号. "在发"由 finish 清; sending_until 只是进程死了没 finish 时的兜底 (send_grace).
    before 记发之前的行, 连同租约令牌和编号, finish 两样都对得上才回滚."""
    sid = secrets.token_hex(8)
    st["sending_until"], st["sending_id"] = now + grace, sid
    st["before"] = {"token": token, "send": sid, "line": line, "blind": blind}
    return sid


def _forget_line(st):
    st["line"], st["blind"] = "", UNKNOWN
    st.pop("before", None)


def _item_text(item):
    return "+".join(item["keys"]) if item["kind"] == "key" else item["text"] + (" + 回车" if item["enter"] else "")


def _remember(st, item, result, by):
    st["history"] = (st["history"] + [{"id": item["id"], "what": _item_text(item), "result": result, "by": by}])[-HISTORY_MAX:]


def _drop_all(st, evs, why):
    for item in st["pending"]:
        evs.append({"ev": "drop", "src": "system", "who": item["who"], "id": item["id"], "text": _item_text(item), "why": why})
        _remember(st, item, "dropped", why)
    st["pending"] = []


def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    lease = doc.get("lease") if isinstance(doc.get("lease"), dict) else None
    if lease and not isinstance(lease.get("until"), (int, float)):
        lease = None
    return {"next": doc["next"] if isinstance(doc.get("next"), int) else 1, "lease": lease,
            "pending": [p for p in doc.get("pending") or () if isinstance(p, dict)] if isinstance(doc.get("pending"), list) else [],
            "line": doc["line"] if isinstance(doc.get("line"), str) else "",
            "blind": _blind(doc.get("blind", UNKNOWN if lease else "")),
            "human_seen": doc["human_seen"] if isinstance(doc.get("human_seen"), dict) else {},
            "sending_id": doc["sending_id"] if isinstance(doc.get("sending_id"), str) else None,
            "line_port": doc["line_port"] if isinstance(doc.get("line_port"), str) else None,
            **({"before": _before(doc.get("before"))} if _before(doc.get("before")) else {}),
            "sending_until": _until(doc.get("sending_until")),
            "estop_seen": _until(doc.get("estop_seen")) if "estop_seen" in doc else None,
            "history": doc["history"] if isinstance(doc.get("history"), list) else []}


def _until(v):
    """时刻字段: 不是数 -> 0; NaN / Infinity -> 无穷大, 由 settle 跟极大值一样截到 now + 最大兜底."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return 0
    return v if math.isfinite(v) else math.inf


def _blind(v):
    """读不懂 (类型不对 / 不认识) 按看不清."""
    return v if isinstance(v, str) and v in _RANK else UNKNOWN


def _before(v):
    ok = isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("send", "line", "blind")) and \
        isinstance(v.get("token"), (str, type(None))) and v["blind"] in _RANK
    return v if ok else None


def settle(st, now, stopped):
    """算掉过期和急停: 租约没了, 挂着的一起作废. 带 _evs 给调用方写日志."""
    evs = []
    lease = st["lease"]
    if st["sending_until"] > now + SEND_GRACE_MAX:     # 极大值 / Infinity / 墙钟回拨: 不能让 gate 和 acquire 一直拒绝
        st["sending_until"] = now + SEND_GRACE_MAX
    if st["sending_id"] and st["sending_until"] <= now:    # 兜底过期也没回报 (进程死在打字和回车之间): 推不出屏幕
        st["sending_until"], st["sending_id"] = 0, None
        _forget_line(st)
    if stopped and st["sending_until"]:         # 人在车上按了全部急停: 代理那边排着的都丢了, "在发"清掉
        st["sending_until"], st["sending_id"] = 0, None
        _forget_line(st)
    if stopped:
        st["estop_seen"] = now
    elif st["estop_seen"] is not None and st["estop_seen"] > now:     # 墙钟回拨
        st["estop_seen"] = now
    if lease and stopped:
        evs.append({"ev": "revoked", "src": "system", "who": lease.get("who"), "why": "全部急停"})
        _drop_all(st, evs, "全部急停")
        st["lease"] = None
        _forget_line(st)
    elif lease and lease["until"] <= now:
        evs.append({"ev": "lease_expired", "src": "system", "who": lease.get("who")})
        _drop_all(st, evs, "租约过期")
        st["lease"] = None
        _forget_line(st)
    elif not lease and st["pending"]:
        _drop_all(st, evs, "没有租约")
    st["_evs"] = evs
    return st


def public_view(st, now, stopped):
    """给 Agent 和界面看: 不含令牌. kiosk 能看挂着的内容; 副屏只拿 confirm_core 算出来的几个数."""
    lease = st["lease"]
    return {"writer": lease["who"] if lease else None,
            "expires_in": int(lease["until"] - now) if lease else None,
            "stopped": bool(stopped), "sending": st["sending_until"] > now,
            "pending": [{"id": p["id"], "what": _item_text(p), "who": p["who"], "t": p["t"], "reason": p["reason"],
                         "danger": p["danger"], "line": p["line"]} for p in st["pending"]],
            "history": st["history"]}


def peek(path=LEASE_PATH, clock=time.time, stopped=kvm_broker.serial_all_stopped):
    """只读看一眼 (车机 / 网页 / 副屏): 不加锁不写盘, 过期和急停在内存里算掉."""
    now = clock()
    halted = stopped()
    st = settle(read_state(path), now, halted)
    st.pop("_evs")
    return public_view(st, now, halted)


def human_action(store, op, arg, by, opener=None, ports=None):
    """车机上人的按钮 -> 一句结果: confirm (arg = 编号, 会发键, 放后台线程调) / reject. 调用方保证是本机人."""
    if op == "reject":
        store.reject(arg, by)
        return f"已拒绝键鼠 #{arg}"
    if op != "confirm":
        raise ValueError(f"不认识的动作 {op}")
    item = store.take(arg, by)
    online = (ports or kvm_core.ports)()
    if isinstance(item.get("port"), str):        # 挂起时的那块键鼠; 不在了就算失败, 不重新挑口
        port = item["port"] if item["port"] in online.values() else None
    else:                                       # 旧版本挂起的条目
        port = online.get(item.get("serial")) if item.get("serial") else next(iter(online.values()), None)
    shown = _item_text(item)
    if not port:
        store.finish(item["who"], item["kind"], shown, False, "键鼠不在线", started=False, send=item["send"])
        raise ValueError("键鼠不在线, 没发")
    started = False
    try:
        ch = (opener or real_opener)(port, item["who"])
        try:
            started = True
            if item["kind"] == "key":
                ch.send_keys(item["keys"])
            else:
                ch.type_text(item["text"])
                if item["enter"]:
                    ch.send_keys(["enter"])
        finally:
            ch.close()
    except (OSError, ValueError) as e:
        store.finish(item["who"], item["kind"], shown, False, str(e), started=started, send=item["send"])
        raise ValueError(f"发键失败: {e}") from None
    except Exception as e:              # 兜底: 不留"在发"标志
        store.finish(item["who"], item["kind"], shown, False, type(e).__name__, started=started, send=item["send"])
        raise
    store.finish(item["who"], item["kind"], shown, True, send=item["send"])
    return f"已发键鼠 #{arg}: {shown}"


def ocr_view(lines):
    """cart_ocr 的行 -> {readable, mean_conf, text, lines}. 按字数加权; 噪声行扔掉, 读不出就明说."""
    good = [ln for ln in lines if ln["text"].strip() and ln["conf"] >= OCR_MIN_LINE]
    chars = sum(len(ln["text"]) for ln in good)
    mean = sum(ln["conf"] * len(ln["text"]) for ln in good) / chars if chars else 0.0
    out = {"readable": bool(good) and mean >= OCR_MIN_MEAN, "mean_conf": round(mean, 1),
           "text": "\n".join(ln["text"] for ln in good),
           "lines": [{"text": ln["text"], "x": ln["x"], "y": ln["y"]} for ln in good]}
    if not out["readable"]:
        out["hint"] = OCR_HINT
    return out


def redact_type(args):
    """审计里 secret 的内容只留长度."""
    if isinstance(args, dict) and args.get("secret") and isinstance(args.get("text"), str):
        return dict(args, text=_shown(args["text"], True))
    return args


def build_tools(store, ports, opener, ocr, wol, broker_up, who=ms.current_who, human_seq=lambda port: None,
                broker_queued=lambda: 0, readonly=False):
    """ports() -> {序列号: 路径}; opener(port, who) -> 代理连接 (send_keys / type_text / close);
    ocr(dev|None, lang) -> (行, 哪块卡的说明, 引擎); wol(mac, iface); broker_up(port) -> bool;
    human_seq(port) -> 代理记的这块键鼠人工输入次数 (拿不到 None); broker_queued() -> 代理里 agent 排着没发的请求数.
    readonly (#49): 只回只读工具, kvm_status 走 store.peek (不加锁不写盘); 动手档照旧.
    测试里全换成假的."""

    def _port(serial):
        online = ports()
        if serial:
            if serial not in online:
                raise mc.ToolError(f"没有序列号 {serial} 的键鼠; 在线的: {', '.join(online) or '没有'}")
            return online[serial]
        if not online:
            raise mc.ToolError("没有在线的键鼠 (CH9329 没插?)")
        if len(online) > 1:
            raise mc.ToolError(f"有几块键鼠, 用 serial 指定: {', '.join(online)}")
        return next(iter(online.values()))

    def _send(a, kind, fn, keys=(), text="", enter=False, secret=False):
        port = _port(a.get("serial"))           # 先挑口: 挑不出一个键都不动, 闸门状态也不动
        shown = "+".join(keys) if kind == "key" else _shown(text, secret) + (" + 回车" if enter else "")
        me = who()                              # 过闸门之前算好: 之后出任何错都要 finish
        sid = store.gate(a["lease"], kind, keys=keys, text=text, enter=enter, secret=secret, serial=a.get("serial"),
                         port=port, human=human_seq(port))
        if isinstance(sid, dict):
            return [mc.json_content(sid)]
        started = False
        try:
            try:
                ch = opener(port, me)
            except (FileNotFoundError, ConnectionRefusedError):
                raise mc.ToolError("键鼠代理 (kvm-broker) 没在跑; 不直开串口, 请人在推车上开服务") from None
            except (OSError, ValueError) as e:
                raise mc.ToolError(f"键鼠打不开: {e}") from None
            try:
                started = True
                fn(ch)
            except (OSError, ValueError) as e:
                raise mc.ToolError(f"发键失败: {e}") from None
            finally:
                ch.close()
        except mc.ToolError as e:
            store.finish(me, kind, shown, False, "secret 发送失败 (原因不记, 可能带内容)" if secret else str(e),
                         started=started, send=sid)
            raise
        except Exception as e:                  # 兜底: 不留"在发"标志, 不把半截状态当真
            store.finish(me, kind, shown, False, type(e).__name__, started=started, send=sid)
            raise
        store.finish(me, kind, shown, True, send=sid)
        return [mc.json_content({"sent": True, "hint": "发完等几秒, 用 screen_ocr 看结果"})]

    def kvm_status(_a):
        online = ports()
        up = any(broker_up(p) for p in online.values()) if online else None
        view = store.peek() if readonly else store.view()
        return [mc.json_content(dict(view, all_stopped=store.stopped(), keyboards=sorted(online), broker=up))]

    def kvm_lease_acquire(a):
        # 代理只在急停没解除时丢 agent 排队的请求, 解除后排着的照样会发: 有排着的就不给新租约, 免得跟新租约的键交错
        if broker_queued() > 0:
            raise mc.ToolError("键鼠代理里还有上一个写入者排着没发的键, 等它发完或丢掉再申请")
        seen = {p: human_seq(p) for p in ports().values()}
        return [mc.json_content(store.acquire(who(), a["ttl_s"], seen))]

    def kvm_lease_renew(a):
        return [mc.json_content(store.renew(a["lease"]))]

    def kvm_lease_release(a):
        return [mc.json_content(store.release(a["lease"]))]

    def kvm_key(a):
        keys = [k for k in a["keys"].split("+") if k]
        return _send(a, "key", lambda ch: ch.send_keys(keys), keys=keys)

    def kvm_type(a):
        secret = bool(a.get("secret"))

        def go(ch):
            ch.type_text(a["text"], secret=secret)
            if a.get("enter"):
                ch.send_keys(["enter"])
        return _send(a, "type", go, text=a["text"], enter=bool(a.get("enter")), secret=secret)

    def screen_ocr(a):
        dev = a.get("device")
        if dev and not dev.startswith("/dev/"):
            dev = "/dev/" + dev
        lines, label, engine = ocr(dev, a["lang"])
        return [mc.json_content(dict(ocr_view(lines), screen=label, engine=engine))]

    def wol_send(a):
        try:
            mac = wol_core.parse_mac(a["mac"])
            wol(mac, LAN1)
        except (ValueError, OSError) as e:
            raise mc.ToolError(f"魔术包发不出: {e}") from None
        return [mc.json_content({"mac": mac, "iface": LAN1, "note": "发了, 不保证醒: 要机器开了网络唤醒且关机后"
                                 "网口还亮. 看 pxe_clients 或 screen_ocr 确认"})]

    LEASE = {"type": "string", "required": True, "maxLength": 64, "description": "kvm_lease_acquire 给的令牌"}
    SERIAL = {"type": "string", "maxLength": 32, "description": "键鼠序列号, 只有一块时不用给"}
    T = ms.Tool
    tools = [
        T("kvm_status", "键鼠状态 (只读): 在线的键鼠序列号、代理在不在、写入者、全部急停、挂起等确认的和最近结果.",
          {}, kvm_status, kind=mc.READONLY),
        T("kvm_lease_acquire", "申请键鼠写租约 (同时一个写入者), 返回令牌. 全部急停时不给.",
          {"ttl_s": {"type": "integer", "minimum": TTL_MIN_S, "maximum": TTL_MAX_S, "default": TTL_S,
                     "description": "期限秒"}}, kvm_lease_acquire, kind=mc.ACT, destructive=False),
        T("kvm_lease_renew", "续键鼠写租约. 过期了要重新申请.", {"lease": LEASE}, kvm_lease_renew, kind=mc.ACT, destructive=False),
        T("kvm_lease_release", "交还键鼠写租约.", {"lease": LEASE}, kvm_lease_release, kind=mc.ACT, destructive=False),
        T("kvm_key", "往被测机按键或组合键, 如 enter / esc / f12 (键名小写). 高危组合键 (ctrl+alt+delete 等) 挂起等人确认; "
          "新租约、调历史、补全、挪光标之后的回车也要人确认. "
          "人最近 3 秒动过键鼠会排队. 按之前先 screen_ocr 确认焦点对.",
          {"keys": {"type": "string", "required": True, "maxLength": 64, "pattern": r"[A-Za-z0-9_+-]+",
                    "description": "键名, 组合键用 + 连"}, "lease": LEASE, "serial": SERIAL},
          kvm_key, kind=mc.ACT, destructive=True),
        T("kvm_type", "往被测机打一段 ASCII (被测机输入法要在英文). 整行带高危词 (format、diskpart 等) 就挂起等人确认, "
          "可能误拦普通文字. 内容进审计日志, 密码给 secret=true. 敲之前先 screen_ocr 确认焦点对.",
          {"text": {"type": "string", "required": True, "maxLength": TEXT_MAX, "description": "要打的字"},
           "enter": {"type": "boolean", "description": "打完按回车"},
           "secret": {"type": "boolean", "description": "密码等: 内容不进审计和日志; 不许换行; 带高危词直接拒绝"},
           "lease": LEASE, "serial": SERIAL},
          kvm_type, kind=mc.ACT, destructive=True, redact=redact_type),
        T("screen_ocr", "读被测机屏幕上的字 (OCR, 带坐标), 比 screenshot 省得多, 看画面先用这个. readable=false 时再用 "
          "screenshot. engine 写明用的引擎; 可能有形近字错误, 命令和路径回车前要核对.",
          {"device": {"type": "string", "pattern": r"(/dev/)?video[0-9]{1,2}", "maxLength": 16,
                      "description": "采集设备, 默认正在预览的那块, 没预览就有信号的采集卡"},
           "lang": {"type": "string", "enum": ["eng", "eng+chi_sim"], "default": "eng",
                    "description": "只对退回 Tesseract 时有用; RapidOCR 中英都读"}},
          screen_ocr, kind=mc.READONLY),
        T("wol_send", "往 LAN1 (装机口) 发网络唤醒魔术包. 不保证醒.",
          {"mac": {"type": "string", "required": True, "maxLength": 17,
                   "pattern": r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", "description": "目标 MAC"}}, wol_send, kind=mc.ACT),
    ]
    return [t for t in tools if t.kind == mc.READONLY] if readonly else tools


# ------------------------------------------------------------ 真机

def describe_dev(dev):
    """哪块卡 + 有没有信号; 探测失败不影响截图."""
    try:
        return mc.snap_label(dev, kc.video_friendly(dev), kc.video_signal(dev))
    except Exception:                   # noqa: BLE001
        return mc.snap_label(dev, None, None)


def real_opener(port, who):
    return kvm_broker.BrokerCh(port, src="agent", who=who)


def broker_probe(sock_path=None, timeout=2.0):
    """代理在不在: 连上发一条 status (控制操作, 不 open 设备, 不占键鼠; #49 审查). 连不上 / 回不来 = 不在."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path or kvm_broker.default_sock())
        s.sendall(json.dumps({"op": "status", "src": "agent", "who": "kvm_status"}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return bool(json.loads(buf or b"{}").get("ok"))
    except (OSError, ValueError):
        return False
    finally:
        s.close()


def broker_up(_port):
    """kvm_status 用; 和哪块键鼠无关, 只问代理在不在."""
    return broker_probe()


def human_seq(port):
    """代理的 [启动 id, 这块键鼠人工输入次数] (车机 / 网页口; 实体 USB 键盘看不到). 代理不在 / 不报启动 id -> None."""
    try:
        ch = kvm_broker.BrokerCh(port, src="agent", who="status")
    except OSError:
        return None
    try:
        boot, st = ch.boot_status()
    except (OSError, ValueError):
        return None
    finally:
        ch.close()
    if not isinstance(boot, str):
        return None
    return [boot, (st.get(os.path.realpath(port)) or {}).get("human_n", 0)]


def broker_queued():
    """代理里各块键鼠 agent 排着没发的请求数之和. 代理不在 / 没键鼠 -> 0 (没有代理就没有排队)."""
    for port in kvm_core.ports().values():      # 连代理要带一块口; status 一次就报全部
        try:
            ch = kvm_broker.BrokerCh(port, src="agent", who="status")
        except OSError:
            continue
        try:
            _boot, st = ch.boot_status()
        except (OSError, ValueError):
            continue
        finally:
            ch.close()
        return sum(int(d.get("queued") or 0) for d in st.values() if isinstance(d, dict))
    return 0


def real_ocr(dev, lang, readonly=False):
    path, why, used = cart_snap.grab(dev, readonly=readonly)     # 只读档限速更紧 (#49)
    if path is None:
        raise mc.ToolError(why)
    try:
        lines, engine = cart_ocr.ocr_best(path, lang)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise mc.ToolError(f"OCR 失败 (tesseract / convert): {e}") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return lines, describe_dev(used), engine


def real_tools(store=None, readonly=False):
    ocr = (lambda dev, lang: real_ocr(dev, lang, readonly=True)) if readonly else real_ocr
    return build_tools(store or LeaseStore(), ports=kvm_core.ports, opener=real_opener, ocr=ocr,
                       wol=wol_core.send, broker_up=broker_up, human_seq=human_seq, broker_queued=broker_queued,
                       readonly=readonly)
