"""远程审批第一阶段: 推车服务端核心的纯逻辑 (#50, 设计稿 docs/DESIGN_remote_approval.md, PR #59).

这一阶段只有逻辑, 不接原生 Book (串口 Book.confirm / 键鼠 LeaseStore.take / PXE take 的 expect 参数是下一步),
不接真飞书 (通道接口和假实现在 approval_channel.py), 网页审批和网页登录 TOTP 也是下一阶段.

- 审批请求模型: make_item() 把挂起项规整成统一的条目 (id / kind / target / action / danger / ctx ...),
  同时定级 (tier_of), 算期限和 digest v1.
- 分级 L0-L4 (设计稿 3.1 / 3.2, 拍板 2 / 4 / 7 / 11):
  串口不带高危词 L2, 带高危词 L3; 键鼠挂起默认 L3, 只有 alt+f4 降 L2; pxe_request 全部 L4;
  远程解除急停 L3; label_print 批量 L2. 认不出的 kind 不收.
- 各通道因素 requirement(): 飞书 L2 直接点批准, L3 要 TOTP (键鼠 L3 只能拒绝, 要看实时画面), L4 不开放远程;
  网页 L2 要 TOTP, L3 要等带外码上线 (A3) 之前一律不开.
- 摘要 render() / 卡片 card() 只由服务端字段拼出来. 命令原文打码后显示; Agent 自述、模型意见、设备输出、
  提示符上的名字一律清洗、限长、标"不可信", 不能另起一行冒充固定行.
- 请求解析 parse_request(): 字段多一个少一个都不认, 只认 60 秒内的.
- decide(): 纯函数, 输入决定记录 (approvals.json 的内容) 和一条请求, 返回 (结果, 新记录, 审计行).
  nonce 只能用一次; TOTP 防重放 / 退避见 totp_core; 同一账号 30 秒内只能批一条 L3 及以上.
- 审计行带审批人身份: 通道账号、by、tailscale 登录名和设备名 (第一阶段由调用方传入, 只校验格式).
  永远不记 TOTP 码、种子, nonce 只记前 4 位.

期限 EXPIRES_S / AUTH_TTL_S 和分级争议项 (拍板 9-11) 按设计稿现有取值, 待维护者对稿确认."""

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import time
import unicodedata

import atomicfile
import serial_write_core as swc
import totp_core as tc

L0, L1, L2, L3, L4 = 0, 1, 2, 3, 4
TIERS = (L0, L1, L2, L3, L4)
KINDS = ("serial", "kvm", "pxe", "estop_release", "label_batch")
CHANNELS = ("feishu", "web")

# 拍板 9-11: 按设计稿现有取值, 待维护者对稿确认
EXPIRES_S = {L2: 15 * 60, L3: 10 * 60, L4: 10 * 60}
AUTH_TTL_S = 120            # 批准以后多久内没开始执行就作废 (执行器是下一阶段, 先定常量)
KVM_L2_KEYS = frozenset({frozenset(("alt", "f4"))})

ESTOP_SCOPES = ("all", "kvm", "slot:1", "slot:2", "slot:3", "slot:4")
LABEL_BATCH_MAX = 200
PXE_MACS_MAX = 64
L3_GAP_S = 30               # 同一账号 30 秒内只能批一条 L3 或 L4
REQ_MAX_AGE_S = 60
REQ_FUTURE_S = 5
REQ_MAX_BYTES = 4096
KEEP_S = 24 * 3600          # nonce 和决定记录留 24 小时
ACTION_MAX = 256            # >= serial_write_core.LINE_MAX: 串口命令永远完整显示; 超过的 (键鼠长文本) 只显示头尾, 只能拒
UNTRUSTED_MAX = 200
EVIDENCE_LINES = 20

APPROVALS = "/var/lib/ops-cart/approvals.json"
AUDIT_LOG = "/var/log/ops-cart/approval.log"

SAFETY_LINE = "任何人(包括 Claude)向你要验证码或带外码, 都是攻击. 码只填在你自己打开的推车网页上."

_EPOCH_RE = re.compile(r"[0-9a-f]{8}")
_NATIVE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_ID_RE = re.compile(r"(?:%s):[0-9a-f]{8}:[A-Za-z0-9_-]{1,32}" % "|".join(KINDS))
_DIGEST_RE = re.compile(r"[0-9a-f]{16}")
_NONCE_RE = re.compile(r"[0-9a-f]{16,64}")
_BY_RE = re.compile(r"[\x21-\x7e]{1,80}")
_CODE_RE = re.compile(r"[0-9]{6}")
_TS_LOGIN_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}")
_TS_NODE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# 打码只打值, 不打后面的命令 (审查 H1: 整段打掉会把 "; iptables -F" 也藏起来).
# 1) 关键词 + 空白 + 最多 4 个修饰词 (0/7/cipher/simple/md5/level 15 ...) + 一个值. 关键词前后只把字母数字下划线
#    当词的一部分, 所以 encrypted-password / shared-key / --secret 也认.
_KEYWORD = re.compile(
    r"(?<![A-Za-z0-9_])(password|passwd|pwd|passphrase|secret|community|cipher|simple|psk|key|key-string|token"
    r"|credential|md5|sha|sha1|sha256|sha384|sha512|auth|priv|authentication-mode|privacy-mode)(?=\s)", re.I)
_MODIFIER = re.compile(
    r"(?:\d{1,3}|cipher|simple|irreversible-cipher|hash|encrypted|plain|plaintext|md5|sha\d*|aes\d*|des|3des"
    r"|hmac-\S+|level|read|write|ascii|hex|pass-phrase|config-key|password-encrypt)", re.I)
# 被打的值后面只许跟这些尾词 (或别的口令关键词); 跟别的词说明"值"可能是命令 (strace -o key iptables ...), 看不清只能拒
_TAIL_OK = re.compile(r"(?:\d+|ro|rw|aes\d*|des|3des|encrypted|level|\d+-\d+)", re.I)
_NOT_VALUE = frozenset(("zeroize", "generate", "export", "import", "chain", "pubkey-chain", "rsa", "dsa", "ecdsa"))
_TOKEN = re.compile(r"\s+(\"[^\"]*\"|'[^']*'|\S+)")
# 2) 固定写法: 只打括号里最后一组
_VALUE_RULES = [re.compile(r, re.I) for r in (
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{0,64}(?:password|passwd|secret|token|key|pwd|pass)[A-Za-z0-9_]*[=:]"
    r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)",     # 紧贴: DB_PASSWORD=x / --password=x / pass:x
    r"://[^/@\s:]+:([^@\s/]+)@",                                   # URL 里的 user:pass@
    r"(?<!\S)-u\s*[^\s:]+:(\S+)",                                  # curl -u user:pass
    r"(?<!\S)-U\s*[^\s%]+%(\S+)",                                  # smbclient -U user%pass
    r"\bwpa_passphrase\s+\S+\s+(\S+)",
    r"\bversion\s+(?:1|2c)\s+(\S+)",                               # snmp-server host H version 2c <community>
    r"\bAuthorization:\s*(?:Bearer\s+|Basic\s+|Token\s+)?([^\s'\"]+)",  # curl -H 'Authorization: Bearer X'
)]
# 带空格的 KEY = x / key: x / key : x: 值后面再跟词就看不清. 不收 "KEY= x" (等号紧贴变量名、后面是空格:
# shell 里是在 KEY 为空的环境下执行 x, x 是命令不是口令)
_LOOSE_VALUE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{0,64}(?:password|passwd|secret|token|key|pwd|pass)[A-Za-z0-9_]*"
    r"(?:\s+=\s*|\s*:\s+|\s+:)(\"[^\"]*\"|'[^']*'|[^\s;&|'\"]+)", re.I)
_CTX_RULES = [(re.compile(c, re.I), re.compile(r, re.I)) for c, r in (
    (r"\b(?:mysql\w*|mariadb\w*)\b", r"(?<!\S)-p(\S+)"),          # mysql -pX
    (r"\bldap\w+", r"(?<!\S)-[wy]\s+(\S+)"),                       # ldapsearch -w X
    (r"\bipmitool\b", r"(?<!\S)-P\s*(\S+)"),                      # ipmitool -P X
    (r"\bredis-cli\b", r"(?<!\S)-a\s*(\S+)"),                     # redis-cli -a X
    (r"\b(?:un)?zip\b|\b7za?\b", r"(?<!\S)-P\s*(\S+)"),            # zip / unzip -P X
    (r"\bssh-keygen\b", r"(?<!\S)-[NP]\s*(\S+)"),                  # ssh-keygen -N X
)]
# 3) 这些工具整条命令都可能是口令: 只显示长度, 只能拒
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_-])(?:chpasswd|passwd|htpasswd|sshpass)(?![A-Za-z0-9_-])|\bnet\s+user\b", re.I)
_UNSAFE_HIDDEN = re.compile(r"[\s;&|<>`]|\$\(")
# 设备停在密码提示符时, 这条动作本身就是密码
_PW_PROMPT = re.compile(r"(?i)(?:password|passwd|pass ?phrase|secret|密码|口令|\bPIN)\b[^\n?？]{0,80}[:：]\s*$"
                        r"|(?:password|passwd|pass ?phrase|密码|口令)\s*$")
_YES_NO = re.compile(r"(?i)\[\s*y(?:es)?\s*/\s*n(?:o)?\s*\]\s*[:：]?\s*$")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_BAD_CATS = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


class BadRequest(Exception):
    pass


class StateUnreadable(Exception):
    pass


# ------------------------------------------------------------ 串口高危判定 (审查三轮 H1)
# 黑名单补不完 (维护者 11-07 拍板: 飞书 L2 仍直点, 接受残余风险), 这里只把已知的变形写法往 L3 推, 往"多拦"错.
# 首词 (去掉 sudo / env / VAR=x 这类前缀后) 是这些 -> L3
_L3_LEAD = frozenset(("no", "undo", "do", "clear", "default", "rollback", "commit", "test", "debug", "crypto",
                      "reset", "delete", "format", "erase", "kill", "pkill", "killall", "umount",
                      "chmod", "chown", "mv", "truncate", "tee", "exec", "eval", "source"))
# 任意位置出现 (含带路径时的 basename) -> L3
_L3_WORDS = frozenset(("blkdiscard", "wipefs", "shred", "telinit", "init", "kexec", "zeroize", "sysrq-trigger",
                       "ifdown", "parted", "sgdisk", "lvremove", "vgremove", "pvremove", "cryptsetup", "mdadm",
                       "dd", "fdisk", "halt", "poweroff", "reboot", "shutdown", "rm", "rmdir"))
_PREFIX_CMDS = frozenset(("sudo", "env", "nohup", "time", "nice", "ionice", "command", "builtin", "xargs",
                          "strace", "ltrace", "timeout", "watch", "doas", "busybox", "chroot", "setsid", "stdbuf"))
# 前缀命令各自带一个参数的选项 (sudo -u root / nice -n 10 / strace -o f ...); 不在表里的选项当不带参数.
# 位置参数: timeout 的时长、chroot 的根目录各剥一个.
_ARG_OPTS = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D", "--user", "--group", "--host", "--prompt",
             "--role", "--type", "--other-user", "--chdir", "--close-from"},
    "doas": {"-u", "-C"}, "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "strace": {"-o", "-e", "-p", "-s", "-u", "-E", "-a", "-P"}, "ltrace": {"-o", "-e", "-p", "-s", "-u", "-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"}, "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-I", "-L", "-P", "-d", "-a", "-s", "-E"}, "time": {"-f", "-o", "--format", "--output"},
    "chroot": {"--userspec", "--groups"}, "watch": {"-n", "-d", "--interval"},
}
_POSITIONAL = {"timeout": 1, "chroot": 1}
_SEG = re.compile(r"[;&|\n]+")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# 子命令组合: 第一个词 + 后面任一词 -> L3
_L3_PAIRS = {"systemctl": {"isolate", "poweroff", "reboot", "halt", "kexec", "rescue", "emergency", "stop", "mask",
                           "disable", "kill"},
             "sysctl": {"-w", "--write", "-p", "--load", "--system"},
             "nft": {"flush", "delete"}, "ip": {"down", "del", "delete", "flush"},
             "nmcli": {"down", "delete", "off"}, "ifconfig": {"down"}, "echo": {"/proc/sysrq-trigger"}}


def serial_danger(text, extra=()):
    """串口命令的高危理由 (规范写法, 去重). 闸门的 danger_words 加上这里的变形规则; 不信调用方传的 danger."""
    out = list(swc.danger_words(text, extra))

    def add(w):
        if w not in out:
            out.append(w)
    low = (text or "").lower()
    if "$'" in low or "`" in low or "$(" in low:
        add("命令替换/转义")
    if re.search(r">\s*/(?:proc|sys|dev)/(?!(?:null|stdout|stderr|tty|fd/\d+)(?:\s|$))", low):
        add("写 /proc /sys /dev")
    for seg_raw in _SEG.split(text or ""):
        raw = [t for t in re.split(r"\s+", seg_raw.strip()) if t]
        toks = [t.lower() for t in raw]
        base = [os.path.basename(t.strip("'\"\\")) or t for t in toks]
        # 去掉环境变量赋值和前缀命令 (按各自的选项表剥选项参数), 剩下第一个词是真正的命令词
        i, cur, first_prefix = 0, None, None    # cur: 正在剥的前缀命令
        while i < len(toks):                    # 前缀命令、它的选项和选项参数、位置参数、环境变量赋值, 可叠好几层
            if base[i] in _PREFIX_CMDS:
                cur, first_prefix = base[i], i if first_prefix is None else first_prefix
                i += 1
                n = _POSITIONAL.get(cur, 0)
                while n and i < len(toks):
                    if toks[i].startswith("-"):
                        i += 2 if raw[i] in _ARG_OPTS.get(cur, ()) else 1
                    else:
                        i, n = i + 1, n - 1
            elif _ASSIGN.match(toks[i]):
                i += 1
            elif toks[i].startswith("-") and cur:
                i += 2 if raw[i] in _ARG_OPTS.get(cur, ()) else 1
            else:
                break
        # 子命令组合类判断 (systemctl stop / iptables -F / sysctl -w / mount 设备) 另外在前缀之后 4 个词的窗口里查,
        # 剥离判错时也不漏 (这几类要命令词和子命令同时出现, 误报少)
        window = range(first_prefix + 1, min(len(toks), first_prefix + 5)) if first_prefix is not None else ()
        for j in sorted(set(range(i, min(len(toks), i + 1))) | set(window)):
            precise = j == i
            b = base[j]
            if precise and b in _L3_LEAD and not (b == "do" and j + 1 < len(toks) and toks[j + 1].startswith(("sh", "dis"))):
                add(b)
            b = base[j]
            if precise and b == "mount":        # 只有 -l / -t ext4 这类只列出; 带设备或挂载点才改东西
                rest, k = toks[j + 1:], 0
                while k < len(rest):
                    if rest[k] in ("-a", "--all"):
                        add("mount")
                        break
                    if rest[k] in ("-t", "-o", "-O"):
                        k += 2
                    elif rest[k].startswith("-"):
                        k += 1
                    else:
                        add("mount")
                        break
            if b in _L3_PAIRS and _L3_PAIRS[b] & set(toks[j + 1:]):
                add(b)
            if b in ("iptables", "ip6tables") and {"-F", "--flush", "-X", "-P", "-D", "-Z"} & set(raw[j + 1:]):
                add(b)                          # 大小写敏感: -p 是协议, -P 是默认策略
            if b == "sysctl" and any("=" in t for t in toks[j + 1:]):
                add("sysctl")
        for t, b in zip(toks, base):
            for cand in {b, t.lstrip("-")}:
                if cand in _L3_WORDS or cand in swc.DANGER:      # 带路径的按 basename 精确匹配, 不按缩写
                    add(cand)
    return out


# ------------------------------------------------------------ 审批请求模型

def tier_of(item):
    kind, act = item.get("kind"), item.get("action") or {}
    if kind == "serial":
        text = str(act.get("text", ""))
        if len(text) > ACTION_MAX:
            return L3                           # 超长不跑正则 (平方级), 卡片上也看不全, 只能拒
        return L3 if item.get("danger") or serial_danger(text) or _spans(text) else L2   # 有打码的也升 L3
    if kind == "kvm":
        keys = frozenset(str(k).lower() for k in act.get("keys") or ())
        return L2 if act.get("type") == "key" and keys in KVM_L2_KEYS else L3
    if kind == "estop_release":
        return L3
    if kind == "label_batch":
        return L2
    return L4                                   # pxe, 以及万一漏进来的未知 kind


def digest(item):
    """v1 公式, 定了以后不改 (设计稿 2.2). evidence / intent / model_reason 只给人看, 不进 digest."""
    body = {"v": 1, "id": item.get("id"), "kind": item.get("kind"), "target": item.get("target"),
            "action": item.get("action"), "danger": item.get("danger"), "created": item.get("created"),
            "ctx": item.get("ctx")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()[:16]


def _is_str(v, empty=False):
    return isinstance(v, str) and (empty or bool(v))


def _is_list_of_str(v):
    return isinstance(v, list) and bool(v) and all(_is_str(x) for x in v)


# 每种 kind 的 action 字段白名单: 多一个少一个、类型不对都不收 (卡片上看不见的字段不许夹带进 digest)
_SCHEMA = {
    ("serial", "line"): {"type": None, "text": _is_str},
    ("serial", "key"): {"type": None, "text": _is_str},
    ("kvm", "key"): {"type": None, "keys": _is_list_of_str},
    ("kvm", "type"): {"type": None, "text": _is_str, "enter": lambda v: isinstance(v, bool),
                      "secret": lambda v: isinstance(v, bool), "prefix": lambda v: _is_str(v, empty=True)},
    ("pxe", None): {"purpose": _is_str, "macs": lambda v: _is_list_of_str(v) and len(v) <= PXE_MACS_MAX},
    ("estop_release", None): {"scope": lambda v: v in ESTOP_SCOPES},
    ("label_batch", None): {"count": lambda v: type(v) is int and 2 <= v <= LABEL_BATCH_MAX, "kind": _is_str},
}


def _check_action(kind, action):
    key = (kind, action.get("type")) if kind in ("serial", "kvm") else (kind, None)
    spec = _SCHEMA.get(key)
    if spec is None or set(action) != set(spec):
        raise ValueError(f"{kind} 的 action 字段不对")
    for k, ok in spec.items():
        if ok is not None and not ok(action[k]):
            raise ValueError(f"{kind} 的 action.{k} 不合格")


def make_item(kind, native_id, epoch, target, created, action, danger=(), ctx=None, agent=None, intent=None,
              model_reason=None, target_label=None, evidence=()):
    """规整成统一条目. target 为空的不收 (不许远程批准); id = kind:epoch:原生编号."""
    if kind not in KINDS:
        raise ValueError(f"不认识的挂起项类型 {kind!r}")
    if not isinstance(epoch, str) or not _EPOCH_RE.fullmatch(epoch):
        raise ValueError("epoch 要是 8 位小写十六进制")
    if not _NATIVE_RE.fullmatch(str(native_id)):
        raise ValueError("原生编号不合格")
    if not isinstance(target, str) or not target.strip() or len(target) > 64:
        raise ValueError("target 为空或太长: 不许远程批准")
    if not isinstance(action, dict):
        raise ValueError("action 要是 dict")
    _check_action(kind, action)
    created = float(created)
    if not math.isfinite(created):
        raise ValueError("created 要是有限的数")
    it = {"id": f"{kind}:{epoch}:{native_id}", "kind": kind, "target": target, "action": dict(action),
          "danger": _union_danger(kind, action, danger), "created": created, "ctx": ctx,
          "agent": agent, "intent": intent, "model_reason": model_reason, "target_label": target_label,
          "evidence": list(evidence or ())}
    it["tier"] = tier_of(it)
    it["expires"] = expires_of(it)
    it["digest"] = digest(it)
    return it


def _union_danger(kind, action, danger):
    out = [str(w) for w in danger or ()]
    if kind == "serial":
        text = str(action.get("text", ""))
        extra = ["超长"] if len(text) > ACTION_MAX else serial_danger(text)
        out += [w for w in extra if w not in out]
    return out


def expires_of(item):
    return item["created"] + EXPIRES_S.get(tier_of(item), min(EXPIRES_S.values()))


def requirement(tier, channel, kind):
    """-> 'none' / 'totp' / 'forbid'. 车机不走这里 (现场确认维持原样)."""
    if channel not in CHANNELS or tier not in TIERS:
        return "forbid"
    if tier <= L1:
        return "none"
    if tier >= L4:
        return "forbid"                         # 拍板 2: L4 不开放远程批准
    if channel == "feishu":
        if tier == L2:
            return "none"                       # 拍板 2: 飞书 L2 直接点批准
        return "forbid" if kind == "kvm" else "totp"
    return "totp" if tier == L2 else "forbid"   # 网页 L3 要等带外码 (A3)


# ------------------------------------------------------------ 打码 / 清洗

def _is_keyword(tok):
    m = _KEYWORD.match(tok + " ")
    return bool(m) and m.end() == len(tok)


def _keyword_hits(text):
    """关键词规则命中的值: [(start, end, 看不清)]. 跳完修饰词没有值了 -> 打最后一个被跳过的 (password 12).
    值后面还跟着不是尾词、也不是口令关键词的词 -> 这个"值"可能是命令, 标看不清."""
    out = []
    for m in _KEYWORD.finditer(text):
        pos, last, t, gave_up = m.end(), None, None, False
        for _ in range(5):
            t = _TOKEN.match(text, pos)
            if not t:
                break
            tok = t.group(1)
            if _MODIFIER.fullmatch(tok) and _TOKEN.match(text, t.end()):
                pos, last, t = t.end(), t, None
                continue
            if tok.lower() in _NOT_VALUE or tok[0] in ";|&":
                t = None
            break
        else:
            gave_up = True                      # 修饰词太多: 认不出哪个是值, 看不清
        if gave_up:
            out.append((m.start(), m.start(), True))
            continue
        if t is None and last is not None and not _TOKEN.match(text, last.end()):
            t = last                            # 跳完修饰词没有值了: password 12 里 12 就是值
        if t is None:
            continue
        nxt = _TOKEN.match(text, t.end())
        unclear = bool(nxt) and not (_TAIL_OK.fullmatch(nxt.group(1)) or _is_keyword(nxt.group(1))
                                     or _MODIFIER.fullmatch(nxt.group(1)))
        out.append((t.start(1), t.end(1), unclear))
    for m in _LOOSE_VALUE.finditer(text):
        nxt = _TOKEN.match(text, m.end(1))
        out.append((m.start(1), m.end(1), bool(nxt) and not _TAIL_OK.fullmatch(nxt.group(1).strip("'\""))))
    return out


def _spans(text):
    """要打码的 [start, end) 区间, 已合并."""
    out = [(a, b) for a, b, _ in _keyword_hits(text) if b > a]
    for rx in _VALUE_RULES:
        out += [(m.start(1), m.end(1)) for m in rx.finditer(text)]
    for ctx, rx in _CTX_RULES:
        if ctx.search(text):
            out += [(m.start(1), m.end(1)) for m in rx.finditer(text)]
    merged = []
    for a, b in sorted(out):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def _mark(n):
    return f"<已打码 {n} 字符>"


def _opaque_mark(text):
    return f"<可能含口令, 只显示长度: {len(text)} 字符>"


def _masked_parts(text):
    """-> [(片段, 是否打码)]."""
    parts, pos = [], 0
    for a, b in _spans(text):
        if a > pos:
            parts.append((text[pos:a], False))
        parts.append((text[a:b], True))
        pos = b
    if pos < len(text):
        parts.append((text[pos:], False))
    return parts


def mask_command(text):
    """只把口令的值换成长度, 命令其余部分原样. chpasswd / passwd / sshpass 这类整条只显示长度. digest 仍按原文算."""
    if _OPAQUE.search(text):
        return _opaque_mark(text)
    return "".join(_mark(len(p)) if hid else p for p, hid in _masked_parts(text))


def _unclear(text):
    """这段原文人在卡片上看不清: 太长被截断、含控制类字符、整条只显示长度、或打掉的部分里有空白或 shell 元字符."""
    if len(text) > ACTION_MAX or _OPAQUE.search(text):
        return True
    if any(ch not in "\n\t" and unicodedata.category(ch) in _BAD_CATS for ch in text):
        return True
    if any(u for _, _, u in _keyword_hits(text)):
        return True
    return any(hid and _UNSAFE_HIDDEN.search(p) for p, hid in _masked_parts(text))


def _escape(text):
    """控制字符、方向控制、零宽、行/段分隔符显示成转义 (\\xNN / \\u{..}); 换行和 Tab 留给 visible."""
    out = []
    for ch in text.replace("\r\n", "\n"):
        if ch not in "\n\t" and unicodedata.category(ch) in _BAD_CATS:
            out.append(f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{{{ord(ch):04x}}}")
        else:
            out.append(ch)
    return "".join(out)


def visible(text):
    return text.replace(" ", "␠").replace("\t", "⇥").replace("\r\n", "\n").replace("\n", "↵ 回车")


def _shown_parts(text):
    return "".join(_mark(len(p)) if hid else visible(_escape(p)) for p, hid in _masked_parts(text))


def _action_shown(text, secret=False, opaque=False):
    if secret:
        return f"<secret {len(text)} 字符>"     # 同 mcp_kvm._shown: secret 只显示长度
    if opaque or _OPAQUE.search(text):
        return _opaque_mark(text)
    if len(text) > ACTION_MAX:
        return f"{_shown_parts(text[:80])} …(共 {len(text)} 字符, 去网页看全文)… {_shown_parts(text[-80:])}"
    return _shown_parts(text)


def at_password_prompt(item):
    """串口挂起时设备最后一行输出是密码提示符."""
    if item.get("kind") != "serial":
        return False
    lines = []
    for ln in (part for e in item.get("evidence") or () for part in re.split(r"\r?\n|\r", str(e))):
        s = _ANSI.sub("", ln)
        s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc",) or ch == "\t")
        if s.strip():
            lines.append(_norm(s).rstrip())
    return bool(lines) and bool(_PW_PROMPT.search(lines[-1])) and not _YES_NO.search(lines[-1])


def truncated(item):
    """动作原文人在卡片上看不全或看不清 -> 不给批准, 只能拒. 键鼠 secret 的内容本来就不显示, 但 prefix 照查."""
    act = item.get("action") or {}
    if item.get("kind") not in ("serial", "kvm"):
        return False
    if at_password_prompt(item):
        return True
    fields = ["prefix"] + ([] if act.get("secret") else ["text"])
    return any(_unclear(act[k]) for k in fields if isinstance(act.get(k), str) and act.get(k))


def _norm(text):
    """去掉 Cf (零宽、方向控制), 再 NFKC (全角 -> 半角): 打码前先做, 防 pass<零宽>word 这类躲过词表."""
    return unicodedata.normalize("NFKC", "".join(ch for ch in text if unicodedata.category(ch) != "Cf"))


def untrusted(text, limit=UNTRUSTED_MAX):
    """来自 Agent / 模型 / 设备的文字: 去掉控制字符、方向控制、零宽字符, 换行压成 ↵, 限长.
    调用方负责加"不可信"前缀."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    out = []
    for ch in s.replace("\r\n", "\n"):
        if ch in "\n\r  \x85":
            out.append(" ↵ ")
        elif ch == "\t":
            out.append("⇥")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        elif ch in "<>":
            out.append("＜" if ch == "<" else "＞")     # 防飞书 <at user_id="all"> 和 lark_md 标签
        else:
            out.append(ch)
    s = "".join(out)
    return s[:limit] + f"…(共 {len(s)} 字)" if len(s) > limit else s


def _untrusted_masked(text, limit=UNTRUSTED_MAX):
    """设备输出这类: 可能回显密码. 先归一化 (去零宽 + NFKC), 再打码, 再清洗."""
    return untrusted(mask_command(_norm(text)) if isinstance(text, str) else text, limit)


# ------------------------------------------------------------ 摘要 / 卡片

def _dur(s):
    s = max(0, int(s))
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


def _title_action(item):
    kind, act, tgt = item["kind"], item["action"], untrusted(item["target"], 64)
    danger = [untrusted(w, 32) for w in item.get("danger") or ()]
    tier = f"[L{tier_of(item)}]"
    if kind == "serial":
        title = f"{tier} {tgt} 串口" + (f" · 高危词 {', '.join(danger)}" if danger else "")
        label = "按键" if act.get("type") == "key" else "动作"
        return title, [f"{label}: {_action_shown(str(act.get('text', '')), opaque=at_password_prompt(item))}"]
    if kind == "kvm":
        title = f"{tier} 键鼠 {tgt}" + (f" · 高危词 {', '.join(danger)}" if danger else "")
        if act.get("type") == "key":
            return title, ["按键: " + untrusted("+".join(str(k) for k in act.get("keys") or ()), 64)]
        lines = []
        if act.get("prefix"):
            lines.append(f"已在屏幕上: {_action_shown(str(act['prefix']))}")
        tail = " + 回车" if act.get("enter") else ""
        lines.append(f"批准后才会敲: {_action_shown(str(act.get('text', '')), bool(act.get('secret')))}{tail}")
        return title, lines
    if kind == "pxe":
        macs = [untrusted(m, 32) for m in act.get("macs") or ()]
        purpose = untrusted(act.get("purpose"), 32)
        return f"{tier} PXE {purpose}", [f"动作: PXE 引导 {purpose} → {len(macs)} 台: {', '.join(macs)}"]
    if kind == "estop_release":
        scope = untrusted(act.get("scope"), 32)
        return f"{tier} 解除急停 ({scope})", [f"动作: 解除急停, 范围 {scope}"]
    count = int(act.get("count") or 0)
    return f"{tier} 批量打标签 {count} 张", [f"动作: 批量打印 {count} 张标签 ({untrusted(act.get('kind'), 32)})"]


def render(item, now):
    """-> {title, lines, text}. 只由服务端字段拼; 不可信字段放后面、带前缀、各自清洗限长."""
    title, action = _title_action(item)
    lines = [title] + action
    tgt = f"目标: {untrusted(item['target'], 64)}"
    if item.get("target_label"):
        tgt += f" (提示符上的名字: {_untrusted_masked(item['target_label'], 64)}, 来自设备输出, 不可信)"
    lines.append(tgt)
    ev = list(item.get("evidence") or ())[-EVIDENCE_LINES:]
    if ev:
        lines.append("现场: 挂起时设备最近输出 (设备输出, 不可信):")
        lines += [f"  | {_untrusted_masked(ln)}" for ln in ev]
    lines.append(f"来源: Agent {untrusted(item.get('agent') or '?', 64)} · {_dur(now - item['created'])}前"
                 f" · 剩 {_dur(item['expires'] - now)}")
    if item.get("danger"):
        lines.append(f"闸门: 带高危词 {', '.join(untrusted(w, 32) for w in item['danger'])}, 要人确认")
    mr = item.get("model_reason")
    lines.append(f"模型意见(不可信): {_untrusted_masked(mr)}" if mr else "模型意见: (没开)")
    if item.get("intent"):
        lines.append(f"Agent 自述(未核实, 不可信): {_untrusted_masked(item['intent'])}")
    lines.append(f"digest: {item['digest']}")
    lines.append(SAFETY_LINE)
    return {"title": title, "lines": lines, "text": "\n".join(lines)}


def card(item, channel, now):
    """通道无关的卡片内容. 按钮按 requirement 定: 禁止远程批准的没有批准按钮; 拒绝、急停、关远程审批常驻.
    文字要按纯文本发 (不当 markdown 渲染)."""
    need = "forbid" if truncated(item) else requirement(tier_of(item), channel, item["kind"])
    r = render(item, now)
    buttons = (["approve"] if need != "forbid" else []) + ["reject", "estop", "lockdown"]
    return {"id": item["id"], "digest": item["digest"], "title": r["title"], "text": r["text"],
            "buttons": buttons, "totp": need == "totp"}


# ------------------------------------------------------------ 请求

FIELDS = ("op", "id", "digest", "account", "by", "totp", "oob", "retype", "nonce", "t")
OPS = ("approve", "reject")         # challenge (带外码) 和 elevate (审批会话) 是下一阶段


def _no_dup(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("重复的键")
    return dict(pairs)


def _no_const(name):
    raise ValueError(f"不收 {name}")


def parse_request(raw, now):
    """审批请求 -> dict; 不合格抛 BadRequest. 通道以所在目录为准, 不看请求里自报的."""
    if isinstance(raw, bytes):
        if len(raw) > REQ_MAX_BYTES:
            raise BadRequest("请求太大")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest("不是 UTF-8") from None
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > REQ_MAX_BYTES:
        raise BadRequest("请求太大或不是文本")
    try:
        r = json.loads(raw, object_pairs_hook=_no_dup, parse_constant=_no_const)
    except ValueError:
        raise BadRequest("不是 JSON") from None
    if not isinstance(r, dict) or set(r) != set(FIELDS):
        raise BadRequest("字段不对 (多一个少一个都不认)")
    if any(isinstance(v, str) and "://" in v for v in r.values()):
        raise BadRequest("请求里不许有链接")
    checks = (("op", lambda v: v in OPS), ("id", lambda v: _ID_RE.fullmatch(v)),
              ("digest", lambda v: _DIGEST_RE.fullmatch(v)), ("account", tc.valid_account),
              ("by", lambda v: _BY_RE.fullmatch(v)), ("totp", lambda v: v == "" or _CODE_RE.fullmatch(v)),
              ("oob", lambda v: v == ""), ("retype", lambda v: v == ""),     # 带外码 / 复述目标: A3
              ("nonce", lambda v: _NONCE_RE.fullmatch(v)))
    for k, ok in checks:
        if not isinstance(r[k], str) or not ok(r[k]):
            raise BadRequest(f"{k} 不合格")
    t = r["t"]
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        raise BadRequest("t 要是数字")
    if not now - REQ_MAX_AGE_S <= t <= now + REQ_FUTURE_S:
        raise BadRequest("请求过期或来自未来")
    return r


def ts_identity(login, node):
    """tailscale 身份 (登录名 + 设备名). 第一阶段由调用方传入, 只校验格式; 都是 None 表示没取到.
    有设备名没登录名不收."""
    if login is None and node is None:
        return {"ts_login": None, "ts_node": None}
    if not isinstance(login, str) or not _TS_LOGIN_RE.fullmatch(login) or len(login) > 254:
        raise ValueError("tailscale 登录名格式不对")
    if node is not None and (not isinstance(node, str) or not _TS_NODE_RE.fullmatch(node)):
        raise ValueError("tailscale 设备名格式不对")
    return {"ts_login": login, "ts_node": node}


# ------------------------------------------------------------ 审计

def audit(ev, item, channel, now, result, account=None, by=None, factor="none", nonce=None,
          ts_login=None, ts_node=None, req=None):
    """一行审计 (设计稿 4.8). summary 是 render 的标题和动作行 (已打码); 不含码, nonce 只记前 4 位."""
    req = req or {}
    summary = ""
    if item:
        title, action = _title_action(item)
        summary = " | ".join([title] + action)
    nonce = nonce or req.get("nonce")
    return {"t": now, "ev": ev, "id": item["id"] if item else req.get("id"),
            "digest": item["digest"] if item else req.get("digest"),
            "kind": item["kind"] if item else None, "tier": item["tier"] if item else None,
            "channel": channel, "account": account or req.get("account"), "by": by or req.get("by"),
            "factor": factor, "summary": summary, "result": result,
            "ts_login": ts_login, "ts_node": ts_node, "nonce": nonce[:4] if nonce else None}


def audit_bad_request(channel, why, now):
    return audit("bad_request", None, channel, now, untrusted(why, 120))


def append_audit(path, rec):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


# ------------------------------------------------------------ 决定

_EV = {"authorized": "authorized", "rejected": "reject", "forbidden": "forbidden", "replay": "replay",
       "totp_bad": "totp_bad", "locked": "totp_lock", "clock_back": "clock_back", "no_seed": "no_seed",
       "stale": "drop", "gone": "drop", "expired": "expire", "already": "already", "conflict": "conflict",
       "too_soon": "too_soon", "state_unreadable": "state_unreadable"}
_TOTP_OUT = {"bad": "totp_bad", "replay": "replay", "clock_back": "clock_back", "locked": "locked",
             "no_seed": "no_seed", "bad_state": "state_unreadable"}


def fresh_state():
    return {"v": 1, "accounts": {}, "nonces": {}, "decisions": {}}


def prune(state, now):
    st = copy.deepcopy(state)
    st["nonces"] = {k: t for k, t in st["nonces"].items() if t >= now - KEEP_S}
    st["decisions"] = {k: d for k, d in st["decisions"].items() if d.get("t", 0) >= now - KEEP_S}
    return st


_OPEN_ID_RE = re.compile(r"ou_[A-Za-z0-9_-]{1,54}")


_ACCOUNT_KEYS = {"feishu", "ts_login", "ts_nodes"}


def valid_accounts(accounts):
    """白名单 {账号: {"feishu": open_id, "ts_login": 登录名, "ts_nodes": [设备名]}}, 三项都可选.
    open_id 和登录名在账号之间必须唯一 (否则一个人能冒另一个账号的 TOTP 和冷却)."""
    if not isinstance(accounts, dict):
        return False
    seen_oid, seen_login = set(), set()
    for acct, info in accounts.items():
        if not tc.valid_account(acct) or not isinstance(info, dict) or not set(info) <= _ACCOUNT_KEYS:
            return False
        oid, login, nodes = info.get("feishu"), info.get("ts_login"), info.get("ts_nodes", [])
        if oid is not None and (not isinstance(oid, str) or not _OPEN_ID_RE.fullmatch(oid) or oid in seen_oid):
            return False
        if login is not None:
            if not isinstance(login, str) or not _TS_LOGIN_RE.fullmatch(login) or login.lower() in seen_login:
                return False
            seen_login.add(login.lower())
        if not isinstance(nodes, list) or not all(isinstance(n, str) and _TS_NODE_RE.fullmatch(n) for n in nodes):
            return False
        if oid is not None:
            seen_oid.add(oid)
    return True


def _identity_ok(channel, req, info, ident):
    """网页: tailscale 登录名必须等于账号绑定的; 绑了设备列表时设备也要在里面.
    飞书: 账号必须绑了 open_id, by 必须正好是 feishu:<那个 open_id> (由飞书回调服务按事件里的 open_id 填)."""
    if channel == "web":
        login = info.get("ts_login")
        if not login or not ident["ts_login"] or ident["ts_login"].lower() != login.lower():
            return False
        nodes = info.get("ts_nodes") or []
        return not nodes or ident["ts_node"] in nodes
    bound = info.get("feishu")
    return bool(bound) and req["by"] == f"feishu:{bound}"


def decide(state, item, req, channel, seed_of, accounts, now, ts_login=None, ts_node=None):
    """一条已解析的请求 -> (结果, 新决定记录, 审计行). 不改输入.
    item 是取件时按原生状态现算的条目 (没了就 None); seed_of(账号) -> 种子字节或 None;
    accounts 是白名单 {账号: {"feishu": open_id}}. 结果见 _EV 的键. 这里只登记 authorized, 真正执行由下一阶段的执行器在原生锁里比对 digest."""
    st = prune(state, now)
    ident = {"ts_login": None, "ts_node": None}
    by = [None]                                     # 网页的 by 由服务端拼, 不用请求自报的

    def done(outcome, result=None, factor="none"):
        rec = audit(_EV[outcome], item, channel, now, result or outcome, by=by[0], factor=factor, req=req, **ident)
        return outcome, st, rec

    try:
        ident = ts_identity(ts_login, ts_node)
    except ValueError:
        return done("forbidden", "identity")
    if channel not in CHANNELS:
        return done("forbidden", "channel")
    if not valid_accounts(accounts):
        return done("forbidden", "accounts")
    acct = req["account"]
    if acct not in accounts:
        return done("forbidden", "account")
    if channel == "web":
        by[0] = "web:" + (ident["ts_login"] or "?") + (f"/{ident['ts_node']}" if ident["ts_node"] else "")
    if not _identity_ok(channel, req, accounts[acct], ident):
        return done("forbidden", "identity")
    if req["nonce"] in st["nonces"]:
        return done("replay", "nonce")
    st["nonces"][req["nonce"]] = now
    if item is None:
        return done("gone")
    if item["id"] != req["id"] or digest(item) != req["digest"]:
        return done("stale")
    if not now <= expires_of(item):                # 期限按 created 现算, NaN 也算过期
        return done("expired")
    def record(op):
        st["decisions"][item["id"]] = {"op": op, "account": acct, "channel": channel, "t": now,
                                       "digest": digest(item)}

    prev = st["decisions"].get(item["id"])
    if prev and prev["op"] == req["op"]:
        return done("already")
    if req["op"] == "reject":                   # 拒绝是"停"的方向, 不要因素; 执行前的拒绝覆盖批准
        record("reject")                        # 执行器 (下一阶段) 在原生锁里查这条, 看到 reject 就不发
        return done("rejected", "override" if prev else None)
    if prev:
        return done("conflict")                 # 先拒后批: 拒绝不能被翻
    tier = tier_of(item)                            # 不信条目里带的 tier, 现算
    if truncated(item):
        return done("forbidden", "truncated")
    need = requirement(tier, channel, item["kind"])
    if need == "forbid":
        return done("forbidden", "tier")
    a = st["accounts"].setdefault(acct, dict(tc.fresh_account(), last_hi=0.0))
    if tier >= L3 and now - a.get("last_hi", 0.0) < L3_GAP_S:
        return done("too_soon")
    factor = "none"
    if need == "totp":
        res, new = tc.verify(a, seed_of(acct), req["totp"], now)
        if res != "bad_state":
            st["accounts"][acct] = a = new
        if res != "ok":
            return done(_TOTP_OUT[res], factor=f"totp:{res}")
        factor = "totp:ok"
    if tier >= L3:
        a["last_hi"] = now
    record("approve")
    return done("authorized", factor=factor)


# ------------------------------------------------------------ 决定记录落盘

def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _valid_state(doc):
    """逐项校验: 任何一项读不懂都当整份读不懂 (调用方一律拒, 不擅自重置)."""
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), dict) for k in ("accounts", "nonces", "decisions")):
        return False
    for acct, a in doc["accounts"].items():
        if not tc.valid_account(acct) or not tc.valid_state(a) or not _finite(a.get("last_hi", 0.0)):
            return False
    if not all(_NONCE_RE.fullmatch(k) and _finite(t) for k, t in doc["nonces"].items()):
        return False
    return all(_ID_RE.fullmatch(k) and isinstance(d, dict) and d.get("op") in OPS and _finite(d.get("t"))
               for k, d in doc["decisions"].items())


class ApprovalStore:
    """/var/lib/ops-cart/approvals.json (root 0600, flock, 原子写). 放 /var/lib 是为了重启以后
    last_step、冷却、nonce 都还在. 读不懂不擅自重置 (否则 last_step 归零, 旧码能重放), 一律拒."""

    def __init__(self, path=APPROVALS, audit_path=None):
        self.path = path
        self.audit_path = audit_path or (AUDIT_LOG if path == APPROVALS else None)

    @contextlib.contextmanager
    def _lock(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        try:
            with open(self.path) as f:
                doc = json.load(f)
        except FileNotFoundError:
            return fresh_state()
        except (OSError, ValueError):
            raise StateUnreadable(self.path) from None
        if not _valid_state(doc):
            raise StateUnreadable(self.path)
        return doc

    def _audit(self, rec):
        if self.audit_path:
            append_audit(self.audit_path, rec)

    def decide(self, item, req, channel, seed_of, accounts, now=None, ts_login=None, ts_node=None):
        now = time.time() if now is None else now
        with self._lock():
            try:
                state = self._load()
            except StateUnreadable:
                try:
                    self._audit(audit("state_unreadable", item, channel, now, "state_unreadable", req=req))
                except OSError:
                    pass
                return "state_unreadable"
            out, st, rec = decide(state, item, req, channel, seed_of, accounts, now, ts_login, ts_node)
            try:
                self._audit(rec)                    # 先记审计; 记不上就不登记这次批准 / 拒绝 (没有审计的批准不算数)
            except OSError:
                # 但输错计数、冷却、nonce 照样落盘: 否则能让审计写失败 (写满日志盘) 来绕过 TOTP 退避
                # 拒绝是"停"的方向, 照样落盘; 批准不登记
                keep = prune(state, now)["decisions"]
                if out == "rejected":
                    keep[item["id"]] = st["decisions"][item["id"]]
                st = dict(st, decisions=keep)
                try:
                    atomicfile.write_json(self.path, st, mode=0o600)
                except OSError:
                    pass
                return "audit_fail"
            try:
                atomicfile.write_json(self.path, st, mode=0o600)
            except OSError:
                try:
                    self._audit(audit("commit_fail", item, channel, now, "commit_fail", req=req))
                except OSError:
                    pass
                return "commit_fail"                # 审计里那行 authorized 不算数, 以这行为准
            return out
