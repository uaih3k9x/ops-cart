"""大肥鱼面板的纯逻辑 (#53): 读大肥鱼导出的状态文件, 清洗成能上车机屏幕的样子; 急停信号.

大肥鱼 (推车上的 Agent, 普通用户 bigfish, rootless Docker) 和车机 (root) 之间只有两个文件, 接口约定见
docs/BIGFISH_PANEL.md:
- 状态: /run/bigfish/status.json, bigfish 写 (白名单字段), 车机 / 副屏只读. 车机不进它的容器、不读它的数据卷.
- 急停: 复用现有的 /run/ops-cart/serial-estop.json (mcp_serial.Store 每次操作都重写, root 0644). 里面 "all" 为
  true = 全部急停 (车机 / 网页 / 命令行 / Agent 自停哪个入口都一样), 大肥鱼那边取消正在跑的任务、不接新任务;
  "解除全部急停"后变回 false. 推车这边不另写信号文件.
- 硬急停兜底: all 从 false 变 true 那一次开始计时, HARD_AFTER_S 秒内状态文件的 state 还不是 estopped (或者离线、
  过期), 车机 systemctl stop user@<bigfish uid>.service (rootless dockerd 和容器跟着停), 记 cartlog. 只停一次.

规矩:
- 状态文件是 bigfish 写的, 当不可信输入: 不跟链接、只认普通文件、属主必须是 bigfish、最多读 MAX_BYTES,
  非阻塞打开 (放个 FIFO 卡不住车机).
- 大肥鱼的话 (title / step / output) 一律过 confirm_core.one_line (删 ANSI、控制 / 格式字符、换行压成一行、截短),
  显示时前面加 SAYS, 和推车自己的字分开. 锁屏一行只用枚举字段, 不露原文.
- 不存在 / 读不懂 / 过期 = 不在线, 不抛.
"""

import json
import os
import pwd
import re
import stat

import cartlog

import confirm_core as cfc

STATUS_PATH = "/run/bigfish/status.json"
FLAG_PATH = "/run/ops-cart/serial-estop.json"   # = mcp_serial 的 STATE_PATH 同目录 + ESTOP_FLAG (测试钉住)
HARD_AFTER_S = 10
HARD_NOTE = "大肥鱼已硬停，需人工恢复"
USER = "bigfish"
MAX_BYTES = 64 * 1024
STALE_S = 30                # 写入方至少每 10 秒刷一次 updated; 超过 30 秒没动 = 过期
FUTURE_S = 5
TEXT_MAX = 200
OUTPUT_LINES = 5
SAYS = "大肥鱼说："

STATES = {"idle": "空闲", "running": "在做事", "estopped": "急停中"}
TASK_STATES = ("starting", "running", "completed", "cancelled", "failed")
FOCUS = re.compile(r"(con-[1-4]|kvm)\Z")
FOCUS_NAME = {"kvm": "键鼠"}      # 中文名前不加空格; con-N 前加


# ------------------------------------------------------------ 读文件

def bigfish_uid(user=USER):
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def read_status(path=STATUS_PATH, uid=None):
    """-> 文件内容 (str) 或 None. 链接 / 非普通文件 / 属主不是 uid / 没有 uid 都 None. 多读一个字节好让 parse 认出超长."""
    if uid is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
            return None
        return os.read(fd, MAX_BYTES + 1).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def load(now, path=None, uid=False):
    """读 + 解析. uid=False 时查 bigfish 用户."""
    return parse(read_status(path or STATUS_PATH, bigfish_uid() if uid is False else uid), now)


# ------------------------------------------------------------ 解析 / 清洗

def clean(text, limit=TEXT_MAX):
    """大肥鱼的话 -> 一行可显示的字 (不可信)."""
    return cfc.one_line(text if isinstance(text, str) else "", limit)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _offline(why):
    return {"online": False, "why": why, "state": None, "task": None}


def _task(t):
    if not isinstance(t, dict):
        return None
    out = t.get("output")
    lines = [clean(x) for x in out if isinstance(x, str)][-OUTPUT_LINES:] if isinstance(out, list) else []
    focus = t.get("focus")
    return {"state": t.get("state") if t.get("state") in TASK_STATES else None,
            "started": _num(t.get("started")),
            "title": clean(t.get("title")), "step": clean(t.get("step")),
            "focus": focus if isinstance(focus, str) and FOCUS.match(focus) else None,
            "output": [x for x in lines if x]}


def parse(text, now):
    """状态文件内容 -> {online, why, state, task}. 不抛."""
    if text is None:
        return _offline("不在线")
    if len(text) > MAX_BYTES:
        return _offline("状态读不懂")
    try:
        d = json.loads(text)
    except ValueError:
        return _offline("状态读不懂")
    if not isinstance(d, dict) or d.get("v") != 1 or d.get("state") not in STATES or _num(d.get("updated")) is None:
        return _offline("状态读不懂")
    if now - d["updated"] > STALE_S or d["updated"] - now > FUTURE_S:
        return _offline("状态过期")
    return {"online": True, "why": "", "state": d["state"], "task": _task(d.get("task"))}


# ------------------------------------------------------------ 显示

def _doing(view):
    """推车自己的一句 (只用枚举字段): 空闲 / 在做事 / 在看 con-1 / 急停中."""
    task = view["task"]
    if view["state"] == "running" and task and task["focus"]:
        return "在看" + FOCUS_NAME.get(task["focus"], " " + task["focus"])
    return STATES[view["state"]]


def lock_line(view):
    """锁屏 / 副屏一行, 不含大肥鱼的原文."""
    return "大肥鱼 · " + (_doing(view) if view["online"] else view["why"])


def elapsed(s):
    if s is None or s < 0:
        return ""
    s = int(s)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


def panel_rows(view, now, note=""):
    """面板上的行 -> [(文字, 标签)]. 标签: own (推车自己的) / dim / bad / untrusted (大肥鱼说的, 带 SAYS 前缀).
    note: 车机这边急停兜底的进展, 放第一行."""
    top = [(note, "bad")] if note else []
    if not view["online"]:
        return top + [("大肥鱼" + view["why"], "bad")]
    task = view["task"]
    if view["state"] == "idle" and not task:
        return top + [("空闲", "dim")]
    head = _doing(view)
    if task and task["started"] is not None and view["state"] == "running":
        head += " · " + elapsed(now - task["started"])
    rows = top + [(head, "bad" if view["state"] == "estopped" else "own")]
    if task:
        rows += [(SAYS + x, "untrusted") for x in (task["title"], task["step"], *task["output"]) if x]
    return rows


# ------------------------------------------------------------ 确认 / 拒绝: 复用现有确认框

def pending_count(slot_states, kvm):
    return sum(s["pending"] for s in slot_states.values()) + ((kvm or {}).get("pending") or 0)


def pending_target(slot_states, kvm):
    """点"确认 / 拒绝"开哪个现有确认框: 第一个有挂起的串口槽位 -> ("serial", n); 否则键鼠 -> ("kvm", None); 都没有 None."""
    for n in sorted(slot_states):
        if slot_states[n]["pending"]:
            return "serial", n
    return ("kvm", None) if (kvm or {}).get("pending") else None


# ------------------------------------------------------------ 硬急停兜底

def flag_all(path=None):
    """serial-estop.json 的 all 是不是 true. 没文件 / 读不懂都算 false: 车机只在真急停时计时 (大肥鱼那边读不懂按急停)."""
    try:
        with open(path or FLAG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("all") is True


class HardStopGuard:
    """车机每秒喂一次 (all, 状态视图). phase: idle -> waiting (all 刚变 true) -> seen (大肥鱼报 estopped) / hard (超时).
    step 只在该硬停的那一刻返回 "hard", 同一次急停里只一次. hard 一直留着 (面板上要看得见),
    直到急停解除且大肥鱼重新在线 (有人恢复了它)."""

    NOTES = {"idle": "", "waiting": "等大肥鱼确认急停…", "seen": "大肥鱼已急停", "hard": HARD_NOTE}

    def __init__(self, wait=HARD_AFTER_S):
        self.wait, self.phase, self.t0, self._prev = wait, "idle", None, False

    def step(self, now, all_on, view):
        rising, self._prev = all_on and not self._prev, all_on
        if rising:
            self.phase, self.t0 = "waiting", now
        if self.phase == "waiting":
            if not all_on:
                self.phase = "idle"
            elif view["online"] and view["state"] == "estopped":
                self.phase = "seen"
            elif now - self.t0 >= self.wait:
                self.phase = "hard"
                return "hard"
        elif self.phase == "seen" and not all_on:
            self.phase = "idle"
        elif self.phase == "hard" and not all_on and view["online"]:
            self.phase = "idle"
        return None

    def note(self):
        return self.NOTES[self.phase]


def hard_stop(run, uid, why, event=None):
    """systemctl stop user@<uid>.service, 记一条 cartlog -> (成功没有, 一句话). 恢复不归车机管."""
    unit = f"user@{uid}.service"
    rc, _out = run(["systemctl", "stop", unit])
    (event or cartlog.event)("bigfish", f"硬急停: systemctl stop {unit} rc={rc} ({why})")
    return rc == 0, f"systemctl stop {unit} rc={rc}"
