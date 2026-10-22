"""Kiosk 无线管理的纯逻辑: 解析 nmcli 输出 + 拼命令. 不碰 Tk, 方便测试.

优先级 = NM 的 connection.autoconnect-priority (大的先连). 无线一律 route-metric 900,
插着网线时永远走有线, 无线只当备胎.
"""

IFACE = "wlp4s0"
WIFI_METRIC = 900


def split_terse(line):
    """nmcli -t 一行: 字段用 ':' 分, 字段里的 ':' 和 '\\' 被转义成 '\\:' '\\\\'."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def cmd_scan(iface=IFACE, rescan=True):
    """rescan: True 立刻扫 / False 用缓存 / "auto" 缓存旧了才扫."""
    return ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
            "ifname", iface, "--rescan", {True: "yes", False: "no"}.get(rescan, rescan)]


def parse_scan(text):
    """-> [{ssid, signal, security, in_use}], 同名合并取最强, 在用的排最前, 其余按信号."""
    best = {}
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 5 or not f[2]:
            continue
        in_use, ssid, sec = f[0].strip() == "*", f[2], f[4].strip()
        sig = int(f[3]) if f[3].isdigit() else 0
        cur = best.get(ssid)
        if not cur:
            best[ssid] = {"ssid": ssid, "signal": sig, "security": sec, "in_use": in_use}
        else:
            cur["signal"] = max(cur["signal"], sig)
            cur["in_use"] = cur["in_use"] or in_use
    return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"]))


def key_mgmt(security):
    s = security.replace("--", "").split()
    if not s:
        return None
    return "sae" if s == ["WPA3"] else "wpa-psk"


CMD_SAVED = ["nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE", "con", "show"]


def parse_saved(text):
    """已保存的无线连接, 按优先级从高到低 (同级按名字)."""
    out = []
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 6 or f[2] != "802-11-wireless":
            continue
        out.append({"name": f[0], "uuid": f[1], "autoconnect": f[3] == "yes",
                    "priority": int(f[4]) if f[4].lstrip("-").isdigit() else 0, "active": f[5] == "yes"})
    return sorted(out, key=lambda s: (-s["priority"], s["name"]))


def move(order, i, delta):
    j = i + delta
    if not 0 <= j < len(order):
        return list(order)
    out = list(order)
    out[i], out[j] = out[j], out[i]
    return out


def priorities(n):
    """排第 0 位的最高: [n*10, ..., 10]."""
    return [(n - k) * 10 for k in range(n)]


def next_priority(saved):
    """新加的网络排到最上面."""
    return max([s["priority"] for s in saved] + [0]) + 10


def cmd_add(ssid, password, security, priority, iface=IFACE):
    cmd = ["nmcli", "con", "add", "type", "wifi", "ifname", iface, "con-name", ssid, "ssid", ssid,
           "ipv4.route-metric", str(WIFI_METRIC), "ipv6.route-metric", str(WIFI_METRIC),
           "connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority)]
    km = key_mgmt(security)
    if km:
        cmd += ["wifi-sec.key-mgmt", km, "wifi-sec.psk", password]
    return cmd


def cmd_up(uuid):
    return ["nmcli", "con", "up", "uuid", uuid]


def cmd_delete(uuid):
    return ["nmcli", "con", "delete", "uuid", uuid]


def cmd_priority(uuid, p):
    return ["nmcli", "con", "mod", "uuid", uuid, "connection.autoconnect-priority", str(p)]


def cmd_show_psk(uuid):
    return ["nmcli", "-s", "-g", "802-11-wireless-security.psk", "con", "show", "uuid", uuid]


def cmd_set_psk(uuid, psk):
    return ["nmcli", "con", "mod", "uuid", uuid, "802-11-wireless-security.psk", psk]


# ---------------------------------------------------------------- 状态板一行

CMD_STATUS_SCAN = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi", "list",
                   "ifname", IFACE, "--rescan", "no"]


def status(scan, addr, routes, iface=IFACE):
    """scan = CMD_STATUS_SCAN 输出; addr = ip -4 -br addr show; routes = ip route.
    egress = 默认路由第一条 (metric 最小) 走无线, 即网线拔了、现在靠它在线."""
    cur = next((f for f in map(split_terse, scan.splitlines()) if len(f) >= 3 and f[0].strip() == "*"), None)
    ip = next((w for w in addr.split()[2:] if "." in w), None)
    if not cur or not ip:
        return {"ssid": None, "signal": 0, "ip": None, "egress": False}
    first = next((l.split() for l in routes.splitlines() if l.startswith("default")), [])
    return {"ssid": cur[1], "signal": int(cur[2]) if cur[2].isdigit() else 0, "ip": ip,
            "egress": "dev" in first and first[first.index("dev") + 1] == iface}



# ---------------------------------------------------------------- 优先级主动生效
# NM 只在断线重连时按优先级挑; 已经连着低优先级的, 高优先级的出现了也不换. 定时看一眼, 该换就换.

MIN_SIGNAL = 40
COOLDOWN_S = 1800


def pick_switch(saved, scan, now, cooldown=None, min_signal=MIN_SIGNAL):
    """saved = parse_saved + 每项补 ssid; scan = parse_scan; cooldown = {ssid: 冷却到的时间}.
    当前连着的不是"看得见、信号够、允许自动连"里优先级最高的 -> 返回该切去的那个, 否则 None.
    没连着就不管 (NM 自己会按优先级连)."""
    cur = next((s for s in saved if s["active"]), None)
    if not cur:
        return None
    seen = {n["ssid"]: n["signal"] for n in scan}
    cooldown = cooldown or {}
    better = [s for s in saved
              if s["autoconnect"] and s["priority"] > cur["priority"]
              and seen.get(s["ssid"], 0) >= min_signal and cooldown.get(s["ssid"], 0) <= now]
    return max(better, key=lambda s: s["priority"]) if better else None
