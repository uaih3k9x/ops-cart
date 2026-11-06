"""mcp_core: 推车 MCP 服务 (issue #3 / #19) 的可测核心层. 只读工具, 不往任何设备写.

- 执行绪命名: 从串口输出的提示符认名字 (华为 <名> / [名]), 调用和日志永远带槽位号, 名字只是标签
- 参数校验: 工具参数用一张小表声明, 同一张表导出 inputSchema; 不认识的参数、类型不对一律拒
- 协议: JSON-RPC 2.0 (initialize / ping / tools/list / tools/call), 不依赖 mcp SDK; 传输在 cart_mcp
- 审计: 每次 tools/call 一行 JSON (谁、档位、哪个工具、参数、成没成、耗时), 落 /var/log/ops-cart/
- 档位 (#49): 每个工具显式标 readonly / act; 只读档的 Server 根本不注册 act 工具, HTTP 两档令牌分开
"""

import base64
import hmac
import fcntl
import json
import os
import re
import threading
import time

import pxeserver
import web_core

SLOTS = (1, 2, 3, 4)            # 操作头槽位, 对应 /dev/con-1~4 (lamps_core.SLOTS)


# ------------------------------------------------------------ 执行绪命名

NAME_SCAN_LINES = 40            # 只看最近这么多行, 太早的提示符可能是上一台设备的
_USER_VIEW = re.compile(r"^<([^\s<>]{1,64})>")                  # <名>: 用户视图
_SYS_VIEW = re.compile(r"^\[[~*]?([^\s\[\]~*][^\s\[\]]{0,127})\]")  # [名] / [~名] / [*名]: 系统视图
# [名-GigabitEthernet0/0/1] 这类子视图: 横杠后面是视图关键字就切掉 (名字本身可以带横杠)
_VIEW_SUFFIX = re.compile(
    r"^(.+?)-(?:X?GigabitEthernet|\d+GE|Ethernet|Eth-Trunk|Vlanif|LoopBack|NULL|MEth|Tunnel|Dialer"
    r"|Virtual-Template|ui-|aaa|vlan|ospf|bgp|isis|rip|acl|radius|hwtacacs|stelnet|ssh|snmp|ntp"
    r"|dhcp|ip-pool|pool|domain|lldp|stp|wlan|policy|traffic|route-policy|zone|nat|ike|ipsec)")
_NOT_NAME = re.compile(r"^(cr|\d+-\d+)$", re.I)                 # ? 帮助里的 <cr> / <1-4094>


def _ok_name(n):
    return bool(n) and "/" not in n and not _NOT_NAME.match(n)


def thread_name(text):
    """一段串口输出 -> 设备名 (最近的提示符为准); 认不出返回 None.
    只认顶格的提示符 (? 帮助是缩进的); [Y/N] 这类确认提示跳过往前找."""
    lines = [ln.rstrip("\r") for ln in (text or "").split("\n")][-NAME_SCAN_LINES:]
    users = [m.group(1) for m in map(_USER_VIEW.match, lines) if m]
    for ln in reversed(lines):
        m = _USER_VIEW.match(ln)
        if m and _ok_name(m.group(1)):
            return m.group(1)
        m = _SYS_VIEW.match(ln)
        if not m:
            continue
        raw = m.group(1)
        # 窗口里见过 <名>: [名] / [名-子视图] 对得上就用它, 最准
        hit = next((u for u in reversed(users) if raw == u or raw.startswith(u + "-")), None)
        if hit is None:
            sm = _VIEW_SUFFIX.match(raw)
            hit = sm.group(1) if sm else raw
        if _ok_name(hit):
            return hit
    return None


_MARKER = re.compile(r"\[\d{4}-\d{2}-\d{2} ")
_MARKER_TIME = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def device_output_seen(text):
    """这段串口日志里有没有设备自己吐的字 (conbroker 的 [时间 ...] 标记行和空行不算). 串口探测不了对端, 只能看这个."""
    return any(ln.strip() and not _MARKER.match(ln) for ln in (text or "").splitlines())


def last_output_age(text, now, log_mtime=None):
    """最后一次设备输出大约多少秒前; 没输出 None. 输出行没时间: 后面有标记行就按它算 (输出不晚于标记),
    在日志尾巴上就按日志修改时间, 都没有算 0."""
    lines = (text or "").splitlines()
    last = next((i for i in range(len(lines) - 1, -1, -1) if device_output_seen(lines[i])), None)
    if last is None:
        return None
    for ln in lines[last + 1:]:
        m = _MARKER_TIME.match(ln)
        if m:
            try:
                return max(0, int(now - time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))))
            except (ValueError, OverflowError):
                break
    return max(0, int(now - log_mtime)) if log_mtime else 0


def snap_label(dev, friendly, signal):
    """截图 / OCR 回给 Agent 的说明: 写清是哪块卡, 没信号明说. signal=None = 不知道."""
    name = f"{friendly} ({dev})" if friendly else dev
    return f"{name} 没信号 (灰屏), 换一块或查 HDMI" if signal is False else f"{name} 当前画面"


def slot_label(slot, name):
    return f"槽位 {slot} ({name})" if name else f"槽位 {slot}"


# ------------------------------------------------------------ 参数校验

class ArgError(ValueError):
    """参数不对: 原样回给 Agent 让它改了再调."""


_SCHEMA_KEYS = ("type", "minimum", "maximum", "maxLength", "enum", "default", "description")


def input_schema(params):
    """参数表 -> JSON Schema (tools/list 里的 inputSchema)."""
    props, req = {}, []
    for k, spec in params.items():
        p = {x: spec[x] for x in _SCHEMA_KEYS if x in spec}
        if "pattern" in spec:
            p["pattern"] = f"^{spec['pattern']}$"
        props[k] = p
        if spec.get("required"):
            req.append(k)
    out = {"type": "object", "properties": props, "additionalProperties": False}
    if req:
        out["required"] = req
    return out


def _check_one(k, spec, v):
    if spec["type"] == "integer":
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ArgError(f"{k} 要整数, 给的是 {v!r}")
        if v < spec.get("minimum", v) or v > spec.get("maximum", v):
            raise ArgError(f"{k} 要在 {spec.get('minimum')}~{spec.get('maximum')} 之间, 给的是 {v}")
    elif spec["type"] == "boolean":
        if not isinstance(v, bool):
            raise ArgError(f"{k} 要 true/false, 给的是 {v!r}")
    elif spec["type"] == "string":
        if not isinstance(v, str):
            raise ArgError(f"{k} 要字符串, 给的是 {v!r}")
        if len(v) > spec.get("maxLength", 256):
            raise ArgError(f"{k} 太长")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
            raise ArgError(f"{k} 格式不对: {v!r}")
    if "enum" in spec and v not in spec["enum"]:
        raise ArgError(f"{k} 只能是 {spec['enum']}, 给的是 {v!r}")
    return v


def check_args(params, args):
    """arguments -> 干净的 dict (缺省值补上); 不对抛 ArgError."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ArgError("arguments 要是对象")
    extra = sorted(set(args) - set(params))
    if extra:
        raise ArgError(f"不认识的参数: {', '.join(extra)}")
    out = {}
    for k, spec in params.items():
        if k in args:
            out[k] = _check_one(k, spec, args[k])
        elif spec.get("required"):
            raise ArgError(f"缺参数 {k}")
        elif "default" in spec:
            out[k] = spec["default"]
    return out


# ------------------------------------------------------------ 内容

def text_content(s):
    return {"type": "text", "text": s}


def json_content(obj):
    return text_content(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def image_content(data, mime="image/png"):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}


# ------------------------------------------------------------ 审计: 每次调用一行 JSON

AUDIT_PATH = "/var/log/ops-cart/mcp-audit.log"     # 名字跟 *.log 走, 现成的 logrotate 规则就管上了
AUDIT_CLIP = 200


def _clip(v):
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k)[:64]: _clip(x) for k, x in list(v.items())[:32]}
    if isinstance(v, (list, tuple)):
        return [_clip(x) for x in list(v)[:32]]
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) <= AUDIT_CLIP else s[:AUDIT_CLIP] + "…"


def audit_line(who, tool, args, ok, error=None, ms=None, now=None, profile=None):
    """'谁、哪个档位、哪个工具、参数、成没成' 一行 JSON (json.dumps 把换行转义掉, 不会断行)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))
    d = {"ts": ts, "who": _clip(who), "tool": _clip(tool), "args": _clip(args), "ok": bool(ok)}
    if profile is not None:
        d["profile"] = profile
    if ms is not None:
        d["ms"] = int(ms)
    if error is not None:
        d["error"] = _clip(str(error))
    return json.dumps(d, ensure_ascii=False)


class Audit:
    """追加写 JSONL. 每行开一次文件: logrotate copytruncate 之后照样写对地方. 写失败吞掉, 返回 False."""

    def __init__(self, path=AUDIT_PATH):
        self.path, self._lock = path, threading.Lock()

    def write(self, line):
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            return True
        except OSError:
            return False


# ------------------------------------------------------------ JSON-RPC 2.0 / MCP

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")    # 第一个 = 最新, 对方给的不认识就回它
INSTRUCTIONS = ("运维推车 (ops-cart) 的工具: 只读的看串口操作头 (槽位 1-4) 的输出、截被测机器画面、"
                "看推车状态和 PXE 装机模式. 写串口要先 lease_acquire 拿写租约并按时续期; 只读命令直接发, "
                "其余挂起等人在推车上确认 (pending_list 看结果); 拿不准就 estop. 说到执行绪时永远带上槽位号, 名字只是标签. "
                "PXE 解锁只能 pxe_request 申请, 人在推车上照抄确认码才生效.")
READONLY_INSTRUCTIONS = ("运维推车 (ops-cart) 只读档: 只能看串口操作头 (槽位 1-4) 的输出、截图 / OCR 被测机器画面、"
                         "看推车、键鼠、PXE 状态和挂起的申请. 这个服务里没有任何会动手的工具 (键鼠、写串口、网络唤醒、"
                         "PXE 申请、打印都不在), 要动手得由人另接动手档. 说到执行绪时永远带上槽位号, 名字只是标签.")

# ------------------------------------------------------------ 档位 (#49)

READONLY, ACT = "readonly", "act"           # 工具的标注: 只看 / 会产生副作用
KINDS = (READONLY, ACT)
PROFILES = {READONLY: (READONLY,), ACT: (READONLY, ACT)}     # 档位 -> 注册哪些标注的工具; act 档 = 现在的全部工具
DEFAULT_PROFILE = ACT       # 现有 `cart_mcp.py stdio` 接法依赖全部工具, 先不改 (待拍板)
READONLY_PORT = 8766        # 只读档 HTTP 默认端口 (动手档沿用 mcp.json 顶层 port)
SERVER_NAMES = {READONLY: "ops-cart-readonly", ACT: "ops-cart"}


def check_profile(profile):
    if profile not in PROFILES:
        raise ValueError(f"不认识的档位 {profile!r}, 只有 {', '.join(PROFILES)}")
    return profile


def select_tools(tools, profile):
    """按档位挑工具. 标注不认识的一律报错 (宁可起不来, 不猜它会不会动手)."""
    allow = PROFILES[check_profile(profile)]
    out = []
    for t in tools:
        if getattr(t, "kind", None) not in KINDS:
            raise ValueError(f"工具 {getattr(t, 'name', t)!r} 没有标 readonly / act")
        if t.kind in allow:
            out.append(t)
    return out


def profile_tokens(cfg, profile):
    """mcp.json -> 这一档认的令牌. 动手档 = 顶层 tokens (兼容现有配置), 只读档 = readonly.tokens;
    只读档没配就是空 (不借动手令牌); 动手档不认和任何只读令牌同值的 (手抖填重了也拿不到动手权限)."""
    check_profile(profile)
    ro = cfg.get("readonly")
    ro = ro.get("tokens") if isinstance(ro, dict) else None
    ro = ro if isinstance(ro, dict) else {}
    if profile == READONLY:
        return dict(ro)
    act = cfg.get("tokens")
    bad = {v for v in ro.values() if isinstance(v, str)}
    return {k: v for k, v in (act if isinstance(act, dict) else {}).items() if v not in bad}


def profile_port(cfg, profile):
    if check_profile(profile) == READONLY:
        ro = cfg.get("readonly")
        return int((ro if isinstance(ro, dict) else {}).get("port", READONLY_PORT))
    return int(cfg.get("port", 8765))


class ToolError(Exception):
    """工具跑不下去 (线没插、服务没开…): 以 isError 结果回给 Agent, 不算协议错误."""


class Tool:
    """kind 必填: READONLY (只看) / ACT (会产生副作用). 只读档靠它决定注册不注册, 所以不给默认值."""

    def __init__(self, name, description, params, fn, *, kind):
        if kind not in KINDS:
            raise ValueError(f"工具 {name!r} 的标注要是 readonly / act, 给的是 {kind!r}")
        self.name, self.description, self.params, self.fn, self.kind = name, description, params, fn, kind

    def spec(self):
        ann = {"readOnlyHint": self.kind == READONLY, "openWorldHint": False}
        if self.kind == READONLY:
            ann["destructiveHint"] = False      # 动手工具不默认写 False: 不写 = 客户端按可能有破坏处理 (#49)
        return {"name": self.name, "description": self.description, "inputSchema": input_schema(self.params),
                "annotations": ann}


def _error(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _result(rid, res):
    return {"jsonrpc": "2.0", "id": rid, "result": res}


RO_RATE = 3.0       # 只读档 tools/call 整体限速: 每秒补几次 (#60 第三轮)
RO_BURST = 10       # 桶容量: 一口气最多几次
RO_AUDIT_EVERY_S = 1.0  # 被限速的调用审计合并: 这么久最多记一条, 带上中间没逐条记的次数


RO_SLOTS = 2        # 只读档同时最多几个进程 (stdio 每个 ssh 会话一个 + HTTP 单元一个), 见 take_ro_slot
RO_SLOT_DIR = "/run/ops-cart"


class SlotBusy(Exception):
    """只读档进程槽位满了 / 槽位文件打不开."""


def take_ro_slot(d=None, n=None):
    """只读档进程启动时抢一个跨进程槽位 (flock, 不等): 拿到返回 fd (进程活着就一直拿着, 退出自动放);
    满了或文件打不开抛 SlotBusy (fail-closed). 令牌桶是进程内的, 槽位把"多开会话成倍放大"封顶在 n 倍 (#60)."""
    d, n = d or RO_SLOT_DIR, n or RO_SLOTS
    for i in range(n):
        path = os.path.join(d, f"mcp-ro-slot{i}.lock")
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            raise SlotBusy(f"只读档槽位文件 {path} 打不开 ({e}), 不起服务") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
    raise SlotBusy(f"只读档已经有 {n} 个进程在跑 (stdio 会话或 cart-mcp-ro), 同时最多 {n} 个; 先关掉一个再连")


AUDIT_SRC_CAP = 1000    # 合并审计里"来自几个不同来源"最多数到多少 (只记个数, 集合有上限)


class AuditMerge:
    """同一类审计 (key) 每 every 秒最多记一条. note(key, src) -> (这次记不记, 上次之后有几次没逐条记, 那几次来自几个
    不同来源); flush(emit) 把还没落盘的尾巴计数交给 emit(key, 次数, 来源数) 写出 (进程退出 / 定时).
    限速调用和 HTTP 认证失败都用它: 刷请求不能把审计写满磁盘、借轮转冲掉真实记录 (#60)."""

    def __init__(self, clock, every=RO_AUDIT_EVERY_S):
        self.clock, self.every, self.lock = clock, every, threading.Lock()
        self.last, self.quiet, self.srcs = {}, {}, {}

    def note(self, key, src=None):
        with self.lock:
            now = self.clock()
            last = self.last.get(key)
            if last is not None and now - last < self.every:
                self.quiet[key] = self.quiet.get(key, 0) + 1
                seen = self.srcs.setdefault(key, set())
                if src is not None and len(seen) < AUDIT_SRC_CAP:
                    seen.add(src)
                return False, 0, 0
            self.last[key] = now
            return True, self.quiet.pop(key, 0), len(self.srcs.pop(key, ()))

    def flush(self, emit):
        with self.lock:
            pend = [(k, q, len(self.srcs.pop(k, ()))) for k, q in self.quiet.items() if q]
            self.quiet.clear()
        for k, q, n in pend:
            emit(k, q, n)


class _Bucket:
    """令牌桶; clock 单调时钟. 线程安全 (HTTP 多线程共用一个 Server)."""

    def __init__(self, rate, burst, clock):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.t, self.lock = float(burst), clock(), threading.Lock()

    def take(self):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0.0, now - self.t) * self.rate)
            self.t = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class Server:
    """消息进, 回包出; 不管传输 (stdio / HTTP 在 cart_mcp). 多线程 HTTP 共用一个实例, 本身无状态.
    profile 决定收哪些工具: 给了档位不允许的工具直接报错, 不是注册了再拒."""

    def __init__(self, tools, audit=None, version="", clock=time.monotonic, profile=DEFAULT_PROFILE):
        tools = list(tools)
        picked = select_tools(tools, profile)
        if len(picked) != len(tools):
            extra = sorted(t.name for t in tools if t not in picked)
            raise ValueError(f"{profile} 档不收这些工具: {', '.join(extra)}")
        self.tools = {t.name: t for t in picked}
        self.audit, self.version, self.clock, self.profile = audit, version, clock, profile
        # 只读档 (#60): 不收批量, tools/call 整体令牌桶; 动手档照旧 (那边有租约 / 闸门 / 人确认)
        self.bucket = _Bucket(RO_RATE, RO_BURST, clock) if profile == READONLY else None
        self._merge = AuditMerge(clock)

    def handle_text(self, text, who="-"):
        """一段 JSON 文本 -> 回包文本 (单行); 全是通知时返回 None."""
        try:
            msg = json.loads(text)
        except ValueError:
            return json.dumps(_error(None, -32700, "JSON 解析失败"), ensure_ascii=False)
        out = self.handle(msg, who)
        return None if out is None else json.dumps(out, ensure_ascii=False)

    def handle(self, msg, who="-"):
        if isinstance(msg, list) and self.profile == READONLY:
            if not self.bucket.take():                  # 被拒的批量也先扣令牌, 审计跟限速合并 (#60 第四轮)
                self._limited(who, None)
            else:
                self._log(who, None, {"batch": len(msg)}, False, error="只读档不接受批量请求")
            return _error(None, -32600, "只读档不接受 JSON-RPC 批量请求, 一次发一条")
        if isinstance(msg, list) and msg:
            outs = [r for r in (self._one(m, who) for m in msg) if r is not None]
            return outs or None
        return self._one(msg, who)

    def _one(self, msg, who):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "不是合法的 JSON-RPC 2.0 请求")
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:                     # 通知: 不回 (initialized / cancelled 都没什么要做的)
            return None
        if not isinstance(params, dict):
            return _error(rid, -32602, "params 要是对象")
        if method == "initialize":
            want = params.get("protocolVersion")
            return _result(rid, {
                "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAMES[self.profile], "version": self.version},
                "instructions": READONLY_INSTRUCTIONS if self.profile == READONLY else INSTRUCTIONS})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [t.spec() for t in self.tools.values()]})
        if method == "tools/call":
            return self._call(rid, params, who)
        return _error(rid, -32601, f"不支持的方法: {method}")

    def _limited(self, who, name):
        """被限速的调用: RO_AUDIT_EVERY_S 内只记一条, 下一条带上中间没逐条记的次数."""
        log, quiet, srcs = self._merge.note("limited", who)
        if not log:
            return
        more = f" (之前另有 {quiet} 次被限速, 来自 {srcs} 个不同来源, 没逐条记)" if quiet else ""
        self._log(who, name, {}, False, error=f"调用太频繁, 已限速{more}")

    def flush_audit(self):
        """把合并后还没落盘的限速计数写出 (进程退出时 atexit 调)."""
        self._merge.flush(lambda key, q, n: self._log(
            "-", None, {}, False, error=f"调用太频繁, 已限速 (另有 {q} 次被限速, 来自 {n} 个不同来源, 没逐条记)"))

    def _log(self, who, tool, args, ok, error=None, ms=None):
        if self.audit is not None:
            self.audit.write(audit_line(who, tool, args, ok, error=error, ms=ms, profile=self.profile))

    def _call(self, rid, params, who):
        name, args = params.get("name"), params.get("arguments", {})
        if self.bucket is not None and not self.bucket.take():     # 先限速再查工具名: 乱填名字也写不满审计
            self._limited(who, name)
            return _result(rid, {"content": [text_content(
                f"调用太频繁 (只读档每秒最多 {RO_RATE:g} 次, 一口气最多 {RO_BURST} 次), 稍后再试")], "isError": True})
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            self._log(who, name, args, False, error="没有这个工具")
            return _error(rid, -32602, f"没有这个工具: {name!r}")
        t0 = self.clock()
        try:
            content, err = tool.fn(check_args(tool.params, args)), None
        except (ArgError, ToolError) as e:
            content, err = None, str(e)
        except Exception as e:          # noqa: BLE001  一个工具炸了不能把服务带走
            content, err = None, f"内部错误 {type(e).__name__}: {e}"
        logged = tool.redact(args) if getattr(tool, "redact", None) else args      # 密码不进审计 (#42)
        self._log(who, name, logged, err is None, error=err, ms=(self.clock() - t0) * 1000)
        if err is not None:
            return _result(rid, {"content": [text_content(err)], "isError": True})
        return _result(rid, {"content": content, "isError": False})


# ------------------------------------------------------------ HTTP: 只绑本机和 tailnet, 要令牌

MIN_TOKEN = 24          # 短于这个的令牌 (样例里的占位、手抖填的) 一律不认


def bind_addrs(ip_json):
    """`ip -j addr` 的输出 -> 要监听的地址: 本机 + tailnet (web_core 认的可信段). 解析不了只听 127.0.0.1."""
    out = ["127.0.0.1"]
    try:
        links = json.loads(ip_json)
    except ValueError:
        return out
    for link in links if isinstance(links, list) else []:
        for a in (link.get("addr_info") or []) if isinstance(link, dict) else []:
            ip = a.get("local") if isinstance(a, dict) else None
            if isinstance(ip, str) and web_core.client_kind(ip) == "trusted" and ip not in out:
                out.append(ip)
    return out


def token_who(header, tokens):
    """Authorization: Bearer <令牌> -> 令牌的名字 (审计里的 '谁'); 对不上返回 None. 逐个定长比较."""
    scheme, _, tok = (header or "").strip().partition(" ")
    tok = tok.strip()
    if scheme.lower() != "bearer" or len(tok) < MIN_TOKEN:
        return None
    hit = None
    for name, want in (tokens or {}).items():
        if isinstance(want, str) and len(want) >= MIN_TOKEN and hmac.compare_digest(tok.encode(), want.encode()):
            hit = name
    return hit


def http_check(ip, headers, tokens, hosts=()):
    """一个 HTTP 请求能不能进 -> (None, 谁) 或 (状态码, 拒绝原因).
    顺序: 来源 (本机/tailnet, 局域网开关在这里不算数) -> Host 白名单 (挡 DNS rebinding) ->
    带了 Origin 就要同源 (挡浏览器跨站) -> 令牌."""
    if web_core.client_kind(ip) != "trusted":
        return 403, "只接受本机和 tailnet 来源"
    why = web_core.request_reason("GET", headers, hosts)
    if why is None and headers.get("Origin"):
        why = web_core.request_reason("POST", headers, hosts)
    if why:
        return 403, why
    who = token_who(headers.get("Authorization"), tokens)
    if who is None:
        return 401, "缺少或不认识的令牌 (Authorization: Bearer ...)"
    return None, f"{who}@{ip}"


# ------------------------------------------------------------ PXE 状态 (只读; 解锁见 #24)

def menu_purpose(text):
    """MAC 菜单内容 -> 解锁用途 (pxe-mode.json 里不记用途, 从车机写的菜单反推); 认不出 None."""
    text = text or ""
    for purpose, title in pxeserver.CLONE_TITLES.items():
        if title in text:
            return purpose
    if pxeserver.WINPE_CHAIN in text:
        return "winpe"
    if "inst.ks=" in text:
        return "rocky"
    return None


def pxe_view(mode, menu_text, guard, now=None):
    """pxeserver.pxe_mode() + 那台 MAC 的菜单 + 守卫状态 -> 给 Agent 看的一份."""
    now = time.time() if now is None else now
    out = {"mode": mode.get("mode", "rescue")}
    for k in ("mac", "macs", "since", "until"):
        if k in mode:
            out[k] = mode[k]
    if "until" in mode:
        out["remaining_s"] = max(0, int(mode["until"] - now))
    out["purpose"] = menu_purpose(menu_text) if menu_text else None
    out["guard"] = {k: guard[k] for k in ("state", "detail", "leases") if k in (guard or {})}
    return out
