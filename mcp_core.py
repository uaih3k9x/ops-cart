"""mcp_core: 推车 MCP 服务 (issue #3 / #19) 的可测核心层. 只读工具, 不往任何设备写.

- 执行绪命名: 从串口输出的提示符认名字 (华为 <名> / [名]), 调用和日志永远带槽位号, 名字只是标签
- 参数校验: 工具参数用一张小表声明, 同一张表导出 inputSchema; 不认识的参数、类型不对一律拒
"""

import re

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
