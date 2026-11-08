#!/usr/bin/env python3
"""串口观察模式 (#85): 人在 console 上登录好, 推车只发只读命令, 把端口状态画成前面板. 纯逻辑, 不碰 Tk.

半自动 (9x 定): 推车完全不碰登录, 往 console 发的只有只读白名单里的命令. 人自己登录好之后点
"开始观察": probe() 读 conbroker 日志尾, 现场正是用户视图提示符 <xxx> 才给人确认设备名, 人确认后 start(xxx).

  parse_brief  display interface brief (华为 S / AR / VRP8); PARSERS 按厂商留位置给 H3C / 锐捷.
  layout       接口名 -> 面板格子 (GE/XGE/25GE/40GE/100GE 分组, 奇数口上、偶数口下), 可按型号用 JSON 覆盖.
  lamp         灯: up 常亮, 有流量按 InUti/OutUti 闪 (lamps_core.blink_on), *down 琥珀, ^down 灰, down 灭.
  Pacer        轮询节奏: 按一轮实际耗时留空 (占空比), 日志在我们没发的时候长了 = 有人在用, 停手 quiet 秒后再来.
  Observer     probe (看现场) / start (人确认后) / tick (到点轮询一次) / stop (只停轮询, 不发任何字节).

只读: 推车往 console 发的只有 survey_core.is_readonly 放行的命令 (整轮预检, 有一条不过一条都不发),
走 survey_core.conbroker_send (等提示符、自动翻页、人在敲时排队).

先看现场再发: 每条命令发之前, 现场 (日志尾往回找的有效行, 跳过 conbroker 标记行和设备异步日志) 必须正是
<确认过的设备名>, 且自上次确认 (字节偏移) 以来没有新增有效内容; 有人 attach 着 / Agent 占着这一路就不发.
跳过没细看的日志必须先扫 (_advance 是挪基线的唯一入口): 出现登出 / 重新登录的迹象就判 lost, 停止轮询.
"""

import json
import math
import os
import re
import threading
import time
import weakref

import cartlog
import lamps_core
import survey_core

LAYOUTS_PATH = "/opt/pxe/observe_layouts.json"     # 可选: 按型号覆盖面板布局
PROFILES_PATH = "/opt/pxe/observe_profiles.json"   # 可选: 按厂商覆盖轮询命令
STATE_DIR = "/run/ops-cart/observe"               # 给副屏的快照, 每一路一个文件 (端口状态, 无凭据)
FRONT_STALE_S = 360                                # 这么久没轮询成功, 前面板的灯全灰 (标"旧数据")

# ------------------------------------------------------------ 设备输出分类

USERNAME_RE = r"(?i)user ?name\s*[:：]\s*$"
PASS_PROMPT_RE = r"(?i)password\s*[:：]\s*$"        # 停在登录的口令提示上 (只用来认现场)
YN_RE = r"(?i)\[y/n\]\s*[:：]?\s*$"
PROMPT_RE = r"^\s*<[^<>\r\n]+>\s*$"                # 华为用户视图 <SW>; sysname 带空格也认
SYSVIEW_RE = r"^\s*\[[~*]?[^\[\]\r\n]+\]\s*$"       # 系统视图 [SW] / [~SW] / [SW-GigabitEthernet0/0/1]
TYPING_RE = r"^\s*(<[^<>\r\n]+>|\[[~*]?[^\[\]\r\n]+\])\s*\S"   # 提示符后面有半行: 有人在敲
PAGER_RE = r"(?i)-{2,}\s*\(?more[^\r\n-]*\)?\s*-{2,}|按任意键继续|按空格键?继续"
IDLE_RE = r"(?i)(press any key|get started|any key to log on|please press enter|按任意键)"
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
    for kind, pat in (("yn", YN_RE), ("password", PASS_PROMPT_RE), ("username", USERNAME_RE), ("prompt", PROMPT_RE),
                      ("sysview", SYSVIEW_RE), ("typing", TYPING_RE), ("pager", PAGER_RE), ("idle", IDLE_RE)):
        if re.search(pat, ln):
            return kind
    return "other"


# 轮询前日志尾停在这些上 = 有人正在操作 / 设备在等回答, 这一轮不发
_BUSY = {"password": "console 停在 Password: 上", "yn": "设备在等别人回答 [Y/N]",
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
    """这段日志里有没有人 attach 进来过 (哪怕之后又断开了)."""
    return bool(_JOIN.search(clean(text or "")))


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
    """同一路串口同时只许一个用户: 一个观察窗口, 或者一次巡检. 两个观察的命令会交错, 巡检的命令会插进观察."""
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


def slot_of(dev, realpath=os.path.realpath, slots=None):
    """设备路径 (/dev/con-N 别名、ttyUSB、by-id 都行) -> 推车操作头号 N; 对不上任何一路 None."""
    slots = slots or lamps_core.SLOTS
    try:
        target = realpath(dev)
        for path in slots:
            if dev == path or realpath(path) == target:
                return int(path.rsplit("-", 1)[1])
    except (OSError, ValueError, TypeError):
        pass
    return None


def agent_busy(dev, states=None, realpath=os.path.realpath):
    """Agent 写串口 (#21/#23) 占着这一路: 有写租约 / 在发 / 有命令等确认 / 急停中 -> 原因; 没有 None.
    先把设备路径解析到 con-N 操作头; 解析不出来 / 状态读不懂都按占着算 (看不清就不轮询)."""
    n = slot_of(dev, realpath)
    if n is None:
        return "看不清这一路对应哪个操作头 (con-N), 查不了 Agent 占用, 按占用算"
    try:
        if states is None:
            import confirm_core
            import mcp_serial
            st = confirm_core.slot_states(mcp_serial.peek())
        else:
            st = states()
        s = st.get(n) or {}
    except Exception:
        return "读不懂 Agent 写串口的状态, 按占用算"
    if s.get("estop"):
        return "这一路急停中"
    if s.get("writer") or s.get("sending"):
        return "Agent 持有这一路的写租约"
    if s.get("pending"):
        return "这一路有 Agent 的命令在等人确认"
    return None


_OBSERVING = set()                      # 正在观察的串口服务名: 串口面板 (哪怕是新建的) 不连这些口、不发唤醒回车
_OBSERVING_LOCK = threading.Lock()
_PANELS = weakref.WeakSet()             # 活着的串口面板 (kiosk 切页会新建): 接回控制台时找当前那个


def observing_add(name):
    with _OBSERVING_LOCK:
        _OBSERVING.add(name)


def observing_discard(name):
    with _OBSERVING_LOCK:
        _OBSERVING.discard(name)


def is_observing(name):
    with _OBSERVING_LOCK:
        return name in _OBSERVING


def register_panel(panel):
    _PANELS.add(panel)


def live_panels():
    return list(_PANELS)


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
                               "timeout": 20}}
POLL_GRACE_S = 10       # conbroker send 子进程在 -t 之外多给的秒数 (巡检是 60); 退出观察最多等一轮 = 20 + 10 秒


def load_profile(vendor, path=PROFILES_PATH):
    """内置 profile, JSON 只能改 poll / version / timeout (关翻页命令写死, 反正也要过闸门)."""
    prof = dict(DEFAULT_PROFILES.get(vendor) or {"poll": [], "version": None, "timeout": 20})
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

# 点"开始观察"时现场不是用户视图提示符: 给人看的原因
_NOT_READY = {
    "sysview": "console 停在系统视图里, 请先 return 回到用户视图 <设备名> 再开始",
    "username": "console 还没登录 (停在 Username:), 请先在 console 上登录",
    "password": "console 还没登录 (停在 Password:), 请先在 console 上登录",
    "idle": "console 还没登录 (停在按任意键), 请先在 console 上登录",
    "yn": "设备在等 [Y/N] 回答, 请先在 console 上处理完",
    "typing": "提示符后面有半行命令, 请先在 console 上敲完或删掉",
    "pager": "console 停在翻页上, 请先翻完或按 Ctrl+C",
}


class _NoLog:
    def size(self):
        return None

    def tail(self):
        return None

    def read_from(self, off):
        return None


class Observer:
    """一路串口的一次观察. 人已经在 console 上登录好; 推车只发只读命令, 不碰登录.

    send(port, cmd, timeout) -> 输出 (survey_core.conbroker_send); log: LogFile (size / tail / read_from 偏移 / humans);
    busy() -> Agent 占用原因. state: idle -> polling -> stopped; 开始不了 failed; 会话掉了 / 换了设备 lost.
    线程: probe 在界面线程, start / tick / stop 在同一个后台线程; 界面只读 ports / panel / state / error.
    """

    def __init__(self, port, vendor, send, profile=None, pacer=None, log=None, layouts=None,
                 clock=time.monotonic, busy=None, wall=time.time):
        self.port, self.vendor, self.send = port, vendor, send
        self.profile = dict(load_profile(vendor), **(profile or {}))
        self.pacer = pacer or Pacer()
        self.log = log or _NoLog()
        self.layouts = layouts if layouts is not None else load_layouts()
        self.clock, self.busy, self.wall = clock, busy or (lambda: None), wall
        self.state, self.error, self.warning, self.device, self.model = "idle", None, None, None, None
        self.ports, self.panel, self.polled_at = {}, [], None
        self._ports_id = None           # 第一轮看到的物理口集合: sysname 一样的两台 (<HUAWEI>) 换了线靠它认
        self._ports_odd = False         # 上一轮口集合对不上 (可疑); 连续两轮对不上才判 lost
        self._joined = False            # 上次挪基线跳过的那段里有人 attach 过
        self._ver_done = False
        self._based = False
        self._advance(None)              # 还没有基线
        self.cancelled = lambda: False

    def _cmds(self):
        return list(self.profile.get("poll") or []) + ([self.profile["version"]] if self.profile.get("version") else [])

    # ---- 看日志

    def _interjected(self):
        """自上次确认 (self._off) 以来日志有没有新增有效内容; 读不了按"有"算."""
        new = self.log.read_from(self._off)
        return new is None or device_text(new) != ""

    def _advance(self, to, out=""):
        """唯一能挪基线 (_off) 的入口 (有测试用 AST 盯着). 挪之前先扫 [旧偏移, 末尾) 和这次命令的输出 out:
        有登出 / 重新登录的迹象, 日志读不了或变短, 就判 lost 不挪 (会话可能已经不是这台 / 这个人的了)."""
        old = self._off if self._based else None
        if self._based and to is None:          # 建立基线之后再读不到大小: 不挪, 一律 lost
            return self._lost("读不到 conbroker 日志, 跳过的部分没法检查, 停止轮询")
        if self._based:
            new = self.log.read_from(old)
            if new is not None and human_joined(new):
                self._joined = True             # 下一条命令前要让出来
            if new is None or session_changed(new) or session_changed(out or ""):
                return self._lost("会话可能已经变了 (日志里有登出 / 重新登录, 或日志读不了 / 变短), 停止轮询")
        elif session_changed(out or ""):
            return self._lost("会话可能已经变了 (输出里有登出 / 重新登录), 停止轮询")
        self._off = to
        self._based = to is not None
        return True

    def _scene(self):
        """(分类, 提示符名); 日志读不了 (None, None)."""
        t = self.log.tail()
        if t is None:
            return None, None
        kind = classify(t)
        return kind, prompt_name(last_line(t)) if kind == "prompt" else None

    def _attached(self):
        """有人 attach 着 (conbroker 日志里"人 接入"没有对应的"断开") -> 给人看的一句; 看不出来也按有人算."""
        n = self.log.humans() if hasattr(self.log, "humans") else None
        if n is None:
            return "看不出有没有人 attach 着这路串口, 轮询暂停"
        return f"有人 attach 着这路串口 ({n} 个), 轮询暂停; 断开后自动开始" if n else None

    # ---- 开始

    def probe(self):
        """点"开始观察"时看现场 -> (设备名, None) 或 (None, 原因). 只读, 不发任何字节."""
        bad = [c for c in self._cmds() if not survey_core.is_readonly(c, self.vendor)]
        if not self.profile.get("poll") or bad or not parser_for(self.vendor):
            return None, (("白名单不放, 一条都不发: " + "; ".join(map(str, bad))) if bad else
                          f"厂商 {self.vendor} 没有轮询命令或解析器")
        why = self.busy()
        if why:
            return None, why
        kind, name = self._scene()
        if kind is None:
            return None, "读不到 conbroker 日志, 看不了现场"
        if kind == "prompt":
            return name, None
        return None, _NOT_READY.get(kind) or "看不出设备停在哪, 请在 console 上按一下回车, 看到 <设备名> 再开始"

    def start(self, name, cancelled=None):
        """人确认过设备名之后: 再看一次现场 (还得是 <name>), 记基线, 开始轮询. 这里不发任何字节 (型号在第一轮读)."""
        self.cancelled = cancelled or (lambda: False)
        got, why = self.probe()
        if why or got != name:
            self.state, self.error = "failed", why or f"提示符从 <{name}> 变成了 <{got}>, 请重新开始"
            return False
        self._advance(self.log.size())
        if self._off is None:
            self.state, self.error = "failed", "读不到 conbroker 日志, 看不了现场"
            return False
        self.device, self.state, self.error = name, "polling", None
        return True

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
            self.error = why
            self.pacer.poke(now)
            return False
        t0 = self.clock()
        ok = self._poll()
        self.pacer.done(now, now + (self.clock() - t0), size=self.log.size())
        if ok == "busy":                    # 有人 / Agent 在用: 这轮没发, 等对方停手 quiet 秒
            self.pacer.poke(now)
        return ok is True

    def _lost(self, msg):
        self.state, self.error = "lost", msg
        return False

    def _scene_ok(self):
        """每条命令发之前的白名单: 现场正是 <本设备> 用户视图, 且自上次确认以来没有新增有效内容.
        返回 None = 可以发; 否则 "lost" / "busy" (error 已写好)."""
        why = self.busy() or self._attached()   # 每条命令前都查: 上一条命令期间可能有人 attach 进来
        if why:
            self.error = why if "attach" in why else f"这轮没发: {why}"
            return "busy"
        if self._joined:                        # 上一条命令期间有人 attach 过 (哪怕又断开了): 这轮让出来
            self._joined = False
            self.error = "这轮没发: 刚才有人 attach 过这路串口"
            return "busy"
        kind, name = self._scene()
        if kind in ("idle", "username"):
            self._lost("会话掉了 (console 回到登录提示), 请人重新登录后再开始观察")
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

    def _send_checked(self, cmd):
        """过白名单 + 现场检查后发一条 -> 输出; 没发 / 出错返回 (None, 结果)."""
        if self.cancelled():                                        # 点了退出: 不等这轮剩下的命令
            return None, False
        if not survey_core.is_readonly(cmd, self.vendor):          # 发前再过一次
            self.error = f"白名单不放: {cmd}"
            return None, False
        bad = self._scene_ok()
        if bad:
            return None, (False if bad == "lost" else "busy")
        try:
            out = self.send(self.port, cmd, self.profile["timeout"]) or ""
        except Exception as e:
            self.error = str(e) or type(e).__name__
            self._advance(self.log.size())          # 发没发出去都扫一遍再挪
            return None, False
        if out.startswith(_REFUSED_CRED) or classify(out) in ("username", "password", "idle"):
            return None, self._lost("会话掉了 (设备回到登录提示), 请人重新登录后再开始观察")
        if not self._advance(self.log.size(), out):   # 排队期间有人登出 / 登回同名设备: 基线不能越过去
            return None, False
        return out, True

    def _poll(self):
        ver = self.profile.get("version")
        if ver and not self._ver_done:                  # 型号在第一轮读, 同样过 attach / 现场检查
            out, res = self._send_checked(ver)
            if out is None:
                return res
            self._ver_done, self.model = True, parse_model(out)
        parse = parser_for(self.vendor)
        found = []
        for cmd in self.profile["poll"]:
            out, res = self._send_checked(cmd)
            if out is None:
                return res
            dev = prompt_name(last_line(out))
            if not dev:
                self.error = f"没回提示符 (设备停在 {classify(out)})"
                return False
            if dev != self.device:
                return self._lost(f"提示符从 {self.device} 变成了 {dev}, 停止轮询")
            found += parse(out)
        if not found:
            self.error = "这轮输出里解析不到端口"
            return False
        ident = sorted(p["name"] for p in found if port_kind(p["name"]))
        if self._ports_id is None:
            self._ports_id = ident
        elif ident != self._ports_id:           # 提示符名一样但口不一样: 换了线 / 换了设备, 或者异步日志打断了一行
            if self._ports_odd:
                return self._lost("连续两轮端口和开始时的不一样, 可能换了设备 (提示符名相同), 停止轮询")
            self._ports_odd = True
            self.warning = "这轮端口和开始时的不一样 (可疑), 下一轮再看"
            return False
        self._ports_odd = False
        odd = [p["name"] for p in found if p["state"] == "unknown"]
        self.warning = f"{len(odd)} 个口的状态没认出来: {', '.join(odd[:4])}" if odd else None
        names = [p["name"] for p in found]
        if sorted(names) != sorted(self.ports):
            self.panel = layout(names, self.model, self.layouts)
        self.ports = {p["name"]: p for p in found}
        self.error, self.polled_at = None, self.wall()
        return True

    # ---- 退出

    def stop(self):
        """退出观察: 只停轮询, 不往 console 发任何字节 (会话是人的, 人自己处理). 不抛.
        暂时性的提示 (这轮没发 / 有人 attach) 清掉; 没开始 / 掉线的原因留着."""
        if self.state not in ("failed", "lost"):
            self.state, self.error = "stopped", None

    def snapshot(self, head, now=None, owner=None):
        """给副屏的快照: 端口位置 + 状态. ts = 写入时间, polled_at = 上次轮询成功的时间."""
        ports = []
        for cell in self.panel:
            p = self.ports.get(cell["name"]) or {}
            ports.append(dict(cell, state=p.get("state", "unknown"), in_uti=p.get("in_uti"), out_uti=p.get("out_uti")))
        return {"ts": self.wall() if now is None else now, "polled_at": self.polled_at, "owner": owner,
                "head": head, "device": self.device, "model": self.model, "state": self.state, "error": self.error,
                "ports": ports}


WRITE_STALE_S = 30      # 车机每 5 秒写一次快照; 这么久没写 = 车机窗口没了, 副屏不画
_STATE_NOTE = {"idle": "准备开始", "failed": "没开始", "lost": "会话掉了", "stopped": "已退出"}


def front_view(doc, now, stale_s=FRONT_STALE_S):
    """快照 -> 副屏前面板区块 {title, stale, note, ports: [{x, y, shape, label, state, lit}]}; 没有 / 车机没在写 None.
    准备中 / 没开始 / 掉线 / 很久没轮询成功 / 出错: 灯全灰 (stale), note 写原因; 还没有端口也照样给标题和原因."""
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
