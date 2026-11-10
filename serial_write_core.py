"""串口写路径的纯逻辑 (#21): 行级命令校验、闸门第 1 层、写租约、挂起等人确认、急停、执行绪日志行. 不碰串口和文件.

谁用: mcp_serial (MCP 工具 + 车机命令行) 把状态存在 /run/ops-cart/serial-write.json, 每次操作
"加锁 -> 读 -> Book 里改 -> 存 -> 写日志"; 这里只管规则, 时间全由调用方传进来 (now = time.time()).

规矩 (fail-closed: 拿不准一律挂起等人确认, 或直接拒):
- 只收行级命令: 一行可打印 ASCII, 不许控制字符 / 换行 / 制表符 / 全角 / 零宽; 原始字节能拆碎、退格拼出 save, 不给.
  特殊键只给三个固定的: 回车、翻页空格 (直接放行), Ctrl+C (打断键, 要人确认).
- 闸门第 1 层: 认得出厂商、设备停在提示符上、命令过只读白名单 (survey_core.is_readonly, 规则文件只能加动词)
  才直接发. #75 影子期: 同时算按厂商的命令前缀白名单 (serial_allowlist), 结论写进 gate.shadow, 不改放行结果; 带高危词 (save / reboot / reset / format / delete ...) 的永远要人确认, 不送模型.
- 闸门第 2 层 (#22): 认得出厂商、停在提示符上、不在白名单里、不带高危词的, 交调用方注入的 judge (决策模型,
  serial_gate_model) 判; 判放行才直发 (layer=2), 判拦 / 出错 / 超时 / 结果不对 / 没开一律挂起等人确认.
- 挂起期间 (#65): 槽位有挂起项时, 这个槽位的任何直发 (第 1/2 层放行的命令和键) 都拒 (pending), 和键鼠一致 ——
  否则人批准的"槽位 N · shutdown"会作用在 Agent 之后切过去的另一个接口上. 纵深: 挂起项记下提交时槽位的发送序号,
  确认时序号变了 (提交后这个槽位又发过别的, 例如人先确认了同槽位另一条) 就作废 (stale), 让 Agent 重新提交.
  在途时不收挂起也不给序号; 序号在读设备输出之前取 (send_mark), 挂起 / 直发时已变就不挂不发. 挂起时读不到
  设备输出就不挂. 确认前重读同样行数的输出和挂起时整体比 (不打码的原文指纹), 有任何新输出就作废 —— 人用通道
  不计序号、迟到的字节、思科这类提示符不带对象名, 都靠这一步兜住; 读不到就先不发. 审批界面另给"可信记录"
  (推车自己记的本槽位最近实际发出的命令), 和不可信的设备输出分开.
  挂起项带提交时设备最近几行输出 (来自设备、不可信, 限长, 口令类字段打码), 只在状态文件和审批界面, 不进执行绪日志.
- 写租约: 每个槽位同一时间一个写入者, 认令牌不认名字; 有期限 (默认 60 秒), 要续期, 过期自动释放,
  它挂着的命令一起作废. 一条在途: 上一条没回来不发下一条.
- 人优先: 人在车机 / 网页上敲键不经租约; 人 3 秒内敲过键 Agent 的输入由 conbroker 排队; 人随时急停、确认、拒绝.
- 急停: 单条 (一个槽位) 或全部 (= 对每个槽位单条急停). 撤销租约 + 丢弃挂起的命令; 只停发送, 不往设备插任何字节.
  恢复只能由人解除 (mcp_serial 的命令行, 只认推车本机). 已经交给 conbroker 的那条在途命令收不回来, 照实记日志.
- 状态文件读不懂 (坏了 / 版本不对) = 全部急停.
"""

import collections
import functools
import hashlib
import json
import math
import re
import secrets
import time
import unicodedata

import serial_allowlist
import serial_core
import survey_core

SLOTS = (1, 2, 3, 4)
LINE_MAX = 256
LEASE_TTL_S = 60            # 默认租约期限; Agent 每半个期限续一次
LEASE_TTL_MIN_S = 10
LEASE_TTL_MAX_S = 300
PENDING_MAX = 5             # 一个槽位最多挂几条等人确认
PENDING_MAX_S = 900         # 挂起超过 15 分钟没人理就作废 (现场早变了)
INFLIGHT_GRACE_S = 90       # 在途标记超过 发送超时 + 这么久还没回来 = 发送进程死了
RECENT_MAX = 5              # 审批界面"可信记录": 最后一条改视图命令之后的只读命令 / 按键显示几条
HISTORY_MAX = 30            # 状态文件里留最近多少条结果给 pending_list 看 (完整的在执行绪日志里)
ECHO_MAX = 1000             # 日志里设备回显摘要的长度
HASH_LINES = 8              # 指纹取最后几行有效行 (调用方 tail 多读, 给标记行和空行留余量)
TRUST_LOG_MAX = 20          # 每槽位单独留的"实际发出的命令"记录条数 (可信记录; 不和全局历史共用)
CHANGE_KEEP = 10            # 其中"可能改视图"的最多留几条; 裁剪先丢只读的, 丢过改视图的就记 log_lost
CONTEXT_LINES = 8           # 挂起项带提交时设备最近几行输出 (#65), 给审批的人参考
CONTEXT_LINE_MAX = 160
CONTEXT_MAX = 1000
STATE_VERSION = 1


class Refused(Exception):
    """不让做. code: bad (命令不合格) / estop / lease / busy / inflight / nope (没有这条或已经不在挂起) /
    pending (槽位有挂起项, 不许直发) / stale (挂起后槽位又发过别的或设备视图变了, 这条作废) /
    prompt (确认时读不到设备输出, 先不发) / context (挂起时读不到设备输出, 不挂)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------ 行级命令

_LINE_OK = re.compile(r"[\x20-\x7e]+")


def check_line(cmd):
    """Agent 给的一行命令 -> 原样返回; 不合格抛 Refused('bad'). 不替它改 (发出去的字面 = 检查的字面)."""
    if not isinstance(cmd, str):
        raise Refused("bad", "命令要是字符串")
    if not cmd.strip():
        raise Refused("bad", "空命令; 只按回车用 send_key enter")
    if len(cmd) > LINE_MAX:
        raise Refused("bad", f"一行最多 {LINE_MAX} 个字符")
    if not _LINE_OK.fullmatch(cmd):
        raise Refused("bad", "一行里只许可打印 ASCII: 不许换行、回车、制表符、退格、Ctrl/Esc 序列、全角或零宽字符")
    if cmd.startswith("-"):
        raise Refused("bad", "命令不能以 - 开头 (会被 conbroker 命令行当成选项)")
    return cmd


# ------------------------------------------------------------ 高危词

# 只增不减: 规则文件能往里加, 删不掉. 认的是整词, 以及整词的缩写 (VRP / IOS 认唯一前缀: sav = save, wr = write).
DANGER = ("save", "reboot", "reload", "reset", "format", "delete", "erase", "rmdir", "rm", "write", "copy",
          "startup", "shutdown", "poweroff", "halt", "init", "mkfs", "fdisk", "wipefs", "dd", "upgrade")
_SPLIT = re.compile(r"[\s|;&<>()`$\[\]{}]+")     # Linux shell 的拼接 / 重定向 / 命令替换、Tcl 的 [ ] { } 也当分隔
_TOK = re.compile(r"[\s|;&<>()`$\[\]{}]+|[^\s|;&<>()`$\[\]{}]+")   # 词和分隔串交替
_QUOTE = re.compile(r"['\"\\]")                  # r'm' "reboot" \rm: 去掉引号反斜杠再切词


def danger_words(cmd, extra=()):
    """命令里出现的高危词 (规范写法, 按出现顺序, 不重复).
    处在命令开头的词 (整行第一个, 以及 [ { ( ` ; 后面第一个: Tcl / shell 里这里又是一条命令) 2 个字符起、
    其余的词 3 个字符起, 是高危词的前缀也算 (缩写); 往"多拦"的方向错."""
    words = tuple(DANGER) + tuple(w for w in extra if w not in DANGER)
    out, first = [], True
    for tok in _TOK.findall(_QUOTE.sub("", (cmd or "").lower())):
        if _SPLIT.fullmatch(tok):
            first = first or any(c in tok for c in "[{(`;")
            continue
        for w in words:
            if tok == w or (len(tok) >= (2 if first else 3) and w.startswith(tok)):
                if w not in out:
                    out.append(w)
        first = False
    return out


# ------------------------------------------------------------ 规则文件

RULES_PATH = "/etc/ops-cart/serial-gate.json"
_VERB = re.compile(r"[a-z][a-z0-9-]{1,31}")
# 规则文件永远加不进来的只读动词: 改配置 / 进交互 / 连别处 / 清东西的; 写成它们的前缀 (缩写) 也不行
_VERB_DENY = ("terminal", "debugging", "undebug", "undo", "no", "clear", "test", "telnet", "ssh", "stelnet",
              "system-view", "configure", "reload", "reboot", "copy", "write", "save", "delete", "format", "reset",
              "erase", "shutdown", "startup", "boot", "install", "upgrade", "license", "patch", "rename", "move",
              "mkdir", "rmdir", "tclsh", "enable", "disable", "request", "run", "bash", "start", "connect",
              "ftp", "tftp", "sftp", "scp", "archive", "crypto", "set",
              "screen-length", "super", "ssh2", "user-interface", "line", "local-user", "guestshell", "python",
              "event", "free", "kill", "quit", "logout", "lock", "send", "monitor", "mount", "umount", "tar",
              "gunzip", "squeeze", "fixdisk", "undelete")
# 禁用表是尽力而为: 各厂商命令太多, 列不全; 规则文件里加动词前自己确认它只读、不进交互、参数有上限
# 第 2 层决策模型 (#22). 默认不开: 没配就和第 1 层之外全人确认一样
MODEL_DEFAULT = {"enabled": False, "timeout_s": 30, "budget_usd": "0.2", "context_chars": 3000, "model": None}
MODEL_TIMEOUT_MIN_S = 5
MODEL_TIMEOUT_MAX_S = 120
MODEL_BUDGET_MAX = 1.0      # 美元, 单次调用
MODEL_CONTEXT_MAX = 8000    # 送给模型的设备最近输出, 字符
NOTES_MAX = 500             # 每个厂商给模型的补充说明
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}")      # 不许 - 开头: 会被当成 claude 的选项


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _model_conf(doc):
    """规则文件的 model 段 -> 规范化; 哪项不对哪项按默认, 数字夹到范围里."""
    out = dict(MODEL_DEFAULT)
    if not isinstance(doc, dict):
        return out
    if isinstance(doc.get("enabled"), bool):
        out["enabled"] = doc["enabled"]
    if _num(doc.get("timeout_s")):
        out["timeout_s"] = int(max(MODEL_TIMEOUT_MIN_S, min(MODEL_TIMEOUT_MAX_S, doc["timeout_s"])))
    if _num(doc.get("context_chars")):
        out["context_chars"] = int(max(0, min(MODEL_CONTEXT_MAX, doc["context_chars"])))
    b = doc.get("budget_usd")
    try:
        b = float(b) if (_num(b) or isinstance(b, str)) else None
    except ValueError:
        b = None
    if b is not None and 0 < b <= MODEL_BUDGET_MAX:
        out["budget_usd"] = str(b)
    if isinstance(doc.get("model"), str) and _MODEL_NAME.fullmatch(doc["model"]):
        out["model"] = doc["model"]
    return out


def load_rules(path=RULES_PATH):
    """规则文件 -> {slots: {槽位: 厂商}, auto_huawei, vendors: {厂商: {extra_verbs, model, notes}},
    danger: [...], model: {enabled, timeout_s, budget_usd, context_chars, model},
    allowlist_disable: [条目 id], allowlist_unknown: [不认识的 id]}.

    allowlist.disable (#75): 只能按条目 id 禁用 serial_allowlist 的条目, 不能新增 (写别的键一律无效);
    不认识的 id 忽略, 放进 allowlist_unknown (gate_allowlist 里看得到).

    只能收紧或加只读动词: 只读动词只能加给内置有只读规则的厂商 (survey_core: huawei / h3c / cisco / ruijie);
    写闸门里 H3C 按华为处理 (槽位登记、vendors 配置都写进 huawei; 写 h3c 会被忽略);
    加的动词过一张写死的禁用表 (_VERB_DENY, 尽力而为), 且只在用户视图生效 (classify 的 user_view);
    Linux 没有第 1 层白名单, 登记了槽位只走第 2 层或人确认; 高危词只加不减. vendors.<厂商>.model=false 关掉该厂商的
    第 2 层, notes 是给模型的补充说明. 文件不在 / 坏了 = 内置默认 (没有额外动词, 槽位不登记, 华为提示符自动认,
    模型不开)."""
    vendors = sorted(_PROMPT)
    out = {"slots": {}, "auto_huawei": True,
           "vendors": {v: {"extra_verbs": [], "model": True, "notes": ""} for v in vendors},
           "danger": list(DANGER), "model": dict(MODEL_DEFAULT), "allowlist_disable": [], "allowlist_unknown": []}
    doc = {}
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    for k, v in (doc.get("slots") or {}).items() if isinstance(doc.get("slots"), dict) else ():
        if str(k).isdigit() and int(k) in SLOTS and v in vendors:
            out["slots"][int(k)] = v
    if isinstance(doc.get("auto_huawei"), bool):
        out["auto_huawei"] = doc["auto_huawei"]
    extra_danger = [w for w in doc.get("danger") or () if isinstance(w, str) and _VERB.fullmatch(w)]
    for v, spec in (doc.get("vendors") or {}).items() if isinstance(doc.get("vendors"), dict) else ():
        if v not in vendors or not isinstance(spec, dict):
            continue
        verbs = [w for w in spec.get("extra_verbs") or () if isinstance(w, str) and _VERB.fullmatch(w)
                 and not any(d == w or d.startswith(w) for d in _VERB_DENY)]
        if v in survey_core._RULES:
            out["vendors"][v]["extra_verbs"] = sorted(set(verbs))
        if isinstance(spec.get("model"), bool):
            out["vendors"][v]["model"] = spec["model"]
        if isinstance(spec.get("notes"), str):
            out["vendors"][v]["notes"] = spec["notes"][:NOTES_MAX]
        extra_danger += [w for w in spec.get("danger") or () if isinstance(w, str) and _VERB.fullmatch(w)]
    out["danger"] += sorted(set(extra_danger) - set(DANGER))
    out["model"] = _model_conf(doc.get("model"))
    al = doc.get("allowlist")
    if isinstance(al, dict) and "disable" in al:
        out["allowlist_disable"], out["allowlist_unknown"] = serial_allowlist.parse_disable(al["disable"])
    return out


# ------------------------------------------------------------ 认厂商 / 设备停没停在提示符上

_PROMPT = {
    "huawei": re.compile(r"<[^\s<>]{1,64}>|\[[~*]?[^\s\[\]~*][^\s\[\]]{0,127}\]"),     # <名> [名] [~名] [名-子视图]
    "cisco": re.compile(r"[A-Za-z0-9][\w.-]{0,63}(?:\([^()\s]{0,32}\))?[#>]"),          # 名# 名> 名(config)#
    "ruijie": re.compile(r"[A-Za-z0-9][\w.-]{0,63}(?:\([^()]{0,48}\))?[#>]"),            # 同思科, 子视图带空格
    # 用户@主机:路径$  [用户@主机 路径]#  bash-4.4#  光秃秃的 $ / #
    "linux": re.compile(r"(?:[\w.-]{1,32}@[\w.-]{1,64}(?::\S{0,128})?|\[[\w.-]{1,32}@[\w.-]{1,64}(?: \S{1,128})?\]"
                        r"|[a-z]{0,4}sh-\d+(?:\.\d+)*)?[#$]"),
}
_MARK = re.compile(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [^\n]*\]")                       # conbroker 自己写进日志的标记行
_ESC = re.compile(r"\x1b\][^\x07\x1b\n]{0,512}(?:\x07|\x1b\\)"                  # OSC (改窗口标题等) 整段剥掉, 不跨行
                  "|" + serial_core._CSI.pattern.decode("ascii") + r"|\x1b.?")    # ANSI 转义 (同 serial_core), 再兜底孤立 ESC
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")                                    # 其余控制字符 (\t \n 之外)
_PAGER = re.compile(r"-{2,}\s*More\s*-{2,}(?:\s*\(\d{1,3}%\))?", re.I)               # --More-- / ---- More ----


def clean(text):
    """剥 ANSI 转义和控制字符 (#74 审查: \x1b[0m、\x07、\x00 不能把确认提示挤出"最后一行")."""
    return _CTRL.sub("", _ESC.sub("", (text or "").replace("\r", "\n").replace("\t", " ")))


def last_line(text):
    """串口输出最后一行有内容的 (先剥转义 / 控制字符; 跳过只剩空白的行和 conbroker 的 [时间 Claude 断开] 标记)."""
    for ln in reversed(clean(text).split("\n")):
        ln = ln.strip()
        if ln and not _MARK.fullmatch(ln):
            return ln
    return ""


# 提示符里的"名字"是这些的不算提示符 (#74 第二轮): 回答词 ([Y] [yes] [continue] y# Continue> OK>) 和
# 引导程序 (U-Boot> boot> grub>): 回车在这些上面等于回答问题或往引导程序里敲, 不是在设备命令行上.
_ANSWER_NAMES = {"y", "n", "yes", "no", "ok", "continue", "confirm", "proceed", "cancel", "abort", "retry", "skip",
                 "delete", "all", "none", "default", "quit", "exit"}
_BOOT_NAMES = {"u-boot", "uboot", "boot", "grub", "loader", "bootloader", "bootrom", "rommon", "cfe", "pmon",
               "redboot", "ipxe", "efi", "shell"}
_NAME = re.compile(r"[<\[][~*]?(.*?)[>\]]|(.*?)(?:\(([^()]*)\))?[#>$]")


def _prompt_names(line):
    """提示符的名字和括号里的部分 (Reboot(y)> -> {"reboot", "y"}), 都小写."""
    m = _NAME.fullmatch(line)
    return {(g or "").strip().lower() for g in m.groups() if g is not None} if m else {""}


def at_any_prompt(line, vendor=None):
    """这一行是不是命令提示符或翻页提示 (回车 / 空格的放行白名单). vendor = 槽位登记的厂商: 只认它的提示符,
    没登记才认所有厂商的合集. 确认类提示、名字是回答词或引导程序的先排除."""
    if not line or is_confirm_prompt(line):
        return False
    if _PAGER.fullmatch(line):
        return True
    pats = [_PROMPT[vendor]] if vendor in _PROMPT else list(_PROMPT.values())
    if not any(p.fullmatch(line) for p in pats):
        return False
    return not (_prompt_names(line) & (_ANSWER_NAMES | _BOOT_NAMES))


def slot_vendor(slot, rules, text):
    """-> (厂商 或 None, 是否停在该厂商的提示符上).
    规则文件给槽位登记了厂商就用它; 没登记且开着自动认, 最后一行是华为 <名>/[名] 才算华为 (思科式 名# 和
    Linux 的 bash-4.4# 分不开, 只能登记). 停在密码 / [Y/N] / [startup-config] / 翻页 / 半截输出上、思科 tclsh
    (名(tcl)# / 名-tcl#) 里一律不算在提示符上. H3C 不单独认: <名> / [名] 按华为处理 (display 系规则相同).
    登记成 linux 的槽位, shell 提示符照常算 (只排除没登记 / 登记成网络设备时的 shell)."""
    ln = last_line(text)
    v = rules.get("slots", {}).get(slot)
    not_prompt = survey_core._BOOT_PROMPT if v == "linux" else survey_core._NOT_PROMPT
    if is_confirm_prompt(ln) or survey_core._CONFIRM.fullmatch(ln) or not_prompt.fullmatch(ln):
        return v or None, False             # [Y/N] [confirm] [startup-config] 确认 / 补齐 (#74, #57), 思科 tclsh, Linux shell
    if v:
        return v, bool(_PROMPT[v].fullmatch(ln))
    if rules.get("auto_huawei") and _PROMPT["huawei"].fullmatch(ln):
        return "huawei", True
    return None, False


# ------------------------------------------------------------ 闸门

def _gate(verdict, reason, danger=(), vendor=None, layer=None, model=None, line=None, line_vendor=None):
    g = {"verdict": verdict, "layer": layer or (1 if verdict == "pass" else 3), "reason": reason,
         "danger": list(danger), "vendor": vendor}
    if line is not None:
        g.update(line_fields(line, line_vendor))     # 发键时设备停在哪一行: 人确认时看 (设备输出, 只当数据显示)
    if model is not None:
        g["model"] = model          # 第 2 层的原始判断 / 出错说明, 随闸门结果整个进执行绪日志
    return g


_TCL = re.compile(r"[\[\]{}]")
_SHELL_META = re.compile(r"[;&<>`\\!]|\$\(")   # Linux: 拼接 / 后台 / 重定向 / 命令替换 / 续行 / 历史展开


_USER_VIEW = {"huawei": re.compile(r"<[^\s<>]{1,64}>"),
              "cisco": re.compile(r"[A-Za-z0-9][\w.-]{0,63}[#>]"), "ruijie": re.compile(r"[A-Za-z0-9][\w.-]{0,63}[#>]")}


def user_view(vendor, text):
    """设备停在用户 (exec) 视图上: 华为 / H3C 的 <名>, 思科 / 锐捷不带括号的 名# / 名>. 规则文件加的动词只在这里放."""
    pat = _USER_VIEW.get(vendor)
    ln = last_line(text)
    return bool(pat and pat.fullmatch(ln) and not survey_core._NOT_PROMPT.fullmatch(ln)
                and not survey_core._CONFIRM.fullmatch(ln))


def classify(cmd, vendor, at_prompt, rules, judge=None, user_view=False, mode=None):
    """一行命令 -> 闸门结果 {verdict: pass|confirm, layer, reason, danger, vendor[, model][, shadow]}. 调用方先 check_line.

    judge(cmd, vendor) -> {allow: bool, reason: 非空字符串, model: {...}}: 第 2 层决策模型 (serial_gate_model);
    None = 没开. 只有认得出厂商、停在提示符上、不在只读白名单里、不带高危词的才问它.
    mode: 提示符 "exec" / "config" (serial_allowlist.prompt_mode), 不给按 config. 只用于影子 (#75):
    轮到第 1 层判的命令, gate.shadow = {allowlist, src, readonly, pass[, why]}, 放行结果仍按 is_readonly."""
    d = danger_words(cmd, rules.get("danger", ()))
    if d:
        return _gate("confirm", f"带高危词 {', '.join(d)}, 永远要人确认", d, vendor)
    if vendor is None:
        return _gate("confirm", "认不出设备厂商 (规则文件没登记这个槽位, 最后一行也不是华为 <名>/[名] 提示符), 要人确认")
    if not at_prompt:
        return _gate("confirm", "设备不在提示符上 (可能停在密码、[Y/N]、翻页或半截输出上), 要人确认", vendor=vendor)
    if _TCL.search(cmd):
        return _gate("confirm", "带 [ ] { } (思科 tclsh 等环境里是 Tcl 命令替换), 不送模型, 要人确认", vendor=vendor)
    # 规则文件加的动词 (ping 之类) 只在用户视图放: 配置视图里同名的词可能是配置命令
    extra = rules.get("vendors", {}).get(vendor, {}).get("extra_verbs", ()) if user_view else ()
    ro = survey_core.is_readonly(cmd, vendor, extra_verbs=extra)
    g = _gate("pass", f"{vendor} 只读命令 (第 1 层放行)", vendor=vendor) if ro else _layer2(cmd, vendor, rules, judge)
    try:                                # 影子只记录: 它出错也不能让闸门出错或改结论
        g["shadow"] = serial_allowlist.shadow(cmd, vendor, mode, ro, rules.get("allowlist_disable", ()))
    except Exception as e:              # noqa: BLE001
        g["shadow"] = {"error": repr(e)[:200]}
    return g


def _layer2(cmd, vendor, rules, judge):
    """第 2 层: 模型判放行才 pass; 其余 (判拦 / 抛异常 / 结果不对 / 没开) 一律 confirm. fail-closed."""
    pre = "不在只读白名单里"
    if vendor == "linux" and _SHELL_META.search(cmd):
        return _gate("confirm", f"{pre}, 带 shell 拼接 / 重定向 / 命令替换 / 反斜杠 / 历史展开, 不送模型, 要人确认",
                     vendor=vendor)
    if judge is None:
        return _gate("confirm", f"{pre}, 第 2 层决策模型没开, 要人确认", vendor=vendor)
    if not rules.get("vendors", {}).get(vendor, {}).get("model", True):
        return _gate("confirm", f"{pre}, 规则文件关了 {vendor} 的第 2 层决策模型, 要人确认", vendor=vendor)
    try:
        r = judge(cmd, vendor)
    except Exception as e:              # noqa: BLE001  judge 本该自己兜住; 漏出来也只能是人确认
        why = f"{type(e).__name__}: {e}"[:300]
        return _gate("confirm", f"{pre}, 第 2 层决策模型出错 ({why}), 要人确认", vendor=vendor, model={"error": why})
    model = r.get("model") if isinstance(r, dict) and isinstance(r.get("model"), dict) else None
    if not (isinstance(r, dict) and isinstance(r.get("allow"), bool)
            and isinstance(r.get("reason"), str) and r["reason"].strip()):
        return _gate("confirm", f"{pre}, 第 2 层决策模型给的结果不对, 要人确认", vendor=vendor, model=model)
    if r["allow"]:
        return _gate("pass", f"模型放行 (第 2 层): {r['reason']}", vendor=vendor, layer=2, model=model)
    return _gate("confirm", f"{pre}, 要人确认: {r['reason']}", vendor=vendor, model=model)


# 固定特殊键: conbroker send 的参数 (text, raw). raw 时 conbroker 会做 unicode_escape, 所以 Ctrl+C 写成 \\x03.
KEYS = {
    "enter": {"label": "回车", "text": "", "raw": False, "confirm": False},
    "space": {"label": "翻页空格", "text": " ", "raw": True, "confirm": False},
    "ctrl_c": {"label": "Ctrl+C", "text": "\\x03", "raw": True, "confirm": True},
}


# 确认类提示 (#73): 设备停在这些上面时, 回车 / 空格就是替人回答. 不分大小写.
# 回车 / 空格放行走白名单 (at_any_prompt), 这张表只用来: 理由写清楚、slot_vendor 不把 [Y/N] 认成华为 [名].
# 注: survey_core 也有一份确认提示识别 (PR #57 在改), 这里先独立实现, 合并后再统一成一份.
_YN = r"\[?(?:y(?:es)?|n(?:o)?)\]?"
_CONFIRM_PROMPT = re.compile(
    r"\[confirm\]"                                     # 思科 Proceed with reload? [confirm]
    rf"|[\[(（<]\s*{_YN}\s*[/|,]\s*{_YN}\s*(?:[/|,][^\])）>]*)?[\])）>]"   # [Y/N] [N/y] (y/n) (yes|no) (y,N) ([y]/n) (yes/no/[fingerprint])
    r"|\[y\]es\s*/\s*\[n\]o"                           # [Y]es/[N]o
    r"|\by\s+or\s+n\b|\byes\s+or\s+no\b|是\s*[/|]\s*否"   # y or n / Save(yes or no)> / 是/否
    r"|\bare\s+you\s+sure\b|\btype\s+['\"]?yes\b"       # Are you sure / Type 'yes' to continue
    r"|\[[^\[\]]{1,64}\]\s*[:：]\s*$",                  # 带默认值: ...[vrpcfg.zip]: / [no]:
    re.I)
CONFIRM_SCAN = 512          # 只在最后这么多字里找确认提示: 长行上的 \s*$ 会退化成平方
LINE_SHOWN = 200
KEY_ANSWERS = ("enter", "space")    # 可能替人回答确认提示的键
HUMAN_QUIET_S = 5           # 设备日志这么多秒内动过 = 可能有人在敲 (conbroker 人类优先窗口 3 秒, 取长)


def is_confirm_prompt(line):
    """这一行是不是在等人回答 (确认 / 是否 / 带默认值的问句). --More-- 翻页不算.
    问号收尾 (Destination filename [x]? / 继续吗？：) 用 rstrip + endswith 判, 不用正则."""
    tail = (line or "")[-CONFIRM_SCAN:]
    t = tail.rstrip()
    if t[-1:] in (":", "："):
        t = t[:-1].rstrip()
    return t[-1:] in ("?", "？") or bool(_CONFIRM_PROMPT.search(tail))


def classify_key(key, text=None, quiet_s=None, vendor=None):
    """回车 / 空格的闸门 (#73, #74 审查). text = 设备最近的输出 (tail); quiet_s = 设备日志多少秒没动 (None = 不知道).
    白名单: 最后一个有效行是某厂商的命令提示符或翻页提示, 且 HUMAN_QUIET_S 秒内没动静, 才放行;
    其余 (确认提示、确认提示后又刷了一行日志、半截输出、看不到、可能有人在敲) 一律要人确认."""
    k = KEYS.get(key)
    if k is None:
        raise Refused("bad", f"只给这几个键: {', '.join(KEYS)}")
    if k["confirm"]:
        return _gate("confirm", f"{k['label']} 是打断键, 要人确认")
    ln = last_line(text)
    if not ln:
        return _gate("confirm", f"看不到设备最后一行 (读不到输出或日志是空的), 不知道 {k['label']} 会回答什么, 要人确认")
    _g = functools.partial(_gate, line=ln, line_vendor=vendor)
    if is_confirm_prompt(ln):
        return _g("confirm", f"设备停在确认提示上, {k['label']} 等于替人回答, 要人确认")
    if not at_any_prompt(ln, vendor):           # 理由进执行绪日志, 不引用设备行 (#93); 那一行在 gate.line, 卡片单独一块
        return _g("confirm", f"设备不在提示符或翻页上 (可能是确认提示后又刷了日志、"
                             f"半截输出或开机滚屏), {k['label']} 可能替人回答, 要人确认")
    if key == "space" and _PAGER.fullmatch(ln):        # 翻页空格不会回答确认提示, 不等 5 秒 (#74 第二轮)
        return _g("pass", f"固定特殊键 {k['label']} (第 1 层放行, 翻页)")
    if quiet_s is None or quiet_s < HUMAN_QUIET_S:
        when = "不知道设备最近有没有动静" if quiet_s is None else f"设备日志 {max(0, quiet_s):.0f} 秒前还有动静"
        return _g("confirm", f"{when}, 可能有人正在敲 (conbroker 会让 Agent 的键排队到人停手), "
                             f"{k['label']} 可能替人回答, 要人确认; 没人时等 {HUMAN_QUIET_S} 秒再发")
    return _g("pass", f"固定特殊键 {k['label']} (第 1 层放行)")


# ------------------------------------------------------------ 日志行

NAME_MAX = 32               # 设备自报的名字 (提示符里认出来的) 最多留几个字


def clean_name(name):
    """设备自报的名字 -> 只留字母数字 (含中文) 和 - _ . : @ ~, 删转义, 截到 NAME_MAX; 剩不下返回 None.
    名字来自被测设备 (mcp_core.thread_name 认提示符), 显示前一律当不可信 (#65 第七轮)."""
    if not isinstance(name, str):
        return None
    s = "".join(ch for ch in _ANSI.sub("", name) if ch.isalnum() or ch in "-_.:@~")[:NAME_MAX]
    return s or None


def stamp(wall):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


def echo_summary(text):
    """设备回显 -> 摘要: 太长的掐中间, 头尾都留 (头有回显的命令, 尾有提示符)."""
    s = (text or "").replace("\r", "")
    if len(s) <= ECHO_MAX:
        return s
    half = ECHO_MAX // 2
    return f"{s[:half]}\n…(省略 {len(s) - 2 * half} 字)…\n{s[-half:]}"


# 口令类字段 (只管显示; 比对用没打码的原文). 短词前后都不能是字母数字 (下划线、连字符算断开: pre_shared_key);
# 长词只要后边界, 前面可以接字母 (PGPASSWORD= / htpasswd / GITHUB_TOKEN= / psksecret). 打码从词后面开始.
_SECRET_SHORT = re.compile(r"(?i)(?<![a-z0-9])(?P<k>key|md5|auth|priv|psk|community|cipher|pass)(?![a-z0-9])")
_SECRET_LONG = re.compile(r"(?i)(?P<k>(?:private|pre-?shared|secret|access)[-_]?key|passw(?:or)?d|passphrase|pwd|secret"
                          r"|token|bearer|authorization|credentials?|api[-_]?key"
                          r"|authentication-mode)(?![a-z0-9])")
_SECRET_DASH = re.compile(r"(?<!\S)(?P<k>-p)(?=\S)|(?<!\S)(?P<u>(?:-u|--user)[\s=]+)(?=\S+:\S)")   # mysql -pX / curl -u a:b
_SECRET_URL = re.compile(r"(?P<k>)[\w.%+-]+:[^\s@/]+@")                                        # https://user:pass@
_SECRET_SMB = re.compile(r"(?<!\S)(?P<k>-U\s+[^\s%]+%)(?=\S)")                             # smbclient -U user%pass
_SECRET_SPACED = re.compile(r"(?i)(?<![\w-])(?:sshpass|mysql\w*|mariadb\w*|usermod|useradd|(?:docker|podman)\s+login)\b(?:.*?\s)?(?P<k>-p\s+)(?=\S)"   # sshpass -p x
                            r"|(?P<u>identified\s+by\s+)(?=\S)")                               # IDENTIFIED BY 'x' 
_SECRET_WHOLE = re.compile(r"(?i)chpasswd|^\s*[\w.-]+:\$[0-9a-z]{1,3}\$"
                           r"|\|\s*(?:sudo\s+(?:-\w+\s+)*-\w*S|passwd\b)")          # echo x | passwd --stdin / sudo -S                       # chpasswd / shadow 行
_SECRET_CONT = re.compile(r"\s*(?:%\^%#|%\$%\^|\$[0-9a-z]{1,3}\$)")                             # 折行后的密文续行
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?")
_PEM_BEGIN = re.compile(r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY")
_PEM_END = re.compile(r"(?i)-----END [A-Z0-9 ]*PRIVATE KEY")
_B64 = re.compile(r"\s*[A-Za-z0-9+/=]{32,}\s*")
_IFNAME = re.compile(r"[A-Za-z][A-Za-z-]*\d+(?:[/:.]\d+)+(?:\([\w.]+\))?")          # GigabitEthernet0/0/1(10G)
_HEXSEP = re.compile(r"[0-9A-Fa-f]+(?:[-:.][0-9A-Fa-f]+)+")                          # MAC / IPv6 / 序列号分段
MASK = "******"
_NO_VALUE = re.compile(r"\s*[:?]?\s*")                                              # 口令词后面什么值都没有
_NAME_SEP = re.compile(r"[-_()]")
_NAME_PART = re.compile(r"[A-Za-z]*(?:\d+(?:[/:.]\d+)*)?")                             # 词 / SW1 / GigabitEthernet0/0/1
_ANSWER_WIN = 32            # 回答标记只在行尾这么多字里找: 长行上的 \s*$ 会退化成平方
_ANSWER_MARK = re.compile(                                                            # 内容固定, 藏不下值; 带默认值的 [x]: 不算
    rf"(?:\[confirm\]|[\[(（<]\s*{_YN}\s*[/|,]\s*{_YN}\s*[\])）>]|\[y\]es\s*/\s*\[n\]o)\s*[:：?？]*\s*$", re.I)
_WORD_END = ".,;:!?？："
# 确认提示里跟在口令词后面的普通英语. 只收问句用词; 别加 default / public / admin 这类常被当口令的词.
_PROSE = frozenset("""
    a an and all any are at be been by can current data delete deleted do does existing expire expired expires
    file files for from has have if in is it its may must need needs new no not now of on or pair please required
    should sure that the then this to was were will with would you your
    apply change changed changes clear cleared config configuration confirm continue create created erase erased
    generate generated overwrite overwritten proceed reboot reload remove removed replace replaced reset restart
    save saved update updated want
""".split()) | {""}


def _show(ln):
    """给人看的: 普通空格和制表留成空格, 其余控制 / 格式 / 分隔字符 (Unicode 类别 C*、Z*, 含 U+2028/2029、
    不换行空格、全角空格、零宽、双向覆盖) 换成 ?; 中文照留."""
    return "".join(" " if ch in " \t" else "?" if unicodedata.category(ch)[0] in "CZ" else ch for ch in ln)


def _norm(ln):
    """删 ANSI/CSI/OSC 转义 + NFKC 归一 (全角 -> 半角). 比对和认口令都从这里开始."""
    return unicodedata.normalize("NFKC", _ANSI.sub("", ln))


def _for_match(ln):
    """认口令用: 归一后再删掉控制 / 格式字符 (零宽、软连字符拆不开 password)."""
    return "".join(ch for ch in _norm(ln) if unicodedata.category(ch)[0] != "C")


_BRACKETED = re.compile(r"\([^()]*\)|<[^<>]*>|\[[^\[\]]*\]")


def _cipher_like(m):
    """折行后的密文续行 (#65 第四轮低4): 整行 16 字起、不含空白、至少三类字符 (小写 / 大写 / 数字 / 符号)、
    字符熵接近这个长度的上限 (>= 0.9 * log2(min(长度, 32))). 接口名、MAC / IPv6 这类分段十六进制不算.
    往"别误伤普通输出"偏: 32 字以上的 base64 另有 _B64, 华为 %^%# 另有 _SECRET_CONT."""
    s = m.strip()
    if len(s) < 16 or any(ch.isspace() for ch in s) or _IFNAME.fullmatch(s) or _HEXSEP.fullmatch(s):
        return False
    classes = sum(any(f(ch) for ch in s) for f in (str.islower, str.isupper, str.isdigit,
                                                     lambda ch: not ch.isalnum()))
    if classes < 3:
        return False
    n = len(s)
    ent = -sum(c / n * math.log2(c / n) for c in collections.Counter(s).values())
    return ent >= 0.9 * math.log2(min(n, 32))


def _bare_prompt(m, vendor=None):
    """整行就是一个提示符 —— 这种行不打码 (主机名里带 auth/key/psk 不该把提示符抹平). 槽位登记了厂商就只认那家的
    正则, 没登记才用合集 (#65 第四轮). 口令词后面直接跟 = 或 :, 或者口令词出现在 () <> [] 里 (R1(snmp-server
    community x)#、<secret-x>、[~x-cipher%^%#..]) 一律不豁免."""
    rxs = (_PROMPT[vendor],) if vendor in _PROMPT else tuple(_PROMPT.values())
    if not any(rx.fullmatch(m) for rx in rxs) or len(m) < 2:
        return False
    for rx in (_SECRET_SHORT, _SECRET_LONG):
        for hit in rx.finditer(m):
            if m[hit.end("k"):hit.end("k") + 1] in ("=", ":"):
                return False
        if any(rx.search(span.group()) for span in _BRACKETED.finditer(m)):
            return False
    return True


def _named_prompt(m, vendor=None):
    """原文过得了提示符白名单 ([HUAWEI-rsa-public-key] 这类, 名字里带口令词才走到这) -> 不按"像密文"整行抹成星号,
    后面照常从口令词起打码. 名字拆开后每段都得是普通的词或接口号, 字母数字掺着、带符号的不算: 密文不能借提示符的形状漏出去."""
    return at_any_prompt(m, vendor) and all(_NAME_PART.fullmatch(p) for p in _NAME_SEP.split(m.strip("[]<>~*#$")))


def _masked_question(kept, tail):
    """确认提示里口令词后面的部分 -> 打码后的一行. 只留两样: 行尾固定形状的回答标记, 和紧贴它、全是常见英语词的问句;
    其余都当值. 一个光秃秃的词 (set password change [Y/N]) 也当值. 整段都是问句就原样留."""
    win = tail[-_ANSWER_WIN:]
    hit = _ANSWER_MARK.search(win)
    body, mark = tail, ""
    if hit:
        body, mark = tail[:len(tail) - len(win) + hit.start()], hit.group().strip()
    words = body.split()
    keep = len(words)
    while keep and words[keep - 1].rstrip(_WORD_END).lower() in _PROSE:
        keep -= 1
    if keep == 0 and len(words) == 1 and words[0].isalnum():
        keep = 1
    if keep == 0:
        return _show(kept + tail)
    question = " ".join(words[keep:] + ([mark] if mark else []))
    return _show(kept) + _mask_glue(kept) + MASK + (" " + _show(question) if question else "")


def _mask_glue(kept):
    return "" if kept.endswith((" ", "=", "-p", "/")) or not kept else " "


def _ctx_line(ln, in_key=False, vendor=None):
    """一行设备输出 -> (给人看的一行, 是否在私钥块里)."""
    m = _for_match(ln)
    if _PEM_BEGIN.search(m):
        in_key = True
    elif in_key:
        return MASK, not _PEM_END.search(m)
    if _bare_prompt(m, vendor):
        return _show(m), in_key
    head, rest = "", m
    if _B64.fullmatch(rest) or _SECRET_CONT.match(rest) or _SECRET_WHOLE.search(rest) \
            or (_cipher_like(rest) and not _named_prompt(rest, vendor)):
        return _show(head) + MASK, in_key
    cut = None
    for rx in (_SECRET_SHORT, _SECRET_LONG, _SECRET_DASH, _SECRET_URL, _SECRET_SPACED, _SECRET_SMB):
        for hit in rx.finditer(rest):
            grp = "k" if hit.group("k") is not None else "u"
            keep = hit.end(grp)
            if cut is None or hit.start() < cut[0]:
                cut = (hit.start(), keep)
            break
    if cut:
        kept, after = rest[:cut[1]], rest[cut[1]:]
        if _NO_VALUE.fullmatch(after):              # Enter Password: 后面没东西, 加星号反而像已经输了口令
            return _show(rest), in_key
        if is_confirm_prompt(rest):                 # 问句要留给确认的人看 (#107 审查); 普通行照旧从口令词起整段打码
            return _masked_question(kept, after), in_key
        return _show(head + kept) + _mask_glue(kept) + MASK, in_key
    return _show(_ANSI.sub("", ln)), in_key


def shown_line(ln, vendor=None):
    """设备的一行 -> 打码后的一行, 给理由说明 / 确认框 / pending_list 用 (#93). 先打码后截短, 否则口令词可能被截掉.
    压空白在打码前: 换行拆开的 password\\nxxx 也认得出."""
    return _ctx_line(" ".join(str(ln or "").split()), False, vendor)[0]


def line_fields(line, vendor=None):
    """挂起时记下的设备最后一行: line 是原文 (确认时比对), line_shown 先打码再截短 (显示用, 口令词可能在截掉的前段)."""
    return {"line": line[-LINE_SHOWN:], "line_shown": shown_line(line, vendor)[-LINE_SHOWN:]}


def gate_shown(gate, vendor=None):
    """闸门记的设备最后一行 -> 显示用的打码文字. 升级前的挂起项没有 line_shown, 回退到对截短后的原文打码."""
    g = gate or {}
    s = g.get("line_shown")
    return s if isinstance(s, str) else shown_line(g.get("line"), vendor)


def masked_view(view, vendors=None):
    """给 Agent 的视图: 挂起项和历史的 gate.line 打码 (#93), 返回副本. Book.view 本身不动: 确认时比对要原文.
    vendors = {槽位: 登记的厂商}."""
    def mask(it):
        g = it.get("gate")
        if not (isinstance(g, dict) and g.get("line")):
            return it
        shown = dict(g, line=gate_shown(g, (vendors or {}).get(it.get("slot"))))
        shown.pop("line_shown", None)
        return dict(it, gate=shown)
    return dict(view, pending=[mask(it) for it in view["pending"]], history=[mask(it) for it in view["history"]])


def _tail_hash(text):
    """提交 / 确认时 tail 的整体指纹: 删转义 + NFKC 后的原文 (不打码), 去掉 \\r、行尾空白; 先滤掉空行和 conbroker
    自己的标记行 ([时间 人 接入] 这类, 人开关控制台看一眼就会有), 再取最后 HASH_LINES 行有效行. 调用方要多读几行."""
    lines = [_norm(ln).rstrip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln.strip() and not _MARK.fullmatch(ln.strip())][-HASH_LINES:]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _log_gate(gate):
    """进执行绪日志的闸门结果: 去掉 #74 的 line (设备最后一行, 设备输出按 #65 不进日志), 只记长度和哈希.
    挂起项 / 审批界面里的 gate 不动 (确认时比对、卡片显示要用)."""
    g = dict(gate)
    line = str(g.pop("line") or "")
    g.pop("line_shown", None)
    g["line_len"] = len(line)
    g["line_sha"] = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
    return g


def device_lines(text):
    """设备输出 (回显等) -> 给终端 / 界面看的行: 每行删 ANSI/OSC、控制字符换成 ?, 并照常打码 (私钥块跨行).
    命令行打印设备回显前必须过这里 (#65 第八轮: 否则 ESC[2A ESC[2K 能擦掉推车自己的结果行)."""
    out, in_key = [], False
    for ln in (text or "").replace("\r", "").split("\n"):
        ln, in_key = _ctx_line(ln, in_key)
        out.append(ln)
    return out


def device_context(text, vendor=None):
    """挂起时设备最近几行输出 -> {src, untrusted, lines}; 没有 -> None. 来自被测设备, 只给人看, 不当指令:
    整段先过一遍 (私钥块跨行), 再掐尾 CONTEXT_LINES 行、每行 CONTEXT_LINE_MAX 字、总共 CONTEXT_MAX 字.
    vendor = 槽位在规则文件里登记的厂商 (只认那家的提示符); 没登记给 None."""
    shown, in_key = [], False
    for ln in (text or "").replace("\r", "").split("\n"):
        ln, in_key = _ctx_line(ln, in_key, vendor)
        shown.append(ln[:CONTEXT_LINE_MAX])
    while shown and not shown[-1].strip():
        shown.pop()
    out, total = [], 0
    for ln in reversed(shown[-CONTEXT_LINES:]):
        if total + len(ln) > CONTEXT_MAX:
            break
        out.insert(0, ln)
        total += len(ln)
    return {"src": "device", "untrusted": True, "lines": out} if out else None


def log_line(rec):
    """一条记录一行 JSON (换行在 JSON 里转义, 不会断行)."""
    return json.dumps(rec, ensure_ascii=False, default=str)


# ------------------------------------------------------------ 状态: 租约 / 挂起 / 在途 / 急停

_PUBLIC = ("id", "slot", "kind", "cmd", "who", "t", "timeout", "gate", "reason", "danger", "status",
           "by", "why", "echo", "name", "context")


def _public(item):
    return {k: item[k] for k in _PUBLIC if k in item}


def _counter_ok(slot_doc):
    """发送序号: 没有 (升级前的状态文件) 或非负整数; 别的按读不懂算."""
    v = slot_doc.get("sent", 0)
    if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
        return False
    log = slot_doc.get("log", [])                   # 可信记录: 没有 (旧文件) 可以, 有就得是这个形状
    if not isinstance(slot_doc.get("log_lost", False), bool):
        return False
    return isinstance(log, list) and all(
        isinstance(e, dict) and isinstance(e.get("id"), int) and isinstance(e.get("cmd"), str)
        and isinstance(e.get("kind"), str) and isinstance(e.get("ro"), bool) and isinstance(e.get("status"), str)
        for e in log)


def _trim_log(slot_doc, log):
    """可信记录裁剪 (#65 第四轮): 改视图的最多 CHANGE_KEEP 条 (丢了就记 log_lost, 界面明说"更早的被挤掉"),
    总数超 TRUST_LOG_MAX 先丢最早的只读条目. 只读的回车 / 空格再多也挤不掉 interface 这类命令."""
    changes = [i for i, e in enumerate(log) if not e["ro"]]
    drop = set(changes[:-CHANGE_KEEP]) if len(changes) > CHANGE_KEEP else set()
    if drop:
        slot_doc["log_lost"] = True
    ro = [i for i, e in enumerate(log) if e["ro"]]
    over = len(log) - len(drop) - TRUST_LOG_MAX
    if over > 0:
        drop |= set(ro[:over])
    log[:] = [e for i, e in enumerate(log) if i not in drop]


def _slot_doc():
    return {"lease": None, "estop": None, "inflight": None, "sent": 0}


class Book:
    """一份可序列化的状态 + 上面的规则. 每个公开方法先 tick(now) 把过期的清掉; 产生的日志记录攒在 self.events,
    由调用方存盘后写进执行绪日志. 调用方保证同一时间只有一个 Book 在改 (文件锁)."""

    def __init__(self, doc):
        self.doc = doc
        self.events = []

    # ---- 读写

    @classmethod
    def from_text(cls, text, now):
        """状态文件内容 -> Book. 没有文件 (None / 空) = 全新; 读不懂 = 全部急停 (fail-closed)."""
        fresh = {"v": STATE_VERSION, "seq": 0, "slots": {str(n): _slot_doc() for n in SLOTS}, "items": []}
        if not text:
            return cls(fresh)
        try:
            doc = json.loads(text)
            ok = (isinstance(doc, dict) and doc.get("v") == STATE_VERSION and isinstance(doc.get("seq"), int)
                  and isinstance(doc.get("slots"), dict) and isinstance(doc.get("items"), list)
                  and all(isinstance(doc["slots"].get(str(n)), dict) for n in SLOTS)
                  and all(_counter_ok(doc["slots"][str(n)]) for n in SLOTS))
        except ValueError:
            ok = False
        if ok:
            b = cls(doc)
            try:
                b.view(now)                 # 条目 / 租约缺字段: 走一遍就炸, 按读不懂算
                return b
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
        b = cls(fresh)
        b.estop(None, "state-corrupt", "system", now)
        return b

    def dumps(self):
        return json.dumps(self.doc, ensure_ascii=False, indent=1)

    # ---- 小工具

    def _ev(self, ev, slot, now, src="agent", **kw):
        rec = {"t": stamp(now), "slot": slot, "ev": ev, "src": src}
        rec.update({k: v for k, v in kw.items() if v is not None})
        if isinstance(rec.get("gate"), dict) and "line" in rec["gate"]:
            rec["gate"] = _log_gate(rec["gate"])
        self.events.append(rec)
        return rec

    def _s(self, slot):
        if slot not in SLOTS:
            raise Refused("bad", f"槽位只有 {SLOTS[0]}-{SLOTS[-1]}")
        return self.doc["slots"][str(slot)]

    def _live_lease(self, slot, now):
        lz = self._s(slot)["lease"]
        return lz if lz and lz["expires"] > now else None

    def _item(self, iid):
        return next((it for it in self.doc["items"] if it["id"] == iid), None)

    def _pending(self, slot=None):
        return [it for it in self.doc["items"] if it["status"] == "pending" and (slot is None or it["slot"] == slot)]

    def _close(self, item, status, now, why=None, by=None, ev="drop", src="system", who=None):
        """挂起 / 在途 -> 结束 (进历史), 记一条日志."""
        item["status"] = status
        item.pop("context", None)                       # 设备上下文只给审批时看, 不进历史
        if why:
            item["why"] = why
        if by:
            item["by"] = by
        self._ev(ev, item["slot"], now, src=src, who=who or by, id=item["id"], kind=item["kind"], cmd=item["cmd"],
                 agent=item["who"], why=why, name=item.get("name"))
        self._trim()

    def _trim(self):
        done = [it for it in self.doc["items"] if it["status"] not in ("pending", "sending")]
        for it in done[:max(0, len(done) - HISTORY_MAX)]:
            self.doc["items"].remove(it)

    def _drop_lease_items(self, slot, token, now, why):
        out = []
        for it in self._pending(slot):
            if it.get("lease") == token:
                self._close(it, "dropped", now, why=why)
                out.append(it)
        return out

    # ---- 过期

    def tick(self, now):
        for n in SLOTS:
            s = self._s(n)
            lz = s["lease"]
            if lz and lz["expires"] <= now:
                s["lease"] = None
                self._ev("lease_expired", n, now, src="system", who=lz["who"])
                self._drop_lease_items(n, lz["token"], now, "租约过期 (Agent 没续期)")
            inf = s["inflight"]
            if inf and inf["deadline"] <= now:
                s["inflight"] = None
                it = self._item(inf["id"])
                if it and it["status"] == "sending":
                    it["status"] = "unknown"
                    self._log_status(n, it["id"], "unknown")
                    self._ev("send", n, now, who=it["who"], id=it["id"], kind=it["kind"], cmd=it["cmd"],
                             gate=it.get("gate"), status="unknown", why="发送进程没回来, 不知道发没发出去",
                             by=it.get("by"), name=it.get("name"))
                    self._trim()
        for it in self._pending():
            if now - it["ts"] > PENDING_MAX_S:
                self._close(it, "dropped", now, why=f"挂起超过 {PENDING_MAX_S // 60} 分钟没人确认")

    # ---- 租约

    def stopped(self, slot):
        return self._s(slot)["estop"] is not None

    def holder(self, slot, now):
        self.tick(now)
        lz = self._live_lease(slot, now)
        return lz["who"] if lz else None

    def lease_left(self, slot, token, now):
        lz = self._live_lease(slot, now)
        return int(round(lz["expires"] - now)) if lz and lz["token"] == token else 0

    def acquire(self, slot, who, now, ttl=LEASE_TTL_S, token=None):
        self.tick(now)
        s = self._s(slot)
        if s["estop"]:
            raise Refused("estop", f"槽位 {slot} 急停中, 要人在推车上解除")
        lz = self._live_lease(slot, now)
        if lz:
            raise Refused("busy", f"槽位 {slot} 的写租约在 {lz['who']} 手里, 还有 {int(lz['expires'] - now)} 秒; "
                                  "一条执行绪同时只有一个写入者")
        ttl = int(max(LEASE_TTL_MIN_S, min(LEASE_TTL_MAX_S, ttl)))
        token = token or secrets.token_urlsafe(16)
        s["lease"] = {"who": who, "token": token, "ttl": ttl, "since": now, "expires": now + ttl}
        self._ev("lease_acquire", slot, now, who=who, ttl_s=ttl)
        return {"slot": slot, "lease": token, "ttl_s": ttl, "expires_in": ttl}

    def writer(self, slot, token, now):
        """token 现在能不能往 slot 写; 不能抛 Refused. 返回租约."""
        self.tick(now)
        s = self._s(slot)
        if s["estop"]:
            raise Refused("estop", f"槽位 {slot} 急停中, 要人在推车上解除")
        lz = self._live_lease(slot, now)
        if not lz or not isinstance(token, str) or lz["token"] != token:
            raise Refused("lease", f"没有槽位 {slot} 的有效写租约 (过期了或不是你的), 先 lease_acquire")
        return lz

    def renew(self, slot, token, now):
        lz = self.writer(slot, token, now)
        lz["expires"] = now + lz["ttl"]
        return {"slot": slot, "ttl_s": lz["ttl"], "expires_in": lz["ttl"]}

    def release(self, slot, token, now):
        lz = self.writer(slot, token, now)
        self._s(slot)["lease"] = None
        self._ev("lease_release", slot, now, who=lz["who"])
        self._drop_lease_items(slot, token, now, "租约已释放")

    # ---- 发送: 直发 (第 1 层放行) / 挂起 -> 人确认 -> 发

    def _new_item(self, slot, token, who, kind, cmd, gate, now, timeout, name, status):
        self.doc["seq"] += 1
        it = {"id": self.doc["seq"], "slot": slot, "kind": kind, "cmd": cmd, "who": who, "lease": token,
              "t": stamp(now), "ts": now, "timeout": timeout, "gate": gate, "reason": gate.get("reason"),
              "danger": list(gate.get("danger") or ()), "status": status}
        name = clean_name(name)
        if name:
            it["name"] = name
        self.doc["items"].append(it)
        return it

    def _sent(self, slot):
        return self._s(slot).get("sent", 0)

    def _log(self, it):
        """这条交给 conbroker 了: 记进槽位自己的可信记录 (不被 _trim 清). 连续同一个只读键合并计数."""
        log = self._s(it["slot"]).setdefault("log", [])
        gate = it.get("gate") or {}
        if it["kind"] == "key":                         # 回车 / 翻页空格不改视图; Ctrl+C (要人确认的) 算改
            ro = it["cmd"] in KEYS and not KEYS[it["cmd"]]["confirm"]
        else:                                           # 只有第 1 层只读白名单直发的算只读; 模型放行 / 人确认的都算改
            ro = gate.get("verdict") == "pass" and gate.get("layer") == 1 and not it.get("by")
        last = log[-1] if log else None
        if last and ro and it["kind"] == "key" and last["kind"] == "key" and last["cmd"] == it["cmd"] and last["ro"]:
            last.update(id=it["id"], count=last.get("count", 1) + 1, status="sending", t=it["t"])
            return
        e = {"id": it["id"], "kind": it["kind"], "cmd": it["cmd"], "who": it["who"], "t": it["t"], "ro": ro,
             "status": "sending"}
        if it.get("by"):
            e["by"] = it["by"]
        log.append(e)
        _trim_log(self._s(it["slot"]), log)

    def _log_status(self, slot, iid, status):
        for e in self._s(slot).get("log", ()):
            if e["id"] == iid:
                e["status"] = status

    def _start(self, it, now):
        it["status"] = "sending"
        it.pop("context", None)
        self._log(it)
        s = self._s(it["slot"])
        if it.get("by"):            # 人看过"更早的记录被挤掉"的提示仍确认发送: 视为人接管了现场, 清掉 (#65 第七轮)
            s["log_lost"] = False
        s["sent"] = s.get("sent", 0) + 1                # 发送序号: 挂起项靠它知道"提交后有没有发过别的"
        s["inflight"] = {"id": it["id"], "cmd": it["cmd"], "since": now,
                                           "deadline": now + it["timeout"] + INFLIGHT_GRACE_S}

    def _idle(self, slot):
        inf = self._s(slot)["inflight"]
        if inf:
            raise Refused("inflight", f"槽位 {slot} 上一条 ({inf['cmd']!r}) 还没回来, 一次一条")

    def _no_queue(self, slot, kind, cmd):
        """同槽位有挂起项时, 除了打断键一律不收 (挂起或直发都不收, #74 第四轮).
        前面那条批了以后设备可能停在 [confirm] 上, 排在后面的任何一条都可能替人回答; 不再判"像不像回答词"."""
        n = len(self._pending(slot))
        if n and not (kind == "key" and cmd == "ctrl_c"):
            raise Refused("busy", f"槽位 {slot} 前面还有 {n} 条等人确认, 这条不收 (前面那条批了以后它可能替人回答); "
                                  f"等前面处理完再发, 用 pending_list 看结果")

    def touched(self, slot):
        """槽位上人确认 / 发送结果的次数 (直发先判后占用, #74 第三轮). 只在内存和状态文件里, 不对外."""
        return self._s(slot).get("touch", 0)

    def _touch(self, slot):
        s = self._s(slot)
        s["touch"] = s.get("touch", 0) + 1

    def _check_mark(self, slot, mark, what):
        if mark is not None and mark != self._sent(slot):
            raise Refused("stale", f"读设备输出之后槽位 {slot} 又发过别的命令, {what}用的上下文已经旧了; "
                                   "重新看现场再来 (一个字节都没发)")

    def begin(self, slot, token, who, kind, cmd, gate, now, timeout, name=None, snap=None, mark=None):
        """闸门放行的直接发: 标在途 -> 调用方交给 conbroker -> finish.
        snap = 判闸门之前 touched() 的值: 之后槽位上有过人确认或发送结果 (设备可能变了), 不发 (#74).
        mark = 读设备输出 (给闸门 / 第 2 层模型看) 之前的 send_mark; 之后槽位又发过别的就不发 (#65).
        有挂起项时一律不直发 (#65, 比 _no_queue 更严: 打断键也不直发), 错误码 pending."""
        if gate.get("verdict") != "pass":
            raise ValueError("闸门没放行的只能 submit 挂起")
        self.writer(slot, token, now)
        self._idle(slot)
        pend = self._pending(slot)
        if pend:
            ids = ", ".join(f"#{p['id']} {p['cmd']!r}" for p in pend)
            raise Refused("pending", f"槽位 {slot} 有挂起等人确认的命令 ({ids}), 这期间不许直发任何命令或按键 "
                                     "(会改变设备上下文, 让人批准的命令落到别的对象上). 先等人确认 / 拒绝, "
                                     "或 lease_release 撤回")
        self._check_mark(slot, mark, "闸门判断")
        if snap is not None and self.touched(slot) != snap:     # 序号没变但有过发送结果 / 人确认 (#74)
            raise Refused("changed", f"槽位 {slot} 在推车看设备之后有人确认过或刚发完一条, 设备可能变了, 没发; 重新发一次")
        it = self._new_item(slot, token, who, kind, cmd, gate, now, timeout, name, "sending")
        self._start(it, now)
        return _public(it)

    def send_mark(self, slot, token, now):
        """读设备输出之前取下发送序号 (顺带查租约 / 急停 / 在途); 发送或挂起时带回来, 中间发过别的就不发不挂.
        在途时不给: 那条的回显还没到, 读到的是它生效前的上下文."""
        self.writer(slot, token, now)
        self._idle(slot)
        return self._sent(slot)

    def submit(self, slot, token, who, kind, cmd, gate, now, timeout, name=None, context=None, mark=None):
        """挂起等人确认. 同槽位已有挂起项时除了打断键一律不收 (#74 第二至四轮: 先挂 reload 再挂 enter / y,
        人批了 reload 后它们会替人回答 [confirm]).
        context = 提交时设备最近的输出 (原文, 这里掐短打码); mark = 读那份输出前的 send_mark (#65).
        在途时不挂: 那条还没生效, 现在看到的上下文是假的."""
        self.writer(slot, token, now)
        self._idle(slot)
        self._no_queue(slot, kind, cmd)
        self._check_mark(slot, mark, "挂起")
        ctx = device_context(context, gate.get("vendor") if gate.get("vendor_registered") else None)
        if not ctx:
            raise Refused("context", f"读不到槽位 {slot} 设备最近的输出, 没法让人核对现场, 不挂起 (一个字节都没发); "
                                     "检查串口服务后重试")
        if len(self._pending(slot)) >= PENDING_MAX:
            raise Refused("busy", f"槽位 {slot} 已经有 {PENDING_MAX} 条等人确认, 先等人处理")
        it = self._new_item(slot, token, who, kind, cmd, gate, now, timeout, name, "pending")
        it["sent_mark"] = self._sent(slot)
        it["tail_hash"] = _tail_hash(context)            # 不进 _public; 确认时重读整段比对
        it["context"] = ctx
        self._ev("pending", slot, now, who=who, id=it["id"], kind=kind, cmd=cmd, gate=gate, name=name)
        return _public(it)

    def confirm(self, iid, by, now, current):
        """人确认 -> 标在途, 返回要发的那条 (调用方发完 finish). 租约没了 / 急停 / 已经不在挂起: 不发.
        current = 确认前刚读的设备输出 (同样的行数, 读不到给 None): 和挂起时那段整体比 (删转义 + NFKC 后的原文,
        不打码), 有任何新输出就作废 —— 兜住人用通道 (网页 / 车机直接敲, 不计发送序号)、conbroker 超时后迟到的字节,
        以及提示符不带对象名的厂商 (思科 (config-if)# 前后一样). 设备自己吐日志也会作废, 宁可重新提交."""
        self.tick(now)
        it = self._item(iid)
        if not it or it["status"] != "pending":
            raise Refused("nope", f"没有挂起的第 {iid} 条 (已发、已拒、已作废或编号不对)")
        slot = it["slot"]
        if self.stopped(slot):
            raise Refused("estop", f"槽位 {slot} 急停中")
        lz = self._live_lease(slot, now)
        if not lz or lz["token"] != it["lease"]:
            self._close(it, "dropped", now, why="提交它的 Agent 租约已失效")
            raise Refused("lease", "提交它的 Agent 租约已失效, 这条作废")
        self._idle(slot)
        why = None
        if "sent_mark" not in it or "tail_hash" not in it:
            why = "升级前就挂着的条目没有发送序号 / 设备输出指纹, 不知道之后发没发过别的; 这条作废, 要 Agent 重新提交"
        elif it["sent_mark"] != self._sent(slot):
            why = "提交后这个槽位又发过别的命令, 设备上下文可能变了; 这条作废, 要 Agent 看过现场后重新提交"
        if why:
            self._close(it, "dropped", now, why=why)
            raise Refused("stale", why)
        if current is None:
            raise Refused("prompt", f"读不到槽位 {slot} 设备现在停在哪, 没法核对上下文, 不发 (还挂着, 稍后再确认)")
        if _tail_hash(current) != it["tail_hash"]:
            last = _ctx_line(last_line(current))[0][:CONTEXT_LINE_MAX]
            why = (f"挂起后设备有新输出 (人敲过键、迟到的字节或设备自己吐的日志; 现在最后一行 {last!r}), "
                   "设备上下文可能变了; 这条作废, 要 Agent 看过现场后重新提交")
            self._close(it, "dropped", now, why=why)
            raise Refused("stale", why)
        it["by"] = by
        self._touch(slot)
        self._start(it, now)
        self._ev("confirm", slot, now, src="human", who=by, id=it["id"], kind=it["kind"], cmd=it["cmd"],
                 agent=it["who"], name=it.get("name"))
        return _public(it)

    def reject(self, iid, by, now, why=None):
        self.tick(now)
        it = self._item(iid)
        if not it or it["status"] != "pending":
            raise Refused("nope", f"没有挂起的第 {iid} 条")
        self._close(it, "rejected", now, why=why or "人拒绝", by=by, ev="reject", src="human")

    def finish(self, iid, result, now):
        """在途的那条有结果了: result {status: sent|refused|timeout|error, echo, ms, note}."""
        it = self._item(iid)
        if not it or it["status"] != "sending":
            return None
        slot = it["slot"]
        s = self._s(slot)
        if s["inflight"] and s["inflight"]["id"] == iid:
            s["inflight"] = None
        self._touch(slot)
        it["status"] = result.get("status") or "error"
        self._log_status(slot, iid, it["status"])
        it["echo"] = echo_summary(result.get("echo"))
        if result.get("note"):
            it["why"] = result["note"]
        self._ev("send", slot, now, who=it["who"], id=iid, kind=it["kind"], cmd=it["cmd"], gate=it.get("gate"),
                 status=it["status"], echo=it["echo"], ms=result.get("ms"), why=result.get("note"),
                 by=it.get("by"), name=it.get("name"))
        lz = self._live_lease(slot, now)                    # 发完续上: Agent 等慢命令时自己续不了
        if lz and lz["token"] == it["lease"] and not s["estop"]:
            lz["expires"] = now + lz["ttl"]
        self._trim()
        return _public(it)

    def log_refused(self, slot, who, kind, cmd, code, msg, now):
        """闸门之前就被拒的 (命令不合格 / 没租约 / 急停中) 也进执行绪日志."""
        self._ev("refused", slot, now, who=who, kind=kind, cmd=cmd, why=code, msg=msg)

    # ---- 急停

    def estop(self, slot, by, src, now):
        """slot=None = 全部 (对每个槽位单条急停). 撤销租约 + 丢弃挂起; 不往设备发任何东西. -> 丢掉的条目."""
        self.tick(now)
        out = []
        for n in (SLOTS if slot is None else (slot,)):
            s = self._s(n)
            if s["estop"] is None:
                s["estop"] = {"by": by, "src": src, "t": stamp(now), "ts": now}
            lz, s["lease"] = s["lease"], None
            dropped = []
            for it in self._pending(n):
                self._close(it, "dropped", now, why="急停")
                dropped.append(it)
            inf = s["inflight"]
            self._ev("estop", n, now, src=src, who=by, by=by, dropped=[it["id"] for it in dropped],
                     revoked=lz["who"] if lz else None, inflight=inf["cmd"] if inf else None)
            out += dropped
        return [_public(it) for it in out]

    def resume(self, slot, by, now):
        """人解除急停. slot=None = 全部. 租约不还, Agent 要重新申请."""
        self.tick(now)
        for n in (SLOTS if slot is None else (slot,)):
            s = self._s(n)
            if s["estop"] is not None:
                s["estop"] = None
                self._ev("resume", n, now, src="human", who=by, by=by)

    # ---- 给人 / Agent 看的 (不含令牌)

    def _trusted(self, slot):
        """可信信息 (#65): 推车自己记的这个槽位实际交给 conbroker 的命令 (Agent 直发和人确认的都算; 人在车机 /
        网页直接敲的键不在这里). changes = 留着的全部可能改变视图 / 对象的 (不是第 1 层只读放行的, 最多 CHANGE_KEEP 条,
        第五轮起全给, 不再只给最近 5 条);
        after = 最后一条 changes 之后又发了几条只读命令 / 按键, recent = 这几条里最近的 RECENT_MAX 条;
        lost = 改视图的记录被裁掉过 (更早的看不到了)."""
        log = self._s(slot).get("log", [])
        pub = [{k: e[k] for k in ("id", "kind", "cmd", "status", "who", "by", "t", "count") if k in e} for e in log]
        idx = [i for i, e in enumerate(log) if not e["ro"]]
        tail = pub[idx[-1] + 1:] if idx else pub
        return {"changes": [pub[i] for i in idx], "lost": bool(self._s(slot).get("log_lost")),
                "after": len(tail), "recent": tail[-RECENT_MAX:]}

    def view(self, now, slot=None):
        self.tick(now)
        slots = []
        for n in SLOTS:
            s = self._s(n)
            lz = self._live_lease(n, now)
            inf = s["inflight"]
            slots.append({"slot": n,
                          "estop": {k: s["estop"][k] for k in ("by", "src", "t")} if s["estop"] else None,
                          "writer": lz["who"] if lz else None,
                          "expires_in": int(lz["expires"] - now) if lz else None,
                          "inflight": {"id": inf["id"], "cmd": inf["cmd"]} if inf else None,
                          "pending": len(self._pending(n))})
        for e in slots:
            e["trusted"] = self._trusted(e["slot"])
        want = (lambda it: True) if slot is None else (lambda it: it["slot"] == slot)
        pend = [dict(_public(it), age_s=int(now - it["ts"])) for it in self._pending() if want(it)]
        hist = [_public(it) for it in self.doc["items"] if it["status"] != "pending" and want(it)]
        return {"slots": slots, "pending": pend, "history": hist[-HISTORY_MAX:]}
