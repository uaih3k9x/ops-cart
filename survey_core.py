#!/usr/bin/env python3
"""串口一键只读巡检 (#15): 按厂商发一套只读命令, 存档, 和同设备上一份比对. 纯逻辑, 不碰 Tk.

执行走 conbroker send (等提示符、自动翻页、人最近敲过键时它自己排队), 不直接开串口.
每条命令发出前都过只读白名单 (#3 三层闸门的第 1 层); 清单里有一条不合格, 整次巡检不跑.
界面在 serial_panel.py 的"巡检"按钮; 命令行: python3 survey_core.py <操作头> [--vendor huawei]
"""

import difflib
import json
import os
import re
import subprocess
import sys
import time

import cartlog
from serial_core import _conbroker_cmd, _run, match_service, parse_list, serve_error

SURVEY_DIR = "/var/log/ops-cart/survey"
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


# ------------------------------------------------------------ 提示符 / 文件名

_PROMPTS = [re.compile(r"<([^<>\s]+)>"),                    # 华为用户视图 <SW-A>
            re.compile(r"\[[~*]?([^\[\]\s]+)\]"),           # 华为系统视图 [SW-A] / VRP8 [~SW-A] [*SW-A]
            re.compile(r"([A-Za-z0-9][\w.-]*)(?:\([^()\s]*\))?[#>]")]   # 思科 SW-A# SW-A> SW-A(config)#


def parse_device(text):
    """输出最后一行是提示符就返回设备名, 否则 None (停在 Username:/Password:/半截输出上)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    for p in _PROMPTS:
        m = p.fullmatch(lines[-1])
        if m:
            return m.group(1)
    return None


def safe_name(s):
    """进文件名: 只留字母数字 . -; '_' 是文件名里的字段分隔, 也换掉."""
    return re.sub(r"[^A-Za-z0-9.-]", "-", s)


_ARCHIVE_NAME = r"[^_]+_{dev}_(\d{{8}}-\d{{4}})(?:-(\d+))?\.txt"


def archive_path(survey_dir, head, device, t):
    """<操作头>_<设备名>_<YYYYmmdd-HHMM>.txt; 同一分钟再跑加 -2 -3, 不覆盖."""
    base = os.path.join(survey_dir, f"{safe_name(head)}_{safe_name(device)}_{time.strftime('%Y%m%d-%H%M', t)}")
    path, n = base + ".txt", 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}.txt"
    return path


def previous_archive(survey_dir, device, exclude=None):
    """同设备 (不管哪个操作头) 最新的一份, 不算 exclude."""
    pat = re.compile(_ARCHIVE_NAME.format(dev=re.escape(safe_name(device))))
    found = []
    try:
        names = os.listdir(survey_dir)
    except OSError:
        return None
    for name in names:
        m = pat.fullmatch(name)
        path = os.path.join(survey_dir, name)
        if m and path != exclude:
            found.append(((m.group(1), int(m.group(2) or 1)), path))
    return max(found)[1] if found else None


# ------------------------------------------------------------ 存档格式

_STATUS = {"ok": "", "timeout": "超时, 没跑完", "refused": "conbroker 拒绝发送, 没跑",
           "error": "发送失败, 没跑完", "noprompt": "没回提示符, 没跑完", "changed": "提示符变了, 中止",
           "skipped": "未执行"}
_SECTION = re.compile(r"===== \[(\d+)/(\d+)\] (.+?) =====(?: \((\w+): (.*)\))?")


def _split_output(out, cmd):
    """conbroker send 的输出 -> 正文行: 去掉第一行的回显和最后一行的提示符."""
    lines = (out or "").replace("\r", "").split("\n")
    if lines and lines[0].strip().endswith(cmd):
        lines = lines[1:]
    if lines and parse_device(lines[-1]):
        lines = lines[:-1]
    return lines


def format_archive(meta, sections):
    out = ["# ops-cart 只读巡检 (survey_core.py)",
           f"# 操作头: {meta['head']}  设备: {meta['device'] or '未知'}  厂商: {meta['vendor']}  时间: {meta['time']}",
           f"# 结果: {meta['result']}", ""]
    total = len(sections)
    for i, s in enumerate(sections, 1):
        tag = ""
        if s["status"] != "ok":
            tag = f" ({s['status']}: {_STATUS[s['status']]}{'; ' + s['note'] if s['note'] else ''})"
        out.append(f"===== [{i}/{total}] {s['cmd']} ====={tag}")
        out.extend(s["lines"])
        out.append("")
    return "\n".join(out) + "\n"


def parse_archive(text):
    """存档 -> {命令: (状态, 正文行)}; 认不出的文件给空 dict."""
    secs, cur = {}, None
    for ln in text.splitlines():
        m = _SECTION.fullmatch(ln)
        if m:
            cur = m.group(3)
            secs[cur] = (m.group(4) or "ok", [])
        elif cur is not None:
            secs[cur][1].append(ln)
    for _st, lines in secs.values():
        while lines and not lines[-1].strip():
            lines.pop()
    return secs


# ------------------------------------------------------------ 比对

def _stable(lines, volatile):
    return [ln for ln in lines if not any(re.search(p, ln) for p in volatile)]


def _order(old, new):
    return list(new) + [c for c in old if c not in new]


def diff_sections(old, new, prof):
    """两份存档的 {命令: (状态, 行)} -> [(命令, 增行数, 删行数)], 只列有变化的.

    易变段 (display clock) 不比, 易变行 (uptime / 时间戳) 先去掉; 任一边没跑完的段不比 (见 not_comparable).
    """
    skip = set(prof.get("volatile_commands", []))
    vol = prof.get("volatile", [])
    out = []
    for cmd in _order(old, new):
        o, n = old.get(cmd), new.get(cmd)
        if cmd in skip or (o and o[0] != "ok") or (n and n[0] != "ok"):
            continue
        a, b = _stable(o[1] if o else [], vol), _stable(n[1] if n else [], vol)
        add = rm = 0
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op != "equal":
                add, rm = add + j2 - j1, rm + i2 - i1
        if add or rm:
            out.append((cmd, add, rm))
    return out


def not_comparable(old, new, prof):
    """[(命令, 上次状态, 本次状态)]: 有一边没跑完的段."""
    skip = set(prof.get("volatile_commands", []))

    def st(d, c):
        return d[c][0] if c in d else "无"
    return [(c, st(old, c), st(new, c)) for c in _order(old, new)
            if c not in skip and (st(old, c) not in ("ok", "无") or st(new, c) not in ("ok", "无"))]


# ------------------------------------------------------------ 执行

def _result(**kw):
    r = {"ok": False, "head": None, "device": None, "path": None, "sections": [], "diff": None,
         "previous": None, "refused": [], "error": None, "summary": ""}
    r.update(kw)
    return r


def _one(send, port, vendor, cmd, timeout, device):
    """发一条 -> (状态, 说明, 正文行, 设备名)."""
    if not is_readonly(cmd, vendor):                    # 发前再过一次, 不信预检之后清单没被动过
        return "refused", "白名单不放", [], device
    try:
        out = send(port, cmd, timeout)
    except Exception as e:                              # 真 send: conbroker 退出码非 0 / 子进程超时
        return "error", str(e) or type(e).__name__, [], device
    out = (out or "").replace("\r", "").rstrip("\n")
    if out.startswith("[conbroker: 拒绝发送"):
        return "refused", out.splitlines()[0], [], device
    rest, _, last = out.rpartition("\n")
    if last.startswith("[conbroker:") and "没等到提示符" in last:
        return "timeout", f"{timeout} 秒内没等到提示符 (超时)", _split_output(rest, cmd), device
    name = parse_device(out)
    if not name:
        return "noprompt", f"设备没回提示符, 最后一行: {last.strip()[:60]}", _split_output(out, cmd), device
    if device and name != device:
        return "changed", f"提示符从 {device} 变成了 {name}", _split_output(out, cmd), device
    return "ok", "", _split_output(out, cmd), name


def _compare(res, survey_dir, prof):
    """和同设备上一份比, 结果写进 res; 返回给人看的几行."""
    prev = previous_archive(survey_dir, res["device"], exclude=res["path"])
    res["previous"] = prev
    if not prev:
        return ["首次巡检这台设备, 没有可比的存档"]
    try:
        with open(prev, encoding="utf-8") as f:
            old = parse_archive(f.read())
    except OSError:
        old = {}
    new = {s["cmd"]: (s["status"], s["lines"]) for s in res["sections"]}
    res["diff"] = diff_sections(old, new, prof)
    name = os.path.basename(prev)
    if res["diff"]:
        out = [f"和上次 {name} 比, {len(res['diff'])} 段有变化: "
               + ", ".join(f"{c} (+{a} -{r})" for c, a, r in res["diff"])]
    else:
        out = [f"和上次 {name} 比: 无变化 (已忽略时钟 / uptime / 时间戳)"]
    nc = not_comparable(old, new, prof)
    if nc:
        out.append("无法比对 (有一边没跑完): " + ", ".join(f"{c} (上次 {a}, 本次 {b})" for c, a, b in nc))
    return out


def run_survey(port, vendor, send, now=time.time, head=None, survey_dir=SURVEY_DIR, profiles=None):
    """跑一次巡检: 白名单预检 -> 逐条 send -> 存档 -> 和同设备上一份比. 不抛异常, 结果全在返回的 dict 里.

    send(port, cmd, timeout) -> conbroker send 的输出 (回显 + 正文 + 提示符); 出错可以抛异常.
    任何一条超时 / 没回提示符 / 被拒 / 提示符变了就中止, 后面的不发, 存档里逐段写明.
    """
    head = head or safe_name(os.path.basename(port))
    if vendor not in _RULES:
        return _result(head=head, summary=f"巡检拒绝执行: 厂商 {vendor} 没有内置只读规则")
    prof = (profiles if profiles is not None else load_profiles()).get(vendor) or {}
    cmds = prof.get("commands") or []
    if not cmds:
        return _result(head=head, summary=f"巡检拒绝执行: 厂商 {vendor} 的命令清单是空的")
    bad = check_commands([c["cmd"] for c in cmds], vendor)
    if bad:
        return _result(head=head, refused=bad,
                       summary="巡检拒绝执行, 一条都没发: 清单里有白名单不放的命令: " + "; ".join(map(str, bad)))

    t = time.localtime(now())
    sections, device, stop = [], None, None
    for i, c in enumerate(cmds, 1):
        if stop:
            sections.append({"cmd": c["cmd"], "status": "skipped", "note": "", "lines": []})
            continue
        st, note, lines, device = _one(send, port, vendor, c["cmd"], c["timeout"], device)
        sections.append({"cmd": c["cmd"], "status": st, "note": note, "lines": lines})
        if st != "ok":
            stop = f"中止于 [{i}/{len(cmds)}] {c['cmd']}: {note}"
    done = sum(s["status"] == "ok" for s in sections)
    res = _result(ok=stop is None, head=head, device=device, sections=sections, error=stop)

    try:
        os.makedirs(survey_dir, exist_ok=True)
        path = archive_path(survey_dir, head, device or "unknown", t)
        meta = {"head": head, "device": device, "vendor": vendor, "time": time.strftime("%Y-%m-%d %H:%M", t),
                "result": f"完成 {done}/{len(cmds)}" if res["ok"] else f"中止, 已跑 {done}/{len(cmds)}; {stop}"}
        with open(path, "x", encoding="utf-8") as f:
            f.write(format_archive(meta, sections))
        res["path"] = path
    except OSError as e:
        res["ok"] = False
        res["error"] = (stop + "; " if stop else "") + f"存档写不进去: {e}"

    lines = [f"{device or '设备未知'} 巡检{'完成' if stop is None else '中止'}, 已跑 {done}/{len(cmds)} 条"]
    if res["error"]:
        lines.append(res["error"])
    if res["path"]:
        lines.append(f"存档: {res['path']}")
        if device:
            lines += _compare(res, survey_dir, prof)
    res["summary"] = "\n".join(lines)
    return res


def record(res, event=None):
    """cartlog 记一笔 (events.log); 正文是 key=value, 不写命令输出."""
    secs = res.get("sections") or []
    msg = cartlog.fields(head=res.get("head"), device=res.get("device"), ok=bool(res.get("ok")),
                         done=f"{sum(s['status'] == 'ok' for s in secs)}/{len(secs)}",
                         changed=",".join(c for c, _a, _r in res["diff"]) if res.get("diff") else None,
                         refused=";".join(map(str, res["refused"])) if res.get("refused") else None,
                         path=res.get("path"), error=res.get("error"))
    return (event or cartlog.event)("survey", msg)


# ------------------------------------------------------------ 真 send (conbroker) / 命令行

def conbroker_send(port, cmd, timeout):
    """经 conbroker send 发一条 (等提示符、自动翻页、人在敲时它自己排队). 非只读命令直接拒, 不起子进程."""
    if not any(is_readonly(cmd, v) for v in _RULES):
        raise ValueError(f"白名单不放: {cmd!r}")
    r = subprocess.run(_conbroker_cmd() + ["send", port, cmd, "-t", str(timeout)],
                       capture_output=True, text=True, timeout=timeout + 60)
    if r.returncode != 0:
        raise RuntimeError(serve_error(r.returncode, r.stderr))
    return r.stdout


def _list_services():
    return parse_list(_run(["list"]))[1]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="串口一键只读巡检 (走 conbroker, 只发白名单里的只读命令)")
    ap.add_argument("head", help="操作头, 如 con-1 或 /dev/con-1")
    ap.add_argument("--vendor", default="huawei")
    ap.add_argument("--profiles", default=None, help=f"命令清单 JSON (默认 {PROFILES_PATH}, 没有就用内置)")
    ap.add_argument("--dir", default=SURVEY_DIR, help="存档目录")
    a = ap.parse_args(argv)
    port = a.head if a.head.startswith("/") else "/dev/" + a.head
    name = match_service(port, _list_services())
    if not name:
        print(f"{port} 上没有在跑的 conbroker 服务, 先在串口面板开服务")
        return 2
    r = run_survey(name, a.vendor, send=conbroker_send, head=safe_name(os.path.basename(port)),
                   survey_dir=a.dir, profiles=load_profiles(a.profiles))
    print(r["summary"])
    record(r)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
