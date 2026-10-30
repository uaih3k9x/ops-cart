#!/usr/bin/env python3
"""串口一键只读巡检 (#15): 按厂商发一套只读命令, 存档, 和同设备上一份比对. 纯逻辑, 不碰 Tk.

执行走 conbroker send (等提示符、自动翻页、人最近敲过键时它自己排队), 不直接开串口.
每条命令发出前都过只读白名单 (#3 三层闸门的第 1 层); 清单里有一条不合格, 整次巡检不跑.
界面在 serial_panel.py 的"巡检"按钮; 命令行: python3 survey_core.py <操作头> [--vendor huawei]
"""

import json
import os
import re

PROFILES_PATH = "/opt/pxe/survey_profiles.json"    # 可选: 覆盖下面的命令清单
DEFAULT_TIMEOUT = 30                                # 单条命令等提示符的秒数

# ------------------------------------------------------------ 只读规则 (写死在代码里, JSON 改不了)
# verbs: 第一个词; pipes: "|" 后允许的过滤词 (True=要带参数); deny: 动词后第二个词黑名单 (只读命令里会写文件的)
_RULES = {
    "huawei": {"verbs": {"display"},
               "pipes": {"include": True, "exclude": True, "begin": True, "section": True,
                         "count": False, "no-more": False},
               "deny": {"diagnostic-information"}},       # 带文件名时把输出存进 flash
    "cisco": {"verbs": {"show"},
              "pipes": {"include": True, "exclude": True, "begin": True, "section": True, "count": False},
              "deny": set()},
}
_TOKEN = re.compile(r"[\x21-\x7e]+")          # 一个词: 可打印 ASCII, 不含空白
_FORBIDDEN = set(";&<>`$#!")                     # 拼接/重定向/命令替换/注释, 出现就拒
MAX_LEN = 200


def is_readonly(cmd, vendor):
    """这条命令能不能不经人确认直接发 (fail-closed: 拿不准一律 False).

    规则 (只认规范写法, 不替设备猜):
      - 厂商得有内置规则 (huawei / cisco); 第一个词必须原样是 display (华为) / show (思科), 全小写.
      - 整条只许可打印 ASCII, 词之间恰好一个空格, 不许前后空白、制表符、换行、控制字符、全角/零宽字符.
      - 不许 ; & < > ` $ # ! —— 多命令拼接、重定向、命令替换、注释.
      - "|" 必须单独成词, 后面只许接过滤词 include/exclude/begin/section (要带参数)、count/no-more (不带);
        save/append/redirect/tee 这类写文件的管道全拒. 过滤词同样不许缩写、不许大写.
      - display 后面紧跟的子命令不许在 deny 里 (diagnostic-information 能把输出写进 flash).
      - 动词后至少要有一个词.

    缩写 (dis / disp / sh) 不放, 理由:
      VRP / IOS 认的是"唯一前缀", 同一个前缀在不同版本、不同视图下可能落到不同命令 (d 在用户视图里
      就同时是 debugging / delete / dir / display 的前缀). 要放缩写, 白名单就得复刻设备的命令解析器,
      而它判错的方向是放行写操作. 清单是我们自己写的, 写全称没有成本; 所以白名单只比字面, 不做前缀推断.
      大小写、多余空白同理: 设备照认, 但只放一种写法, 检查的字面就是发出去的字面.
    """
    rules = _RULES.get(vendor) if isinstance(vendor, str) else None
    if not rules or not isinstance(cmd, str) or not cmd or len(cmd) > MAX_LEN:
        return False
    if any(ch in _FORBIDDEN for ch in cmd):
        return False
    words = cmd.split(" ")
    if not all(_TOKEN.fullmatch(w) for w in words):       # 空词 = 前后空白或连续空格
        return False
    if words[0] not in rules["verbs"] or len(words) < 2 or words[1] in rules["deny"]:
        return False
    segs, cur = [], []
    for w in words[1:]:
        if w == "|":
            segs.append(cur)
            cur = []
        elif "|" in w:
            return False                               # |save 这种粘在一起的
        else:
            cur.append(w)
    segs.append(cur)
    if not segs[0]:
        return False
    for seg in segs[1:]:
        if not seg or seg[0] not in rules["pipes"]:
            return False
        if rules["pipes"][seg[0]] != (len(seg) > 1):
            return False
    return True


def check_commands(cmds, vendor):
    """不合格的命令 (原样), 空列表 = 全部放行."""
    return [c for c in cmds if not is_readonly(c, vendor)]


# ------------------------------------------------------------ 命令清单 (数据, 可经 JSON 覆盖)

_DEFAULT_PROFILES = {
    "huawei": {
        "commands": ["display version", "display clock", "display ip interface brief",
                     "display interface brief", "display lldp neighbor brief", "display mac-address",
                     "display arp", {"cmd": "display current-configuration", "timeout": 120}],
        "volatile_commands": ["display clock"],
        "volatile": [r"uptime is", r"\bup ?time\b", r"\d{4}[-/]\d\d[-/]\d\d[ T]\d\d:\d\d:\d\d",
                     r"^\s*!?\s*Last configuration was", r"^\s*!Time:"],
    },
    "cisco": {
        "commands": ["show version", "show clock", "show ip interface brief", "show interfaces status",
                     "show cdp neighbors", "show mac address-table", "show arp",
                     {"cmd": "show running-config", "timeout": 120}],
        "volatile_commands": ["show clock"],
        "volatile": [r"uptime is", r"\bup ?time\b", r"\d{4}[-/]\d\d[-/]\d\d[ T]\d\d:\d\d:\d\d",
                     r"^\s*! Last configuration change", r"^\s*! NVRAM config last updated",
                     r"^\s*Current configuration :", r"^\s*Building configuration"],
    },
}
_OVERRIDABLE = ("commands", "volatile_commands", "volatile")


def _norm_cmd(c):
    if isinstance(c, dict):
        return {"cmd": c.get("cmd"), "timeout": c.get("timeout", DEFAULT_TIMEOUT)}
    return {"cmd": c, "timeout": DEFAULT_TIMEOUT}


def load_profiles(path=None):
    """{厂商: {commands: [{cmd, timeout}], volatile_commands, volatile}}.

    JSON 只能覆盖 commands / volatile_commands / volatile; 只读规则在 _RULES 里, 文件改不动.
    JSON 新加的厂商没有内置规则, is_readonly 一律 False, 巡检会被拒. 文件坏了/不存在就用内置.
    """
    prof = {v: dict(p) for v, p in _DEFAULT_PROFILES.items()}
    try:
        with open(path or PROFILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    for vendor, over in data.items():
        if isinstance(over, dict):
            base = prof.setdefault(vendor, {"commands": [], "volatile_commands": [], "volatile": []})
            base.update({k: over[k] for k in _OVERRIDABLE if isinstance(over.get(k), list)})
    for p in prof.values():
        p["commands"] = [_norm_cmd(c) for c in p["commands"]]
    return prof
