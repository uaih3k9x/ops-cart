"""车机给大肥鱼派任务的纯逻辑 (#99): 文字清洗、发送闸门、面板状态机 Desk、经 unix socket 调 supervisor.

只接派任务 / 看任务 / 取消三个工具 (TOOLS), 这个输入框没有任何批准动作; 大肥鱼要动手照旧走 MCP 闸门和现有确认框.

kiosk 是 root, socket 属于 bigfish, 所以连接前后都要核对对方是谁:
- 连接前用 O_PATH | O_NOFOLLOW 依次钉住目录和 socket 本身, 在 fd 上核对类型和属主, 再经 /proc/self/fd/<fd> 连这个 inode.
  目录归 bigfish 所有, 按名字连的话校验之后 mcp.sock 仍可被换成符号链接, 所以连的必须是校验过的 fd, 不是名字;
- 连接后用 SO_PEERCRED 核对对端 uid, 作兜底, 过不了就一个字节都不发. 拿不到 peer 凭据按连不上处理.

supervisor 的回复全部当不可信数据: 状态只认白名单, 文字一律过 bigfish_core.clean, 显示带 SAYS 前缀.
"""

import contextlib
import json
import os
import re
import socket
import stat
import struct
import time
import unicodedata

import bigfish_core as bf
import cartlog

PROMPT_MAX = 2000
CALL_S = 5.0
REPLY_MAX = 1 << 20
SOURCE = "车机·人"
OFFLINE = "大肥鱼不在线"
LOCKED = "车机已锁屏，解锁后才能发"
ESTOP = "急停中，不能发；解除急停后才能发"
TOOL_SUBMIT, TOOL_GET, TOOL_CANCEL = "bigfish_submit_task", "bigfish_get_task", "bigfish_cancel_task"
TOOLS = frozenset((TOOL_SUBMIT, TOOL_GET, TOOL_CANCEL))
MCP_VERSION = "2024-11-05"
STATES = {"starting": "启动中", "running": "在做事", "completed": "做完了", "failed": "失败", "cancelled": "已取消"}
ACTIVE = ("starting", "running")
_TASK_ID = re.compile(r"t-[0-9a-f]{1,32}")
_DROP = ("Cc", "Cf", "Cs")      # 控制 / 格式 / 孤立代理
_GONE = "no such task"          # supervisor 找不到任务时的开头; 只用来提前放弃, 不显示. 文案变了也有 LOST_AFTER 兜底
_ASKED = "已请求取消"
LOST_AFTER = 5                  # 连续这么多次查询被拒 (约 10 秒) 就不再跟踪; 连不上不算, 等它回来
_O_PATH = getattr(os, "O_PATH", os.O_RDONLY)    # 只有 Linux 有 O_PATH; 这里只用于打开目录, 别的系统也能走到校验


class Offline(Exception):
    """没连上或没等到可读的回复. sent: 请求可能已经发出去了 (重发前要先看状态)."""

    def __init__(self, why="", sent=False):
        super().__init__(why)
        self.sent = sent


class Refused(Exception):
    """supervisor 明确拒了 (急停中、并发满、没这个任务...), 文字是不可信数据."""


def valid_id(task_id):
    return isinstance(task_id, str) and _TASK_ID.fullmatch(task_id) is not None


def clean_prompt(text):
    """输入框的字 -> 要发的字: 空白 (含换行) 压成一个空格, 删控制和格式字符. 不截短, 超长由 Desk 拒收."""
    s = " ".join(str(text or "").split())
    return "".join(c for c in s if unicodedata.category(c) not in _DROP).strip()


def _held(fn):
    """锁屏 / 急停读不出来按"是"."""
    try:
        return bool(fn())
    except Exception:                       # noqa: BLE001
        return True


def send_block(locked, estop, view):
    """现在能不能发: 能发 "", 不能发返回给人看的原因. locked / estop 是可调用对象, 出错按不能发."""
    if _held(locked):
        return LOCKED
    if _held(estop) or view.get("estop_seen"):
        return ESTOP
    return "" if view.get("online") else OFFLINE


def parse_task(body):
    """bigfish_get_task 的回复 -> 能显示的摘要; 状态不在白名单或任务 ID 不对 -> None."""
    if not isinstance(body, dict) or body.get("state") not in STATES or not valid_id(body.get("task_id")):
        return None
    u = body.get("untrusted_agent_output")
    u = u if isinstance(u, dict) else {}
    output = u.get("output") if isinstance(u.get("output"), str) else ""
    reason = u.get("stop_reason")
    return {"task_id": body["task_id"], "state": body["state"], "elapsed_s": bf._num(body.get("elapsed_s"), 0),
            "total": bf._int(body.get("output_total_chars"), bf.SEQ_MAX) or 0, "output": output[-bf.OUTPUT_TAIL:],
            "steps": len(u["tools"]) if isinstance(u.get("tools"), list) else 0,
            "rejected": len(u["rejected_approvals"]) if isinstance(u.get("rejected_approvals"), list) else 0,
            "stop_reason": bf.STOP_REASONS.get(reason, "其他") if isinstance(reason, str) and reason else "",
            "error": bf.clean(u.get("error"))}


class Desk:
    """面板上"这一个任务"的状态 (主线程用, 不碰 Tk 和 socket); 放在 kiosk 上, 关掉面板再开不丢.
    一条消息一个新任务, 同一时间只跟一个: 前一个没结束不能发下一个."""

    def __init__(self):
        self.draft, self.msg, self.said = "", "", ""
        self.bad = self.sending = self.cancel_asked = False
        self.task_id, self.task = None, None
        self.cancelling = None              # 在途的取消是给哪个任务的
        self.refused = 0                    # 连续被拒的轮询次数
        self.gen = 0                        # 发送成功一次 +1: 面板据此清空输入框

    @property
    def active(self):
        return self.task is not None and self.task["state"] in ACTIVE

    def _say(self, msg, bad=False):
        self.msg, self.bad = msg, bad

    def begin(self, text, block):
        """按下发送: 返回要发的字; 发不了返回 None, 原因放 msg, 字留着."""
        self.draft = text
        prompt = clean_prompt(text)
        why = (block or ("正在发送" if self.sending else "") or ("上一个任务还没结束，可以先取消" if self.active else "")
               or ("先写点什么" if not prompt else "")
               or (f"太长了，最多 {PROMPT_MAX} 字" if len(prompt) > PROMPT_MAX else ""))
        if why:
            self._say(why, True)
            return None
        self.sending, self.said = True, ""
        self._say("正在发送…")
        return prompt

    def sent(self, res):
        self.sending = False
        if isinstance(res, Refused):
            self.said = bf.clean(str(res))
            self._say("大肥鱼没接这个任务，字还在", True)
        elif isinstance(res, Offline) and res.sent:
            self._say("没等到回复，可能已经发出去了；字还在，重发前先看任务状态", True)
        elif isinstance(res, Exception):
            self._say(f"{OFFLINE}，字还在", True)
        elif not isinstance(res, dict) or not valid_id(res.get("task_id")):
            self._say("大肥鱼的回复读不懂，可能已经发出去了；字还在", True)
        else:
            self.task_id, self.draft, self.cancel_asked, self.refused = res["task_id"], "", False, 0
            self.task = dict(parse_task({"task_id": self.task_id, "state": "starting"}))
            self.gen += 1
            self._say("已发出")

    def poll_args(self):
        """正在跟任务 -> (task_id, 从第几个字符读); 只读尾巴, 长任务不用每次搬全文."""
        return (self.task_id, max(0, self.task["total"] - bf.OUTPUT_TAIL)) if self.active else None

    def _gone(self, why):
        self.task_id, self.task = None, None
        self._say(why, True)

    def polled(self, res):
        if self.task_id is None:
            return
        if isinstance(res, Refused) and str(res).startswith(_GONE):
            self._gone("任务已不在（大肥鱼可能重启过）")
        elif isinstance(res, Exception):
            self.refused += not isinstance(res, Offline)
            if self.refused >= LOST_AFTER:
                self._gone("跟踪不到这个任务了，它可能还在跑，可以到别的入口看")
            else:
                self._say("暂时连不上大肥鱼，状态没更新", True)
        else:
            task = parse_task(res)
            if task is None:
                self._gone("任务状态读不懂，不再跟踪")
            elif task["task_id"] == self.task_id:
                self.task, self.refused = task, 0
                self._say(_ASKED if self.cancel_asked and self.active else "")

    def begin_cancel(self):
        if not self.active or self.cancelling:
            return None
        self.cancelling = self.task_id
        self._say("正在取消…")
        return self.task_id

    def cancelled(self, res):
        """取消的结果. 只更新提示, 放弃跟踪归轮询: 取消被拒不代表任务不在了."""
        target, self.cancelling = self.cancelling, None
        if target != self.task_id or not self.active:       # 在途时任务已结束或换了下一个, 结果和现在无关
            return
        if isinstance(res, Refused):
            self._say("大肥鱼没接受取消", True)
        elif isinstance(res, Exception):
            self._say(f"取消没发出去：{OFFLINE}", True)
        elif isinstance(res, dict) and res.get("cancel_requested") is True:
            self.cancel_asked = True
            self._say(_ASKED)
        else:
            self._say("任务可能已经结束，没有再取消")

    def rows(self):
        """面板上这个任务的行 -> [(文字, 标签)], 标签同 bigfish_core.panel_rows."""
        out = []
        t = self.task
        if t:
            head = f"{SOURCE}派的任务 · {STATES[t['state']]}"
            if t["elapsed_s"] is not None:
                head += " · " + bf.elapsed(t["elapsed_s"])
            if t["state"] in ("failed", "cancelled") and t["stop_reason"]:
                head += " · " + t["stop_reason"]
            out.append((head, "bad" if t["state"] == "failed" else "own"))
            if t["steps"]:
                out.append((f"调用工具 {t['steps']} 次", "dim"))
            if t["rejected"]:
                out.append((f"{t['rejected']} 个动作被自动拒绝", "bad"))
            lines = [x for x in (bf.clean(ln) for ln in t["output"].splitlines()) if x][-bf.OUTPUT_LINES:]
            out += [(bf.SAYS + x, "untrusted") for x in lines + ([t["error"]] if t["error"] else [])]
        if self.said:
            out.append((bf.SAYS + self.said, "untrusted"))
        return out


# ------------------------------------------------------------ socket

def sock_path(uid):
    return f"/run/user/{uid}/bigfish/mcp.sock"


def peer_uid(s):
    """对端 uid (Linux SO_PEERCRED); 别的系统没有这个常量, 抛 AttributeError, call 按连不上处理."""
    raw = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def _require(st, kind, uid):
    if not kind(st.st_mode) or st.st_uid != uid:
        raise Offline("socket 的类型或属主不对")


def _open_socket(dir_fd, path):
    """钉住 socket 本身 -> (fd, 它的 stat, connect 用的路径). 是符号链接时 fd 指向链接本身, 类型校验会拒.
    os.O_PATH 只有 Linux 有, 别的系统在这里抛 AttributeError, call 按连不上处理."""
    fd = os.open(os.path.basename(path), os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    return fd, os.fstat(fd), f"/proc/self/fd/{fd}"


@contextlib.contextmanager
def _pinned(path, uid):
    """校验目录和 socket, 产出只指向校验过的 inode 的 connect 路径; 退出时关掉 fd."""
    fd = None
    dir_fd = os.open(os.path.dirname(path), _O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _require(os.fstat(dir_fd), stat.S_ISDIR, uid)
        fd, st, target = _open_socket(dir_fd, path)
        _require(st, stat.S_ISSOCK, uid)
        yield target
    finally:
        os.close(dir_fd)
        if fd is not None:
            os.close(fd)


def _request(tool, args):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": MCP_VERSION, "capabilities": {},
                "clientInfo": {"name": "ops-cart-kiosk", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}]
    return "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8")


def _read_reply(s, deadline):
    """一行一个 JSON, 读到 id=2 那行为止; 总时长和单行长度都有上限, 对端慢慢滴字卡不住后台线程."""
    buf = b""
    while True:
        nl = buf.find(b"\n")
        if nl >= 0:
            line, buf = buf[:nl], buf[nl + 1:]
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise Offline("回复读不懂")
            if msg.get("id") == 2:
                return msg
            continue
        left = deadline - time.monotonic()
        if left <= 0 or len(buf) > REPLY_MAX:
            raise Offline("没等到完整回复")
        s.settimeout(left)
        chunk = s.recv(65536)
        if not chunk:
            raise Offline("连接断了")
        buf += chunk


def _result(msg):
    if "error" in msg:
        raise Refused(str(msg["error"]["message"]))
    res = msg["result"]
    text = res["content"][0]["text"]
    if res.get("isError"):
        raise Refused(str(text))
    body = json.loads(text)
    if not isinstance(body, dict):
        raise Offline("回复读不懂")
    return body


def call(tool, args, path=None, uid=None, peer=peer_uid, timeout=CALL_S):
    """调一个工具 -> 回复 dict. 连不上 / 属主不对 / 超时 / 读不懂 -> Offline; 对方拒绝 -> Refused. 在后台线程调."""
    if tool not in TOOLS:
        raise ValueError(f"车机不调这个工具: {tool}")
    uid = bf.bigfish_uid() if uid is None else uid
    if uid is None:
        raise Offline("没有 bigfish 用户")
    path = path or sock_path(uid)
    deadline, sent = time.monotonic() + timeout, False
    try:
        with _pinned(path, uid) as target, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(target)
            if peer(s) != uid:
                raise Offline("对端不是 bigfish")
            sent = True
            s.sendall(_request(tool, args))
            return _result(_read_reply(s, deadline))
    except Refused:
        raise
    except Offline as e:
        e.sent = sent
        raise
    except Exception as e:                  # noqa: BLE001  OSError、超时、回复结构不对都算没等到结果
        raise Offline(type(e).__name__, sent=sent) from e


# ------------------------------------------------------------ 三个动作 (后台线程); 日志只记来源和编号, 不记打的字

def submit(prompt, call=call, log=cartlog.event):
    try:
        body = call(TOOL_SUBMIT, {"prompt": prompt})
    except Exception as e:                  # noqa: BLE001  记一笔照样抛给面板
        log("bigfish", "车机派任务失败 " + cartlog.fields(source=SOURCE, chars=len(prompt), why=type(e).__name__))
        raise
    tid = body.get("task_id")
    log("bigfish", "车机派任务 " + cartlog.fields(source=SOURCE, task=tid if valid_id(tid) else "?", chars=len(prompt)))
    return body


def get_task(task_id, offset=0, call=call):
    return call(TOOL_GET, {"task_id": task_id, "output_offset": offset})


def cancel_task(task_id, call=call, log=cartlog.event):
    if not valid_id(task_id):
        raise ValueError("任务 ID 不对")
    res = call(TOOL_CANCEL, {"task_id": task_id})
    log("bigfish", "车机取消任务 " + cartlog.fields(source=SOURCE, task=task_id))
    return res
