"""pxe_req_core: Agent 经 MCP 申请 PXE 解锁的纯逻辑 (#24). 不碰文件、不起进程, 不调 pxe_arm.

规矩 (核心不变量: 没有人在车机本机照抄确认码, 任何 MCP 调用都不能让任何 MAC 拿到装机 / 克隆菜单):
- Agent 只能提申请: 推车按现有 clone_plan / 守卫算清单, 发一个随机确认码, 挂起. 确认码和清单摘要 (digest)
  只在状态文件 (root 0600) 和车机屏幕上, 不回给 Agent、不进日志.
- 车机上人照抄确认码 (take): 码不对原地换码; 界面上看的清单 (shown digest) 不是申请时那份不认;
  解锁前推车再算一遍清单, 和申请时的 digest 不一样就拒 (调用方做, 结果经 settle 记下).
- 申请 REQ_TTL_S 过期作废; 急停 (串口"全部急停") 作废所有未确认的申请, 急停中不收新申请.
- 状态读不懂 = 没有任何有效申请 (fail-closed), 不是"恢复旧申请".
- 提前上锁只收紧: retire_action 只会给出 disarm (整个上锁) / retire (删一台的菜单) / 什么都不做.
"""

import hashlib
import hmac
import json
import secrets
import time

import clone_core
from pxeserver import norm_mac

PURPOSES = ("rocky", "winpe") + clone_core.CLONE_PURPOSES
REQ_TTL_S = 10 * 60         # 申请多久没人确认就作废
MAX_PENDING = 3             # 同时挂着的申请上限 (别刷屏)
HISTORY_MAX = 20
CODE_DIGITS = 6
STATE_VERSION = 1
WHAT = {"rocky": "装 Rocky (自动安装, 清空整块硬盘)", "winpe": "进 WinPE (不自动装机)",
        "clone-save": "抓镜像 (整盘存到推车)", "clone-restore": "恢复镜像 (整盘覆盖)"}


class Refused(Exception):
    """不让做. code: bad (参数) / plan (清单算不出) / estop / dup / busy / nope (没有这条或已不在等) /
    code (确认码不对) / stale (界面上的清单旧了)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def stamp(wall):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


# ------------------------------------------------------------ 目标 / 清单

def target(purpose, mac, batch):
    """申请的目标 -> 规范化的 MAC (clone-restore 是 None = 按推车上的克隆清单). 不合格抛 Refused("bad").
    clone-restore 只认 batch=true 不给 mac; 其余只认一台 mac 不给 batch."""
    if purpose not in PURPOSES:
        raise Refused("bad", f"用途只能是 {'/'.join(PURPOSES)}, 给的是 {purpose!r}")
    if purpose == "clone-restore":
        if batch is not True or mac:
            raise Refused("bad", "clone-restore 按推车上的克隆清单来: 给 batch=true, 不给 mac")
        return None
    if batch or not isinstance(mac, str):
        raise Refused("bad", f"{purpose} 要给一台机器的 mac, 不给 batch")
    try:
        return norm_mac(mac)
    except ValueError as e:
        raise Refused("bad", str(e)) from None


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def simple_plan(purpose, mac, missing=()):
    """rocky / winpe 的"清单": 只有一台; 形状同 clone_core.plan. missing = winpe 缺的文件 (不齐就不能解锁)."""
    out = {"purpose": purpose, "error": None, "rows": [], "digest": None}
    if purpose == "winpe" and missing:
        out["error"] = f"WinPE 文件不齐, 缺 {', '.join(missing)}"
        return out
    out["rows"] = [{"mac": mac, "image": None, "device": None, "reason": None, "params": [purpose]}]
    out["digest"] = _digest([purpose, mac])
    return out


def plan_lines(p):
    """确认框里一台一行."""
    if p["purpose"] in clone_core.CLONE_PURPOSES:
        return clone_core.plan_lines(p)
    return [f"{r['mac']}  {WHAT[p['purpose']]}" for r in p["rows"]]


WARN = {"rocky": "批准后这台机器下次 PXE 会自动安装 Rocky 并清空整块硬盘 (clearpart --all)",
        "winpe": "批准后这台机器下次 PXE 菜单默认进 WinPE (不自动装机)",
        "clone-save": "批准后这台机器下次 PXE 会整盘抓镜像存到推车",
        "clone-restore": "批准后下面每台下次 PXE 都会被镜像整盘覆盖"}


def card(view):
    """车机申请框: 最早那条在等的申请 -> {id, digest, code, code_text, title, meta, warn, lines, more}; 没有 None.
    视图读不懂 (缺字段) 也当没有, 不给确认."""
    try:
        p, rest = view["pending"][0], len(view["pending"]) - 1
        left = int(p["expires_in"])
        return {"id": p["id"], "digest": p["digest"], "code": p["code"], "code_text": " ".join(p["code"]),
                "title": f"Agent 申请 PXE 解锁 #{p['id']}",
                "meta": f"来自 {p['who']} · {WHAT[p['purpose']]} · 剩 {left // 60}:{left % 60:02d}",
                "warn": WARN[p["purpose"]], "lines": list(p["lines"]), "more": rest}
    except (KeyError, TypeError, IndexError, ValueError):
        return None


def new_code():
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def code_ok(typed, code):
    """人敲的 (空格随便) 和确认码一样才算; 两边都要是 CODE_DIGITS 位数字. 定长比较."""
    if not isinstance(typed, str) or not isinstance(code, str) or len(code) != CODE_DIGITS:
        return False
    t = "".join(typed.split())
    return len(t) == CODE_DIGITS and t.isdigit() and hmac.compare_digest(t, code)


def retire_action(mode, mac):
    """Agent 提前上锁 -> "disarm" (整个上锁) / "retire" (一批里只删这台的菜单) / None (这台没解锁, 不用动).
    只收紧: 不给 mac 一律整个上锁; 给的 mac 是解锁的唯一一台也整个上锁."""
    if mac is None:
        return "disarm"
    if mode.get("mode") not in ("install", "expired"):
        return None
    macs = mode.get("macs") or [mode.get("mac")]
    if mac not in macs:
        return None
    return "retire" if len(macs) > 1 else "disarm"


# ------------------------------------------------------------ 申请 book

_PUBLIC = ("id", "who", "purpose", "mac", "batch", "macs", "lines", "status", "by", "why")
_SECRET = ("code", "digest")
_EV = {"expired": "expire", "dropped": "drop", "armed": "confirm", "rejected": "reject", "refused": "refused",
       "failed": "failed"}
_NEED = {"id": int, "who": str, "purpose": str, "batch": bool, "macs": list, "lines": list, "t": (int, float),
         "until": (int, float), "code": str, "digest": str}


def _fresh():
    return {"v": STATE_VERSION, "seq": 0, "reqs": [], "history": []}


def _sane(doc):
    if not (isinstance(doc, dict) and doc.get("v") == STATE_VERSION and isinstance(doc.get("seq"), int)
            and isinstance(doc.get("reqs"), list) and isinstance(doc.get("history"), list)):
        return False
    for r in doc["reqs"]:
        if not isinstance(r, dict) or r.get("purpose") not in PURPOSES:
            return False
        # bool 也是 int: 数字字段给 true 不认, batch 只认真的 bool
        if any(not isinstance(r.get(k), t) or isinstance(r.get(k), bool) != (t is bool) for k, t in _NEED.items()):
            return False
        if r.get("mac") is not None and not isinstance(r["mac"], str):
            return False
    return all(isinstance(h, dict) for h in doc["history"])


class Book:
    """一份可序列化的申请状态 + 规则. 改状态的方法先 tick 把过期 / 急停的清掉; 日志记录攒在 self.events,
    由调用方存盘后写日志. 调用方保证同一时间只有一个 Book 在改 (文件锁). code() 生成确认码 (测试里换掉)."""

    def __init__(self, doc, code=new_code):
        self.doc, self.code, self.events = doc, code, []

    @classmethod
    def from_text(cls, text, now, code=new_code):
        """状态文件内容 -> Book. 没有文件 = 全新; 读不懂 = 全新 + 一条 corrupt (没有任何有效申请)."""
        if not text:
            return cls(_fresh(), code)
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if _sane(doc):
            return cls(doc, code)
        b = cls(_fresh(), code)
        b._ev("corrupt", now, why="状态文件读不懂, 按没有任何申请算")
        return b

    def dumps(self):
        return json.dumps(self.doc, ensure_ascii=False, indent=1)

    # ---- 小工具

    def _ev(self, ev, now, **kw):
        rec = {"t": stamp(now), "ev": ev}
        rec.update({k: v for k, v in kw.items() if v is not None})
        self.events.append(rec)
        return rec

    def note(self, ev, now, **kw):
        """调用方补一条日志 (如提前上锁做了什么)."""
        return self._ev(ev, now, **kw)

    def _close(self, r, status, by, now, why=None):
        """挂起 -> 历史 (不带码和摘要)."""
        self.doc["reqs"].remove(r)
        h = {k: r[k] for k in _PUBLIC if k in r}
        h.update(status=status, by=by, why=why, t=stamp(r["t"]), t_end=stamp(now))
        self.doc["history"] = (self.doc["history"] + [h])[-HISTORY_MAX:]
        self._ev(_EV[status], now, id=r["id"], who=r["who"], by=by, purpose=r["purpose"], macs=r["macs"], why=why)
        return h

    def _find(self, iid):
        r = next((r for r in self.doc["reqs"] if r["id"] == iid), None)
        if r is None:
            raise Refused("nope", f"#{iid} 已经不在等了 (确认了 / 拒了 / 过期 / 急停作废)")
        return r

    def tick(self, now, stopped):
        """过期的作废; 急停 (stopped) 时所有挂起的作废."""
        for r in list(self.doc["reqs"]):
            if stopped:
                self._close(r, "dropped", "system", now, why="急停")
            elif now > r["until"]:
                self._close(r, "expired", "system", now, why=f"{REQ_TTL_S // 60} 分钟没人确认")

    # ---- Agent

    def submit(self, who, purpose, mac, batch, plan, now, stopped):
        """挂一条申请 -> 公开字段 (没有码和摘要). plan 是调用方按现有逻辑算的清单 (clone_plan / simple_plan)."""
        self.tick(now, stopped)
        if stopped:
            raise Refused("estop", "Agent 全部急停中, 不收 PXE 申请; 要人在推车上解除")
        if plan.get("purpose") != purpose or plan.get("error") or not plan.get("digest"):
            raise Refused("plan", f"不能解锁: {plan.get('error') or '清单算不出'}")
        dup = next((r for r in self.doc["reqs"] if r["purpose"] == purpose and r["digest"] == plan["digest"]), None)
        if dup is not None:
            raise Refused("dup", f"同样的申请 #{dup['id']} 还在等人确认")
        if len(self.doc["reqs"]) >= MAX_PENDING:
            raise Refused("busy", f"已经有 {MAX_PENDING} 条申请在等, 先等人处理或 pxe_retire 撤掉")
        self.doc["seq"] += 1
        r = {"id": self.doc["seq"], "who": who, "purpose": purpose, "mac": mac, "batch": bool(batch),
             "macs": [x["mac"] for x in plan["rows"] if x["params"]], "lines": plan_lines(plan),
             "t": now, "until": now + REQ_TTL_S, "code": self.code(), "digest": plan["digest"]}
        self.doc["reqs"].append(r)
        self._ev("request", now, id=r["id"], who=who, purpose=purpose, macs=r["macs"])
        return self._public(r, now)

    def drop(self, by, now, why, mac=None):
        """作废挂起的申请 (全部, 或涉及 mac 的) -> [编号]. 只收紧, 谁都能调."""
        out = []
        for r in list(self.doc["reqs"]):
            if mac is None or mac in r["macs"] or r.get("mac") == mac:
                self._close(r, "dropped", by, now, why=why)
                out.append(r["id"])
        return out

    # ---- 车机上的人

    def take(self, iid, typed, shown_digest, now, stopped):
        """人在车机上照抄确认码 -> 申请的完整内容 (含 digest, 调用方重算清单比对后解锁, 再 settle).
        码不对: 原地换码, 抛 Refused("code"); 界面上的清单不是这一份: 抛 Refused("stale"), 申请还挂着."""
        self.tick(now, stopped)
        r = self._find(iid)
        if not code_ok(typed, r["code"]):
            r["code"] = self.code()
            self._ev("bad_code", now, id=iid, who=r["who"])
            raise Refused("code", "确认码不对, 已换新码, 请照抄屏幕上的新码")
        if not isinstance(shown_digest, str) or not hmac.compare_digest(shown_digest, r["digest"]):
            raise Refused("stale", "界面上的清单不是这条申请的, 请看新清单再确认")
        return dict(r)

    def settle(self, iid, status, by, now, why=None):
        """take 之后的结果: armed (已解锁) / refused (清单变了、装机口正忙…) / failed (写菜单出错)."""
        return self._close(self._find(iid), status, by, now, why=why)

    def reject(self, iid, by, now, why=None):
        self.tick(now, False)
        return self._close(self._find(iid), "rejected", by, now, why=why)

    # ---- 看

    def _public(self, r, now, secret=False):
        d = {k: r[k] for k in _PUBLIC if k in r}
        d.update(status="pending", t=stamp(r["t"]), until=stamp(r["until"]),
                 expires_in=max(0, int(r["until"] - now)))
        if secret:
            d.update({k: r[k] for k in _SECRET})
        return d

    def view(self, now, stopped=False, secret=False):
        """{pending, history}. 不改状态 (过期 / 急停的在视图里算掉). secret=True 才带确认码和摘要 (只给车机)."""
        live = [] if stopped else [r for r in self.doc["reqs"] if now <= r["until"]]
        return {"pending": [self._public(r, now, secret) for r in live], "history": list(self.doc["history"])}
