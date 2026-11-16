"""大肥鱼面板的纯逻辑 (#53): 读大肥鱼导出的状态文件, 清洗成能上车机屏幕的样子; 急停信号和硬急停兜底.

大肥鱼 (推车上的 Agent, 普通用户 bigfish, rootless Docker) 和车机 (root) 之间只有两个文件, 接口 v1 最终版原文见
docs/BIGFISH_PANEL.md:
- 状态: /home/bigfish/status/bigfish.json, bigfish 写, 车机 / 副屏只读. 车机不进它的容器、不读它的数据卷.
- 急停: /run/bigfish-ctl/estop.json, 车机写 (root:bigfish 0640, seq 递增), 大肥鱼读; 解除全部急停时删掉.
  写完 HARD_AFTER_S 秒内状态文件没确认 (estop.seen; v1.1 还要 estop.seq 对上) 或者离线, 车机 loginctl
  terminate-user bigfish + systemctl stop user@<bigfish uid>.service. 只有车机入口写 estop.json, 终端急停不写.
  两个目录都由大肥鱼那边的 setup-root.sh 建, 本仓库不加 tmpfiles.
- 权限 (docs 有详细依据): estop.json 先 fchown 成 bigfish 组 + fchmod 0640 再改名, 不看 umask, bigfish 经属组可读
  (/run/bigfish-ctl 是 root:bigfish 0750). bigfish.json 由 root (kiosk / cart-dash, 单元无 ProtectHome) 读, 不靠
  /home/bigfish/status 的 0711 和文件的 0644. 解除急停删文件, 下次急停 seq 从 1 重来.

规矩:
- 状态文件是 bigfish 写的, 当不可信输入: 不跟链接、非阻塞打开 (FIFO 卡不住车机)、fstat 核对普通文件 / 属主 bigfish /
  不超过 MAX_BYTES, 任何一条不满足当没有数据.
- 可信字段 (wrapper 写的) 也做类型和取值白名单校验, 不合格的丢掉. untrusted 里的 (title / output_tail / steps)
  一律过 confirm_core.one_line (删 ANSI、控制 / 格式字符、压成一行、截短), 显示时前面加 SAYS. 锁屏一行只用可信字段.
- 不存在 / 读不懂 / 过期 = 不在线, 不抛.
"""

import json
import math
import os
import pwd
import grp
import stat
import time

import cartlog
import confirm_core as cfc

STATUS_PATH = "/home/bigfish/status/bigfish.json"
ESTOP_DIR = "/run/bigfish-ctl"
ESTOP_PATH = ESTOP_DIR + "/estop.json"
USER = "bigfish"
MAX_BYTES = 64 * 1024
STALE_S = 20                # 空闲也每 5 秒心跳; 20 秒没动 = 离线
FUTURE_S = 5
HARD_AFTER_S = 10           # 写了急停这么久大肥鱼还没确认 -> 硬停
TITLE_MAX, STEP_TITLE_MAX, STEP_STATUS_MAX, TRUSTED_STR_MAX = 200, 120, 20, 40
OUTPUT_TAIL = 2048
OUTPUT_LINES = 5
STEPS_MAX = 10
STEP_MAX = 10000            # steps_total / step_current 的合理上限
SAYS = "大肥鱼说："

TASK_STATES = {"starting": "启动中", "running": "在做事", "done": "做完了", "failed": "失败",
               "cancelled": "已取消", "estopped": "急停中"}
# stop_reason 只显示枚举 (ACP 的 stopReason + wrapper 自己的 estop / cancelled), 不认识的显示"其他", 不露原文
STOP_REASONS = {"estop": "急停", "cancelled": "已取消", "end_turn": "正常结束", "max_tokens": "超出长度",
                "max_turn_requests": "轮数到上限", "refusal": "模型拒绝"}
# v1.2: 健康和失败分类都只认枚举, 文字由车机这边写死, 不显示状态文件里的原始字符串. 不认识的值显示"其他".
# 健康: 枚举 -> (锁屏一行用的短句, 面板和发送提示用的原因 + 恢复办法). ok / unknown 不显示
HEALTH_QUIET = ("ok", "unknown")
HEALTH_TEXT = {
    "container_down": ("Agent 容器没起来", "Agent 容器没起来：可能是开机时缺少模型密钥，检查 bigfish-egress 服务"),
    "egress_down": ("模型出口没起来", "模型出口容器没起来，Agent 连不上模型：检查 bigfish-egress 服务"),
    "docker_down": ("Docker 没响应", "大肥鱼的 Docker 没响应：刚硬急停过要人工恢复，否则检查 bigfish 用户的 docker 服务"),
}
HEALTH_OTHER = ("运行环境其他异常", "大肥鱼运行环境报告了其他异常：任务可能失败，看 bigfish-supervisor 日志")
FAIL_TEXT = {
    "container_down": "没跑起来：Agent 容器不在，检查 bigfish-egress 服务",
    "docker_down": "没跑起来：大肥鱼的 Docker 没响应",
    "egress_down": "模型调用失败：模型出口容器不在，检查 bigfish-egress 服务",
    "acp_exit": "Agent 进程中途退出了，可以重发；反复出现看 bigfish-supervisor 日志",
    "acp_error": "Agent 启动时出错，可以重发；反复出现看 bigfish-supervisor 日志",
    "model_error": "模型调用失败：中转不通或今日额度用完，稍后再试",
    "timeout": "等 Agent 响应超时，稍后再试",
    "estop": "被急停打断",
}
FAIL_OTHER = "其他错误；看 bigfish-supervisor 日志"
BAD_SEQ = -1                # v1.1 estop.seq 给了但不合格: 当成和谁都对不上
SEQ_MAX = 2 ** 53
ELAPSED_MAX = 366 * 86400    # 已运行秒数的合理上限, 超过按读不懂
cartlog_event = cartlog.event


# ------------------------------------------------------------ 读文件

def bigfish_uid(user=USER):
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def bigfish_gid(group=USER):
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        return None


def read_status(path=None, uid=None):
    """-> 文件内容 (str) 或 None. 链接 / 非普通文件 / 属主不是 uid / 超过 MAX_BYTES / 没有 uid 都 None."""
    if uid is None:
        return None
    try:
        fd = os.open(path or STATUS_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_size > MAX_BYTES:
            return None
        return os.read(fd, MAX_BYTES + 1).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def load(now, path=None):
    """读 + 解析 (按用户名查 bigfish 的 uid)."""
    return parse(read_status(path, bigfish_uid()), now)


# ------------------------------------------------------------ 解析 / 清洗

def clean(text, limit=TITLE_MAX):
    """大肥鱼的话 -> 一行可显示的字 (不可信)."""
    return cfc.one_line(text if isinstance(text, str) else "", limit)


def _num(v, lo=None, hi=None):
    """合格的有限数原样返回, 否则 None. json.loads 认 NaN / Infinity / 1e999, 超大整数转不了 float."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        finite = math.isfinite(v)
    except OverflowError:
        return None
    return v if finite and (lo is None or v >= lo) and (hi is None or v <= hi) else None


def _int(v, hi=STEP_MAX):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None


def _offline(why):
    return {"online": False, "why": why, "estop_seen": False, "estop_seq": None, "current": None, "running_count": 0,
            "health": None, "said": {"title": "", "output": [], "steps": []}}


def fail_text(code):
    """失败分类 -> 中文. 没有这个字段 (旧 supervisor) 或不是字符串 -> "", 不认识的 -> FAIL_OTHER."""
    return FAIL_TEXT.get(code, FAIL_OTHER) if isinstance(code, str) and code else ""


def _health(v):
    """状态文件的 health -> 枚举; 不认识的 -> "other"; 没有 / 不是字符串 -> None (旧 supervisor)."""
    if not isinstance(v, str) or not v:
        return None
    return v if v in HEALTH_QUIET or v in HEALTH_TEXT else "other"


def health_text(code):
    """-> (短句, 原因 + 恢复办法); 不用显示时 None."""
    return HEALTH_OTHER if code == "other" else HEALTH_TEXT.get(code)


def _current(c):
    if not isinstance(c, dict) or not isinstance(c.get("state"), str) or c["state"] not in TASK_STATES:
        return None
    total = _int(c.get("steps_total"))             # 0 = 总步数未知
    cur = _int(c.get("step_current"), total or STEP_MAX) if total is not None else None
    reason = c.get("stop_reason")
    return {"state": c["state"], "started_at": _num(c.get("started_at")), "elapsed_s": _num(c.get("elapsed_s"), 0, ELAPSED_MAX),
            "steps_total": total, "step_current": cur,
            "stop_reason": STOP_REASONS.get(reason, "其他") if isinstance(reason, str) and reason else "",
            "source": clean(c.get("source"), TRUSTED_STR_MAX), "fail": fail_text(c.get("fail_code"))}


def _said(u):
    if not isinstance(u, dict):
        u = {}
    tail = u.get("output_tail")
    tail = tail[-OUTPUT_TAIL:] if isinstance(tail, str) else ""
    lines = [x for x in (clean(ln) for ln in tail.splitlines()) if x][-OUTPUT_LINES:]
    steps = u.get("steps") if isinstance(u.get("steps"), list) else []
    steps = [(clean(st.get("status"), STEP_STATUS_MAX), clean(st.get("title"), STEP_TITLE_MAX))
             for st in steps if isinstance(st, dict)][:STEPS_MAX]
    return {"title": clean(u.get("title")), "output": lines, "steps": [p for p in steps if p[1]]}


def parse(text, now):
    """状态文件内容 -> {online, why, estop_seen, current, running_count, said}. 不抛."""
    if text is None:
        return _offline("不在线")
    if len(text) > MAX_BYTES:
        return _offline("状态读不懂")
    try:
        d = json.loads(text)
    except ValueError:
        return _offline("状态读不懂")
    if not isinstance(d, dict) or d.get("schema") != 1 or _num(d.get("written_at")) is None:
        return _offline("状态读不懂")
    if now - d["written_at"] > STALE_S or d["written_at"] - now > FUTURE_S:
        return _offline("离线")
    es = d.get("estop") if isinstance(d.get("estop"), dict) else {}
    seq = None if "seq" not in es else (_int(es["seq"], SEQ_MAX) if _int(es["seq"], SEQ_MAX) is not None else BAD_SEQ)
    return {"online": True, "why": "", "estop_seen": es.get("seen") is True, "estop_seq": seq,   # seq: v1.1
            "current": _current(d.get("current")), "running_count": _int(d.get("running_count")) or 0,
            "health": _health(d.get("health")),     # v1.2
            "said": _said(d.get("untrusted"))}


# ------------------------------------------------------------ 显示

def _doing(view):
    """推车自己的一句 (只用可信字段): 空闲 / 在做事 · 第 2/5 步 / 急停中 ..."""
    cur = view["current"]
    if view["estop_seen"]:
        return "急停中"
    if cur is None:
        return "空闲"
    text = TASK_STATES[cur["state"]]
    if cur["state"] == "running" and cur["steps_total"] is not None and cur["step_current"] is not None:
        total = f"/{cur['steps_total']}" if cur["steps_total"] else ""        # 总数 0 = 未知, 只说第几步
        text += f" · 第 {cur['step_current']}{total} 步"
    return text


def lock_line(view):
    """锁屏 / 副屏一行, 不含大肥鱼的原文."""
    if not view["online"]:
        return "大肥鱼 · " + view["why"]
    cur, h = view["current"], health_text(view.get("health"))
    if h and not view["estop_seen"] and (cur is None or cur["state"] not in ("starting", "running")):
        return "大肥鱼 · " + h[0]
    return "大肥鱼 · " + _doing(view)


def elapsed(s):
    if _num(s, 0) is None:
        return ""
    s = int(s)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


def panel_rows(view, now, note="", ready=True):
    """面板上的行 -> [(文字, 标签)]. 标签: own / dim / bad / untrusted (大肥鱼说的, 带 SAYS 前缀).
    note: 车机这边急停的进展 (等确认 / 已硬停); ready=False: 急停通道目录不在."""
    top = [(note, "bad")] if note else [] if ready else [("急停通道未就绪", "bad")]
    if not view["online"]:
        return top + [("大肥鱼" + view["why"], "bad")]
    cur, said = view["current"], view["said"]
    h = health_text(view.get("health"))
    alert = [(h[1], "bad")] if h else []
    if cur is None and not view["estop_seen"]:
        return top + (alert or [("空闲", "dim")])
    head = _doing(view)
    if cur and cur["state"] == "running" and cur["elapsed_s"] is not None:
        head += " · " + elapsed(cur["elapsed_s"])
    if cur and cur["state"] in ("failed", "cancelled") and cur["stop_reason"]:
        head += " · " + cur["stop_reason"]
    rows = top + alert + [(head, "bad" if head == "急停中" or (cur and cur["state"] == "estopped") else "own")]
    if cur and cur["state"] == "failed" and cur["fail"]:
        rows.append((cur["fail"], "bad"))
    lines = [said["title"]] + [f"[{st}] {t}" if st else t for st, t in said["steps"]] + said["output"]
    return rows + [(SAYS + x, "untrusted") for x in lines if x]


# ------------------------------------------------------------ 确认 / 拒绝: 复用现有确认框

def pending_count(slot_states, kvm):
    return sum(s["pending"] for s in slot_states.values()) + ((kvm or {}).get("pending") or 0)


def pending_target(slot_states, kvm):
    """点"确认 / 拒绝"开哪个现有确认框: 第一个有挂起的串口槽位 -> ("serial", n); 否则键鼠 -> ("kvm", None); 都没有 None."""
    for n in sorted(slot_states):
        if slot_states[n]["pending"]:
            return "serial", n
    return ("kvm", None) if (kvm or {}).get("pending") else None


# ------------------------------------------------------------ 急停 (车机写, 大肥鱼读)

def channel_ready(d=None):
    return os.path.isdir(d or ESTOP_DIR)


def _old_seq(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        seq = json.loads(os.read(fd, 4096)).get("seq")
        return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else 0
    except (OSError, ValueError, AttributeError):
        return 0
    finally:
        os.close(fd)


def estop_write(now, by="kiosk", path=None, gid=None):
    """原子写 estop.json -> 新 seq (旧值 + 1, 没有 / 读不懂从 1 起). root:bigfish 0640: 临时文件先改好属组和权限再改名,
    大肥鱼不会看到半个文件或一个它读不了的文件. 目录不在抛 FileNotFoundError."""
    path = path or ESTOP_PATH
    seq = _old_seq(path) + 1
    gid = bigfish_gid() if gid is None else gid
    tmp = f"{path}.tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if gid is not None:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, 0o640)
        os.write(fd, json.dumps({"seq": seq, "at": now, "by": by}).encode())
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, path)
    return seq


def estop_clear(path=None):
    try:
        os.unlink(path or ESTOP_PATH)
    except FileNotFoundError:
        pass


class EstopWatch:
    """写了急停之后每秒看一眼状态文件: 确认了 -> "seen"; 到了 HARD_AFTER_S 还没 (或者离线) -> "hard".
    确认 = estop.seen 为 true; v1.1 起状态里有 estop.seq 时还要等于这次写的 seq (没有这个字段退回 v1)."""

    def __init__(self, t0, wait=HARD_AFTER_S, seq=None):
        self.t0, self.wait, self.seq = t0, wait, seq

    def step(self, now, view):
        echo = view.get("estop_seq")
        if view["online"] and view["estop_seen"] and (echo is None or self.seq is None or echo == self.seq):
            return "seen"
        return "hard" if now - self.t0 >= self.wait else None


def hard_stop(run, uid, why, event=None, user=USER):
    """硬急停: 先 loginctl terminate-user (结束会话), 再 systemctl stop user@<uid>.service (整个用户管理器,
    rootless dockerd 和容器跟着停), 都用列表参数; 记一条 cartlog. 成败看 systemctl 那条 (没有会话时
    terminate-user 会失败, 不算). -> (成功没有, 一句话). 恢复不归车机管."""
    event = event or cartlog_event
    if uid is None:
        msg = "找不到 bigfish 用户, 没法硬停"
        event("bigfish", f"硬急停失败: {msg} ({why})")
        return False, msg
    unit = f"user@{uid}.service"
    rc_t, _out = run(["loginctl", "terminate-user", user])
    rc, _out = run(["systemctl", "stop", unit])
    msg = f"loginctl terminate-user {user} rc={rc_t}; systemctl stop {unit} rc={rc}"
    event("bigfish", f"硬急停: {msg} ({why})")
    return rc == 0, msg


def estop_active(path=None):
    """estop.json 在不在 (在 = 大肥鱼还在拒任务). 面板据此显示"解除大肥鱼急停"."""
    return os.path.lexists(path or ESTOP_PATH)
