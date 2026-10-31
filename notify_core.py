#!/usr/bin/env python3
"""notify_core: 推车出了要紧的事推到手机 (issue #16) —— 读现有状态文件, 和上次比, 只在变化时出事件.

状态源 (只读, 不和 cart-dash 抢采集):
  /run/ops-cart/state.json             cart-dash 的快照, 取 UPS (OB/OL、电量、低电量阈值)
  /run/ops-cart/pxe-guard.json         装机口守卫 serving / expired (限时到停发) / proxy / 停 (no-link、no-ip)
  /run/ops-cart/campus-auth-status.json 校园网认证结果 (ok 字段, 见 ruijie_portal.OK_STATES)
  systemctl is-active                  pxe-server / kiosk / cart-dash
  boot_id + cart-boot-success          上一次启动有没有走到 boot-success (开机 60 s kiosk 还活着)

每个监视项一个值, None = 这次读不到, 不算变化. diff 的规则 (RULES: 稳定秒数, 冷却秒数):
  - 新值要连续保持 settle 秒才算数; 没稳住就回到原值 = 抖了一下, 只推一条"短暂…已回到…"
  - 推过一条后 cooldown 秒内的变化先压着, 冷却结束时仍和上次推的不同才推最终状态, 附带变过几次
上次状态、冷却记录、没发出去的消息都在 /var/lib/ops-cart/notify.json (原子写).
"""

import copy
import json

import atomicfile
import ups_core

STATE_PATH = "/var/lib/ops-cart/notify.json"
DASH_STATE = "/run/ops-cart/state.json"
GUARD_STATE = "/run/ops-cart/pxe-guard.json"
AUTH_STATUS = "/run/ops-cart/campus-auth-status.json"
SERVICES = ("pxe-server", "kiosk", "cart-dash")
STATE_STALE_S = 90          # cart-dash 快照这么久没更新, UPS 当读不到 (cart-dash 挂了另有服务事件)

# 监视项 -> (settle 秒, cooldown 秒). timer 每 30 s 跑一次: settle 20 = 第二次看到还这样才推.
# UPS 断电、低电量要立刻知道, 不等稳定; 认证 2 分钟才跑一次, 等下一次结果确认.
RULES = {
    "ups.power": (0, 300),
    "ups.low": (0, 1800),
    "guard": (20, 300),
    "auth": (100, 600),
    "svc": (20, 300),
}
# 第一次见到时当作"上次是这样"的正常值; 没列出的 (守卫) 第一次见到就是基线, 不推
NORMAL = {"ups.power": "OL", "ups.low": False, "auth": "ok", "svc": "up"}

_GUARD = {"serving": "serving", "expired": "expired", "proxy": "proxy", "no-link": "stop", "no-ip": "stop"}
_GUARD_WHY = {"no-link": "没插线", "no-ip": "网口上没有装机地址"}
_LABEL = {
    "ups.power": {"OB": "电池供电", "OL": "市电"},
    "guard": {"serving": "自己发地址", "expired": "限时到停发", "proxy": "代理模式", "stop": "停发"},
    "auth": {"ok": "在线", "fail": "认证失败"},
    "svc": {"up": "运行", "down": "停了"},
}
_SUBJECT = {"ups.power": "推车供电", "guard": "装机口守卫", "auth": "校园网认证"}


def _rule(key):
    return RULES[key.split(".")[0] if key.startswith("svc.") else key]


def _kind(key):
    return "svc" if key.startswith("svc.") else key


def _one_line(s):
    return " ".join(str(s).split())


# ---------------------------------------------------------------- 采集

def _pull(read, key):
    f = read.get(key)
    if f is None:
        return None
    try:
        return f()
    except Exception:            # noqa: BLE001  单项坏了当读不到, 不拖垮别的
        return None


def collect(read):
    """读取函数 dict -> {"values": {监视项: 值或 None}, "info": 写消息用的细节, "boot": {id, ok}}. 不抛."""
    now = _pull(read, "now")
    snap = _pull(read, "state")
    guard = _pull(read, "guard")
    auth = _pull(read, "auth")
    svcs = _pull(read, "services")
    boot = _pull(read, "boot")
    values, info = {}, {}

    ups = snap.get("ups") if isinstance(snap, dict) else None
    ts = snap.get("ts") if isinstance(snap, dict) else None
    if ups and isinstance(ts, (int, float)) and now is not None and now - ts > STATE_STALE_S:
        ups = None
    if isinstance(ups, dict) and isinstance(ups.get("percent"), (int, float)):
        values["ups.power"] = "OB" if ups_core.on_battery(ups) else "OL"
        values["ups.low"] = bool(ups_core.low(ups))
        info["pct"] = int(ups["percent"])
    else:
        values["ups.power"] = values["ups.low"] = None

    svcs = svcs if isinstance(svcs, dict) else {}
    for name in SERVICES:
        raw = svcs.get(name)
        info[f"svc_state.{name}"] = raw
        if raw == "active":
            values[f"svc.{name}"] = "up"
        elif raw in ("inactive", "failed", "deactivating"):
            values[f"svc.{name}"] = "down"
        else:                       # activating (正在重启)、读不到
            values[f"svc.{name}"] = None

    g = guard.get("state") if isinstance(guard, dict) else None
    values["guard"] = _GUARD.get(g) if values.get("svc.pxe-server") == "up" else None
    info["guard_state"] = g

    if isinstance(auth, dict) and auth.get("state"):
        values["auth"] = "ok" if auth.get("ok") else "fail"
        info["auth_state"] = str(auth["state"])
        info["auth_msg"] = str(auth.get("message") or "")
    else:
        values["auth"] = None

    boot = boot if isinstance(boot, dict) else {}
    return {"values": values, "info": info, "boot": {"id": boot.get("id"), "ok": boot.get("ok")}}


# ---------------------------------------------------------------- 消息: 一行, 先写要做什么/出了什么

def _text(key, old, new, info):
    """监视项从 old 变成 new 的消息; None = 这种变化不推."""
    pct = info.get("pct")
    if key == "ups.power":
        return f"推车转电池供电 {pct}%，市电断了" if new == "OB" else f"推车市电恢复，电池 {pct}%"
    if key == "ups.low":
        return f"推车电池只剩 {pct}%，到关机阈值 {ups_core.LOW_PCT}%，赶紧接市电" if new else None
    if key == "guard":
        if new == "proxy":
            return "装机口转代理模式：线上有别的 DHCP，推车不发地址"
        if new == "serving":
            return "装机口恢复自己发地址（给整个网段发，限时，车机上有倒计时）"
        if new == "expired":
            return "装机口自己发地址到时，已停发：新机器拿不到地址，要继续去车机上点续期"
        return f"装机口停发：{_GUARD_WHY.get(info.get('guard_state'), '守卫没在服务')}"
    if key == "auth":
        st = info.get("auth_state", "")
        if new == "ok":
            return f"校园网认证恢复（{st}）"
        if st == "untrusted":       # message 是认证页主机, 不往外推
            return "校园网认证页换了主机，去无线面板确认要不要信任"
        msg = _one_line(info.get("auth_msg", ""))[:40]
        return f"校园网认证失败（{st}），去无线面板看看" + (f"：{msg}" if msg else "")
    name = key[4:]
    if new == "down":
        return f"推车服务 {name} 挂了（{info.get('svc_state.' + name)}），看 journalctl -u {name}"
    return f"推车服务 {name} 恢复运行"


def _flap_text(key, was, back):
    lab = _LABEL[_kind(key)]
    subject = _SUBJECT.get(key) or f"推车服务 {key[4:]}"
    return f"{subject}短暂{lab.get(was, was)}，已回到{lab.get(back, back)}"


def _prio(key, new, flap=False):
    """ntfy 优先级 1-5."""
    if flap:
        return 2
    if key == "ups.low":
        return 5
    if new in ("OB", "down") or key == "boot":
        return 4
    return 3


def _event(key, text, prio, now):
    return {"key": key, "text": _one_line(text), "prio": prio, "ts": now}


# ---------------------------------------------------------------- diff

def diff(prev, cur, now):
    """(上次的状态记录, collect() 结果, 现在) -> (事件列表, 新的状态记录). 纯函数, 不改 prev."""
    st = copy.deepcopy(prev) if isinstance(prev, dict) else {}
    keys = st.setdefault("keys", {})
    events = []
    for key, v in cur["values"].items():
        if v is None:
            continue
        settle, cool = _rule(key)
        s = keys.get(key)
        if s is None:
            s = keys[key] = {"sent": NORMAL.get(_kind(key), v), "last": 0, "pend": None, "since": 0, "held": 0}
        cooling = now - s["last"] < cool
        if v == s["sent"]:
            if s["pend"] is not None:           # 没稳住就回来了: 抖了一下
                if cooling:
                    s["held"] += 1
                else:
                    events.append(_event(key, _flap_text(key, s["pend"], v), _prio(key, v, True), now))
                    s["last"], s["held"] = now, 0
                s["pend"] = None
            elif not cooling:
                s["held"] = 0
            continue
        if s["pend"] != v:
            s["pend"], s["since"] = v, now
            if cooling:
                s["held"] += 1
        if now - s["since"] < settle or cooling:
            continue
        text = _text(key, s["sent"], v, cur["info"])
        if text:
            if s["held"] > 1:
                text += f"（冷却期内还变过 {s['held'] - 1} 次）"
            events.append(_event(key, text, _prio(key, v), now))
            s["last"] = now
        s["sent"], s["pend"], s["held"] = v, None, 0

    events += _boot(st, cur.get("boot") or {}, now)
    return events, st


def _boot(st, boot, now):
    """换了 boot id 且上一次启动明确没走到 boot-success -> 一条事件. ok 一次为真就一直为真."""
    bid, ok = boot.get("id"), boot.get("ok")
    if not bid:
        return []
    old = st.get("boot") or {}
    out = []
    if old.get("id") and old["id"] != bid and old.get("ok") is False:
        out.append(_event("boot", "推车刚才非正常重启：上一次启动没走到 boot-success，看 journalctl -b -1",
                          _prio("boot", None), now))
    if old.get("id") == bid:
        ok = True if old.get("ok") is True or ok is True else (old.get("ok") if ok is None else ok)
    st["boot"] = {"id": bid, "ok": ok}
    return out


# ---------------------------------------------------------------- 持久化

def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def save_state(path, st):
    atomicfile.write_json(path, st)


# ---------------------------------------------------------------- 配置 / 发送器
# /etc/ops-cart/notify.json (root 0600, 里面可能有令牌), 示例 examples/notify.example.json:
#   {"kind": "ntfy", "url": "https://push.example.invalid/<topic>", "token": "", "title": "推车",
#    "priority": true, "timeout": 10}
# 没有这个文件 / enabled=false = 只写 events.log, 一个包都不往外发.

CONF_PATH = "/etc/ops-cart/notify.json"
KINDS = ("ntfy", "feishu-webhook", "feishu-app")
FEISHU_API = "https://open.feishu.cn/open-apis"
MAX_OUTBOX = 20             # 网断太久时最多攒这么多条 (同一监视项只留最后一条)
LATE_S = 60                 # 比事件晚这么久才发出去的, 前面标 [补发 时:分]


def load_conf(path=CONF_PATH):
    """-> 配置 dict; 没文件或 enabled=false 返回 None; 写错了抛 ValueError (不猜, 不发)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    try:
        conf = json.loads(text)
    except ValueError:
        raise ValueError(f"{path} 不是合法 JSON") from None
    if not isinstance(conf, dict):
        raise ValueError(f"{path} 应该是一个对象")
    if conf.get("enabled") is False:
        return None
    if conf.get("kind") not in KINDS:
        raise ValueError(f"{path}: kind 只支持 {', '.join(KINDS)}")
    if conf["kind"] == "feishu-app":
        missing = [k for k in ("app_id", "app_secret", "chat_id") if not str(conf.get(k) or "").strip()]
        if missing:
            raise ValueError(f"{path}: 飞书应用要写 {', '.join(missing)}")
        return conf
    if not str(conf.get("url") or "").startswith(("https://", "http://")):
        raise ValueError(f"{path}: url 要是 http(s) 地址")
    if conf["kind"] == "feishu-webhook" and not conf["url"].startswith("https://"):
        raise ValueError(f"{path}: 飞书 webhook 只走 https")
    return conf


def ntfy_sender(conf, opener=None):
    """ntfy 兼容的 HTTP POST: 正文 = 消息, Title / Priority / Authorization 头可选. 失败抛 OSError."""
    import urllib.request
    from email.header import Header
    opener = opener or urllib.request.urlopen
    timeout = float(conf.get("timeout") or 10)

    def send(ev):
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if conf.get("title"):
            headers["Title"] = Header(str(conf["title"]), "utf-8").encode()    # 中文按 RFC 2047
        if conf.get("priority", True):
            headers["Priority"] = str(int(ev.get("prio") or 3))
        if conf.get("token"):
            headers["Authorization"] = f"Bearer {conf['token']}"
        req = urllib.request.Request(conf["url"], data=ev["text"].encode("utf-8"), headers=headers,
                                     method="POST")
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if not 200 <= status < 300:
                raise OSError(f"推送服务回 HTTP {status}")
    return send


def feishu_sign(secret, ts):
    """飞书群自定义机器人的签名: "时间戳\n密钥" 当 HMAC-SHA256 的密钥, 消息为空, 再 Base64."""
    import base64
    import hashlib
    import hmac
    key = f"{int(ts)}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def feishu_sender(conf, opener=None, clock=None):
    """飞书群自定义机器人 webhook: 只能往那一个群发, 不需要应用凭据. 配了 secret 就带签名.
    飞书出错也回 HTTP 200, 要看正文里的 code (StatusCode 是旧格式). 失败抛 OSError."""
    import time
    import urllib.request
    opener = opener or urllib.request.urlopen
    clock = clock or time.time
    timeout = float(conf.get("timeout") or 10)

    def send(ev):
        text = ev["text"]
        if conf.get("title"):
            text = f"【{conf['title']}】{text}"
        body = {"msg_type": "text", "content": {"text": text}}
        if conf.get("secret"):
            ts = int(clock())
            body.update(timestamp=str(ts), sign=feishu_sign(conf["secret"], ts))
        req = urllib.request.Request(conf["url"], data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
        if not 200 <= status < 300:
            raise OSError(f"飞书回 HTTP {status}")
        try:
            r = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise OSError(f"飞书回的不是 JSON: {raw[:80]!r}") from None
        code = r.get("code", r.get("StatusCode"))
        if code != 0:
            raise OSError(f"飞书回 code={code} {r.get('msg') or r.get('StatusMessage') or ''}".strip())
    return send


def _feishu_post(opener, url, body, timeout, token=None):
    """飞书开放平台 POST -> 正文 dict; code != 0 (HTTP 200 也算) 抛 OSError, 不带请求内容 (里面可能有密钥)."""
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as ex:            # 权限不够等回 4xx, 正文里照样有 code/msg
        raw = ex.read()
    try:
        r = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise OSError(f"飞书回的不是 JSON: {raw[:80]!r}") from None
    if r.get("code") != 0:
        raise OSError(f"飞书回 code={r.get('code')} {str(r.get('msg') or '')[:120]}".strip())
    return r


def feishu_app_sender(conf, opener=None, clock=None):
    """推车专用飞书应用 (只开发消息): app_id/app_secret 换 tenant_access_token, 往 chat_id 那个群发文本.
    token 2 小时有效, 同一进程里留 5 分钟余量复用. 失败抛 OSError."""
    import time
    import urllib.request
    opener = opener or urllib.request.urlopen
    clock = clock or time.time
    timeout = float(conf.get("timeout") or 10)
    cache = {"tok": None, "until": 0.0}

    def token():
        now = clock()
        if not cache["tok"] or now >= cache["until"]:
            r = _feishu_post(opener, FEISHU_API + "/auth/v3/tenant_access_token/internal",
                             {"app_id": conf["app_id"], "app_secret": conf["app_secret"]}, timeout)
            cache["tok"], cache["until"] = r["tenant_access_token"], now + int(r.get("expire") or 7200) - 300
        return cache["tok"]

    def send(ev):
        text = ev["text"]
        if conf.get("title"):
            text = f"【{conf['title']}】{text}"
        _feishu_post(opener, FEISHU_API + "/im/v1/messages?receive_id_type=chat_id",
                     {"receive_id": conf["chat_id"], "msg_type": "text",
                      "content": json.dumps({"text": text}, ensure_ascii=False)}, timeout, token())
    return send


def make_sender(conf, opener=None, clock=None):
    """按 kind 选发送器."""
    if conf.get("kind") == "feishu-app":
        return feishu_app_sender(conf, opener, clock)
    if conf.get("kind") == "feishu-webhook":
        return feishu_sender(conf, opener, clock)
    return ntfy_sender(conf, opener)


def queue(outbox, events):
    """新事件进发件箱: 同一监视项只留最后一条 (补推最后状态), 超过上限丢最老的."""
    box = [e for e in (outbox or []) if e.get("key") not in {n["key"] for n in events}]
    return (box + list(events))[-MAX_OUTBOX:]


def deliver(outbox, send, now):
    """按顺序发, 碰到第一条失败就停 (网断了后面的也发不出去, 别每条都等超时).
    -> (没发出去的, 失败原因或 None)."""
    import time
    for i, e in enumerate(outbox):
        text = e["text"]
        if now - e.get("ts", now) > LATE_S:
            text = f"[补发 {time.strftime('%H:%M', time.localtime(e['ts']))}] {text}"
        try:
            send(dict(e, text=text))
        except Exception as ex:          # noqa: BLE001  urllib 各种错, 一律留着下次重试
            return list(outbox[i:]), _one_line(f"{type(ex).__name__}: {ex}")[:120]
    return [], None


# ---------------------------------------------------------------- 跑一轮 (timer 每 30 s 一次)

def _cart_event(kind, msg):
    import cartlog
    return cartlog.event(kind, msg)


def run_once(read, state_path=STATE_PATH, conf_path=CONF_PATH, now=None, opener=None, log=_cart_event):
    """采集 -> diff -> 记 events.log -> (配置了才) 发. 先落盘再发: 发到一半断电也不丢事件."""
    import time
    now = time.time() if now is None else now
    prev = load_state(state_path)
    events, st = diff(prev, collect(read), now)
    for e in events:
        log("notify", e["text"])

    try:
        conf, err = load_conf(conf_path), None
    except ValueError as ex:
        conf, err = None, str(ex)
    if err != st.get("conf_err"):
        if err:
            log("notify", f"推送配置 notify.json 有问题, 只记日志不外发: {err}")
        st["conf_err"] = err
    if conf is None:
        st["outbox"], st["failing"] = [], None
        save_state(state_path, st)
        return {"events": len(events), "sent": 0, "left": 0}

    box = queue(st.get("outbox"), events)
    st["outbox"] = box
    save_state(state_path, st)
    left, why = deliver(box, make_sender(conf, opener), now) if box else ([], None)
    if why and not st.get("failing"):
        log("notify", f"推送失败, 留着下次重试 ({len(left)} 条): {why}")
    elif not why and st.get("failing"):
        log("notify", f"推送恢复, 补发了 {len(box)} 条")
    st["outbox"], st["failing"] = left, (why or None)
    save_state(state_path, st)
    return {"events": len(events), "sent": len(box) - len(left), "left": len(left)}


# ---------------------------------------------------------------- 真读取 (推车上)

BOOT_ID = "/proc/sys/kernel/random/boot_id"
BOOT_UNIT = "cart-boot-success.service"


def parse_is_active(names, out):
    """`systemctl is-active a b c` 每个单元一行, 按顺序对上."""
    lines = [ln.strip() for ln in (out or "").splitlines()]
    return {n: (lines[i] or None) if i < len(lines) else None for i, n in enumerate(names)}


def parse_boot_success(out):
    """`systemctl show -p Result -p ExecMainExitTimestampMonotonic cart-boot-success.service`
    -> 这次开机它跑成功过没有; 读不到 None. 它是 oneshot, 跑完就 inactive, 只能看退出时间戳.
    kiosk 没起来时 Requisite 让它根本不启动, 时间戳是 0."""
    kv = dict(ln.split("=", 1) for ln in (out or "").splitlines() if "=" in ln)
    if "Result" not in kv or "ExecMainExitTimestampMonotonic" not in kv:
        return None
    try:
        ran = int(kv["ExecMainExitTimestampMonotonic"]) > 0
    except ValueError:
        return None
    return ran and kv["Result"] == "success"


def _run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def default_read(runner=_run, dash=DASH_STATE, guard=GUARD_STATE, auth=AUTH_STATUS, boot_id=BOOT_ID):
    import time

    def boot():
        with open(boot_id) as f:
            bid = f.read().strip() or None
        try:
            ok = parse_boot_success(runner(["systemctl", "show", "-p", "Result",
                                            "-p", "ExecMainExitTimestampMonotonic", BOOT_UNIT]))
        except Exception:        # noqa: BLE001  systemctl 不在/超时: 判断不了
            ok = None
        return {"id": bid, "ok": ok}

    return {
        "now": time.time,
        "state": lambda: _read_json(dash),
        "guard": lambda: _read_json(guard),
        "auth": lambda: _read_json(auth),
        "services": lambda: parse_is_active(SERVICES, runner(["systemctl", "is-active", *SERVICES])),
        "boot": boot,
    }


def main(argv=None, opener=None):
    """cart-notify.service 每次跑一轮; --test 发一条测试消息, 部署时确认手机收得到."""
    import argparse
    import time
    ap = argparse.ArgumentParser(description="推车关键事件推送 (#16)")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--conf", default=CONF_PATH)
    ap.add_argument("--test", action="store_true", help="按配置发一条测试消息")
    a = ap.parse_args(argv)
    if a.test:
        try:
            conf = load_conf(a.conf)
        except ValueError as ex:
            print(ex)
            return 1
        if conf is None:
            print(f"{a.conf} 没有或 enabled=false: 不外发")
            return 1
        now = time.time()
        _left, why = deliver([_event("test", "推车推送测试：收到这条就说明渠道通了", 3, now)],
                             make_sender(conf, opener), now)
        print(why or "已发出")
        return 1 if why else 0
    r = run_once(default_read(), a.state, a.conf, opener=opener)
    print(f"事件 {r['events']} 条, 发出 {r['sent']} 条, 积压 {r['left']} 条")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
