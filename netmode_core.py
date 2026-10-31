"""网口模式 (#12) 的纯逻辑: 按网口管模式, 每个模式都能 undo. 不碰 Tk, 不跑命令 (执行器的 run 由调用方注入).

- 每个网口同一时间一个模式; 模式 = {mode, ports, params}, apply 时记下自己占了什么 (res):
  推车自己建的 NM 连接 (cart-<网口>-<模式>, 按 uuid 认)、改过的内核参数 (连原值一起记).
- 切换 = undo 当前 -> apply 新. apply 失败: 撤掉新的, 恢复原模式; 原模式也起不来就落到干净状态.
- 干净状态: LAN1 关闭, LAN2 DHCP 客户端. 无线 (wlp4s0) 不归这里管, 永远留作后路, 命令里不会出现它.
- 状态文件 /var/lib/ops-cart/netmode.json 记每个模式占了什么; 动手前先写意图, 断电重启也知道要撤什么.
- 旧连接 (Lan1-pxe、enp3s0、旧面板建的 cart-<网口>-<种类>-<名字>) 只认不删: 没有记录的口显示"未托管",
  切走时不撤它, 失败恢复 = 把它重新 up; 也可以把口"交还"给某个旧连接 (退出托管).
- 跨两个口的模式 (共享上网 / 透明桥, 后续批次) 的 ports 写两个口, 占用期间另一个口不能单独切.
"""

import ipaddress
import json
import shlex
import time

import atomicfile
import netprof_core as npc
import wifi_core as wc

LAN1, LAN2 = "eno1", "enp3s0"
PORTS = {LAN1: "LAN1", LAN2: "LAN2"}
CLEAN = {LAN1: "off", LAN2: "dhcp"}
MODES = {
    "off": {"title": "关闭", "ports": (LAN1,)},
    "dhcp": {"title": "DHCP 客户端", "ports": (LAN1, LAN2)},
    "static": {"title": "静态 IP", "ports": (LAN1, LAN2)},
}
LEGACY = "legacy"           # 未托管 / 交还给旧连接: 只 up, 不建不删
EGRESS = {"never": "不当出口", "backup": "备用出口", "primary": "主出口"}
EGRESS_METRIC = {"primary": wc.WIRED_PRIMARY_METRIC, "backup": wc.WIRED_BACKUP_METRIC}
NO_EGRESS = (LAN1,)         # 装机口永不当出口 (#25)
STATE_PATH = "/var/lib/ops-cart/netmode.json"
PREFIX = "cart"
AUTOCONNECT_PRIORITY = 50   # 高过旧连接 (默认 0): 重启后 NM 先连推车托管的那条


# ------------------------------------------------------------ 规则 / 记录

def check_mode(iface, mode):
    """不允许的组合返回原因, 允许返回 None."""
    if iface == wc.IFACE:
        return f"无线 ({iface}) 不归网口模式管, 永远留作后路"
    if iface not in PORTS:
        return f"{iface} 不归网口模式管 (只管 {'/'.join(PORTS.values())})"
    if mode == LEGACY:
        return None
    if mode not in MODES:
        return f"这一版还没有「{mode}」模式"
    if iface not in MODES[mode]["ports"]:
        return f"{PORTS[iface]} 不能用「{MODES[mode]['title']}」(管理口至少留 DHCP)"
    return None


def rec(mode, iface, params=None):
    why = check_mode(iface, mode)
    if why:
        raise ValueError(why)
    return {"mode": mode, "ports": [iface], "params": dict(params or {})}


def legacy(iface, uuid="", name=""):
    """旧连接 (不归推车删). uuid 为空 = 网口上原来什么都没生效."""
    return {"mode": LEGACY, "ports": [iface], "params": {"uuid": uuid, "name": name}}


def same(a, b):
    return bool(a and b) and all(a.get(k) == b.get(k) for k in ("mode", "ports", "params"))


def _ports_text(ports):
    return " + ".join(PORTS.get(p, p) for p in ports)


def describe(r):
    if r["mode"] == LEGACY:
        name = r["params"].get("name")
        return f"未托管 · 旧配置「{name}」" if name else "未托管 · 没有生效的连接"
    p, text = r["params"], MODES.get(r["mode"], {}).get("title", r["mode"])
    if r["mode"] == "static":
        text += f" {p['addr']}" + (f" 网关 {p['gateway']}" if p.get("gateway") else "") \
            + (f" DNS {p['dns']}" if p.get("dns") else "") + f" · {EGRESS[p['egress']]}"
    return text


# ------------------------------------------------------------ 静态表单 (一页填完, #8/#13/#25)

def static_params(iface, addr, gateway="", dns="", egress="never", avoid=""):
    """地址/前缀走 static_form (不猜 /24); 不当出口时网关不写; 备用/主出口网关必填; LAN1 不许当出口."""
    if egress not in EGRESS:
        raise ValueError(f"不认识的出口优先级 {egress}")
    if iface in NO_EGRESS and egress != "never":
        raise ValueError(f"{PORTS[iface]} 是装机口, 不能当出口")
    form = npc.static_form(addr, gateway if egress != "never" else "", dns, avoid=avoid)
    if egress != "never" and not form["gateway"]:
        raise ValueError(f"选了{EGRESS[egress]}要填网关, 不然出不去")
    return {"addr": form["addr"], "gateway": form["gateway"], "dns": form["dns"], "egress": egress}


def form_state(iface, mode, egress):
    """表单里哪些能填: 地址只在静态; 网关只在静态且当出口; LAN1 的出口两项置灰."""
    st = mode == "static"
    return {"addr": st, "gateway": st and egress != "never",
            "egress": {k: st and (k == "never" or iface not in NO_EGRESS) for k in EGRESS}}


def target(iface, form, olds=()):
    """面板表单 {mode, addr, peer, gateway, dns, egress, legacy} -> 要切成的记录; 不对就 ValueError(人话).
    olds = 这个口可交还的旧连接 (port_views 的 legacy)."""
    mode = form.get("mode")
    if mode == LEGACY:
        c = next((c for c in olds if c["name"] == form.get("legacy")), None)
        if not c:
            raise ValueError("先选一个要交还的旧配置")
        return legacy(iface, c["uuid"], c["name"])
    if mode == "static":
        return rec("static", iface, static_params(iface, form.get("addr"), form.get("gateway"), form.get("dns"),
                                                  form.get("egress") or "never", avoid=form.get("peer") or ""))
    return rec(mode, iface)


def form_hint(iface, mode, form):
    """表单下方的小灰字: 这个模式会怎样; 静态就当场校验, 有错直接说错在哪."""
    if mode == "off":
        return "关闭: 不配地址, 推车自己建一条不带地址的连接占住这个口"
    if mode == "dhcp":
        return "DHCP 客户端: 插上就自动拿地址" + (" · 装机口不当出口" if iface in NO_EGRESS else "")
    if mode == LEGACY:
        return "交还旧配置: 撤掉推车托管的, 把选中的旧连接 up 起来; 旧连接推车不会删"
    if not (form.get("addr") or "").strip():
        return npc.static_hint("")
    egress = form.get("egress") or "never"
    try:
        p = static_params(iface, form.get("addr"), form.get("gateway"), form.get("dns"), egress,
                          avoid=form.get("peer") or "")
    except ValueError as ex:
        return str(ex)
    net = ipaddress.ip_interface(p["addr"]).network
    if egress == "never":
        how = "不当出口: 只连同网段, 网关不写"
    else:
        rel = "大于" if egress == "backup" else "小于"
        how = (f"{EGRESS[egress]}: metric {EGRESS_METRIC[egress]} {rel}无线的 {wc.WIFI_METRIC}, "
               + ("无线断了才走它" if egress == "backup" else "默认路由走这个口"))
    return f"推车用 {p['addr']} (网段 {net}) · {how}; 切之前还会确认"


# ------------------------------------------------------------ apply / undo 命令清单

def con_name(iface, mode):
    return f"{PREFIX}-{iface}-{mode}"


def _con_add(iface, mode, uuid, settings):
    return ["nmcli", "con", "add", "type", "ethernet", "ifname", iface, "con-name", con_name(iface, mode),
            "connection.uuid", uuid, "connection.autoconnect", "yes",
            "connection.autoconnect-priority", str(AUTOCONNECT_PRIORITY)] + settings


def _egress_settings(iface, egress):
    if iface in NO_EGRESS and egress != "never":
        raise ValueError(f"{PORTS[iface]} 是装机口, 不能当出口")
    if egress == "never":
        return ["ipv4.never-default", "yes", "ipv6.never-default", "yes"]
    m = str(EGRESS_METRIC[egress])
    return ["ipv4.never-default", "no", "ipv4.route-metric", m, "ipv6.never-default", "no", "ipv6.route-metric", m]


def _settings(iface, r):
    mode, p = r["mode"], r["params"]
    if mode == "off":
        return ["ipv4.method", "disabled", "ipv6.method", "disabled"]
    if mode == "dhcp":
        return ["ipv4.method", "auto", "ipv6.method", "auto"] + (
            _egress_settings(iface, "never") if iface in NO_EGRESS else [])
    # static: 网关 / DNS 留空也显式写 "", 不留旧值
    return (["ipv4.method", "manual", "ipv4.addresses", p["addr"], "ipv4.gateway", p.get("gateway") or "",
             "ipv4.dns", p.get("dns") or "", "ipv6.method", "auto"]
            + _egress_settings(iface, p.get("egress", "never")))


def _stop_pxe(iface, serve=False):
    """LAN1 换成别的配置前先停 PXE 三件套 (同旧面板); 交还装机配置时不停."""
    return [["systemctl", "stop", *npc.PXE_UNITS]] if iface in npc.SERVE_IFACES and not serve else []


def _is_serve(name):
    p = npc.parse_name(name or "")
    return bool(p and p["kind"] == "serve")


def apply_steps(r, new_uuid):
    """记录 -> (命令清单, 占用). new_uuid() 给新连接定 uuid (测试里注入, 好对得上)."""
    for iface in r["ports"]:
        why = check_mode(iface, r["mode"])
        if why:
            raise ValueError(why)
    if r["mode"] == LEGACY:
        u, name = r["params"].get("uuid"), r["params"].get("name")
        if not u:
            return [], []
        return _stop_pxe(r["ports"][0], _is_serve(name)) + [["nmcli", "con", "up", "uuid", u]], []
    iface = r["ports"][0]
    u = new_uuid()
    cmds = _stop_pxe(iface) + [_con_add(iface, r["mode"], u, _settings(iface, r)),
                               ["nmcli", "con", "up", "uuid", u]]
    return cmds, [{"con": u, "name": con_name(iface, r["mode"]), "iface": iface}]


def sysctl_res(key, orig):
    return {"sysctl": key, "orig": orig}


def set_sysctl(key, value, orig):
    """给后续模式 (共享上网的 ip_forward 等) 用: 改之前读到的原值 orig 记进占用, undo 时写回."""
    return ["sysctl", "-w", f"{key}={value}"], sysctl_res(key, orig)


def _undo_one(res):
    if "con" in res:
        return ["nmcli", "con", "delete", "uuid", res["con"]]
    if "sysctl" in res:
        return ["sysctl", "-w", f"{res['sysctl']}={res['orig']}"]
    raise ValueError(f"不认识的占用 {res}, 状态文件可能是新版本写的")


def undo_steps(r):
    """按 apply 的反序撤; stale = 上次没撤完的 (断电/出错), 一起撤. 旧连接本身没有占用, 不归推车删."""
    return [_undo_one(x) for x in list(reversed(r.get("res", []))) + list(reversed(r.get("stale", [])))]


# ------------------------------------------------------------ 状态文件

def empty_state():
    return {"version": 1, "modes": [], "prev": {}}


def load_state(path=STATE_PATH):
    """没有文件 = 首次运行, 全部未托管; 坏了就当空的, 带 broken 给界面提示."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as ex:
        return dict(empty_state(), broken=f"状态文件读不了 ({ex}), 按未托管显示")
    if not isinstance(data, dict) or not isinstance(data.get("modes"), list):
        return dict(empty_state(), broken="状态文件格式不对, 按未托管显示")
    data.setdefault("prev", {})
    return data


def save_state(path, st):
    atomicfile.write_json(path, {k: v for k, v in st.items() if k != "broken"}, indent=1)


def owner(st, iface):
    return next((r for r in st["modes"] if iface in r["ports"]), None)


# ------------------------------------------------------------ 计划

def _eth_cons(cons_text):
    out = []
    for line in (cons_text or "").splitlines():
        f = wc.split_terse(line)
        if len(f) >= 5 and f[2] == "802-3-ethernet":
            out.append({"name": f[0], "uuid": f[1], "device": f[3], "active": f[4] == "yes"})
    return out


def active_legacy(iface, cons_text):
    c = next((c for c in _eth_cons(cons_text) if c["device"] == iface and c["active"]), None)
    return legacy(iface, c["uuid"], c["name"]) if c else legacy(iface)


def _bare(r):
    return {"mode": r["mode"], "ports": list(r["ports"]), "params": dict(r["params"])}


def plan_switch(st, new, cons_text, new_uuid):
    """当前 (状态文件里的, 没有就按现状认旧连接) -> new. 返回执行器和确认框要的全部东西, 不跑任何命令."""
    ports = list(new["ports"])
    cur = []
    for p in ports:
        r = owner(st, p)
        if r is None or r["mode"] == LEGACY:        # 交还旧连接时断电留下的记录: 带着没撤完的 stale
            cur.append(dict(active_legacy(p, cons_text), stale=list((r or {}).get("stale", []))))
            continue
        extra = [q for q in r["ports"] if q not in ports]
        if extra:
            raise ValueError(f"{_ports_text(r['ports'])} 被「{describe(r)}」一起占着, "
                             f"先撤掉它, 再单独切 {PORTS[p]}")
        if r not in cur:
            cur.append(r)
    apply, res = apply_steps(new, new_uuid)
    new = dict(_bare(new), res=res)
    restore, restore_recs = [], []
    for r in cur:
        cmds, rres = apply_steps(_bare(r), new_uuid)
        restore += cmds
        restore_recs.append(dict(_bare(r), res=rres))
    clean, clean_recs = [], []
    for p in ports:
        cmds, cres = apply_steps(rec(CLEAN[p], p), new_uuid)
        clean += cmds
        clean_recs.append(dict(rec(CLEAN[p], p), res=cres))
    undo = [c for r in cur for c in undo_steps(r)]
    return {"ports": ports, "cur": cur, "new": new, "undo": undo, "apply": apply, "commands": undo + apply,
            "rollback": undo_steps(new), "restore": restore, "restore_recs": restore_recs,
            "clean": clean, "clean_recs": clean_recs,
            "probe": new["mode"] == LEGACY and _is_serve(new["params"].get("name")),
            "stop_pxe": any(c[0] == "systemctl" for c in apply),
            "warn_remote": LAN2 in ports, "primary": new["params"].get("egress") == "primary"}


# ------------------------------------------------------------ 执行器

def _run_all(cmds, run):
    """尽力而为 (undo 用): 每条都跑, 返回失败的条数. 连接早就没了这类失败不拦路."""
    return sum(1 for c in cmds if run(c)[0])


def _run_until_fail(cmds, run):
    for c in cmds:
        rc, out = run(c)
        if rc:
            return False, f"{shlex.join(c[:4])} 返回 {rc}: {(out or '')[-120:]}"
    return True, ""


def _sh(cmds):
    return " && ".join(shlex.join(c) for c in cmds) or "无"


def run_switch(plan, st, run, save, log=None, now=time.time):
    """按计划切: 先写意图 -> undo 当前 -> apply 新; 失败撤新的、恢复原来的, 再不行落到干净状态.
    run(cmd) -> (rc, out); save(state) 写状态文件; log(一行) 记 events.log. 返回 (成功, 新状态, 人话)."""
    log = log or (lambda _m: None)
    ports, where = plan["ports"], _ports_text(plan["ports"])
    others = [r for r in st["modes"] if not set(r["ports"]) & set(ports)]
    stale = [x for r in plan["cur"] for x in r.get("res", []) + r.get("stale", [])]
    base = {k: v for k, v in st.items() if k not in ("modes", "broken")}
    try:
        save(dict(base, modes=others + [dict(plan["new"], status="applying", stale=stale)]))
    except OSError as ex:
        return False, st, f"状态文件写不了 ({ex}), 网口没动"
    before = " / ".join(describe(r) for r in plan["cur"])
    bad = _run_all(plan["undo"], run)
    log(f"undo {where} {before}: " + ("不用撤 (旧连接不归推车删)" if not plan["undo"] else
                                      "成功" if not bad else f"{bad} 条没成 (多半是早就没了)"))
    ok, why = _run_until_fail(plan["apply"], run)
    t = int(now())

    def done(recs, prev, status="ok"):
        new_st = dict(base, modes=others + [dict(r, status=status, since=t) for r in recs if r["mode"] != LEGACY],
                      prev=prev)
        try:
            save(new_st)
        except OSError as ex:
            log(f"状态文件写不了: {ex}")
        return new_st

    if ok:
        log(f"apply {where} {describe(plan['new'])}: 成功; 撤销命令: {_sh(plan['rollback'])}")
        prev = dict(st.get("prev", {}))
        for r in plan["cur"]:
            if r["mode"] != LEGACY or r["params"].get("uuid"):
                prev.update({p: _bare(r) for p in r["ports"]})
        return True, done([plan["new"]], prev), f"{where} 切换成功: {describe(plan['new'])}"

    log(f"apply {where} {describe(plan['new'])}: 失败 {why}")
    _run_all(plan["rollback"], run)
    log(f"undo {where} {describe(plan['new'])}: apply 失败自动撤 ({_sh(plan['rollback'])})")
    prev = st.get("prev", {})
    ok, why2 = _run_until_fail(plan["restore"], run)
    if ok:
        log(f"apply {where} 恢复原来的 {before}: 成功")
        return False, done(plan["restore_recs"], prev), f"切换失败 ({why}), 已回到原来的 {before}"
    for r in plan["restore_recs"]:
        _run_all(undo_steps(r), run)
    ok, why3 = _run_until_fail(plan["clean"], run)
    clean = " / ".join(describe(r) for r in plan["clean_recs"])
    log(f"apply {where} 恢复原来的失败 ({why2}), 落到干净状态 {clean}: " + ("成功" if ok else f"失败 {why3}"))
    new_st = done(plan["clean_recs"], prev, "ok" if ok else "failed")
    if ok:
        return False, new_st, f"切换失败 ({why}), 原来的也恢复不了, 已落到干净状态: {clean}"
    return False, new_st, (f"切换失败, 原来的恢复不了, 干净状态也没起来 ({why3}); "
                           f"车边终端看 nmcli dev status, 无线还在")


# ------------------------------------------------------------ 界面口径: 按网口看实际状态

def parse_default_routes(text):
    """ip -4 route show default -> [(网口, metric)], metric 小的在前 (第一条就是默认出口)."""
    out = []
    for line in (text or "").splitlines():
        f = line.split()
        if not f or f[0] != "default" or "dev" not in f[:-1]:
            continue
        metric = f[f.index("metric") + 1] if "metric" in f[:-1] else "0"
        out.append((f[f.index("dev") + 1], int(metric) if metric.isdigit() else 0))
    return sorted(out, key=lambda x: x[1])


CMD_ROUTES = ["ip", "-4", "route", "show", "default"]


def _drift(r, active):
    if r.get("status") == "applying":
        return "上次切换没做完 (断电或出错): 再切一次或撤到干净状态就能收拾干净"
    if r.get("status") == "failed":
        return "上次切换失败, 连干净状态都没起来: 车边终端看 nmcli dev status"
    mine = {x["con"] for x in r.get("res", []) if "con" in x}
    if not mine or (active and active["uuid"] in mine):
        return ""
    return f"记录是「{describe(r)}」, 但网口上实际生效的是「{active['name']}」" if active else \
        f"记录是「{describe(r)}」, 但网口上现在没有生效的连接"


def port_views(st, cons_text, ip_br, routes, links):
    """每个网口一行: 当前模式 (未托管就按现状认)、和记录对不对得上、实际地址、是不是默认出口、可交还的旧连接."""
    cons, ips, defaults = _eth_cons(cons_text), npc.parse_ip_br(ip_br), parse_default_routes(routes)
    mine = {x["con"] for r in st["modes"] for x in r.get("res", []) + r.get("stale", []) if "con" in x}
    out = []
    for iface in PORTS:
        r = owner(st, iface)
        active = next((c for c in cons if c["device"] == iface and c["active"]), None)
        managed = r is not None and r["mode"] != LEGACY
        cur = r if managed else (legacy(iface, active["uuid"], active["name"]) if active else legacy(iface))
        olds = [dict(c, serve=_is_serve(c["name"])) for c in cons if c["uuid"] not in mine and
                (c["device"] == iface or (npc.parse_name(c["name"]) or {}).get("iface") == iface)]
        metric = dict(reversed(defaults)).get(iface)
        egress = "" if metric is None else "默认出口" if defaults[0][0] == iface else f"备用出口 (metric {metric})"
        out.append({"iface": iface, "port": PORTS[iface], "mode": cur["mode"], "title": describe(cur),
                    "managed": managed, "drift": _drift(r, active) if r else "", "rec": cur,
                    "ip": ips.get(iface, "-"), "link": links.get(iface), "egress": egress, "legacy": olds})
    return out


# ------------------------------------------------------------ 确认框

def confirm_text(plan, ip=""):
    """确认框: 原来/改为、要注意的、将执行的命令、出问题时的 undo 命令 (接 #8 的回滚文案)."""
    where = " + ".join(f"{PORTS[p]} ({p})" for p in plan["ports"])
    before = " / ".join(describe(r) for r in plan["cur"]) + (f" · {ip}" if ip else "")
    lines = [f"{where} 切换网口模式", "", f"原来: {before}", f"改为: {describe(plan['new'])}", ""]
    if plan["warn_remote"]:
        lines.append("LAN2 是管理口, 切换时经它的远程连接会断 (无线和 tailscale 还在).")
    if plan["primary"]:
        lines.append("选了主出口: 推车默认出网会走这个口, tailscale / 远程可能断一下.")
    if plan["stop_pxe"]:
        lines.append("会先停 PXE 装机服务; 要用再去「装机」页打开.")
    if plan["probe"]:
        lines.append("交还装机配置前先探测链路上有没有别的 DHCP, 有 (或没插线探测不了) 就不切.")
    if lines[-1]:
        lines.append("")
    lines.append("将执行的命令:")
    lines += ["  " + shlex.join(c) for c in plan["commands"]] or ["  (无)"]
    lines.append("出问题时的 undo 命令 (切换失败会自动执行; 车边终端也能照抄):")
    lines.append("  撤掉新模式:")
    lines += ["    " + shlex.join(c) for c in plan["rollback"]] or ["    (不用撤, 旧连接不归推车删)"]
    lines.append("  回到原来:")
    lines += ["    " + shlex.join(c) for c in plan["restore"]] or ["    (原来没有生效的连接)"]
    return "\n".join(lines + ["", "要继续吗?"])
