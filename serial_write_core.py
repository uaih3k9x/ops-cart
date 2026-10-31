"""串口写路径的纯逻辑 (#21): 行级命令校验、闸门第 1 层、写租约、挂起等人确认、急停、执行绪日志行. 不碰串口和文件.

谁用: mcp_serial (MCP 工具 + 车机命令行) 把状态存在 /run/ops-cart/serial-write.json, 每次操作
"加锁 -> 读 -> Book 里改 -> 存 -> 写日志"; 这里只管规则, 时间全由调用方传进来 (now = time.time()).

规矩 (fail-closed: 拿不准一律挂起等人确认, 或直接拒):
- 只收行级命令: 一行可打印 ASCII, 不许控制字符 / 换行 / 制表符 / 全角 / 零宽; 原始字节能拆碎、退格拼出 save, 不给.
  特殊键只给三个固定的: 回车、翻页空格 (直接放行), Ctrl+C (打断键, 要人确认).
- 闸门第 1 层: 认得出厂商、设备停在提示符上、命令过只读白名单 (survey_core.is_readonly, 规则文件只能加动词)
  才直接发; 带高危词 (save / reboot / reset / format / delete ...) 的永远要人确认, 不送模型.
- 闸门第 2 层 (#22): 认得出厂商、停在提示符上、不在白名单里、不带高危词的, 交调用方注入的 judge (决策模型,
  serial_gate_model) 判; 判放行才直发 (layer=2), 判拦 / 出错 / 超时 / 结果不对 / 没开一律挂起等人确认.
- 写租约: 每个槽位同一时间一个写入者, 认令牌不认名字; 有期限 (默认 60 秒), 要续期, 过期自动释放,
  它挂着的命令一起作废. 一条在途: 上一条没回来不发下一条.
- 人优先: 人在车机 / 网页上敲键不经租约; 人 3 秒内敲过键 Agent 的输入由 conbroker 排队; 人随时急停、确认、拒绝.
- 急停: 单条 (一个槽位) 或全部 (= 对每个槽位单条急停). 撤销租约 + 丢弃挂起的命令; 只停发送, 不往设备插任何字节.
  恢复只能由人解除 (mcp_serial 的命令行, 只认推车本机). 已经交给 conbroker 的那条在途命令收不回来, 照实记日志.
- 状态文件读不懂 (坏了 / 版本不对) = 全部急停.
"""

import json
import re
import secrets
import time

import survey_core

SLOTS = (1, 2, 3, 4)
LINE_MAX = 256
LEASE_TTL_S = 60            # 默认租约期限; Agent 每半个期限续一次
LEASE_TTL_MIN_S = 10
LEASE_TTL_MAX_S = 300
PENDING_MAX = 5             # 一个槽位最多挂几条等人确认
PENDING_MAX_S = 900         # 挂起超过 15 分钟没人理就作废 (现场早变了)
INFLIGHT_GRACE_S = 90       # 在途标记超过 发送超时 + 这么久还没回来 = 发送进程死了
HISTORY_MAX = 30            # 状态文件里留最近多少条结果给 pending_list 看 (完整的在执行绪日志里)
ECHO_MAX = 1000             # 日志里设备回显摘要的长度
STATE_VERSION = 1


class Refused(Exception):
    """不让做. code: bad (命令不合格) / estop / lease / busy / inflight / nope (没有这条或已经不在挂起)."""

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
_SPLIT = re.compile(r"[\s|;&<>()`$]+")         # Linux shell 的拼接 / 重定向 / 命令替换也当分隔
_QUOTE = re.compile(r"['\"\\]")                  # r'm' "reboot" \rm: 去掉引号反斜杠再切词


def danger_words(cmd, extra=()):
    """命令里出现的高危词 (规范写法, 按出现顺序, 不重复).
    第一个词 2 个字符起、后面的词 3 个字符起, 是高危词的前缀也算 (缩写); 往"多拦"的方向错."""
    words = tuple(DANGER) + tuple(w for w in extra if w not in DANGER)
    out = []
    for i, tok in enumerate(t for t in _SPLIT.split(_QUOTE.sub("", (cmd or "").lower())) if t):
        for w in words:
            if tok == w or (len(tok) >= (2 if i == 0 else 3) and w.startswith(tok)):
                if w not in out:
                    out.append(w)
    return out


# ------------------------------------------------------------ 规则文件

RULES_PATH = "/etc/ops-cart/serial-gate.json"
_VERB = re.compile(r"[a-z][a-z0-9-]{1,31}")
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
    danger: [...], model: {enabled, timeout_s, budget_usd, context_chars, model}}.

    只能收紧或加只读动词: 只读动词只能加给内置有只读规则的厂商 (survey_core: huawei / cisco); 锐捷 / Linux
    没有第 1 层白名单, 登记了槽位只走第 2 层或人确认; 高危词只加不减. vendors.<厂商>.model=false 关掉该厂商的
    第 2 层, notes 是给模型的补充说明. 文件不在 / 坏了 = 内置默认 (没有额外动词, 槽位不登记, 华为提示符自动认,
    模型不开)."""
    vendors = sorted(_PROMPT)
    out = {"slots": {}, "auto_huawei": True,
           "vendors": {v: {"extra_verbs": [], "model": True, "notes": ""} for v in vendors},
           "danger": list(DANGER), "model": dict(MODEL_DEFAULT)}
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
        verbs = [w for w in spec.get("extra_verbs") or () if isinstance(w, str) and _VERB.fullmatch(w)]
        if v in survey_core._RULES:
            out["vendors"][v]["extra_verbs"] = sorted(set(verbs))
        if isinstance(spec.get("model"), bool):
            out["vendors"][v]["model"] = spec["model"]
        if isinstance(spec.get("notes"), str):
            out["vendors"][v]["notes"] = spec["notes"][:NOTES_MAX]
        extra_danger += [w for w in spec.get("danger") or () if isinstance(w, str) and _VERB.fullmatch(w)]
    out["danger"] += sorted(set(extra_danger) - set(DANGER))
    out["model"] = _model_conf(doc.get("model"))
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


def last_line(text):
    """串口输出最后一行有内容的 (跳过 conbroker 的 [时间 Claude 断开] 这类标记)."""
    for ln in reversed((text or "").replace("\r", "").split("\n")):
        ln = ln.strip()
        if ln and not _MARK.fullmatch(ln):
            return ln
    return ""


def slot_vendor(slot, rules, text):
    """-> (厂商 或 None, 是否停在该厂商的提示符上).
    规则文件给槽位登记了厂商就用它; 没登记且开着自动认, 最后一行是华为 <名>/[名] 才算华为 (思科式 名# 和
    Linux 的 bash-4.4# 分不开, 只能登记). 停在密码 / [Y/N] / 翻页 / 半截输出上一律不算在提示符上."""
    ln = last_line(text)
    v = rules.get("slots", {}).get(slot)
    if v:
        return v, bool(_PROMPT[v].fullmatch(ln))
    if rules.get("auto_huawei") and _PROMPT["huawei"].fullmatch(ln):
        return "huawei", True
    return None, False


# ------------------------------------------------------------ 闸门

def _gate(verdict, reason, danger=(), vendor=None, layer=None, model=None):
    g = {"verdict": verdict, "layer": layer or (1 if verdict == "pass" else 3), "reason": reason,
         "danger": list(danger), "vendor": vendor}
    if model is not None:
        g["model"] = model          # 第 2 层的原始判断 / 出错说明, 随闸门结果整个进执行绪日志
    return g


_SHELL_META = re.compile(r"[;&<>`\\!]|\$\(")   # Linux: 拼接 / 后台 / 重定向 / 命令替换 / 续行 / 历史展开


def classify(cmd, vendor, at_prompt, rules, judge=None):
    """一行命令 -> 闸门结果 {verdict: pass|confirm, layer, reason, danger, vendor[, model]}. 调用方先 check_line.

    judge(cmd, vendor) -> {allow: bool, reason: 非空字符串, model: {...}}: 第 2 层决策模型 (serial_gate_model);
    None = 没开. 只有认得出厂商、停在提示符上、不在只读白名单里、不带高危词的才问它."""
    d = danger_words(cmd, rules.get("danger", ()))
    if d:
        return _gate("confirm", f"带高危词 {', '.join(d)}, 永远要人确认", d, vendor)
    if vendor is None:
        return _gate("confirm", "认不出设备厂商 (规则文件没登记这个槽位, 最后一行也不是华为 <名>/[名] 提示符), 要人确认")
    if not at_prompt:
        return _gate("confirm", "设备不在提示符上 (可能停在密码、[Y/N]、翻页或半截输出上), 要人确认", vendor=vendor)
    extra = rules.get("vendors", {}).get(vendor, {}).get("extra_verbs", ())
    if survey_core.is_readonly(cmd, vendor, extra_verbs=extra):
        return _gate("pass", f"{vendor} 只读命令 (第 1 层放行)", vendor=vendor)
    return _layer2(cmd, vendor, rules, judge)


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


def classify_key(key):
    k = KEYS.get(key)
    if k is None:
        raise Refused("bad", f"只给这几个键: {', '.join(KEYS)}")
    if k["confirm"]:
        return _gate("confirm", f"{k['label']} 是打断键, 要人确认")
    return _gate("pass", f"固定特殊键 {k['label']} (第 1 层放行)")


# ------------------------------------------------------------ 日志行

def stamp(wall):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


def echo_summary(text):
    """设备回显 -> 摘要: 太长的掐中间, 头尾都留 (头有回显的命令, 尾有提示符)."""
    s = (text or "").replace("\r", "")
    if len(s) <= ECHO_MAX:
        return s
    half = ECHO_MAX // 2
    return f"{s[:half]}\n…(省略 {len(s) - 2 * half} 字)…\n{s[-half:]}"


def log_line(rec):
    """一条记录一行 JSON (换行在 JSON 里转义, 不会断行)."""
    return json.dumps(rec, ensure_ascii=False, default=str)


# ------------------------------------------------------------ 状态: 租约 / 挂起 / 在途 / 急停

_PUBLIC = ("id", "slot", "kind", "cmd", "who", "t", "timeout", "gate", "reason", "danger", "status",
           "by", "why", "echo", "name")


def _public(item):
    return {k: item[k] for k in _PUBLIC if k in item}


def _slot_doc():
    return {"lease": None, "estop": None, "inflight": None}


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
                  and all(isinstance(doc["slots"].get(str(n)), dict) for n in SLOTS))
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
        if name:
            it["name"] = name
        self.doc["items"].append(it)
        return it

    def _start(self, it, now):
        it["status"] = "sending"
        self._s(it["slot"])["inflight"] = {"id": it["id"], "cmd": it["cmd"], "since": now,
                                           "deadline": now + it["timeout"] + INFLIGHT_GRACE_S}

    def _idle(self, slot):
        inf = self._s(slot)["inflight"]
        if inf:
            raise Refused("inflight", f"槽位 {slot} 上一条 ({inf['cmd']!r}) 还没回来, 一次一条")

    def begin(self, slot, token, who, kind, cmd, gate, now, timeout, name=None):
        """闸门放行的直接发: 标在途 -> 调用方交给 conbroker -> finish."""
        if gate.get("verdict") != "pass":
            raise ValueError("闸门没放行的只能 submit 挂起")
        self.writer(slot, token, now)
        self._idle(slot)
        it = self._new_item(slot, token, who, kind, cmd, gate, now, timeout, name, "sending")
        self._start(it, now)
        return _public(it)

    def submit(self, slot, token, who, kind, cmd, gate, now, timeout, name=None):
        """挂起等人确认."""
        self.writer(slot, token, now)
        if len(self._pending(slot)) >= PENDING_MAX:
            raise Refused("busy", f"槽位 {slot} 已经有 {PENDING_MAX} 条等人确认, 先等人处理")
        it = self._new_item(slot, token, who, kind, cmd, gate, now, timeout, name, "pending")
        self._ev("pending", slot, now, who=who, id=it["id"], kind=kind, cmd=cmd, gate=gate, name=name)
        return _public(it)

    def confirm(self, iid, by, now):
        """人确认 -> 标在途, 返回要发的那条 (调用方发完 finish). 租约没了 / 急停 / 已经不在挂起: 不发."""
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
        it["by"] = by
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
        it["status"] = result.get("status") or "error"
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
        want = (lambda it: True) if slot is None else (lambda it: it["slot"] == slot)
        pend = [dict(_public(it), age_s=int(now - it["ts"])) for it in self._pending() if want(it)]
        hist = [_public(it) for it in self.doc["items"] if it["status"] != "pending" and want(it)]
        return {"slots": slots, "pending": pend, "history": hist[-HISTORY_MAX:]}
