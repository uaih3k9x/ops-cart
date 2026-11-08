#!/usr/bin/env python3
"""串口观察模式 (#85): 登录一台设备, 轮询只读命令, 把端口状态画成前面板. 纯逻辑, 不碰 Tk.

  Login        登录状态机: 喂设备输出, 返回下一步 (Step). 华为 console 顺序: 唤醒 (单个回车) -> 见 Username: 才发用户名
               -> 见 Password: 才发密码 (secret) -> 见 <提示符> 发 screen-length 0 temporary.
  parse_brief  display interface brief (华为 S / AR / VRP8); PARSERS 按厂商留位置给 H3C / 锐捷.
  layout       接口名 -> 面板格子 (GE/XGE/25GE/40GE/100GE 分组, 奇数口上、偶数口下), 可按型号用 JSON 覆盖.
  lamp         灯: up 常亮, 有流量按 InUti/OutUti 闪 (lamps_core.blink_on), *down 琥珀, ^down 灰, down 灭.
  Pacer        轮询节奏: 按一轮实际耗时留空 (占空比), 日志在我们没发的时候长了 = 有人在用, 停手 quiet 秒后再来.
  Observer     把上面串起来: start(登录) / tick(到点轮询一次) / stop(只停轮询, 不往 console 发任何字节).

只读: 轮询命令一律过 survey_core.is_readonly (整轮预检, 有一条不过一条都不发); 会话命令只多放
SESSION_COMMANDS 这一条字面 (关翻页). 登录的唤醒 / 用户名 / 密码 / [Y/N] 回答由状态机按提示给出, 不走命令闸门.

密码: 只在 Login 里待到发出去那一步, 发完就丢; Step 的 repr 打码. 登录各步直接接 conbroker 本地口
(broker_exchange, HELLO claude secret), 不走 `sudo ... conbroker send <密码>`: 那样密码会出现在进程参数和
sudo 的日志里. 轮询仍走 survey_core.conbroker_send (等提示符、自动翻页、人在敲时排队).

先看现场再发 (conbroker 排队等人停手后照样写出去, 我们超时走了也收不回): 登录前读 conbroker 日志尾
(往回找有效行, 跳过 conbroker 标记行和设备异步日志; 认不出现场就不开始),
别人停在密码 / [Y/N] / 系统视图 / 半行命令 / 翻页上, 或 Agent 持有写租约, 一律不开始; 已经停在 <提示符> 上
只从"空闲" (按任意键 / Please Press ENTER / console exit) 开始: 停在 Username: 或提示符上都可能是 telnet 到了
别的设备, 认不出是谁的, 一律不开始, 也不借用. 唤醒后要看到新的 Username:, 而且唤醒以来的输出里不能有
telnet / stelnet / ssh / Connected to / Trying (嵌套会话). 发密码前日志尾要静止 QUIET_S 秒并且正停在 Password:.

不自动 quit (9x 定, #85): 要百分之百确认会话归属才能安全地 quit, 六轮审查都没收敛; 设备的 console 空闲超时
(华为默认 10 分钟) 足够兜底. 退出观察只停轮询, console 留在登录状态, 界面和 cartlog 写明, 要立刻登出请人去敲 quit.
"""

import functools
import json
import math
import os
import re
import socket
import threading
import time

import cartlog
import lamps_core
import survey_core

LAYOUTS_PATH = "/opt/pxe/observe_layouts.json"     # 可选: 按型号覆盖面板布局
PROFILES_PATH = "/opt/pxe/observe_profiles.json"   # 可选: 按厂商覆盖轮询命令
STATE_DIR = "/run/ops-cart/observe"               # 给副屏的快照, 每一路一个文件 (端口状态, 无凭据)
FRONT_STALE_S = 360                                # 这么久没轮询成功, 前面板的灯全灰 (标"旧数据")

# ------------------------------------------------------------ 会话命令闸门

SESSION_COMMANDS = frozenset({"screen-length 0 temporary"})   # 只认这一条字面; 不自动 quit, 所以 quit 不在里面
IDLE_LOGOUT = "交给设备的空闲超时登出 (华为 console 默认 10 分钟); 需要立刻登出, 请人到 console 上执行 quit"


def allowed(cmd, vendor):
    """观察模式能发的命令: 只读白名单, 或会话命令 (只有关翻页). 厂商得有内置只读规则."""
    if not isinstance(cmd, str) or vendor not in survey_core._RULES:
        return False
    return cmd in SESSION_COMMANDS or survey_core.is_readonly(cmd, vendor)


# ------------------------------------------------------------ 设备输出分类

USERNAME_RE = r"(?i)user ?name\s*[:：]\s*$"
PASSWORD_RE = r"(?i)password\s*[:：]\s*$"
YN_RE = r"(?i)\[y/n\]\s*[:：]?\s*$"
PROMPT_RE = r"^\s*<[^<>\r\n]+>\s*$"                # 华为用户视图 <SW>; sysname 带空格也认
SYSVIEW_RE = r"^\s*\[[~*]?[^\[\]\r\n]+\]\s*$"       # 系统视图 [SW] / [~SW] / [SW-GigabitEthernet0/0/1]
TYPING_RE = r"^\s*(<[^<>\r\n]+>|\[[~*]?[^\[\]\r\n]+\])\s*\S"   # 提示符后面有半行: 有人在敲
PAGER_RE = r"(?i)-{2,}\s*\(?more[^\r\n-]*\)?\s*-{2,}|按任意键继续|按空格键?继续"
IDLE_RE = r"(?i)(press any key|get started|any key to log on|please press enter|按任意键)"
ANY_RE = "|".join(f"(?:{p})" for p in (r"(?i:user ?name\s*[:：]\s*$)", r"(?i:password\s*[:：]\s*$)",
                                        r"(?i:\[y/n\]\s*[:：]?\s*$)", PROMPT_RE, SYSVIEW_RE))
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]")
_MARK = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [^\]]*|conbroker:[^\]]*)\]\s*$")   # conbroker 自己的标记行
# 设备往 console 吐的异步日志: %%01IFNET/4/LINK_STATE..., 或不带 %% 的"时间戳 主机名 模块/级别/"trap 行
_ASYNC = re.compile(r"%%\d\d[A-Za-z0-9_]+/\d+/|^\s*#?[A-Z][a-z]{2} +\d{1,2} \d{4} \d\d:\d\d:\d\d\S* \S+ [A-Z0-9_]+/\d/")


def clean(text):
    """去 ANSI / 退格 / 回车, 跟 conbroker 的 clean 一个意思."""
    text = _ANSI.sub("", text or "").replace("\x08", "")
    return text.replace("\r\n", "\n").replace("\r", "")


def _lines(text):
    """有效行: 去掉空行、conbroker 的标记行 ([时间 Claude 发送: …] / [conbroker: …没等到提示符]) 和设备异步日志."""
    return [ln for ln in clean(text).split("\n")
            if ln.strip() and not _MARK.match(ln.strip()) and not _ASYNC.search(ln)]


def last_line(text):
    """往回找最近一行有效行 (异步日志 / 标记行不算现场)."""
    lines = _lines(text)
    return lines[-1] if lines else ""


def device_text(text):
    """比对"有没有人插话"用: 只留有效行, conbroker 自己的接入 / 发送 / 断开标记和异步日志都不算."""
    return "\n".join(_lines(text))


def prompt_name(line):
    m = re.match(r"^\s*<([^<>\r\n]+)>\s*$", line or "")
    return m.group(1).strip() if m else None


def classify(text):
    """设备停在哪: username / password / yn / prompt / sysview / typing / pager / idle / empty / other."""
    ln = last_line(text)
    if not ln:
        return "empty"
    for kind, pat in (("yn", YN_RE), ("password", PASSWORD_RE), ("username", USERNAME_RE), ("prompt", PROMPT_RE),
                      ("sysview", SYSVIEW_RE), ("typing", TYPING_RE), ("pager", PAGER_RE), ("idle", IDLE_RE)):
        if re.search(pat, ln):
            return kind
    return "other"


# 登录前日志尾停在这些上 = 别人正在操作, 不唤醒不开始
_BUSY = {"password": "有人正在登录 (停在 Password:)", "yn": "设备在等别人回答 [Y/N]",
         "sysview": "有人停在系统视图里", "typing": "提示符后面有半行命令, 有人在敲", "pager": "有人在翻页看输出"}


_SESSION_GONE = [re.compile(p) for p in (
    # 用户名 / 密码只认行尾的登录提示 (display users 输出里的 "Username : admin" 不算)
    IDLE_RE, r"(?i)user ?name\s*[:：]\s*$", r"(?i)password\s*[:：]\s*$", r"^\s*(<[^<>\r\n]+>)?\s*quit\s*$",
    r"(?i)login authentication|the current login time|user last login|max number of vty users|console exit")]


def session_changed(text):
    """跳过没细看的日志里有没有会话换了人的迹象: 回到按任意键 / Username: / Password: / quit 回显 / 登录 banner."""
    return any(p.search(ln) for ln in _lines(text) for p in _SESSION_GONE)


_HUMAN = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (服务启动|人 接入|人 断开)")


_JOIN = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d 人 接入\]", re.M)


def human_joined(text):
    """这段日志里有没有人 attach 进来过 (哪怕之后又断开了: 他可能在 Password: 上敲了不回显的键)."""
    return bool(_JOIN.search(clean(text or "")))


NESTED_RE = re.compile(r"(?i)\b(s?telnet|ssh)\b|connected to|\btrying\b")   # 在 console 上又连去了别的设备


def bad_credential(s):
    """用户名 / 密码里带控制字符 (回车换行、\\x00-\\x1f、DEL) 就不收: 会被设备当成按键."""
    return any(ord(c) < 0x20 or ord(c) == 0x7f for c in s or "")


def humans_attached(text):
    """conbroker 日志 (从某次服务启动之后) 里现在 attach 着几个人: 接入 - 断开; 服务启动清零."""
    n = 0
    for ln in clean(text).split("\n"):
        m = _HUMAN.match(ln.strip())
        if m:
            n = 0 if m.group(1) == "服务启动" else max(0, n + (1 if m.group(1) == "人 接入" else -1))
    return n


def read_tail(path, n=4096):
    """conbroker 日志最后 n 字节 (人和设备的原始输出 + 标记行); 读不了 None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


_START_PREFIX = re.compile(rb"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")


def _service_start_at(buf, i, pre, at_file_start):
    """buf[i:] 的"服务启动"是不是 conbroker 自己的标记行: 前面紧挨着 "[时间 ", 再往前是行首."""
    if i < pre or not _START_PREFIX.fullmatch(buf[i - pre:i]):
        return False
    return at_file_start or buf[i - pre - 1:i - pre] in (b"\n", b"\r")


class LogFile:
    """conbroker 的日志文件. 判断"有没有人插话"按字节偏移只读新增部分 (尾巴滑动窗口的第一行会被截半, 不能整段比)."""

    def __init__(self, path):
        self.path = path

    def size(self):
        return lamps_core.file_size(self.path)

    def tail(self, n=16384):            # 够装下一屏异步日志 + 标记行, 往回找得到有效行
        return read_tail(self.path, n)

    def humans(self, limit=256 << 20, chunk=1 << 20):
        """现在 attach 着几个人: 从文件尾分块往回找最近一次"服务启动", 从那里往后数接入 / 断开.
        往回读满 limit 还没找到 (又不是读到了文件头) = 不知道, None. 日志不归 logrotate 管, 会一直长.
        轮询阶段每轮都要问: 记住数到哪了 (_hcache), 下次只往回找到那里、只数新增的部分."""
        size = self.size()
        if size is None:
            return None
        cache = getattr(self, "_hcache", None)
        if cache and cache[0] > size:               # 文件变短了 (截断 / 轮转): 从头来
            cache = None
        try:
            with open(self.path, "rb") as f:
                floor = cache[0] if cache else 0
                start, pos = self._find_start(f, size, limit, chunk, floor)
                if start is not None:
                    begin, n = start, 0
                elif cache:
                    begin, n = cache                    # 之后没有新的服务启动: 接着上次数
                elif pos > 0:
                    return None
                else:
                    begin, n = 0, 0
                f.seek(begin)
                at = begin
                for raw in f:                           # 逐行数, 不把几百 MB 读进内存
                    if not raw.endswith(b"\n"):
                        break                           # 半行留到下次
                    at += len(raw)
                    m = _HUMAN.match(clean(raw.decode("utf-8", "replace")).strip())
                    if m:
                        n = 0 if m.group(1) == "服务启动" else max(0, n + (1 if m.group(1) == "人 接入" else -1))
                self._hcache = (at, n)
                return n
        except OSError:
            return None

    @staticmethod
    def _find_start(f, size, limit, chunk, floor):
        """在 [floor, size) 里从后往前找 conbroker 的"服务启动"标记行 -> (行首偏移 或 None, 停在哪)."""
        mark = "服务启动".encode()
        pre = 21                                            # "[YYYY-MM-DD HH:MM:SS " 的长度
        pos = size
        while pos > floor and size - pos < limit:
            lo = max(floor, pos - chunk, size - limit)
            rs = max(0, lo - pre - 1)                       # 往前多读一点, 标记前缀跨块也能校验
            f.seek(rs)
            buf = f.read(pos - rs + len(mark))
            j = len(buf)
            while True:
                i = buf.rfind(mark, 0, j)
                if i < 0 or rs + i < lo:                    # 落在前一块的留给下一轮
                    break
                if _service_start_at(buf, i, pre, at_file_start=(rs + i - pre == 0)):
                    return rs + i - pre, lo
                j = i                                       # 设备回显 / 命令里的"服务启动"四个字: 继续往前找
            pos = lo
        return None, pos

    def read_from(self, off):
        """偏移 off 之后新增的内容; 文件变短 (轮转 / 截断) 或读不了 None, 调用方按"有变化"算."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                if off is None or off > end:
                    return None
                f.seek(off)
                return f.read(end - off).decode("utf-8", "replace")
        except OSError:
            return None


_CLAIMS = {}                            # 串口服务名 -> 谁占着 (观察窗口令牌 / "survey")
_CLAIMS_LOCK = threading.Lock()


def claim(name, who):
    """同一路串口同时只许一个用户: 一个观察窗口, 或者一次巡检. 两个 Observer 的登录会交错, 巡检的命令会插进观察."""
    with _CLAIMS_LOCK:
        if _CLAIMS.get(name, who) != who:
            return False
        _CLAIMS[name] = who
        return True


def release(name, who):
    with _CLAIMS_LOCK:
        if _CLAIMS.get(name) == who:
            del _CLAIMS[name]


def holder(name):
    return _CLAIMS.get(name)


def agent_busy(head, states=None):
    """Agent 写串口 (#21/#23) 占着这一路: 有写租约 / 在发 / 急停中 -> 原因; 没有 None. 读不懂按占着算."""
    m = re.fullmatch(r"con-(\d+)", head or "")
    if not m:
        return None
    try:
        if states is None:
            import confirm_core
            import mcp_serial
            st = confirm_core.slot_states(mcp_serial.peek())
        else:
            st = states()
        s = st.get(int(m.group(1))) or {}
    except Exception:
        return "读不懂 Agent 写串口的状态, 按占用算"
    if s.get("estop"):
        return "这一路急停中"
    if s.get("writer") or s.get("sending"):
        return "Agent 持有这一路的写租约"
    if s.get("pending"):
        return "这一路有 Agent 的命令在等人确认"
    return None


# ------------------------------------------------------------ 登录状态机

class Step:
    """状态机给出的一步. kind: send / done / fail; role: wake / user / pass / answer / cmd."""
    __slots__ = ("kind", "role", "text", "secret", "expect", "timeout", "note")

    def __init__(self, kind, role=None, text="", secret=False, expect=ANY_RE, timeout=10, note=""):
        self.kind, self.role, self.text, self.secret = kind, role, text, secret
        self.expect, self.timeout, self.note = expect, timeout, note

    def __repr__(self):
        text = "******" if self.secret else self.text
        return f"Step({self.kind}, {self.role}, {text!r}, note={self.note!r})"
    __str__ = __repr__


def _fail(note):
    return Step("fail", note=note)


class Login:
    """华为 console 登录. start(日志尾分类) 给第一步, 之后每拿到一次设备输出就 feed(), 直到 done / fail.

    老坑: quit 之后 console 停在"按任意键", 第一下按键只是唤醒. 唤醒只发一个回车 (打码; 落在已登录的提示符上
    只会重印提示符; 唤醒出来是提示符说明不是推车的会话, 停下),
    之后只有亲眼看到 Username: 才发用户名, 看到 Password: 才发密码. 唤醒后直接是 Password: = 纯密码认证的
    console, 不支持, 停下. [Y/N] 一律 N. 认证失败不重试 (防锁账号), 看不懂的提示停下等人.

    authed: 见过登录后的提示符 (device 记下它的名字); pw_sent: 密码确实发出去了 (由调用方在 exchange 返回后置).
    """
    MAX_WAKE, MAX_ANSWER = 3, 3

    def __init__(self, username, password, pager_off="screen-length 0 temporary"):
        self._user, self._password, self._pager_off = username, password, pager_off
        self.state, self.device = "wake", None
        self.authed = self.pw_sent = False
        self.pager_ok = None
        self._wakes = self._answers = 0

    def _wake(self):
        self._wakes += 1
        self.state = "wake"
        return Step("send", "wake", "", secret=True)     # 单个回车: 落在已登录的提示符上只重印一行提示符

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
                return Step("send", "user", self._user)
            if kind == "password":              # 回车唤醒直接到 Password: = console 只认密码 (没有 Username:)
                return self._stop("console 用的是纯密码认证, 暂不支持; 请人登录后再观察 (会借用那个会话)")
            if kind == "prompt":                # 从按任意键唤醒出来却是提示符: 现场判断错了 / 别人的会话, 不借用
                return self._stop("唤醒出来是已登录的提示符 (不是推车登录的会话), 不借用; 请人 quit 后再观察")
            if self._wakes >= self.MAX_WAKE:
                return self._stop(f"唤醒 {self._wakes} 次没等到 Username:, 最后一行: {last_line(output)[:40]!r}")
            return self._wake()
        if st == "user":
            if kind == "password":
                self.state = "pass"                 # pw_sent 由调用方在这一步真正发出 (exchange 返回) 后再置
                pw, self._password = self._password, None       # 发出去这一步之后就不留
                return Step("send", "pass", pw, secret=True)
            return self._stop(f"发了用户名没等到 Password:, 请人来处理: {last_line(output)[:40]!r}")
        if st == "pass":
            if kind == "prompt":                    # 登录后亲眼见到的提示符名 = 本设备, 轮询只认它
                self.authed, self.device = True, prompt_name(last_line(output))
                return self._pager()
            # 密码发出之后的报错只写分类, 不带设备原文 (万一回显了密码会经 cartlog / 状态栏带出去)
            if self._answers or re.search(r"(?i)(old|new|enter|confirm)\s+password", last_line(output)):
                return self._stop("设备要求先改密码 (回 N 不被接受), 请人在 console 上改完再观察")
            if kind in ("username", "password"):
                return self._stop("认证失败 (不重试, 免得锁账号)")
            return self._stop(f"看不懂的提示 (设备停在 {kind}), 请人来处理")
        if st == "screen":
            if kind != "prompt":
                return self._stop(f"关翻页没回提示符 (设备停在 {kind})")
            self.pager_ok = not _cmd_failed(output, self._pager_off)
            self.state = "done"
            self.device = prompt_name(last_line(output))
            return Step("done")
        return self._stop(f"状态机在 {st} 收到输出")

    def forget(self):
        self._password = None

    def _pager(self):
        self.state, self._password = "screen", None
        return Step("send", "cmd", self._pager_off)


def _cmd_failed(output, cmd):
    """命令回显之后那段里有没有 VRP 的报错 (Error: 开头的行 / 单独一个 ^ 的定位行); 异步日志里夹着的 Error 不算."""
    lines = [ln for ln in clean(output).split("\n") if not _MARK.match(ln.strip())]
    idx = next((i for i, ln in enumerate(lines) if ln.rstrip().endswith(cmd)), -1)
    return any(re.match(r"^\s*(Error:|\^\s*$)", ln) for ln in lines[idx + 1:])


class Interrupted(Exception):
    """登录中途有人插话 / 被取消: 不再往下发."""


def run_login(login, exchange, vendor, max_steps=12):
    """驱动 Login: exchange(step) -> 设备输出. cmd 步必须过 allowed(). 返回最后的 done / fail 步, 不抛.
    不管怎么结束 (成功 / 状态机失败 / 被打断 / 发送出错 / 步数超限), 出来时密码都已从 Login 里清掉."""
    try:
        return _drive_login(login, exchange, vendor, max_steps)
    finally:
        login.forget()


def _drive_login(login, exchange, vendor, max_steps):
    step = login.start()
    for _ in range(max_steps):
        if step.kind != "send":
            return step
        if step.role == "cmd" and not allowed(step.text, vendor):
            return _fail(f"会话命令不在放行范围: {step.text!r}")
        try:
            out = exchange(step)
        except Interrupted as e:
            return _fail(str(e))
        except Exception as e:
            return _fail(f"发送失败: {e or type(e).__name__}")
        step = login.feed(out)
    return step if step.kind != "send" else _fail("登录步数超限")


# ------------------------------------------------------------ display interface brief

_FLAGS = r"(?:\((?:[A-Za-z]{1,2}|\d+[GM])\))*"       # (l)(s)(d) 标记; (10G)(100M) 速率后缀只认不记
_UTI = r"\d+(?:\.\d+)?%|--"
_ROW = re.compile(rf"^\s*(?P<name>[A-Za-z0-9][\w./:-]*?)(?P<nf>{_FLAGS})\s+(?P<phy>[*^]?(?:up|down))(?P<pf>{_FLAGS})"
                  rf"\s+(?P<proto>up|down)(?P<rf>{_FLAGS})\s+(?P<in>{_UTI})\s+(?P<out>{_UTI})(?:\s+\d+\s+\d+)?\s*$")
_ODD = re.compile(rf"^\s*(?P<name>[A-Za-z0-9][\w./:-]*?){_FLAGS}\s+\S+\s+\S+")
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
            odd = _ODD.match(ln)            # 名字是物理口但状态列认不出 (#down 之类): 记成 unknown, 不静默丢
            if odd and port_kind(odd.group("name")):
                out.append({"name": odd.group("name"), "phy": "?", "proto": "?", "state": "unknown", "flags": [],
                            "in_uti": None, "out_uti": None})
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
                   r"(\d+(?:/\d+)*)/(\d+)(?::(\d+))?$")          # 拆分口 10GE1/0/1:1
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
            sub = _KIND.match(n).group(4)
            groups.setdefault((k[1], GROUP_ORDER.index(k[0])), []).append(((k[2], int(sub or 0)), n, k))
    out, x = [], x0
    for key in sorted(groups):
        items = sorted(groups[key])
        for i, ((num, sub), n, k) in enumerate(items):
            out.append({"name": n, "label": f"{num}:{sub}" if sub else str(num), "group": k[0],
                        "x": x + i // 2, "y": i % 2, "shape": k[3]})
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
        if not isinstance(ports, dict) or not all(_cell_ok(p) for p in ports.values()):
            return {}                   # 有一格不对整份不用 (半份覆盖会和自动排布叠在一起)
        out[model] = {"ports": ports}
    return out


MAX_X = 63                              # 面板最多 64 列, 两排 (y 只能 0 / 1)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _cell_ok(p):
    return isinstance(p, dict) and _int(p.get("x")) and 0 <= p["x"] <= MAX_X and p.get("y") in (0, 1) and _int(p["y"])


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
    if st in ("admin", "standby", "unknown"):     # unknown: brief 里有这个口但状态没认出来, 单独画
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
                               "pager_off": "screen-length 0 temporary", "timeout": 20}}
POLL_GRACE_S = 10       # conbroker send 子进程在 -t 之外多给的秒数 (巡检是 60); 退出观察最多等一轮 = 20 + 10 秒


def load_profile(vendor, path=PROFILES_PATH):
    """内置 profile, JSON 只能改 poll / version / timeout (关翻页命令写死, 反正也要过闸门)."""
    prof = dict(DEFAULT_PROFILES.get(vendor) or {"poll": [], "version": None, "pager_off": None, "timeout": 20})
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
QUIET_S = 6         # conbroker 的 HUMAN_QUIET (3 秒) 再加 3 秒: 这么久日志没新增有效内容才算没人在敲
EXPECT = {"wake": ("idle",), "user": ("username",), "pass": ("password",), "answer": ("yn",), "cmd": ("prompt",)}


class _NoLog:
    def size(self):
        return None

    def tail(self):
        return None

    def read_from(self, off):
        return None


class Observer:
    """一路串口的一次观察. 线程: start / tick / stop 都在同一个后台线程里调, 界面只读 ports / panel / state.

    exchange(step) -> 输出: 登录各步 (broker_exchange); send(port, cmd, timeout) -> 输出: 轮询
    (survey_core.conbroker_send). log: LogFile (size / tail / read_from 偏移); 读不了一律不发. busy() -> Agent 占用原因.
    state: idle -> polling -> stopped; 登录失败 failed; 会话掉了 lost.
    note: 退出后给人看的一句 (console 还登录着 / 可能还登录着, 交给空闲超时; 要立刻登出请人去 quit).

    白名单 (conbroker 排队等人停手后照样写出去, 我们超时走了也收不回, 所以拿不准就不发):
      发送: 现场 (日志尾往回找的有效行) 正好是这一步期望的状态, 且自上次确认 (字节偏移) 以来日志没有新增有效内容
            (conbroker 标记行和设备异步日志不算). 发密码前还要再静止 QUIET_S 秒. 不满足: 登录中止, 轮询跳过.
      退出: 不发任何字节 (不自动 quit), 只停轮询、写明 console 的状态.
    """

    def __init__(self, port, vendor, exchange, send, profile=None, pacer=None, log=None,
                 layouts=None, clock=time.monotonic, busy=None, sleep=time.sleep, wall=time.time,
                 quiet_s=QUIET_S):
        self.port, self.vendor, self.exchange, self.send = port, vendor, exchange, send
        base = load_profile(vendor)
        self.profile = dict(base, **(profile or {}))
        self.pacer = pacer or Pacer()
        self.log = log or _NoLog()
        self.layouts = layouts if layouts is not None else load_layouts()
        self.clock, self.busy = clock, busy or (lambda: None)
        self.sleep, self.wall, self.quiet_s = sleep, wall, quiet_s
        self.state, self.error, self.warning, self.device, self.model = "idle", None, None, None, None
        self.note = None
        self.ports, self.panel, self.polled_at = {}, [], None
        self.login = None
        self.user_sent = self._gone = False
        self._login_from = None
        self._ports_id = None           # 登录后第一轮看到的物理口集合: sysname 一样的两台 (<HUAWEI>) 换了线靠它认
        self._based = False
        self._advance(None)              # 还没有基线
        self.cancelled = lambda: False

    @property
    def pager_ok(self):
        return bool(self.login and self.login.pager_ok)

    def _cmds(self):
        return list(self.profile.get("poll") or []) + ([self.profile["version"]] if self.profile.get("version") else [])

    # ---- 看日志

    def _interjected(self):
        """自上次确认 (self._off) 以来日志有没有新增有效内容; 读不了按"有"算."""
        new = self.log.read_from(self._off)
        return new is None or device_text(new) != ""

    def _advance(self, to, out="", scan=True):
        """唯一能挪基线 (_off) 的入口 (有测试用 AST 盯着). 挪之前先扫 [旧偏移, 末尾) 和这次命令的输出 out:
        有登出 / 重新登录的迹象, 或日志读不了, 就判 lost 不挪 (会话可能已经不是我们的了, 不再往上发命令).
        scan=False 只给登录阶段的 _checkpoint 用: 那段里本来就有我们自己引出来的 Username: / Password:,
        而登录每一步发之前已经要求"没有新增有效内容 + 现场正是期望状态", 比扫更严."""
        old = self._off if self._based else None
        if self._based and to is None:          # 建立基线之后再读不到大小: 不挪, 一律 lost
            return self._lost("读不到 conbroker 日志, 跳过的部分没法检查, 停止轮询")
        if scan and self._based:
            new = self.log.read_from(old)
            if new is None or to is None or session_changed(new) or session_changed(out or ""):
                return self._lost("会话可能已经不是我们的了 (日志里有登出 / 重新登录, 或读不了), 停止轮询")
        elif scan and session_changed(out or ""):
            return self._lost("会话可能已经不是我们的了 (输出里有登出 / 重新登录), 停止轮询")
        self._off = to
        self._based = to is not None
        return True

    def _checkpoint(self, span=0.5, step=0.1, cap=3.0):
        """一步发完: 等日志大小静止 span 秒 (conbroker 在服务端收到 EOF 后才写"断开"标记) 再记偏移."""
        last, still, waited = self.log.size(), 0.0, 0.0
        while still < span and waited < cap:
            self.sleep(step)
            waited += step
            cur = self.log.size()
            still, last = (still + step, cur) if cur == last else (0.0, cur)
        new = self.log.read_from(self._off) if last is not None else None
        if new is None:
            raise Interrupted("中止: 读不到 conbroker 日志, 登录停在这里")
        if human_joined(new):
            raise Interrupted("中止: 登录中途有人 attach 过这路串口")
        self._advance(last, scan=False)

    def _quiet(self):
        """静止 quiet_s 秒: 这段时间和上次确认以来都没有新增有效内容."""
        if self._interjected():
            return False
        self.sleep(self.quiet_s)
        return not self._interjected()

    def _scene(self):
        """(分类, 提示符名); 日志读不了 (None, None)."""
        t = self.log.tail()
        if t is None:
            return None, None
        kind = classify(t)
        return kind, prompt_name(last_line(t)) if kind == "prompt" else None

    def _refuse(self, msg):
        self.state, self.error = "failed", msg
        return False

    # ---- 登录

    def start(self, username, password, cancelled=None):
        """预检 -> 看现场 -> 登录 -> (型号). 成功返回 True; 预检 / 现场不对一个字节都不发."""
        cancelled = cancelled or (lambda: False)
        bad = [c for c in self._cmds() if not survey_core.is_readonly(c, self.vendor)]
        if not self.profile.get("poll") or bad or not parser_for(self.vendor):
            return self._refuse(("白名单不放, 一条都没发: " + "; ".join(map(str, bad))) if bad else
                                f"厂商 {self.vendor} 没有轮询命令或解析器")
        why = self.busy()
        if why:
            return self._refuse(f"没开始: {why}")
        self._advance(self.log.size())  # 第一次记基线 (之前没有, 不用扫)
        if self._off is None or self.log.tail() is None:
            return self._refuse("没开始: 读不到 conbroker 日志, 看不了现场")
        if not self._quiet():
            return self._refuse("没开始: 有人在用这路串口 (日志还在新增)")
        why = self._attached()
        if why:
            return self._refuse(f"没开始: {why}")
        kind, _name = self._scene()
        if kind in _BUSY:
            return self._refuse(f"没开始: {_BUSY[kind]}")
        if kind in ("username", "prompt"):              # 可能是 telnet 到别的设备的: 认不出归属, 不碰
            return self._refuse("没开始: console 不在空闲状态: 已登录请先 quit, 停在 Username: 请先回车放弃, "
                                "让 console 回到\"按任意键\"再观察")
        if kind != "idle":                              # 往回找也认不出现场: 不唤醒
            return self._refuse("没开始: 看不出设备停在哪, console 不在空闲状态; 先在控制台上回车看看, 让它回到\"按任意键\"再来")
        self.cancelled = cancelled
        self._login_from = self._off                    # 唤醒以来的输出都要查嵌套会话
        self.login = Login(username, password, self.profile.get("pager_off"))
        del password
        res = run_login(self.login, functools.partial(self._guarded, cancelled), self.vendor)
        self.device = self.login.device
        if res.kind != "done":
            note = res.note
            if self.user_sent and not self.login.pw_sent:
                at, _n = self._scene()
                note += ("; 用户名是我们发的, console 停在 Password: 上, 请人到 console 上回车放弃或接着登录"
                         if at == "password" else "; 用户名已发出, 请人到 console 上看一眼")
            return self._refuse(note)
        self.state, self.error = "polling", None
        ver = self.profile.get("version")
        if ver and self._scene_ok() is None:
            out = ""
            try:
                out = self.send(self.port, ver, self.profile["timeout"]) or ""
                self.model = parse_model(out)
            except Exception:
                self.model = None
            self._advance(self.log.size(), out)     # 排队期间会话易主: 判 lost, 调用方看 state
        return True                     # 日志基线留给第一次 tick: 登录那几步的断开标记不算人

    def _attached(self):
        """登录阶段: 有人 attach 着 (conbroker 日志里"人 接入"没有对应的"断开") 就不发. 看不出来也不发."""
        n = self.log.humans() if hasattr(self.log, "humans") else None
        if n is None:
            return "看不出有没有人 attach 着这路串口"
        return f"有 {n} 个人 attach 着这路串口 (车机控制台以外的), 等他们断开" if n else None

    def _guarded(self, cancelled, step):
        """登录的每一步都过白名单: 没人插话 + 没人 attach + 现场正是这一步期望的状态 (密码前再静止 quiet_s 秒)."""
        why = self.busy()
        if why:
            raise Interrupted(f"中止: {why}")
        if step.role == "pass" and not self._quiet():
            raise Interrupted("中止: 发密码前有人插话 (日志有新增), 密码没发")
        if self._interjected():
            raise Interrupted("中止: 登录中途有人插话 (日志有新增)")
        if human_joined(self.log.read_from(self._off)):
            raise Interrupted("中止: 登录中途有人 attach 过这路串口 (可能在 Password: 上敲过不回显的键)")
        if step.role != "wake" and NESTED_RE.search(device_text(self.log.read_from(self._login_from) or "")):
            raise Interrupted("中止: 唤醒以来的输出里有 telnet / ssh / Connected to 之类, 像是嵌套到别的设备的会话")
        why = self._attached()
        if why:
            raise Interrupted(f"中止: {why}")
        kind, name = self._scene()
        want = EXPECT.get(step.role, ())
        if kind not in want or (kind == "prompt" and self.login.device and name != self.login.device):
            raise Interrupted(f"中止: 现场停在 {kind}, 不是这一步 ({step.role}) 该有的 {'/'.join(want)}")
        if cancelled():                     # 放在所有等待之后: 发密码前那 6 秒里点了退出也算数
            raise Interrupted("已取消")
        if step.role == "pass":
            self.login.pw_sent = True       # "密码可能已发出": 交给 exchange 那一刻起就算 (抛异常也可能已写出)
        out = self.exchange(step)
        if step.role == "user":
            self.user_sent = True
        self._checkpoint()
        return out

    # ---- 轮询

    def tick(self, now):
        """到点就轮询一轮. 发了且成功 True; 没到点 / 人在用 / 失败 False."""
        if self.state != "polling":
            return False
        self.pacer.note_log(self.log.size(), now)
        if self.pacer.paused(now) and not self._advance(self.log.size()):  # 人在用: 他敲的先扫一遍, 再从这里接着看
            return False
        if not self.pacer.due(now):
            return False
        if self.log.size() is None:         # 建立基线之后读不到日志: 跳过的部分没法检查, lost
            return self._advance(None)
        why = self._attached()              # 有人 attach 着就一直不轮询 (conbroker 会把命令接在他的半行后面并按回车)
        if why:
            self.error = f"这轮没发: {why}"
            self.pacer.poke(now)
            return False
        t0 = self.clock()
        ok = self._poll()
        self.pacer.done(now, now + (self.clock() - t0), size=self.log.size())
        if ok == "busy":                    # 有人 / Agent 在用: 这轮没发, 等对方停手 quiet 秒
            self.pacer.poke(now)
        return ok is True

    def _lost(self, msg, gone=False):
        """gone: 亲眼看到 console 回到按任意键 / Username: (推车的会话确实没了); 否则退出时照样提示可能还登录着."""
        self.state, self.error, self._gone = "lost", msg, gone
        return False

    def _scene_ok(self):
        """轮询 / 读型号前的白名单: 现场正是 <本设备> 用户视图, 且自上次确认以来没有新增有效内容.
        返回 None = 可以发; 否则 "lost" / "busy" (error 已写好)."""
        why = self.busy()
        if why:
            self.error = f"这轮没发: {why}"
            return "busy"
        kind, name = self._scene()
        if kind in ("idle", "username"):
            self._lost("会话掉了 (console 回到登录提示), 需要重新登录", gone=True)
            return "lost"
        if kind is None:
            self.error = "这轮没发: 读不到 conbroker 日志"
            return "busy"
        if self._interjected():
            if not self._advance(self.log.size()):   # 扫过再挪基线; 人停手 quiet 秒后从这里接着看
                return "lost"
            self.error = "这轮没发: 有人在用这路串口"
            return "busy"
        if kind != "prompt" or name != self.device:
            self.error = "这轮没发: " + (_BUSY.get(kind) or (f"现场停在 {kind}" if kind != "prompt" else "提示符不是本设备"))
            return "busy"
        return None

    def _poll(self):
        parse = parser_for(self.vendor)
        found = []
        for cmd in self.profile["poll"]:
            if self.cancelled():                                    # 点了退出: 不等这轮剩下的命令
                return False
            if not survey_core.is_readonly(cmd, self.vendor):      # 发前再过一次
                self.error = f"白名单不放: {cmd}"
                return False
            bad = self._scene_ok()
            if bad:
                return False if bad == "lost" else "busy"
            try:
                out = self.send(self.port, cmd, self.profile["timeout"]) or ""
            except Exception as e:
                self.error = str(e) or type(e).__name__
                self._advance(self.log.size())          # 发没发出去都扫一遍再挪
                return False
            if out.startswith(_REFUSED_CRED) or classify(out) in ("username", "password", "idle"):
                return self._lost("会话掉了 (设备回到登录提示), 需要重新登录", gone=True)
            if not self._advance(self.log.size(), out):   # 排队期间有人登出 / 登回同名设备: 基线不能越过去
                return False
            dev = prompt_name(last_line(out))
            if not dev:
                self.error = f"没回提示符 (设备停在 {classify(out)})"
                return False
            if self.device and dev != self.device:
                return self._lost(f"提示符从 {self.device} 变成了 {dev}")
            found += parse(out)
        if not found:
            self.error = "这轮输出里解析不到端口"
            return False
        odd = [p["name"] for p in found if p["state"] == "unknown"]
        self.warning = f"{len(odd)} 个口的状态没认出来: {', '.join(odd[:4])}" if odd else None
        ident = sorted(p["name"] for p in found if port_kind(p["name"]))
        if self._ports_id is None:
            self._ports_id = ident
        elif ident != self._ports_id:           # 提示符名一样但口不一样: 换了线 / 换了设备
            return self._lost("端口和登录时的不一样, 可能换了设备 (提示符名相同), 停止轮询")
        names = [p["name"] for p in found]
        if sorted(names) != sorted(self.ports):
            self.panel = layout(names, self.model, self.layouts)
        self.ports = {p["name"]: p for p in found}
        self.error, self.polled_at = None, self.wall()
        return True

    # ---- 退出

    def stop(self, now=None):
        """退出观察: 只停轮询, 不往 console 发任何字节 (不自动 quit). note 写明 console 现在大概什么状态. 不抛."""
        if self.state not in ("failed", "lost"):
            self.state = "stopped"
        self.note = self.exit_note()

    def exit_note(self):
        lg = self.login
        if not lg or (self.state == "lost" and self._gone):   # 没登录过 / 亲眼看到 console 回到登录提示
            return None
        if self.state == "lost" and (lg.authed or lg.pw_sent):
            return f"推车登录的会话可能仍处于登录状态, {IDLE_LOGOUT}"
        if lg.authed:
            return f"console 仍处于登录状态 (推车登录的会话), {IDLE_LOGOUT}"
        if lg.pw_sent:
            return f"密码可能已发出, console 可能仍处于登录状态, {IDLE_LOGOUT}"
        return None

    def snapshot(self, head, now=None, owner=None):
        """给副屏的快照: 端口位置 + 状态, 没有凭据. ts = 写入时间, polled_at = 上次轮询成功的时间."""
        ports = []
        for cell in self.panel:
            p = self.ports.get(cell["name"]) or {}
            ports.append(dict(cell, state=p.get("state", "unknown"), in_uti=p.get("in_uti"), out_uti=p.get("out_uti")))
        return {"ts": self.wall() if now is None else now, "polled_at": self.polled_at, "owner": owner,
                "head": head, "device": self.device, "model": self.model, "state": self.state, "error": self.error,
                "ports": ports}


WRITE_STALE_S = 30      # 车机每 5 秒写一次快照; 这么久没写 = 车机窗口没了, 副屏不画
_STATE_NOTE = {"idle": "登录中", "failed": "登录失败", "lost": "会话掉了", "stopped": "已退出"}


def front_view(doc, now, stale_s=FRONT_STALE_S):
    """快照 -> 副屏前面板区块 {title, stale, note, ports: [{x, y, shape, label, state, lit}]}; 没有 / 车机没在写 None.
    登录中 / 失败 / 掉线 / 很久没轮询成功 / 出错: 灯全灰 (stale), note 写原因; 还没有端口也照样给标题和原因."""
    if not isinstance(doc, dict) or not isinstance(doc.get("ts"), (int, float)) or now - doc["ts"] > WRITE_STALE_S:
        return None
    ports, state, polled = doc.get("ports") or [], doc.get("state"), doc.get("polled_at")
    note = doc.get("error") or _STATE_NOTE.get(state)
    if not note and (not isinstance(polled, (int, float)) or now - polled > stale_s):
        note = "旧数据" if polled else "等第一轮轮询"
    out = []
    for p in ports:
        st, lit = ("unknown", False) if note else lamp(p, now)
        out.append({"x": p.get("x", 0), "y": p.get("y", 0), "shape": p.get("shape", "rj45"),
                    "label": p.get("label", ""), "state": st, "lit": lit})
    title = " · ".join(str(x) for x in (doc.get("head"), doc.get("device") or doc.get("model")) if x)
    return {"title": title, "state": state, "stale": bool(note), "note": note, "ports": out}


def snapshot_path(head, state_dir=None):
    return os.path.join(state_dir or STATE_DIR, survey_core.safe_name(head) + ".json")


def read_snapshot(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def read_snapshots(state_dir=None):
    """每一路的快照, 新写的在前; 读不了的跳过."""
    state_dir = state_dir or STATE_DIR
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        return []
    docs = [read_snapshot(os.path.join(state_dir, n)) for n in names if n.endswith(".json")]
    docs = [d for d in docs if d and isinstance(d.get("ts"), (int, float))]
    return sorted(docs, key=lambda d: d["ts"], reverse=True)


def remove_snapshot(owner, path):
    """只删自己写的那份."""
    doc = read_snapshot(path)
    if not isinstance(doc, dict) or doc.get("owner") != owner:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


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
