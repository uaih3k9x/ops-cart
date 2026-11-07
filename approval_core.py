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
import totp_core as tc

L0, L1, L2, L3, L4 = 0, 1, 2, 3, 4
TIERS = (L0, L1, L2, L3, L4)
KINDS = ("serial", "kvm", "pxe", "estop_release", "label_batch")
CHANNELS = ("feishu", "web")

# 拍板 9-11: 按设计稿现有取值, 待维护者对稿确认
EXPIRES_S = {L2: 15 * 60, L3: 10 * 60, L4: 10 * 60}
AUTH_TTL_S = 120            # 批准以后多久内没开始执行就作废 (执行器是下一阶段, 先定常量)
KVM_L2_KEYS = frozenset({frozenset(("alt", "f4"))})

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
_BY_RE = re.compile(r"[\x21-\x7e]{1,64}")
_CODE_RE = re.compile(r"[0-9]{6}")
_TS_LOGIN_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}")
_TS_NODE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# 命中任一敏感词, 从第一个敏感词后面起整段打码 (往多打的方向错); 敏感词本身留着, 人能看出这条在设密码.
# 两边只把字母数字下划线当词的一部分: encrypted-password / -secret / shared-key 这类复合词也认.
_SENSITIVE = re.compile(
    r"(?<![A-Za-z0-9_])(password|passwd|pwd|passphrase|secret|community|cipher|simple|psk|key|token"
    r"|credential|md5|sha|sha1|sha256|sha384|sha512|auth|authentication|priv|privacy|encrypted|hash)"
    r"(?![A-Za-z0-9_])", re.I)
_BAD_CATS = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


class BadRequest(Exception):
    pass


class StateUnreadable(Exception):
    pass


# ------------------------------------------------------------ 审批请求模型

def tier_of(item):
    kind, act = item.get("kind"), item.get("action") or {}
    if kind == "serial":
        return L3 if item.get("danger") else L2
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
    ("pxe", None): {"purpose": _is_str, "macs": _is_list_of_str},
    ("estop_release", None): {"scope": _is_str},
    ("label_batch", None): {"count": lambda v: type(v) is int and v >= 2, "kind": _is_str},
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
          "danger": [str(w) for w in danger or ()], "created": created, "ctx": ctx,
          "agent": agent, "intent": intent, "model_reason": model_reason, "target_label": target_label,
          "evidence": list(evidence or ())}
    it["tier"] = tier_of(it)
    it["expires"] = expires_of(it)
    it["digest"] = digest(it)
    return it


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

def _split_mask(text):
    m = _SENSITIVE.search(text)
    if m and text[m.end():].strip():
        return text[:m.end()], len(text[m.end():].strip())
    return text, 0


def _mark(n):
    return f"<已打码 {n} 字符>"


def mask_command(text):
    """敏感词 (password / secret / community / cipher ...) 后面的内容整段换成长度. digest 仍按原文算."""
    head, hidden = _split_mask(text)
    return f"{head} {_mark(hidden)}" if hidden else head


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


def _action_shown(text, secret=False):
    if secret:
        return f"<secret {len(text)} 字符>"     # 同 mcp_kvm._shown: secret 只显示长度
    head, hidden = _split_mask(text)
    if len(head) > ACTION_MAX:
        shown = f"{visible(_escape(head[:80]))} …(共 {len(text)} 字符, 去网页看全文)… {visible(_escape(head[-80:]))}"
    else:
        shown = visible(_escape(head))
    return f"{shown} {_mark(hidden)}" if hidden else shown


def truncated(item):
    """动作原文有没有因为太长被截断显示. 截断了的条目不给批准, 只能拒 (人没看到全文)."""
    act = item.get("action") or {}
    if item.get("kind") not in ("serial", "kvm") or act.get("secret"):
        return False
    return any(len(_split_mask(str(act.get(k) or ""))[0]) > ACTION_MAX for k in ("text", "prefix"))


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
        else:
            out.append(ch)
    s = "".join(out)
    return s[:limit] + f"…(共 {len(s)} 字)" if len(s) > limit else s


def _untrusted_masked(text, limit=UNTRUSTED_MAX):
    """设备输出这类: 可能回显密码, 先打码再清洗."""
    return untrusted(mask_command(text) if isinstance(text, str) else text, limit)


# ------------------------------------------------------------ 摘要 / 卡片

def _dur(s):
    s = max(0, int(s))
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


def _title_action(item):
    kind, act, tgt = item["kind"], item["action"], untrusted(item["target"], 64)
    danger = [untrusted(w, 32) for w in item.get("danger") or ()]
    tier = f"[L{item['tier']}]"
    if kind == "serial":
        title = f"{tier} {tgt} 串口" + (f" · 高危词 {', '.join(danger)}" if danger else "")
        label = "按键" if act.get("type") == "key" else "动作"
        return title, [f"{label}: {_action_shown(str(act.get('text', '')))}"]
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
    lines.append(f"模型意见(不可信): {untrusted(mr)}" if mr else "模型意见: (没开)")
    if item.get("intent"):
        lines.append(f"Agent 自述(未核实, 不可信): {untrusted(item['intent'])}")
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


_OPEN_ID_RE = re.compile(r"ou_[A-Za-z0-9_-]{1,64}")


def _identity_ok(channel, req, accounts, acct, ident):
    """网页: by 是 web:..., 必须带 tailscale 登录名. 飞书: 账号必须绑了 open_id, by 必须正好是 feishu:<那个 open_id>."""
    if channel == "web":
        return req["by"].startswith("web:") and ident["ts_login"] is not None
    info = accounts.get(acct) if isinstance(accounts, dict) else None
    bound = info.get("feishu") if isinstance(info, dict) else None
    return isinstance(bound, str) and bool(_OPEN_ID_RE.fullmatch(bound)) and req["by"] == f"feishu:{bound}"


def decide(state, item, req, channel, seed_of, accounts, now, ts_login=None, ts_node=None):
    """一条已解析的请求 -> (结果, 新决定记录, 审计行). 不改输入.
    item 是取件时按原生状态现算的条目 (没了就 None); seed_of(账号) -> 种子字节或 None;
    accounts 是白名单 {账号: {"feishu": open_id}}. 结果见 _EV 的键. 这里只登记 authorized, 真正执行由下一阶段的执行器在原生锁里比对 digest."""
    st = prune(state, now)
    ident = {"ts_login": None, "ts_node": None}

    def done(outcome, result=None, factor="none"):
        rec = audit(_EV[outcome], item, channel, now, result or outcome, factor=factor, req=req, **ident)
        return outcome, st, rec

    try:
        ident = ts_identity(ts_login, ts_node)
    except ValueError:
        return done("forbidden", "identity")
    if channel not in CHANNELS:
        return done("forbidden", "channel")
    acct = req["account"]
    if acct not in accounts:
        return done("forbidden", "account")
    if not _identity_ok(channel, req, accounts, acct, ident):
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
    prev = st["decisions"].get(item["id"])
    if prev:
        return done("already" if prev["op"] == req["op"] else "conflict")

    def record(op):
        st["decisions"][item["id"]] = {"op": op, "account": acct, "channel": channel, "t": now,
                                       "digest": item["digest"]}

    if req["op"] == "reject":                   # 拒绝是"停"的方向, 不要因素
        record("reject")
        return done("rejected")
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
                self._audit(rec)                    # 先记审计; 记不上就不提交 (没有审计的批准不算数)
            except OSError:
                return "audit_fail"
            atomicfile.write_json(self.path, st, mode=0o600)
            return out
