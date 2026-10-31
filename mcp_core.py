"""mcp_core: 推车 MCP 服务 (issue #3 / #19) 的可测核心层. 只读工具, 不往任何设备写.

- 执行绪命名: 从串口输出的提示符认名字 (华为 <名> / [名]), 调用和日志永远带槽位号, 名字只是标签
- 参数校验: 工具参数用一张小表声明, 同一张表导出 inputSchema; 不认识的参数、类型不对一律拒
- 协议: JSON-RPC 2.0 (initialize / ping / tools/list / tools/call), 不依赖 mcp SDK; 传输在 cart_mcp
- 审计: 每次 tools/call 一行 JSON (谁、哪个工具、参数、成没成、耗时), 落 /var/log/ops-cart/
"""

import base64
import hmac
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


def audit_line(who, tool, args, ok, error=None, ms=None, now=None):
    """'谁、哪个工具、参数、成没成' 一行 JSON (json.dumps 把换行转义掉, 不会断行)."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))
    d = {"ts": ts, "who": _clip(who), "tool": _clip(tool), "args": _clip(args), "ok": bool(ok)}
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
INSTRUCTIONS = ("运维推车 (ops-cart) 的只读工具: 看串口操作头 (槽位 1-4) 的输出、截被测机器画面、"
                "看推车状态和 PXE 装机模式. 这一版不能往任何设备写. 说到执行绪时永远带上槽位号, 名字只是标签.")


class ToolError(Exception):
    """工具跑不下去 (线没插、服务没开…): 以 isError 结果回给 Agent, 不算协议错误."""


class Tool:
    def __init__(self, name, description, params, fn):
        self.name, self.description, self.params, self.fn = name, description, params, fn

    def spec(self):
        return {"name": self.name, "description": self.description, "inputSchema": input_schema(self.params),
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}


def _error(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _result(rid, res):
    return {"jsonrpc": "2.0", "id": rid, "result": res}


class Server:
    """消息进, 回包出; 不管传输 (stdio / HTTP 在 cart_mcp). 多线程 HTTP 共用一个实例, 本身无状态."""

    def __init__(self, tools, audit=None, version="", clock=time.monotonic):
        self.tools = {t.name: t for t in tools}
        self.audit, self.version, self.clock = audit, version, clock

    def handle_text(self, text, who="-"):
        """一段 JSON 文本 -> 回包文本 (单行); 全是通知时返回 None."""
        try:
            msg = json.loads(text)
        except ValueError:
            return json.dumps(_error(None, -32700, "JSON 解析失败"), ensure_ascii=False)
        out = self.handle(msg, who)
        return None if out is None else json.dumps(out, ensure_ascii=False)

    def handle(self, msg, who="-"):
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
                "serverInfo": {"name": "ops-cart", "version": self.version},
                "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [t.spec() for t in self.tools.values()]})
        if method == "tools/call":
            return self._call(rid, params, who)
        return _error(rid, -32601, f"不支持的方法: {method}")

    def _log(self, who, tool, args, ok, error=None, ms=None):
        if self.audit is not None:
            self.audit.write(audit_line(who, tool, args, ok, error=error, ms=ms))

    def _call(self, rid, params, who):
        name, args = params.get("name"), params.get("arguments", {})
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
        self._log(who, name, args, err is None, error=err, ms=(self.clock() - t0) * 1000)
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
