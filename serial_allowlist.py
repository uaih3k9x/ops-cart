"""串口写闸门第 1 层: 按厂商的命令前缀白名单 (#75). 纯逻辑, 纯数据, 不 import 设备相关的东西.

现在是影子期: 写闸门和巡检预检同时算这份白名单, 结论写进 gate.shadow / cartlog, **放行结果仍按
survey_core.is_readonly**. 切换条件见 docs/DESIGN_layer1_allowlist.md (覆盖纳管一轮后再切).

条目 = 固定词 + 有类型的参数槽 (设计稿 2.2 / 2.3):
- 条目文本按一个空格切词; 字面量逐字相等 (全称, 不收缩写, 大小写敏感), {类型} 槽 fullmatch 封闭类型.
- 词数正好相等; 管道最多一段, 只许条目声明过的过滤词; 过滤参数只许一个安全词 ({filter}).
- session=True (关分页) 只在 exec 提示符下命中; big=True (大输出) 只给巡检, 不进写闸门第 1 层.
- 规则文件只能按条目 id 禁用, 不能新增; 新增要改这里, 带测试走 PR.
- src: "survey" = 巡检默认清单里、现场跑过的; "待核" = 照手册列的, 留到纳管现场随巡检核实 (设计稿 3.6).

拿不准一律不命中 (交给第 2 / 3 层). 只认 Agent 给的命令字面, 从不读设备回显来扩充清单.
"""

import re
from collections import namedtuple

MAX_LEN = 200
FORBIDDEN = frozenset(";&<>`$#!?[]{}")
FILE_WORDS = frozenset({"to-file", "file", "filename", "tftp", "ftp", "sftp", "scp", "http", "https",
                        "clear", "reset"})
TYPES = ("ifname", "vlan", "ipv4", "mac", "filter")

Entry = namedtuple("Entry", "text src session big pipes")


def E(text, src="待核", session=False, big=False, pipes=None):
    return Entry(text, src, session, big, pipes)


# 管道: {过滤词: 要不要带一个 {filter}}
PIPES_HUAWEI = {"include": True, "exclude": True, "begin": True, "section": True, "count": False, "no-more": False}
PIPES_SHOW = {"include": True, "exclude": True, "begin": True, "section": True, "count": False}      # 思科
PIPES_MIN = {"include": True, "exclude": True, "begin": True}                                          # H3C / 锐捷

ALLOW = {
    "huawei": [                     # 写闸门里 H3C 也走这张 (提示符同形, 登记不了 h3c)
        E("screen-length 0 temporary", "survey", session=True),
        E("display version", "survey"),
        E("display clock", "survey"),
        E("display ip interface brief", "survey"),
        E("display interface brief", "survey"),
        E("display lldp neighbor brief", "survey"),
        E("display mac-address", "survey", big=True),
        E("display arp", "survey", big=True),
        E("display current-configuration", "survey", big=True, pipes=PIPES_HUAWEI),
        E("display interface {ifname}"),
        E("display vlan"),
        E("display vlan {vlan}"),
        E("display stp brief"),
        E("display eth-trunk"),
        E("display device"),
        E("display elabel"),
        E("display cpu-usage"),
        E("display memory-usage"),
        E("display alarm active"),
        E("display transceiver interface {ifname} verbose"),
        E("display mac-address {mac}"),
        E("display ip routing-table {ipv4}"),
    ],
    "h3c": [                        # 只对巡检生效 (--vendor h3c)
        E("screen-length disable", "survey", session=True),
        E("display version", "survey"),
        E("display clock", "survey"),
        E("display ip interface brief", "survey"),
        E("display interface brief", "survey"),
        E("display lldp neighbor-information list", "survey"),
        E("display mac-address", "survey", big=True),
        E("display arp", "survey", big=True),
        E("display current-configuration", "survey", big=True, pipes=PIPES_MIN),
        E("display interface {ifname}"),
        E("display vlan {vlan}"),
        E("display stp brief"),
        E("display link-aggregation summary"),
        E("display device"),
        E("display cpu-usage"),
        E("display transceiver interface {ifname}"),
    ],
    "cisco": [
        E("terminal length 0", "survey", session=True),
        E("show version", "survey"),
        E("show clock", "survey"),
        E("show ip interface brief", "survey"),
        E("show interfaces status", "survey"),
        E("show cdp neighbors", "survey"),
        E("show lldp neighbors", "survey"),
        E("show mac address-table", "survey", big=True),
        E("show arp", "survey", big=True),
        E("show running-config", "survey", big=True, pipes=PIPES_SHOW),
        E("show interfaces {ifname}"),
        E("show vlan brief"),
        E("show spanning-tree summary"),
        E("show etherchannel summary"),
        E("show inventory"),
        E("show cdp neighbors detail"),
        E("show lldp neighbors detail"),
        E("show ip route {ipv4}"),
        E("show interfaces {ifname} transceiver"),
    ],
    "ruijie": [
        E("terminal length 0", "survey", session=True),
        E("show version", "survey"),
        E("show clock", "survey"),
        E("show ip interface brief", "survey"),
        E("show interfaces status", "survey"),
        E("show lldp neighbors", "survey"),
        E("show mac-address-table", "survey", big=True),
        E("show arp", "survey", big=True),
        E("show running-config", "survey", big=True, pipes=PIPES_MIN),
        E("show interfaces {ifname}"),
        E("show vlan"),
        E("show aggregatePort summary"),
    ],
}
# 字面量照手册原样、不是全小写的条目 (待核; 设备照认小写与否不猜, 只放这一种写法)
CASE_EXCEPTIONS = {("ruijie", "show aggregatePort summary")}
# 白名单命中但 is_readonly 不过、并且是有意的 (设计稿 3.7). 关分页要等 #57 给 is_readonly 加上 session 规则.
KNOWN_DIFF = frozenset({"huawei:screen-length 0 temporary", "cisco:terminal length 0"})

# ------------------------------------------------------------ 参数类型 (封闭集合)

_NUM = r"\d{1,3}(?:/\d{1,3}){0,3}(?:\.\d{1,4})?"
_IF_H3C = ["GigabitEthernet", "Ten-GigabitEthernet", "Twenty-FiveGigE", "FortyGigE", "HundredGigE",
           "Bridge-Aggregation", "Vlan-interface", "LoopBack", "M-GigabitEthernet"]
_IF = {
    "huawei": ["GigabitEthernet", "XGigabitEthernet", "25GE", "40GE", "100GE", "Eth-Trunk", "Vlanif", "LoopBack",
               "MEth"] + _IF_H3C,
    "h3c": _IF_H3C,
    "cisco": ["GigabitEthernet", "TenGigabitEthernet", "TwentyFiveGigE", "FortyGigabitEthernet", "HundredGigE",
              "FastEthernet", "Ethernet", "Port-channel", "Vlan", "Loopback"],
    "ruijie": ["GigabitEthernet", "TenGigabitEthernet", "AggregatePort", "VLAN", "Loopback"],
}
_IF_RE = {v: re.compile("(?:" + "|".join(map(re.escape, sorted(set(p), key=len, reverse=True))) + ")" + _NUM)
          for v, p in _IF.items()}
_MAC_RE = {"huawei": re.compile(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"),
           "cisco": re.compile(r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}")}
_MAC_RE["h3c"], _MAC_RE["ruijie"] = _MAC_RE["huawei"], _MAC_RE["cisco"]
_VLAN = re.compile(r"[1-9]\d{0,3}")
_OCTET = re.compile(r"0|[1-9]\d{0,2}")
_FILTER = re.compile(r"[A-Za-z0-9._/-]{1,64}")
_TOKEN = re.compile(r"[\x21-\x7e]+")


def _ipv4(w):
    parts = w.split(".")
    return len(parts) == 4 and all(_OCTET.fullmatch(p) and int(p) <= 255 for p in parts)


def type_ok(typ, w, vendor):
    if typ == "ifname":
        return bool(_IF_RE[vendor].fullmatch(w))
    if typ == "vlan":
        return bool(_VLAN.fullmatch(w)) and int(w) <= 4094
    if typ == "ipv4":
        return _ipv4(w)
    if typ == "mac":
        return bool(_MAC_RE[vendor].fullmatch(w))
    if typ == "filter":
        return bool(_FILTER.fullmatch(w)) and w.lower() not in FILE_WORDS
    return False


def type_help(vendor):
    """给 Agent 看的参数类型说明 (gate_allowlist)."""
    return {
        "ifname": "接口全称 + 编号, 一个词, 不收缩写; 前缀: " + " ".join(_IF.get(vendor, ())),
        "vlan": "1-4094, 不许前导 0",
        "ipv4": "点分四段字面量, 每段 0-255 不许前导 0; 不收主机名, 不收 IPv6",
        "mac": "小写 " + ("xxxx-xxxx-xxxx" if vendor in ("huawei", "h3c") else "xxxx.xxxx.xxxx"),
        "filter": "管道过滤参数, 只许一个安全词 [A-Za-z0-9._/-]{1,64}; 不许空格 / 引号 / 冒号 / 正则量词, "
                  "不许 " + " ".join(sorted(FILE_WORDS)),
    }


# ------------------------------------------------------------ 提示符 mode

_EXEC = {"huawei": re.compile(r"<[^\s<>]{1,64}>"),
         "cisco": re.compile(r"[A-Za-z0-9][\w.-]{0,63}[#>]")}
_EXEC["h3c"], _EXEC["ruijie"] = _EXEC["huawei"], _EXEC["cisco"]


def prompt_mode(vendor, line):
    """最后一行 -> "exec" (华为 <名>, 思科 / 锐捷 不带括号的 名# 名>) 或 "config". 判断不了按 config (更严)."""
    rx = _EXEC.get(vendor) if isinstance(vendor, str) else None
    return "exec" if rx and isinstance(line, str) and rx.fullmatch(line.strip()) else "config"


# ------------------------------------------------------------ 匹配

def entry_id(vendor, e):
    return f"{vendor}:{e.text}"


def all_ids():
    return {entry_id(v, e) for v, es in ALLOW.items() for e in es}


def pending_review():
    """src=待核 的条目 id (纳管现场随巡检核实)."""
    return sorted(entry_id(v, e) for v, es in ALLOW.items() for e in es if e.src == "待核")


def _main_ok(e, words, vendor):
    lits = e.text.split(" ")
    if len(lits) != len(words):
        return False
    for lit, w in zip(lits, words):
        if lit.startswith("{"):
            if not type_ok(lit[1:-1], w, vendor):
                return False
        elif lit != w:
            return False
    return True


def hits(main, vendor):
    """主段 (不含管道) 命中的全部条目, 不管禁用 / mode / 范围. 用于判重叠."""
    words = main.split(" ")
    return [e for e in ALLOW.get(vendor, ()) if _main_ok(e, words, vendor)]


def _miss(why):
    return {"entry": None, "src": None, "why": why}


def match(cmd, vendor, mode="config", scope="gate", disabled=()):
    """-> {entry: 条目 id 或 None, src, why}. scope: "gate" (写闸门, 不含大输出) / "survey" (巡检).
    mode 不是 "exec" 一律按 config. 恰好命中一条才算; 0 条或多条都不命中."""
    if not isinstance(vendor, str) or vendor not in ALLOW:
        return _miss(f"厂商 {vendor} 没有第 1 层白名单" + (" (linux 槽位不开第 1 层)" if vendor == "linux" else ""))
    if not isinstance(cmd, str) or not cmd or len(cmd) > MAX_LEN:
        return _miss("命令为空或太长")
    if set(cmd) & FORBIDDEN:
        return _miss("带禁用字符")
    words = cmd.split(" ")
    if not all(_TOKEN.fullmatch(w) for w in words):
        return _miss("只许可打印 ASCII, 词之间恰好一个空格")
    segs, cur = [], []
    for w in words:
        if w == "|":
            segs.append(cur)
            cur = []
        elif "|" in w:
            return _miss("| 要单独成词")
        else:
            cur.append(w)
    segs.append(cur)
    if len(segs) > 2:
        return _miss("管道最多一段")
    hs = hits(" ".join(segs[0]), vendor)
    if len(hs) != 1:
        return _miss("不在白名单里" if not hs else "命中多条条目 (白名单重叠, 是 bug)")
    e = hs[0]
    eid = entry_id(vendor, e)
    if eid in set(disabled or ()):
        return _miss(f"条目 {eid} 被规则文件禁用")
    if e.big and scope != "survey":
        return _miss(f"条目 {eid} 是大输出, 只给巡检, 不进写闸门第 1 层")
    if e.session and mode != "exec":
        return _miss(f"条目 {eid} 是会话命令, 只在 exec 提示符下")
    if len(segs) == 2:
        pipe = segs[1]
        if not pipe or not e.pipes or pipe[0] not in e.pipes:
            return _miss("管道过滤词不在条目声明里")
        want = 2 if e.pipes[pipe[0]] else 1
        if len(pipe) != want or (want == 2 and not type_ok("filter", pipe[1], vendor)):
            return _miss("管道参数要正好一个安全词" if want == 2 else f"管道 {pipe[0]} 不带参数")
    return {"entry": eid, "src": e.src, "why": ""}


def shadow(cmd, vendor, mode, readonly, disabled=(), scope="gate"):
    """影子结论 (gate.shadow): allowlist = 命中的条目 id 或 None; pass = 白名单命中 且 is_readonly 也过
    (长期口径, 拍板 6). 影子期只记录, 不改放行结果."""
    m = match(cmd, vendor, mode=mode, scope=scope, disabled=disabled)
    out = {"allowlist": m["entry"], "src": m["src"], "readonly": bool(readonly),
           "pass": bool(m["entry"]) and bool(readonly)}
    if not m["entry"]:
        out["why"] = m["why"]
        del out["src"]
    return out


def parse_disable(value):
    """规则文件 allowlist.disable -> (认识的 id 排好序去重, 不认识的原样转成字符串). 不是列表 = 全不认."""
    known = all_ids()
    if not isinstance(value, list):
        return [], ([] if value is None else [str(value)])
    ids, unknown = set(), []
    for v in value:
        if isinstance(v, str) and v in known:
            ids.add(v)
        elif str(v) not in unknown:
            unknown.append(str(v))
    return sorted(ids), unknown


def catalog(vendor, mode, disabled=(), scope="gate"):
    """该厂商在这个 mode 下可用的条目 (gate_allowlist): [{id, cmd, src, session, pipes}]."""
    dis = set(disabled or ())
    out = []
    for e in ALLOW.get(vendor, ()) if isinstance(vendor, str) else ():
        eid = entry_id(vendor, e)
        if eid in dis or (e.big and scope != "survey") or (e.session and mode != "exec"):
            continue
        out.append({"id": eid, "cmd": e.text, "src": e.src, "session": e.session,
                    "pipes": dict(e.pipes) if e.pipes else {}})
    return out
