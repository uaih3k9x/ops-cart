"""键鼠代理 (kvm_broker) 的纯逻辑: 请求解析与拆步、人 / Agent 仲裁、急停、日志行. 不碰串口和 socket (#9).

协议: 本地 Unix socket, 一行一个 JSON 请求, 一行一个 JSON 应答.
  请求 {"op": ..., "src": "human"|"agent", "who": "kiosk", "port": "/dev/serial/by-id/...", 参数...}
  应答 {"ok": true, ...} / {"ok": false, "err": 代码, "msg": 中文说明}

仲裁 (同 conbroker 的规矩, 按每块键鼠分开算):
- 来源只分 human / agent; 没写 src 一律当 agent, 不能白拿人的优先级.
- 人的输入不排队, 来了就插到最前面.
- 这块键鼠上人最近 quiet_s 秒内有输入, agent 在这块上的输入暂停 (已发的不撤回, 剩下的等人停手再接着发);
  等太久 (wait_max_s 没进展) 就丢掉, 回 timeout.
- 急停: 丢弃 agent 还没发的步骤, 之后 agent 新请求一律拒, 直到人明确 resume.
  只停发送, 不往设备插任何按键 (不补 Ctrl+C、不补松开) —— 每一步本身就是"按下+抬起"成对的,
  正在发的那一步发完为止, 所以急停也不会把键卡在按下.
- 租约钩子 lease(who, dev, now) -> bool: 默认放行; #21 接写租约, 提交时和每一步前都问.
- 读信息 / 打开设备不算输入: 谁发都不排队、不受急停管, 也不算人在操作.
"""

import json
import re
import time
from collections import deque

import ch9329

QUIET_S = 3.0               # 人停手多久 agent 才能接着发, 同 conbroker HUMAN_QUIET
AGENT_WAIT_MAX_S = 60.0     # agent 一个请求最多干等多久 (人一直在操作)
HOLD_MAX_MS = 10000         # 按住一个键最多多久; 按住期间这块键鼠别人都插不进来
PRESS_MS = 30               # 默认按下到抬起的间隔, 同 ch9329.Ch9329.send_keys
TYPE_MAX = 4096             # 一次打字最多几个字符, 同网页口
WHO_MAX = 32
LINE_MAX = 64 * 1024        # 一行请求的上限

DEVICE_OPS = {"open", "info", "keys", "type", "report", "mouse", "mouse_abs", "release"}
CONTROL_OPS = {"estop", "resume", "status"}
AGENT_OPS = {"open", "info", "keys", "type"}        # agent 只给整键和整行, 不给原始报告 / 鼠标 (#3)
PASSIVE_OPS = {"open", "info"}                      # 不发键: 不排队, 不受急停 / 租约管

_PORT = re.compile(r"/dev/tty(ACM|USB)[0-9]+")
_WHO_BAD = re.compile(r"[^A-Za-z0-9_.:@-]")
ZERO = bytes(8)


class BadRequest(ValueError):
    """请求格式不对 / 参数非法. 应答 err=bad."""


class Refused(Exception):
    """仲裁拒绝 (急停中 / 租约无效). code 进应答的 err."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def port_ok(real):
    """realpath 之后只认 CH9329 那种 USB 串口节点, 别的一律不开 (代理以 root 跑)."""
    return bool(_PORT.fullmatch(real or ""))


def _int(r, k, default=0):
    v = r.get(k, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadRequest(f"{k} 必须是整数")
    return v


def parse_request(line):
    """一行 JSON -> 规整过的请求 dict. 坏的抛 BadRequest."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    try:
        r = json.loads(line)
    except ValueError:
        raise BadRequest("不是 JSON") from None
    if not isinstance(r, dict):
        raise BadRequest("请求必须是对象")
    op = r.get("op")
    if op not in DEVICE_OPS | CONTROL_OPS:
        raise BadRequest(f"不认识的 op: {op!r}")
    src = r.get("src", "agent")
    if src not in ("human", "agent"):
        raise BadRequest("src 只能是 human / agent")
    who = _WHO_BAD.sub("_", str(r.get("who") or ""))[:WHO_MAX] or "?"
    out = dict(r, op=op, src=src, who=who, secret=bool(r.get("secret")))
    if op in DEVICE_OPS:
        if not isinstance(r.get("port"), str) or not r["port"]:
            raise BadRequest("要指明哪块键鼠 (port)")
        if src == "agent" and op not in AGENT_OPS:
            raise BadRequest(f"agent 不能发 {op} (只给整键和整行)")
    elif "port" in r and not isinstance(r["port"], str):
        raise BadRequest("port 必须是字符串")
    return out


def _press(r):
    ms = _int(r, "press_ms", PRESS_MS)
    if not 0 <= ms <= HOLD_MAX_MS:
        raise BadRequest(f"press_ms 要在 0~{HOLD_MAX_MS}")
    return ms / 1000.0


def _key_unit(names, press_s):
    try:
        down, unknown = ch9329.key_report(names)
    except ValueError as e:
        raise BadRequest(str(e)) from None
    if unknown:
        raise BadRequest(f"不认识的键: {unknown}")
    wait = (("wait", press_s),) if press_s else ()
    return (("report", down),) + wait + (("report", ZERO),)


def plan(r):
    """请求 -> 步骤列表. 一步 = 一串原子发送 (中间不让别人插); 人能在两步之间插进来.
    动作: ("open",) ("info",) ("report", 8字节) ("wait", 秒) ("mouse", b,dx,dy,w) ("mouse_abs", b,x,y,w)."""
    op = r["op"]
    if op in ("open", "info"):
        return [((op,),)]
    if op == "keys":
        keys = r.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise BadRequest("keys 要是非空字符串列表")
        return [_key_unit(keys, _press(r))]
    if op == "type":
        text = r.get("text")
        if not isinstance(text, str) or not text:
            raise BadRequest("text 要是非空字符串")
        if len(text) > TYPE_MAX:
            raise BadRequest(f"一次最多 {TYPE_MAX} 个字符")
        bad = ch9329.untypable(text)
        if bad:
            raise BadRequest(f"无法输入的字符: {''.join(bad)!r}")        # 有打不出的就一个都不发
        press = _press(r)
        return [_key_unit(["enter"] if c == "\n" else [c], press) for c in text]
    if op == "report":
        try:
            rep = bytes.fromhex(str(r.get("report", "")))
        except ValueError:
            raise BadRequest("report 要是 16 位十六进制") from None
        if len(rep) != 8:
            raise BadRequest("report 要是 8 字节")
        return [(("report", rep),)]
    if op == "mouse":
        b = _int(r, "b") & 0x07
        return [tuple(("mouse", b, dx, dy, w)
                      for dx, dy, w in ch9329.mouse_chunks(_int(r, "dx"), _int(r, "dy"), _int(r, "w")))]
    if op == "mouse_abs":
        x, y = (max(0, min(4095, _int(r, k))) for k in ("x", "y"))
        return [(("mouse_abs", _int(r, "b") & 0x07, x, y, max(-127, min(127, _int(r, "w")))),)]
    if op == "release":                 # 全部松开; 给了 x/y (绝对模式) 光标留原地
        if "x" in r and "y" in r:
            return [(("report", ZERO), ("mouse_abs", 0, _int(r, "x"), _int(r, "y"), 0))]
        return [(("report", ZERO), ("mouse", 0, 0, 0, 0))]
    raise BadRequest(f"{op} 不发键")


def is_activity(r):
    """这个请求算不算"有人在操作". 全部松开 / 零位移不算: 车机空闲放锁前那次松开不该再挡 agent 3 秒."""
    op = r["op"]
    if op in PASSIVE_OPS or op == "release":
        return False
    if op == "report":
        return str(r.get("report", "")).strip("0") != ""
    if op == "mouse":
        return any(r.get(k) for k in ("b", "dx", "dy", "w"))
    return True


class Job:
    """一个请求的执行状态. state: queued -> running -> done / dropped (why 说原因)."""

    _seq = 0

    def __init__(self, req, units, now, dev=None):
        Job._seq += 1
        self.id, self.req, self.units = Job._seq, req, list(units)
        self.op, self.src, self.who = req["op"], req["src"], req["who"]
        self.dev = dev or req.get("port")
        self.i = 0                      # 已发完几步
        self.t_last = now               # 上次有进展 (提交 / 发完一步)
        self.state, self.why, self.reply = "queued", None, None
        self.passive = self.op in PASSIVE_OPS
        self.active = is_activity(req)

    @property
    def priority(self):
        """人和不发键的请求走优先队列, 不等人停手."""
        return self.src == "human" or self.passive


class Arbiter:
    """按键鼠 (dev) 分开的两条队列: 优先 (人 + 不发键的) / agent. 不碰线程: 调用方自己加锁."""

    ALL = "*"

    def __init__(self, quiet_s=QUIET_S, wait_max_s=AGENT_WAIT_MAX_S, lease=None):
        self.quiet_s, self.wait_max_s = quiet_s, wait_max_s
        self.lease = lease or (lambda who, dev, now: True)
        self.prio, self.agent = {}, {}          # dev -> deque[Job]
        self.last_human = {}                    # dev -> 人最后一次有效输入的时刻
        self.last_who = {}                      # dev -> 最后一个真发出去的来源
        self.stopped = set()                    # 急停中的 dev; 含 ALL = 全部

    # ---- 状态

    def is_stopped(self, dev):
        return self.ALL in self.stopped or dev in self.stopped

    def human_active(self, dev, now):
        t = self.last_human.get(dev)
        return t is not None and now - t < self.quiet_s

    # ---- 提交 / 取步 / 回报

    def submit(self, job, now):
        dev = job.dev
        if job.priority:
            if job.src == "human" and job.active:
                self.last_human[dev] = now
            self.prio.setdefault(dev, deque()).append(job)
            return
        if self.is_stopped(dev):
            raise Refused("estop", "急停中, agent 不能发键; 要人在推车上解除")
        if not self.lease(job.who, dev, now):
            raise Refused("lease", "没有这块键鼠的写租约")
        self.agent.setdefault(dev, deque()).append(job)

    def take(self, dev, now):
        """下一步该发什么 -> (job, 步骤) 或 None. 人的先发; agent 要人停手够久、没急停、租约还在."""
        q = self.prio.get(dev)
        if q:
            j = q[0]
            j.state = "running"
            return j, j.units[j.i]
        q = self.agent.get(dev)
        if not q or self.human_active(dev, now):
            return None
        j = q[0]
        if not self.lease(j.who, dev, now):
            self.fail(j, "lease")
            return self.take(dev, now)
        j.state = "running"
        return j, j.units[j.i]

    def done(self, job, now, reply=None):
        """一步发完 -> 这个作业是否结束 (发完 / 期间被急停丢了)."""
        if job.state == "dropped":
            return True
        job.i += 1
        job.t_last = now
        if reply is not None:
            job.reply = reply
        if not job.passive:
            self.last_who[job.dev] = job.who
        if job.i < len(job.units):
            return False
        job.state = "done"
        self._remove(job)
        return True

    def fail(self, job, why):
        job.state, job.why = "dropped", why
        self._remove(job)

    def _remove(self, job):
        for qs in (self.prio, self.agent):
            q = qs.get(job.dev)
            if q and job in q:
                q.remove(job)

    def expire(self, now):
        """agent 干等太久的丢掉 -> 丢了哪些."""
        out = []
        for q in self.agent.values():
            for j in list(q):
                if now - j.t_last > self.wait_max_s:
                    self.fail(j, "timeout")
                    out.append(j)
        return out

    def wait_hint(self, dev, now):
        """发送线程该睡多久再来问: 0 = 马上有活; None = 没活, 等新请求."""
        if self.prio.get(dev):
            return 0.0
        if not self.agent.get(dev):
            return None
        t = self.last_human.get(dev)
        return 0.0 if t is None else max(0.0, self.quiet_s - (now - t))

    # ---- 急停

    def estop(self, dev=None):
        """dev=None 全部. 丢掉 agent 还没发的 -> 丢了哪些. 不往设备发任何东西."""
        self.stopped.add(self.ALL if dev is None else dev)
        out = []
        for d, q in self.agent.items():
            if dev is None or d == dev:
                for j in list(q):
                    self.fail(j, "estop")
                    out.append(j)
        return out

    def resume(self, dev=None):
        """dev=None 全部解除; 单独解一块解不了"全部急停"."""
        if dev is None:
            self.stopped.clear()
        else:
            self.stopped.discard(dev)

    def status(self, now):
        devs = set(self.prio) | set(self.agent) | set(self.last_human) | set(self.last_who)
        devs |= self.stopped - {self.ALL}
        return {d: {"human": self.human_active(d, now), "queued": len(self.agent.get(d, ())),
                    "stopped": self.is_stopped(d), "last": self.last_who.get(d)} for d in sorted(devs)}


class HumanBursts:
    """人的输入按"一阵"记日志 (开始 / 结束 + 次数), 不逐键记: 一来鼠标每秒几十帧, 二来别把人的密码记进日志."""

    def __init__(self, quiet_s=QUIET_S):
        self.quiet_s = quiet_s
        self._b = {}                    # (dev, who) -> [最后时刻, 次数]

    def note(self, dev, who, now):
        """记一次 -> 是否新一阵的开始."""
        b = self._b.get((dev, who))
        if b is None:
            self._b[(dev, who)] = [now, 1]
            return True
        b[0], b[1] = now, b[1] + 1
        return False

    def ended(self, now):
        """停手够 quiet_s 的 -> [(dev, who, 次数)], 并清掉."""
        out = [(d, w, b[1]) for (d, w), b in self._b.items() if now - b[0] >= self.quiet_s]
        for d, w, _n in out:
            del self._b[(d, w)]
        return out


def stamp(wall):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


def record(ev, job=None, wall=None, **kw):
    """一条日志记录. ev: sent / dropped / human_start / human_end / estop / resume / ...
    agent 打的字原样记 (留档要"发了什么"), secret 的只记 ******; 人的输入不记内容."""
    rec = {"t": stamp(time.time() if wall is None else wall), "ev": ev}
    if job is not None:
        rec.update(src=job.src, who=job.who, dev=(job.dev or "").rsplit("/", 1)[-1], op=job.op)
        r = job.req
        if job.op == "type":
            rec["text"] = "******" if r.get("secret") else r["text"]
            rec["n"] = len(r["text"])
        elif job.op == "keys":
            rec["keys"] = "+".join(r["keys"])
            if r.get("press_ms") not in (None, PRESS_MS):
                rec["press_ms"] = r["press_ms"]
        if job.state == "dropped":
            rec["why"], rec["sent"] = job.why, job.i
    rec.update(kw)
    return rec


def log_line(rec):
    """一条记录一行 JSON (换行在 JSON 里转义, 不会断行)."""
    return json.dumps(rec, ensure_ascii=False)
