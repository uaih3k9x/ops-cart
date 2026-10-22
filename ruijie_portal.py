#!/usr/bin/env python3
"""锐捷 ePortal 无线网页认证, 给没有浏览器的推车用. 由 systemd timer 定时跑一次:
探测 -> 被 302 到 portal 就登录 -> 已在线什么都不做.

配置 (root 0600): {"iface": "wlp4s0", "ssid": "SCNUNET", "user": "...", "password": "..."}
用法: ruijie_portal.py [配置路径]     网络操作全走 curl --interface, 不碰默认路由.

只在无线连着 ssid 时才跑: 别的网也可能是锐捷认证, 不能把 SCNUNET 的密码交给它.
每次结果写 STATUS (Kiosk 面板/状态板读), 状态变了才记 events.log; 没登上就以 1 退出.
"""

import json
import random
import socket
import struct
import subprocess
import sys
import urllib.parse

CONF = "/etc/ops-cart/scnunet.json"
STATUS = "/run/ops-cart/scnunet-status.json"
DEFAULT_SSID = "SCNUNET"
OK_STATES = ("online", "login ok", "skip")
PROBE = "http://www.msftconnecttest.com/connecttest.txt"
PROBE_OK = "Microsoft Connect Test"
DEFAULT_MAC = "111111111"                    # 页面 JS: 地址栏取不到 mac 时用这个


def ec(s):
    """JS encodeURIComponent."""
    return urllib.parse.quote(s, safe="-_.!~*'()")


def encrypt_password(password, e_hex, n_hex, mac=DEFAULT_MAC):
    """页面 encryptedPassword("密码>mac"): 反转后小端拼大数 == 原串大端; 无填充 RSA, 十六进制输出."""
    n = int(n_hex, 16)
    m = int.from_bytes(f"{password}>{mac}".encode(), "big")
    return format(pow(m, int(e_hex, 16), n), "x").zfill(len(n_hex))


def split_eportal(location):
    """.../eportal/index.jsp?xxx -> (".../eportal/", "xxx"); 不是 eportal 页返回 None."""
    head, sep, query = location.partition("?")
    if not head.endswith("/eportal/index.jsp"):
        return None
    return head[: -len("index.jsp")], query


def mac_from_query(query):
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k.lower() == "mac" and v:
            return v
    return DEFAULT_MAC


def login_body(user, enc_password, query):
    """和 AuthInterFace.login 拼的一样: 各字段 encodeURIComponent 两次."""
    f = lambda s: ec(ec(s))
    return (f"userId={f(user)}&password={f(enc_password)}&service=&queryString={f(query)}"
            "&operatorPwd=&operatorUserId=&validcode=&passwordEncrypt=true")


def classify(status, location, body):
    if status == 200 and PROBE_OK in body:
        return "online"
    if status in (301, 302, 303, 307) and location:
        return "portal"
    return "down"


def parse_result(text):
    try:
        d = json.loads(text)
    except ValueError:
        return False, text[:200]
    return d.get("result") == "success", d.get("message", "")


# ---------------------------------------------------------------- DNS
# portal 主机名 (auth-xxx.wifi.com) 只有无线这边的 DNS 认识; 系统 resolv.conf 先问有线, 查不到

def dns_query(qid, name):
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return q + struct.pack(">HH", 1, 1)


def dns_parse_a(r):
    qd, an = struct.unpack(">HH", r[4:8])
    i = 12
    for _ in range(qd):                      # 问题段的名字不压缩: 跳过标签到 0, 再跳 type/class
        while r[i]:
            i += r[i] + 1
        i += 5
    out = []
    for _ in range(an):
        if r[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while r[i]:
                i += r[i] + 1
            i += 1
        typ, _cls, _ttl, ln = struct.unpack(">HHIH", r[i:i + 10])
        i += 10
        if typ == 1 and ln == 4:
            out.append(socket.inet_ntoa(r[i:i + 4]))
        i += ln
    return out


def resolve(iface, server, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, 25, iface.encode())        # SO_BINDTODEVICE
    s.settimeout(3)
    qid = random.randrange(65536)
    s.sendto(dns_query(qid, name), (server, 53))
    return dns_parse_a(s.recv(2048))


# ---------------------------------------------------------------- 网络

def curl(iface, url, data=None, resolve_to=None):
    """-> (状态码, Location, 正文). 失败返回 (0, "", "")."""
    cmd = ["curl", "--interface", iface, "-s", "-m", "8", "-o", "-", "-w", "\n%{http_code} %{redirect_url}"]
    if data is not None:
        cmd += ["--data-raw", data, "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8"]
    if resolve_to:
        host = urllib.parse.urlsplit(url).hostname
        cmd += ["--resolve", f"{host}:80:{resolve_to}"]
    p = subprocess.run(cmd + [url], capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    body, _, tail = out.rpartition("\n")
    code, _, loc = tail.partition(" ")
    return (int(code) if code.isdigit() else 0), loc.strip(), body


def gateway(iface):
    out = subprocess.run(["ip", "-4", "route", "show", "default", "dev", iface],
                         capture_output=True, text=True).stdout.split()
    return out[out.index("via") + 1] if "via" in out else None


def find_eportal(iface, location):
    """跟着 302 走到 eportal 页 (中间的 auth 主机名用无线侧网关的 DNS 解析)."""
    for _ in range(4):
        hit = split_eportal(location)
        if hit:
            return hit
        host = urllib.parse.urlsplit(location).hostname
        ip = None
        if host and not host.replace(".", "").isdigit():
            gw = gateway(iface)
            ip = (resolve(iface, gw, host) or [None])[0] if gw else None
        _code, location, _body = curl(iface, location, resolve_to=ip)
        if not location:
            return None
    return None


def run_once(conf):
    iface = conf["iface"]
    code, loc, body = curl(iface, PROBE)
    state = classify(code, loc, body)
    if state != "portal":
        return state
    hit = find_eportal(iface, loc)
    if not hit:
        return "portal?"
    base, query = hit
    _c, _l, info = curl(iface, base + "InterFace.do?method=pageInfo", data="queryString=" + ec(ec(query)))
    info = json.loads(info)
    pw = conf["password"]
    if info.get("passwordEncrypt") == "true":
        pw = encrypt_password(pw, info["publicKeyExponent"], info["publicKeyModulus"], mac_from_query(query))
    _c, _l, res = curl(iface, base + "InterFace.do?method=login", data=login_body(conf["user"], pw, query))
    ok, msg = parse_result(res)
    return "login ok" if ok else f"login fail: {msg}"


# ---------------------------------------------------------------- 守门 / 结果

def parse_iw_ssid(text):
    """`iw dev <if> link` -> 当前 SSID, 没连返回 None."""
    for line in text.splitlines():
        k, _, v = line.strip().partition(": ")
        if k == "SSID":
            return v
    return None


def current_ssid(iface):
    return parse_iw_ssid(subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True).stdout)


def on_target(ssid, conf):
    return bool(ssid) and ssid.lower() == conf.get("ssid", DEFAULT_SSID).lower()


def record(state, message, now):
    return {"time": now, "state": state, "ok": state in OK_STATES, "message": message}


def changed(prev, cur):
    return prev is None or (prev.get("state"), prev.get("message")) != (cur["state"], cur["message"])


def conf_update(conf, user, password):
    """Kiosk 面板改账号密码: 保留别的键, 补齐 iface/ssid."""
    user = user.strip()
    if not user or not password:
        raise ValueError("账号和密码都不能为空")
    new = {"iface": "wlp4s0", "ssid": DEFAULT_SSID}
    new.update(conf or {})
    new.update(user=user, password=password)
    return new


def conf_save(conf, path=CONF):
    """写配置, 永远 0600 (里面有密码); 原子写, 写一半断电也不坏."""
    import atomicfile
    atomicfile.write_json(path, conf, mode=0o600)


def attempt(conf, ssid, now):
    """跑一次 (不在目标网上就跳过), 异常也收成一条结果, 不让 systemd 只看到一段 traceback."""
    if not on_target(ssid, conf):
        return record("skip", f"连的是 {ssid or '(没连无线)'}, 不是 {conf.get('ssid', DEFAULT_SSID)}", now)
    try:
        res = run_once(conf)
    except Exception as ex:          # noqa: BLE001  portal 页面千奇百怪, 统一报出来
        return record("error", f"{type(ex).__name__}: {ex}", now)
    state, _, msg = res.partition(": ")
    return record(state, msg, now)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    import time
    conf = json.load(open(sys.argv[1] if len(sys.argv) > 1 else CONF))
    cur = attempt(conf, current_ssid(conf["iface"]), int(time.time()))
    prev = _read_json(STATUS)
    try:
        import atomicfile
        atomicfile.write_json(STATUS, cur)
    except OSError:
        pass
    if changed(prev, cur):
        try:
            import cartlog
            cartlog.event("scnunet", f"{cur['state']} {cur['message']}".strip())
        except ImportError:
            pass
    print(f"{cur['state']} {cur['message']}".strip())
    sys.exit(0 if cur["ok"] else 1)


if __name__ == "__main__":
    main()
