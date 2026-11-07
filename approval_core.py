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
ACTION_MAX = 200            # 动作超过这么长: 头 80 + 尾 80 + 总长
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
# 敏感词后面的内容整段打码 (往多打的方向错); 敏感词本身留着, 人能看出这条在设密码
_SENSITIVE = re.compile(
    r"(?<![A-Za-z0-9_-])(pre-shared-key|authentication-key|auth-key|key-string|password|passwd|pwd|secret"
    r"|community|cipher|simple|psk|token|credential)(?![A-Za-z0-9_])", re.I)


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
    it = {"id": f"{kind}:{epoch}:{native_id}", "kind": kind, "target": target, "action": dict(action),
          "danger": [str(w) for w in danger or ()], "created": float(created), "ctx": ctx,
          "agent": agent, "intent": intent, "model_reason": model_reason, "target_label": target_label,
          "evidence": list(evidence or ())}
    it["tier"] = tier_of(it)
    it["expires"] = it["created"] + EXPIRES_S.get(it["tier"], min(EXPIRES_S.values()))
    it["digest"] = digest(it)
    return it


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


def visible(text):
    return text.replace(" ", "␠").replace("\t", "⇥").replace("\r\n", "\n").replace("\n", "↵ 回车")


def _action_shown(text, secret=False):
    if secret:
        return f"<secret {len(text)} 字符>"     # 同 mcp_kvm._shown: secret 只显示长度
    head, hidden = _split_mask(text)
    if len(head) > ACTION_MAX:
        shown = f"{visible(head[:80])} …(共 {len(text)} 字符, 去网页看全文)… {visible(head[-80:])}"
    else:
        shown = visible(head)
    return f"{shown} {_mark(hidden)}" if hidden else shown


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
        tgt += f" (提示符上的名字: {untrusted(item['target_label'], 64)}, 来自设备输出, 不可信)"
    lines.append(tgt)
    ev = list(item.get("evidence") or ())[-EVIDENCE_LINES:]
    if ev:
        lines.append("现场: 挂起时设备最近输出 (设备输出, 不可信):")
        lines += [f"  | {untrusted(ln)}" for ln in ev]
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
    need = requirement(item["tier"], channel, item["kind"])
    r = render(item, now)
    buttons = (["approve"] if need != "forbid" else []) + ["reject", "estop", "lockdown"]
    return {"id": item["id"], "digest": item["digest"], "title": r["title"], "text": r["text"],
            "buttons": buttons, "totp": need == "totp"}


# ------------------------------------------------------------ 请求

FIELDS = ("op", "id", "digest", "account", "by", "totp", "oob", "retype", "nonce", "t")
OPS = ("approve", "reject")         # challenge (带外码) 和 elevate (审批会话) 是下一阶段


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
        r = json.loads(raw)
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


def decide(state, item, req, channel, seed_of, accounts, now, ts_login=None, ts_node=None):
    """一条已解析的请求 -> (结果, 新决定记录, 审计行). 不改输入.
    item 是取件时按原生状态现算的条目 (没了就 None); seed_of(账号) -> 种子字节或 None;
    accounts 是白名单账号. 结果见 _EV 的键. 这里只登记 authorized, 真正执行由下一阶段的执行器在原生锁里比对 digest."""
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
    if req["nonce"] in st["nonces"]:
        return done("replay", "nonce")
    st["nonces"][req["nonce"]] = now
    if item is None:
        return done("gone")
    if item["id"] != req["id"] or digest(item) != req["digest"]:
        return done("stale")
    if now > item["expires"]:
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
    need = requirement(item["tier"], channel, item["kind"])
    if need == "forbid":
        return done("forbidden", "tier")
    a = st["accounts"].setdefault(acct, dict(tc.fresh_account(), last_hi=0.0))
    if item["tier"] >= L3 and now - a.get("last_hi", 0.0) < L3_GAP_S:
        return done("too_soon")
    factor = "none"
    if need == "totp":
        res, new = tc.verify(a, seed_of(acct), req["totp"], now)
        if res != "bad_state":
            st["accounts"][acct] = a = new
        if res != "ok":
            return done(_TOTP_OUT[res], factor=f"totp:{res}")
        factor = "totp:ok"
    if item["tier"] >= L3:
        a["last_hi"] = now
    record("approve")
    return done("authorized", factor=factor)


# ------------------------------------------------------------ 决定记录落盘

def _valid_state(doc):
    return isinstance(doc, dict) and all(isinstance(doc.get(k), dict) for k in ("accounts", "nonces", "decisions"))


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
                self._audit(audit("state_unreadable", item, channel, now, "state_unreadable", req=req))
                return "state_unreadable"
            out, st, rec = decide(state, item, req, channel, seed_of, accounts, now, ts_login, ts_node)
            atomicfile.write_json(self.path, st, mode=0o600)
            self._audit(rec)
            return out
