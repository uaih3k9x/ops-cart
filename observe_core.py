#!/usr/bin/env python3
"""串口观察模式 (#85): 登录一台设备, 轮询只读命令, 把端口状态画成前面板. 纯逻辑, 不碰 Tk.

  Login        登录状态机: 喂设备输出, 返回下一步 (Step). 华为 console 顺序: 唤醒 -> 见 Username: 才发用户名
               -> 见 Password: 才发密码 (secret) -> 见 <提示符> 发 screen-length 0 temporary.
  parse_brief  display interface brief (华为 S / AR / VRP8); PARSERS 按厂商留位置给 H3C / 锐捷.
  layout       接口名 -> 面板格子 (GE/XGE/25GE/40GE/100GE 分组, 奇数口上、偶数口下), 可按型号用 JSON 覆盖.
  lamp         灯: up 常亮, 有流量按 InUti/OutUti 闪 (lamps_core.blink_on), *down 琥珀, ^down 灰, down 灭.
  Pacer        轮询节奏: 按一轮实际耗时留空 (占空比), 日志在我们没发的时候长了 = 有人在用, 停手 quiet 秒后再来.
  Observer     把上面串起来: start(登录) / tick(到点轮询一次) / stop(quit 退出登录).

只读: 轮询命令一律过 survey_core.is_readonly (整轮预检, 有一条不过一条都不发); 会话命令只多放
SESSION_COMMANDS 这两条字面. 登录的唤醒 / 用户名 / 密码 / [Y/N] 回答由状态机按提示给出, 不走命令闸门.

密码: 只在 Login 里待到发出去那一步, 发完就丢; Step 的 repr 打码. 登录各步直接接 conbroker 本地口
(broker_exchange, HELLO claude secret), 不走 `sudo ... conbroker send <密码>`: 那样密码会出现在进程参数和
sudo 的日志里. 轮询仍走 survey_core.conbroker_send (等提示符、自动翻页、人在敲时排队).
"""

import json
import math
import re
import socket
import time

import cartlog
import lamps_core
import survey_core

LAYOUTS_PATH = "/opt/pxe/observe_layouts.json"     # 可选: 按型号覆盖面板布局
PROFILES_PATH = "/opt/pxe/observe_profiles.json"   # 可选: 按厂商覆盖轮询命令
STATE_PATH = "/run/ops-cart/observe.json"          # 给副屏的快照 (端口状态, 无凭据)
FRONT_STALE_S = 300                                # 快照这么久没更新, 副屏就不画了

# ------------------------------------------------------------ 会话命令闸门

SESSION_COMMANDS = frozenset({"screen-length 0 temporary", "quit"})   # 只认这两条字面


def allowed(cmd, vendor):
    """观察模式能发的命令: 只读白名单, 或会话命令 (关翻页 / 退出登录). 厂商得有内置只读规则."""
    if not isinstance(cmd, str) or vendor not in survey_core._RULES:
        return False
    return cmd in SESSION_COMMANDS or survey_core.is_readonly(cmd, vendor)


# ------------------------------------------------------------ 设备输出分类

USERNAME_RE = r"(?i)user ?name\s*[:：]\s*$"
PASSWORD_RE = r"(?i)password\s*[:：]\s*$"
YN_RE = r"(?i)\[y/n\]\s*[:：]?\s*$"
PROMPT_RE = r"^\s*<[^<>\s]+>\s*$"                  # 华为用户视图 <SW>
SYSVIEW_RE = r"^\s*\[[~*]?[^\[\]\s]+\]\s*$"         # 系统视图 [SW] / [~SW]
IDLE_RE = r"(?i)(press any key|get started|按任意键)"
ANY_RE = "|".join(f"(?:{p})" for p in (r"(?i:user ?name\s*[:：]\s*$)", r"(?i:password\s*[:：]\s*$)",
                                        r"(?i:\[y/n\]\s*[:：]?\s*$)", PROMPT_RE, SYSVIEW_RE))
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]")


def clean(text):
    """去 ANSI / 退格 / 回车, 跟 conbroker 的 clean 一个意思."""
    text = _ANSI.sub("", text or "").replace("\x08", "")
    return text.replace("\r\n", "\n").replace("\r", "")


def last_line(text):
    lines = [ln for ln in clean(text).split("\n") if ln.strip()]
    return lines[-1] if lines else ""


def classify(text):
    """设备停在哪: username / password / yn / prompt / sysview / idle / empty / other."""
    ln = last_line(text)
    if not ln:
        return "empty"
    for kind, pat in (("yn", YN_RE), ("password", PASSWORD_RE), ("username", USERNAME_RE),
                      ("prompt", PROMPT_RE), ("sysview", SYSVIEW_RE), ("idle", IDLE_RE)):
        if re.search(pat, ln):
            return kind
    return "other"


# ------------------------------------------------------------ 登录状态机

class Step:
    """状态机给出的一步. kind: send / done / fail; role: wake / bounce / user / pass / answer / cmd."""
    __slots__ = ("kind", "role", "text", "secret", "force", "expect", "timeout", "note")

    def __init__(self, kind, role=None, text="", secret=False, force=False, expect=ANY_RE, timeout=10, note=""):
        self.kind, self.role, self.text, self.secret = kind, role, text, secret
        self.force, self.expect, self.timeout, self.note = force, expect, timeout, note

    def __repr__(self):
        text = "******" if self.secret else self.text
        return f"Step({self.kind}, {self.role}, {text!r}, note={self.note!r})"
    __str__ = __repr__


def _fail(note):
    return Step("fail", note=note)


class Login:
    """华为 console 登录. start() 给第一步, 之后每拿到一次设备输出就 feed(), 直到 done / fail.

    老坑: quit 之后 console 停在"按任意键", 第一下按键只是唤醒. 唤醒打码 (万一落在 Password: 上也不露),
    之后只有亲眼看到 Username: 才发用户名, 看到 Password: 才发密码. 唤醒键被当成了用户名 (本来就停在
    Username: 上) 会出 Password:, 回一个空密码弹回去, 只弹一次. [Y/N] 一律 N. 认证失败不重试 (防锁账号),
    看不懂的提示停下等人.
    """
    MAX_WAKE, MAX_ANSWER = 3, 3

    def __init__(self, username, password, pager_off="screen-length 0 temporary"):
        self._user, self._password, self._pager_off = username, password, pager_off
        self.state, self.device = "wake", None
        self._wakes = self._answers = 0
        self._bounced = False

    def _wake(self):
        self._wakes += 1
        self.state = "wake"
        return Step("send", "wake", "x", secret=True, force=True)

    def start(self):
        return self._wake()

    def _stop(self, note):
        self.state, self._password = "failed", None
        return _fail(note)

    def feed(self, output):
        kind, st = classify(output), self.state
        if kind == "yn":
            self._answers += 1
            if self._answers > self.MAX_ANSWER:
                return self._stop("[Y/N] 提示反复出现, 请人来处理")
            return Step("send", "answer", "N")
        if kind == "sysview":
            return self._stop("设备停在系统视图, 请人来处理")
        if st == "wake":
            if kind == "username":
                self.state = "user"
                return Step("send", "user", self._user, force=True)
            if kind == "password":
                if self._bounced:
                    return self._stop("唤醒后反复停在 Password:, 请人来处理")
                self._bounced = True
                return Step("send", "bounce", "", secret=True, force=True)
            if kind == "prompt":
                return self._pager()
            if self._wakes >= self.MAX_WAKE:
                return self._stop(f"唤醒 {self._wakes} 次没等到 Username:, 最后一行: {last_line(output)[:40]!r}")
            return self._wake()
        if st == "user":
            if kind == "password":
                self.state = "pass"
                pw, self._password = self._password, None       # 发出去这一步之后就不留
                return Step("send", "pass", pw, secret=True)
            return self._stop(f"发了用户名没等到 Password:, 请人来处理: {last_line(output)[:40]!r}")
        if st == "pass":
            if kind == "prompt":
                return self._pager()
            if kind in ("username", "password"):
                return self._stop("认证失败 (不重试, 免得锁账号)")
            return self._stop(f"看不懂的提示, 请人来处理: {last_line(output)[:40]!r}")
        if st == "screen":
            if kind == "prompt" and "Error" not in clean(output):
                self.state = "done"
                self.device = re.search(r"<([^<>\s]+)>", last_line(output)).group(1)
                return Step("done")
            return self._stop(f"关翻页失败: {last_line(output)[:40]!r}")
        return self._stop(f"状态机在 {st} 收到输出")

    def _pager(self):
        self.state, self._password = "screen", None
        return Step("send", "cmd", self._pager_off)


def run_login(login, exchange, vendor, max_steps=12):
    """驱动 Login: exchange(step) -> 设备输出. cmd 步必须过 allowed(). 返回最后的 done / fail 步, 不抛."""
    step = login.start()
    for _ in range(max_steps):
        if step.kind != "send":
            return step
        if step.role == "cmd" and not allowed(step.text, vendor):
            return _fail(f"会话命令不在放行范围: {step.text!r}")
        try:
            out = exchange(step)
        except Exception as e:
            return _fail(f"发送失败: {e or type(e).__name__}")
        step = login.feed(out)
    return step if step.kind != "send" else _fail("登录步数超限")


# ------------------------------------------------------------ display interface brief

_FLAGS = r"(?:\([A-Za-z]{1,2}\))*"
_UTI = r"\d+(?:\.\d+)?%|--"
_ROW = re.compile(rf"^\s*(?P<name>[A-Za-z0-9][\w./:-]*?)(?P<nf>{_FLAGS})\s+(?P<phy>[*^]?(?:up|down))(?P<pf>{_FLAGS})"
                  rf"\s+(?P<proto>up|down)(?P<rf>{_FLAGS})\s+(?P<in>{_UTI})\s+(?P<out>{_UTI})(?:\s+\d+\s+\d+)?\s*$")
_STATE = {"up": "up", "down": "down", "*down": "admin", "^down": "standby"}


def _uti(s):
    return None if s == "--" else float(s[:-1])


def parse_brief(text):
    """华为 display interface brief -> [{name, phy, proto, state, flags, in_uti, out_uti}].
    图例 / 表头 / 回显 / 提示符都不匹配行格式, 自然跳过. (l)(s)(d) 这类标记可能贴在名字、PHY 或协议后面."""
    out = []
    for ln in clean(text).split("\n"):
        m = _ROW.match(ln)
        if not m:
            continue
        flags = re.findall(r"\(([A-Za-z]{1,2})\)", m.group("nf") + m.group("pf") + m.group("rf"))
        out.append({"name": m.group("name"), "phy": m.group("phy"), "proto": m.group("proto"),
                    "state": _STATE[m.group("phy")], "flags": flags,
                    "in_uti": _uti(m.group("in")), "out_uti": _uti(m.group("out"))})
    return out


PARSERS = {"huawei": parse_brief}      # h3c / ruijie: 以后在这里登记


def parser_for(vendor):
    return PARSERS.get(vendor)


_MODEL = re.compile(r"^\s*huawei\s+([A-Za-z]+\d[\w-]*)\s.*?uptime is", re.I | re.M)


def parse_model(text):
    """display version -> 型号 (S5735-L24T4S-A1 / AR651C), 认不出 None."""
    m = _MODEL.search(clean(text))
    return m.group(1) if m else None


# ------------------------------------------------------------ 前面板排布

_KIND = re.compile(r"^(Ethernet|GigabitEthernet|XGigabitEthernet|MultiGE|10GE|25GE|40GE|100GE|GE|XGE)"
                   r"(\d+(?:/\d+)*)/(\d+)$")
_TYPES = {"Ethernet": ("FE", "rj45"), "GigabitEthernet": ("GE", "rj45"), "GE": ("GE", "rj45"),
          "MultiGE": ("MGE", "rj45"), "XGigabitEthernet": ("XGE", "sfp"), "XGE": ("XGE", "sfp"),
          "10GE": ("XGE", "sfp"), "25GE": ("25GE", "sfp"), "40GE": ("40GE", "qsfp"), "100GE": ("100GE", "qsfp")}
GROUP_ORDER = ["FE", "GE", "MGE", "XGE", "25GE", "40GE", "100GE"]


def port_kind(name):
    """物理口 -> (分组, 槽位元组, 口号, 形状); Vlanif / NULL / Eth-Trunk / MEth 这些不上面板, None."""
    m = _KIND.match(name or "")
    if not m:
        return None
    group, shape = _TYPES[m.group(1)]
    return group, tuple(int(x) for x in m.group(2).split("/")), int(m.group(3)), shape


def auto_layout(names, x0=0):
    """按 (槽位, 分组) 从左往右排, 组间空一列; 组内按口号, 第 1、3、5… 个在上排, 第 2、4、6… 个在下排."""
    groups = {}
    for n in names:
        k = port_kind(n)
        if k:
            groups.setdefault((k[1], GROUP_ORDER.index(k[0])), []).append((k[2], n, k))
    out, x = [], x0
    for key in sorted(groups):
        items = sorted(groups[key])
        for i, (num, n, k) in enumerate(items):
            out.append({"name": n, "label": str(num), "group": k[0], "x": x + i // 2, "y": i % 2, "shape": k[3]})
        x += (len(items) + 1) // 2 + 1
    return out


def load_layouts(path=LAYOUTS_PATH):
    """{型号前缀: {"ports": {接口名: {x, y, shape?, label?}}}}; 文件坏了 / 没有 = {}, 用自动排布."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for model, v in data.items():
        ports = v.get("ports") if isinstance(v, dict) else None
        if isinstance(ports, dict):
            out[model] = {"ports": {n: p for n, p in ports.items() if isinstance(p, dict)
                                    and isinstance(p.get("x"), int) and isinstance(p.get("y"), int)}}
    return out


def layout(names, model=None, layouts=None):
    """有型号覆盖 (最长前缀匹配) 就按覆盖摆, 覆盖里没写到的物理口接在右边自动排; 否则全自动."""
    layouts = layouts or {}
    keys = [k for k in layouts if model and model.startswith(k)]
    if not keys:
        return auto_layout(names)
    ov = layouts[max(keys, key=len)]["ports"]
    out = []
    for n in names:
        if n in ov:
            k = port_kind(n)
            p = ov[n]
            out.append({"name": n, "label": str(p.get("label") or (k[2] if k else n)), "group": k[0] if k else "",
                        "x": p["x"], "y": p["y"], "shape": p.get("shape") or (k[3] if k else "rj45")})
    right = max((p["x"] for p in out), default=-2) + 2
    return out + auto_layout([n for n in names if n not in ov], x0=right)


# ------------------------------------------------------------ 灯

def blink_period(util):
    """利用率 (%) -> 闪烁半周期秒数, 越忙越快 (约 0.5 -> 0.17); 没流量 / 不知道 None (不闪)."""
    if not util or util <= 0:
        return None
    return 0.5 / (1 + math.log10(1 + util))


def lamp(port, now):
    """一个口的灯 -> (状态, 亮不亮). 状态: up / down / admin / standby / unknown."""
    if not port:
        return "unknown", False
    st = port.get("state", "unknown")
    if st == "up":
        utils = [u for u in (port.get("in_uti"), port.get("out_uti")) if u is not None]
        period = blink_period(max(utils) if utils else None)
        return st, lamps_core.blink_on(now, period) if period else True
    if st in ("admin", "standby"):
        return st, True
    return st, False


# ------------------------------------------------------------ 轮询节奏

class Pacer:
    """什么时候发下一轮.

    duty: 串口最多被我们占多大比例 (9600 下一轮几秒, 0.2 = 每轮之后空出 4 倍耗时); 夹在 [min_gap, max_gap].
    人优先: conbroker 日志在我们没发的时候长了 (有人 attach 在敲、面板里打字、设备自己吐日志) = poke,
    停手 quiet 秒后再恢复. settle: 自己那一轮刚结束时 conbroker 还会写"断开"标记, 这段时间的增长不算人.
    """

    def __init__(self, duty=0.2, min_gap=15, max_gap=300, quiet=15, settle=2.0):
        self.duty, self.min_gap, self.max_gap, self.quiet, self.settle = duty, min_gap, max_gap, quiet, settle
        self.next_at, self.human_at, self.ignore_until, self.last_size, self.gap = 0, None, 0, None, None

    def gap_for(self, cost):
        return min(self.max_gap, max(self.min_gap, cost * (1 - self.duty) / self.duty))

    def done(self, started, finished, size=None):
        self.gap = self.gap_for(finished - started)
        self.next_at = finished + self.gap
        self.ignore_until = finished + self.settle
        self.last_size = size

    def note_log(self, size, now):
        if size is None:
            return
        if self.last_size is not None and size != self.last_size and now >= self.ignore_until:
            self.human_at = now
        self.last_size = size

    def poke(self, now):
        self.human_at = now

    def paused(self, now):
        return self.human_at is not None and now - self.human_at < self.quiet

    def due(self, now):
        return not self.paused(now) and now >= self.next_at


# ------------------------------------------------------------ 会话编排

DEFAULT_PROFILES = {"huawei": {"poll": ["display interface brief"], "version": "display version",
                               "pager_off": "screen-length 0 temporary", "timeout": 60}}


def load_profile(vendor, path=PROFILES_PATH):
    """内置 profile, JSON 只能改 poll / version / timeout (关翻页命令写死, 反正也要过闸门)."""
    prof = dict(DEFAULT_PROFILES.get(vendor) or {"poll": [], "version": None, "pager_off": None, "timeout": 60})
    try:
        with open(path, encoding="utf-8") as f:
            over = json.load(f).get(vendor)
    except (OSError, ValueError, AttributeError):
        over = None
    if isinstance(over, dict):
        if isinstance(over.get("poll"), list):
            prof["poll"] = over["poll"]
        if "version" in over:
            prof["version"] = over["version"]
        if isinstance(over.get("timeout"), (int, float)):
            prof["timeout"] = over["timeout"]
    return prof


_REFUSED_CRED = "[conbroker: 拒绝发送, 设备停在密码/用户名提示上"


class Observer:
    """一路串口的一次观察. 线程: start / tick / stop 都在同一个后台线程里调, 界面只读 ports / panel / state.

    exchange(step) -> 输出: 登录各步和 quit (broker_exchange); send(port, cmd, timeout) -> 输出: 轮询
    (survey_core.conbroker_send). state: idle -> polling -> stopped; 登录失败 failed; 会话掉了 lost.
    """

    def __init__(self, port, vendor, exchange, send, profile=None, pacer=None, log_size=None,
                 layouts=None, clock=time.monotonic):
        self.port, self.vendor, self.exchange, self.send = port, vendor, exchange, send
        base = load_profile(vendor)
        self.profile = dict(base, **(profile or {}))
        self.pacer = pacer or Pacer()
        self.log_size = log_size or (lambda: None)
        self.layouts = layouts if layouts is not None else load_layouts()
        self.clock = clock
        self.state, self.error, self.device, self.model = "idle", None, None, None
        self.ports, self.panel, self.polled_at = {}, [], None

    def _cmds(self):
        return list(self.profile.get("poll") or []) + ([self.profile["version"]] if self.profile.get("version") else [])

    def start(self, username, password):
        """预检 -> 登录 -> (型号). 成功返回 True; 预检不过一条都不发."""
        bad = [c for c in self._cmds() if not survey_core.is_readonly(c, self.vendor)]
        if not self.profile.get("poll") or bad or not parser_for(self.vendor):
            self.state = "failed"
            self.error = ("白名单不放, 一条都没发: " + "; ".join(map(str, bad))) if bad else \
                f"厂商 {self.vendor} 没有轮询命令或解析器"
            return False
        login = Login(username, password, self.profile.get("pager_off"))
        del password
        res = run_login(login, self.exchange, self.vendor)
        if res.kind != "done":
            self.state, self.error = "failed", res.note
            return False
        self.device = login.device
        self.state, self.error = "polling", None
        ver = self.profile.get("version")
        if ver:
            try:
                self.model = parse_model(self.send(self.port, ver, self.profile["timeout"]))
            except Exception:
                self.model = None
        return True                     # 日志基线留给第一次 tick: 登录那几步的断开标记不算人

    def tick(self, now):
        """到点就轮询一轮. 发了且成功 True; 没到点 / 人在用 / 失败 False."""
        if self.state != "polling":
            return False
        self.pacer.note_log(self.log_size(), now)
        if not self.pacer.due(now):
            return False
        t0 = self.clock()
        ok = self._poll()
        self.pacer.done(now, now + (self.clock() - t0), size=self.log_size())
        return ok

    def _poll(self):
        parse = parser_for(self.vendor)
        found = []
        for cmd in self.profile["poll"]:
            if not survey_core.is_readonly(cmd, self.vendor):      # 发前再过一次
                self.error = f"白名单不放: {cmd}"
                return False
            try:
                out = self.send(self.port, cmd, self.profile["timeout"])
            except Exception as e:
                self.error = str(e) or type(e).__name__
                return False
            out = out or ""
            if out.startswith(_REFUSED_CRED) or classify(out) in ("username", "password", "idle"):
                self.state, self.error = "lost", "会话掉了 (设备回到登录提示), 需要重新登录"
                return False
            dev = survey_core.parse_device(clean(out))
            if not dev:
                self.error = f"没回提示符: {last_line(out)[:40]!r}"
                return False
            if self.device and dev != self.device:
                self.state, self.error = "lost", f"提示符从 {self.device} 变成了 {dev}"
                return False
            found += parse(out)
        names = [p["name"] for p in found]
        if sorted(names) != sorted(self.ports):
            self.panel = layout(names, self.model, self.layouts)
        self.ports = {p["name"]: p for p in found}
        self.error, self.polled_at = None, time.time()
        return True

    def stop(self):
        """退出观察: 还登录着就发 quit, 不在 console 上留会话. 不抛."""
        if self.state == "polling":
            try:
                self.exchange(Step("send", "cmd", "quit", expect=None, timeout=3))
            except Exception as e:
                self.error = f"quit 没发出去: {e}"
        if self.state not in ("failed", "lost"):
            self.state = "stopped"

    def snapshot(self, head, now=None):
        """给副屏的快照: 端口位置 + 状态, 没有凭据."""
        ports = []
        for cell in self.panel:
            p = self.ports.get(cell["name"]) or {}
            ports.append(dict(cell, state=p.get("state", "unknown"), in_uti=p.get("in_uti"), out_uti=p.get("out_uti")))
        return {"ts": time.time() if now is None else now, "head": head, "device": self.device, "model": self.model,
                "state": self.state, "error": self.error, "ports": ports}


def front_view(doc, now, stale_s=FRONT_STALE_S):
    """快照 -> 副屏前面板区块 {title, state, ports: [{x, y, shape, label, state, lit}]}; 没有 / 过期 None."""
    if not isinstance(doc, dict) or not isinstance(doc.get("ts"), (int, float)) or now - doc["ts"] > stale_s:
        return None
    ports = doc.get("ports") or []
    if not ports:
        return None
    out = []
    for p in ports:
        st, lit = lamp(p, now)
        out.append({"x": p.get("x", 0), "y": p.get("y", 0), "shape": p.get("shape", "rj45"),
                    "label": p.get("label", ""), "state": st, "lit": lit})
    title = " · ".join(str(x) for x in (doc.get("head"), doc.get("device") or doc.get("model")) if x)
    return {"title": title, "state": doc.get("state"), "ports": out}


def read_snapshot(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def record(action, event=None, **kv):
    """cartlog 记一笔: 动作 + 操作头 / 设备 / 结果. 别传凭据进来."""
    return (event or cartlog.event)("observe", cartlog.fields(action=action, **kv))


# ------------------------------------------------------------ 真 exchange: 直接接 conbroker 本地口

def broker_exchange(ddir, name, step, vendor="huawei", idle=0.8):
    """按 conbroker 的协议 (HELLO claude[ secret]) 发一步, 等最后一行匹配 step.expect; expect=None 等安静 idle 秒.

    conbroker 服务端照样排队 (人最近敲过键) 和急停; secret 时它在日志和人的屏幕上把这一步打码.
    cmd 步再过一次 allowed(). 服务没在跑抛 OSError.
    """
    import serial_core
    if step.role == "cmd" and not allowed(step.text, vendor):
        raise ValueError(f"会话命令不在放行范围: {step.text!r}")
    port = serial_core.broker_port(ddir, name)
    if not port:
        raise OSError(f"{name} 没有在跑的 conbroker 服务")
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        s.sendall(b"HELLO claude secret\n" if step.secret else b"HELLO claude\n")
        s.sendall(step.text.encode() + b"\r")
        s.settimeout(0.1)
        buf, end, last = b"", time.time() + step.timeout, time.time()
        while time.time() < end:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                chunk = None
            if chunk == b"":
                break
            if chunk:
                buf, last = buf + chunk, time.time()
                if step.expect and re.search(step.expect, last_line(buf.decode("utf-8", "replace"))):
                    break
            elif not step.expect and buf and time.time() - last > idle:
                break
    finally:
        s.close()
    return clean(buf.decode("utf-8", "replace"))
