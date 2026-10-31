"""网口模式 (#12) 的纯逻辑: 按网口管模式, 每个模式都能 undo. 不碰 Tk, 不跑命令 (执行器的 run 由调用方注入).

- 每个网口同一时间一个模式; 模式 = {mode, ports, params}, apply 时记下自己占了什么 (res):
  推车自己建的 NM 连接 (cart-<网口>-<模式>, 按 uuid 认)、改过的内核参数 (连原值一起记).
- 切换 = undo 当前 -> apply 新. apply 失败: 撤掉新的, 恢复原模式; 原模式也起不来就落到干净状态.
- 干净状态: LAN1 关闭, LAN2 DHCP 客户端. 无线 (wlp4s0) 不归这里管, 永远留作后路, 命令里不会出现它.
- 状态文件 /var/lib/ops-cart/netmode.json 记每个模式占了什么; 动手前先写意图, 断电重启也知道要撤什么.
- 旧连接 (Lan1-pxe、enp3s0、旧面板建的 cart-<网口>-<种类>-<名字>): 没有记录的口显示"未托管", 打开面板不动它.
  第一次切到推车的模式 (接管) 时, 新模式起来之后才把这个口上的旧连接备份 (keyfile) 再删; 备份不成就不删.
  切换失败时旧连接还在, 恢复 = 把它重新 up. 「交还旧配置」10/31 去掉了.
- 出口 (10/31): 主出口只有 Wi-Fi; LAN2 最多当备用出口; LAN1 永不当出口, 网关只给「经网关去的网段」用.
- 跨两个口的模式 (共享上网 / 透明桥, 后续批次) 的 ports 写两个口, 占用期间另一个口不能单独切.
"""

import ipaddress
import json
import os
import shlex
import shutil
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
LEGACY = "legacy"           # 未托管的旧连接: 只认, 失败恢复时 up 回去; 接管成功后备份再删
EGRESS = {"never": "不当出口", "backup": "备用出口"}       # 主出口只有 Wi-Fi (10/31), 有线最多当备用
EGRESS_METRIC = {"backup": wc.WIRED_BACKUP_METRIC}
NO_EGRESS = (LAN1,)         # 装机口永不当出口, 连备用都不当 (#25, 10/31); 网关只给「经网关去的网段」用
STATE_PATH = "/var/lib/ops-cart/netmode.json"
BACKUP_DIR = "/var/lib/ops-cart/nm-backup"                  # 删旧连接前的备份, 每次一个 <时间戳>/ 子目录
NM_DIR = "/etc/NetworkManager/system-connections"
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
            + (f" 经网关去 {p['routes']}" if p.get("routes") else "") \
            + (f" DNS {p['dns']}" if p.get("dns") else "") + f" · {EGRESS.get(p['egress'], p['egress'])}"
    return text


# ------------------------------------------------------------ 静态表单 (一页填完, #8/#13/#25)

def _bad_egress(egress):
    return f"不认识的出口优先级 {egress}" + ("; 主出口只有无线, 有线最多当备用出口" if egress == "primary" else "")


def egress_for_gateway(iface, egress, gateway):
    """网关框一变就调 (10/31 现场): LAN2 填了网关还是「不当出口」-> 备用出口; 清空网关 -> 回到不当出口;
    别的组合尊重用户选的. LAN1 永远不当出口."""
    if iface in NO_EGRESS or not (gateway or "").strip():
        return "never"
    return "backup" if egress == "never" else egress


def _routes(text, net):
    """「经网关去的网段」: 逗号分隔的 网段/前缀; 不许 0.0.0.0/0 (那就是当出口), 不许和本口网段重叠."""
    out = []
    for item in (text or "").replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if "/" not in item:
                raise ValueError
            n = ipaddress.IPv4Network(item)
        except ValueError:
            raise ValueError(f"经网关去的网段写成 网段号/前缀, 比如 198.51.100.0/24: {item} 不对") from None
        if n.prefixlen == 0:
            raise ValueError("0.0.0.0/0 是默认路由 (= 当出口), 这里只写要经网关去的具体网段")
        if n.overlaps(net):
            raise ValueError(f"{n} 和本口网段 {net} 重叠, 同网段不用经网关")
        out.append(str(n))
    return ", ".join(out)


def static_params(iface, addr, gateway="", dns="", egress="never", avoid="", routes=""):
    """地址/前缀走 static_form (不猜 /24); 网关一直能填 (10/31):
    LAN2「不当出口 + 网关」是矛盾组合, 按备用出口算; 备用出口网关必填.
    LAN1 永不当出口, 网关只给「经网关去的网段」(routes) 用; 有网段没网关报错."""
    if egress not in EGRESS:
        raise ValueError(_bad_egress(egress))
    if iface in NO_EGRESS and egress != "never":
        raise ValueError(f"{PORTS[iface]} 是装机口, 不能当出口")
    form = npc.static_form(addr, gateway, dns, avoid=avoid)
    egress = egress_for_gateway(iface, egress, form["gateway"]) if egress == "never" else egress
    if egress != "never" and not form["gateway"]:
        raise ValueError(f"选了{EGRESS[egress]}要填网关, 不然出不去")
    routes = _routes(routes, ipaddress.ip_interface(form["addr"]).network)
    if routes and not form["gateway"]:
        raise ValueError("填了「经网关去的网段」要填网关, 不然去不了")
    return {"addr": form["addr"], "gateway": form["gateway"], "dns": form["dns"], "egress": egress,
            "routes": routes}


def form_state(iface, mode, egress):
    """表单里哪些能填: 地址、网关、经网关去的网段在静态模式下都能填; LAN1 的「备用出口」置灰."""
    st = mode == "static"
    return {"addr": st, "gateway": st, "routes": st,
            "egress": {k: st and (k == "never" or iface not in NO_EGRESS) for k in EGRESS}}


def target(iface, form):
    """面板表单 {mode, addr, peer, gateway, routes, dns, egress} -> 要切成的记录; 不对就 ValueError(人话)."""
    mode = form.get("mode")
    if mode == LEGACY:
        raise ValueError(NO_HANDBACK)
    if mode == "static":
        return rec("static", iface, static_params(iface, form.get("addr"), form.get("gateway"), form.get("dns"),
                                                  form.get("egress") or "never", avoid=form.get("peer") or "",
                                                  routes=form.get("routes") or ""))
    return rec(mode, iface)


NO_HANDBACK = "「交还旧配置」去掉了 (10/31): 旧配置在第一次切到新模式时备份后删掉, 原来的地址用静态 IP 重建"


def form_hint(iface, mode, form):
    """表单下方的小灰字: 这个模式会怎样; 静态就当场校验, 有错直接说错在哪."""
    if mode == "off":
        return "关闭: 不配地址, 推车自己建一条不带地址的连接占住这个口"
    if mode == "dhcp":
        return "DHCP 客户端: 插上就自动拿地址" + (" · 装机口不当出口" if iface in NO_EGRESS else "")
    if not (form.get("addr") or "").strip():
        return ("写成 地址/前缀, 比如 192.0.2.5/24, 掩码按你填的来; "
                "对端地址可选: 填了只防撞址, 也可点「按对端预填」(对端也要带前缀)")
    try:
        p = static_params(iface, form.get("addr"), form.get("gateway"), form.get("dns"),
                          form.get("egress") or "never", avoid=form.get("peer") or "",
                          routes=form.get("routes") or "")
    except ValueError as ex:
        return str(ex)
    net, gw = ipaddress.ip_interface(p["addr"]).network, p["gateway"]
    if p["egress"] == "backup":
        how = (f"备用出口: metric {EGRESS_METRIC['backup']} 大于无线的 {wc.WIFI_METRIC}, 无线断了才走它"
               + (" (填了网关自动改的)" if (form.get("egress") or "never") == "never" else "")
               + "; 清空网关回到不当出口")
    elif iface not in NO_EGRESS:
        how = "不当出口: 只连同网段; 填了网关会自动改成备用出口 (排在无线后面)"
    elif gw and not p["routes"]:
        how = f"不当出口: 只连同网段; 网关 {gw} 现在不起作用, 要去哪些网段写进「经网关去的网段」"
    else:
        how = "不当出口: 只连同网段" + ("" if gw else "; 要去别的网段就填网关和「经网关去的网段」")
    if p["routes"]:
        how += f" · 经网关 {gw} 去 {p['routes']}"
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
    if egress not in EGRESS:
        raise ValueError(_bad_egress(egress))
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
    # static: 网关 / 路由 / DNS 留空也显式写 "", 不留旧值.
    # never-default 和 ipv4.gateway 不同时写: LAN2 老记录的「不当出口 + 网关」按备用出口建;
    # LAN1 的网关只进 ipv4.routes (经网关去的网段), 不进默认路由.
    gw = p.get("gateway") or ""
    egress = p.get("egress", "never")
    egress = egress_for_gateway(iface, egress, gw) if egress == "never" else egress
    routes = ", ".join(f"{n.strip()} {gw}" for n in (p.get("routes") or "").split(",") if n.strip())
    return (["ipv4.method", "manual", "ipv4.addresses", p["addr"],
             "ipv4.gateway", gw if egress != "never" else "", "ipv4.routes", routes,
             "ipv4.dns", p.get("dns") or "", "ipv6.method", "auto"]
            + _egress_settings(iface, egress))


def _stop_pxe(iface, serve=False):
    """LAN1 换成别的配置前先停 PXE 三件套 (同旧面板); 失败恢复回装机配置时不停."""
    return [["systemctl", "stop", *npc.PXE_UNITS]] if iface in npc.SERVE_IFACES and not serve else []


def _is_serve(name):
    p = npc.parse_name(name or "")
    return bool(p and p["kind"] == "serve")


def apply_steps(r, new_uuid, link=True):
    """记录 -> (命令清单, 占用). new_uuid() 给新连接定 uuid (测试里注入, 好对得上).
    link=False (确定没插线): NM 不让 con up, 只建好配置, 插上线 NM 按优先级自动连."""
    for iface in r["ports"]:
        why = check_mode(iface, r["mode"])
        if why:
            raise ValueError(why)
    up = [] if link is False else [["nmcli", "con", "up", "uuid"]]
    if r["mode"] == LEGACY:
        u, name = r["params"].get("uuid"), r["params"].get("name")
        if not u:
            return [], []
        return _stop_pxe(r["ports"][0], _is_serve(name)) + [c + [u] for c in up], []
    iface = r["ports"][0]
    u = new_uuid()
    cmds = _stop_pxe(iface) + [_con_add(iface, r["mode"], u, _settings(iface, r))] + [c + [u] for c in up]
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


def _undo_order(r):
    return list(reversed(r.get("res", []))) + list(reversed(r.get("stale", [])))


def undo_steps(r):
    """按 apply 的反序撤; stale = 上次没撤完的 (断电/出错), 一起撤. 旧连接本身没有占用, 不归推车删."""
    return [_undo_one(x) for x in _undo_order(r)]


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


def _mine(st):
    return {x["con"] for r in st["modes"] for x in r.get("res", []) + r.get("stale", []) if "con" in x}


def _ifname(dump):
    """nmcli -t con show uuid <u> 的 connection.interface-name; 读不到详情返回 None, 没绑网口返回 ""."""
    for line in (dump or "").splitlines():
        k, _, v = line.partition(":")
        if k == "connection.interface-name":
            return v.strip()
    return None


def cmd_con_detail(uuid):
    return ["nmcli", "-t", "con", "show", "uuid", uuid]


def eth_uuids(cons_text):
    """要读详情 (interface-name, 也当备份文本) 的: 只有以太网连接, 无线不读."""
    return [c["uuid"] for c in _eth_cons(cons_text)]


def old_cons(st, iface, cons_text, details=None):
    """接管这个口时要删的旧配置, 规则保守: 以太网连接、推车状态文件里没记着, 并且
      - connection.interface-name 就是这个口, 或
      - 正在这个口上生效 (那它不可能绑在别的口上; 没绑网口的也算, 它就在这儿用着).
    没绑网口又没在这个口上生效的 (比如 Wired connection 1) 不删 —— 它可能在别的口上起;
    绑在别的口上的、无线、推车自己的, 一律不碰. details = {uuid: nmcli -t con show 输出}, 读不到的只认正在生效的."""
    details, mine, out = details or {}, _mine(st), []
    for c in _eth_cons(cons_text):
        if c["uuid"] in mine:
            continue
        dump = details.get(c["uuid"], "")
        ifname = _ifname(dump)
        here = c["active"] and c["device"] == iface and ifname in (None, "", iface)
        if here or ifname == iface:
            out.append({"name": c["name"], "uuid": c["uuid"], "dump": dump})
    return out


def active_legacy(iface, cons_text):
    c = next((c for c in _eth_cons(cons_text) if c["device"] == iface and c["active"]), None)
    return legacy(iface, c["uuid"], c["name"]) if c else legacy(iface)


def _bare(r):
    return {"mode": r["mode"], "ports": list(r["ports"]), "params": dict(r["params"])}


def plan_switch(st, new, cons_text, new_uuid, links=None, details=None, pxe_active=False):
    """当前 (状态文件里的, 没有就按现状认旧连接) -> new. 返回执行器和确认框要的全部东西, 不跑任何命令.
    links = {网口: 插没插线}; 确定没插线 (False) 的口只建配置不 up, 读不到 (None) 照常 up.
    从未托管切走 (接管) 时 purge = 这个口上要删的旧配置 (old_cons), 新模式起来后备份再删.
    details = {uuid: nmcli -t con show 输出}; pxe_active = pxe-server 在跑 (确认框提醒装机口要重建)."""
    if new["mode"] == LEGACY:
        raise ValueError(NO_HANDBACK)
    ports = list(new["ports"])
    links = links or {}

    def steps(r):
        return apply_steps(r, new_uuid, links.get(r["ports"][0]) is not False)
    cur, purge = [], []
    for p in ports:
        r = owner(st, p)
        if r is None or r["mode"] == LEGACY:        # 未托管 (或回到旧连接时留下的记录: 带着没撤完的 stale)
            cur.append(dict(active_legacy(p, cons_text), stale=list((r or {}).get("stale", []))))
            purge += old_cons(st, p, cons_text, details)
            continue
        extra = [q for q in r["ports"] if q not in ports]
        if extra:
            raise ValueError(f"{_ports_text(r['ports'])} 被「{describe(r)}」一起占着, "
                             f"先撤掉它, 再单独切 {PORTS[p]}")
        if r not in cur:
            cur.append(r)
    apply, res = steps(new)
    new = dict(_bare(new), res=res)
    restore, restore_recs = [], []
    for r in cur:
        cmds, rres = steps(_bare(r))
        restore += cmds
        restore_recs.append(dict(_bare(r), res=rres))
    clean, clean_recs = [], []
    for p in ports:
        cmds, cres = steps(rec(CLEAN[p], p))
        clean += cmds
        clean_recs.append(dict(rec(CLEAN[p], p), res=cres))
    undo = [c for r in cur for c in undo_steps(r)]
    pxe_here = pxe_active and any(p in npc.SERVE_IFACES for p in ports)
    return {"ports": ports, "cur": cur, "new": new, "undo": undo, "apply": apply, "commands": undo + apply,
            "rollback": undo_steps(new), "restore": restore, "restore_recs": restore_recs,
            "clean": clean, "clean_recs": clean_recs, "purge": purge,
            "pxe_rebuild": bool(purge) and (pxe_here or any("pxe" in c["name"].lower() for c in purge)),
            "stop_pxe": any(c[0] == "systemctl" for c in apply),
            "no_link": [p for p in ports if links.get(p) is False],
            "warn_remote": LAN2 in ports}


# ------------------------------------------------------------ 执行器

def _undo_run(recs, run):
    """尽力而为: 每条都跑, 失败不拦路; 返回没撤掉的占用, 留进 stale 下次再撤 (早就没了的再撤一次也无妨)."""
    return [x for r in recs for x in _undo_order(r) if run(_undo_one(x))[0]]


def _run_until_fail(cmds, run):
    for c in cmds:
        rc, out = run(c)
        if rc:
            return False, f"{shlex.join(c[:4])} 返回 {rc}: {(out or '')[-120:]}"
    return True, ""


def _sh(cmds):
    return " && ".join(shlex.join(c) for c in cmds) or "无"


def _safe_name(name):
    return name.replace("/", "_").lstrip(".") or "unnamed"


def _keyfile(c, nm_dir):
    """旧连接的 keyfile: 先看 <名>.nmconnection, 再在目录里按 uuid= 找 (改过名的); 内容要对得上 uuid."""
    want = f"uuid={c['uuid']}"
    try:
        names = sorted(n for n in os.listdir(nm_dir) if n.endswith(".nmconnection"))
    except OSError:
        return None
    first = c["name"] + ".nmconnection"
    for n in ([first] if first in names else []) + [n for n in names if n != first]:
        path = os.path.join(nm_dir, n)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if any(line.strip() == want for line in f):
                    return path
        except OSError:
            continue
    return None


def backup_cons(cons, root=BACKUP_DIR, nm_dir=NM_DIR, now=time.time):
    """删旧配置前备份到 root/<时间戳>/: keyfile 原样复制; 没 keyfile 就存 nmcli -t con show 的输出.
    哪一条备份不成都抛 OSError (fail-closed: 调用方就不删). 返回备份目录. 目录 0700, keyfile 里可能有密码."""
    dest = os.path.join(root, time.strftime("%Y%m%d-%H%M%S", time.localtime(now())))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for c in cons:
        src = _keyfile(c, nm_dir)
        if src:
            shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
        elif c.get("dump"):
            fd = os.open(os.path.join(dest, _safe_name(c["name"]) + ".nmcli.txt"),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(c["dump"].rstrip("\n") + "\n")
        else:
            raise OSError(f"{c['name']} 找不到 keyfile, 也读不到 nmcli 详情, 没法备份")
    return dest


def _purge(cons, run, log, backup, where):
    """新模式起来之后删旧配置: 先整体备份, 备份不成一条不删 (留着不碍事, 推车的优先级更高). 返回接在结果后的人话."""
    names = "、".join(c["name"] for c in cons)
    if backup is None:
        log(f"{where} 旧配置 {names} 没删: 没有备份手段")
        return f" · 旧配置 {names} 没删 (不备份不删)"
    try:
        dest = backup(cons)
    except (OSError, ValueError) as ex:
        log(f"{where} 旧配置 {names} 没删: 备份失败 {ex}")
        return f" · 旧配置 {names} 没删: 备份失败 ({ex}); 留着不碍事, 推车的连接优先级更高"
    gone, bad = [], []
    for c in cons:
        rc, out = run(["nmcli", "con", "delete", "uuid", c["uuid"]])
        (bad if rc else gone).append(c["name"])
        log(f"删旧配置 {where} {c['name']} ({c['uuid']}): "
            + (f"失败 {rc} {(out or '')[-80:]}" if rc else "成功") + f", 备份在 {dest}")
    msg = f" · 旧配置 {'、'.join(gone)} 已删, 备份在 {dest}" if gone else ""
    return msg + (f" · {'、'.join(bad)} 没删掉, 车边 nmcli con delete 再删" if bad else "")


def run_switch(plan, st, run, save, log=None, now=time.time, backup=None):
    """按计划切: 先写意图 -> undo 当前 -> apply 新; 失败撤新的、恢复原来的, 再不行落到干净状态.
    run(cmd) -> (rc, out); save(state) 写状态文件; log(一行) 记 events.log. 返回 (成功, 新状态, 人话).
    成功且 plan 有 purge (接管) 时: backup(旧配置) -> 备份目录, 再逐条删; backup 没给或失败就不删."""
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
    left = _undo_run(plan["cur"], run)
    log(f"undo {where} {before}: " + ("不用撤 (旧配置等新模式起来再处理)" if not plan["undo"] else
                                      "成功" if not left else f"{len(left)} 条没撤掉, 记着下次再撤"))
    ok, why = _run_until_fail(plan["apply"], run)
    t = int(now())

    def done(recs, prev, status="ok"):
        """落最终状态; 没撤掉的挂在第一条记录的 stale 上. 旧连接没有遗留就不留记录 (= 未托管)."""
        recs = [dict(r, status=status, since=t) for r in recs]
        if left and recs:
            recs[0]["stale"] = list(left)
        recs = [r for r in recs if r["mode"] != LEGACY or r.get("stale")]
        new_st = dict(base, modes=others + recs, prev=prev)
        try:
            save(new_st)
        except OSError as ex:
            log(f"状态文件写不了: {ex}")
        return new_st

    if ok:
        log(f"apply {where} {describe(plan['new'])}: 成功; 撤销命令: {_sh(plan['rollback'])}")
        prev = dict(st.get("prev", {}))
        for r in plan["cur"]:
            if r["mode"] != LEGACY:                     # 旧连接接管后要删, 不当「上一个」
                prev.update({p: _bare(r) for p in r["ports"]})
        later = f" · {_ports_text(plan['no_link'])} 没插线, 插上线自动连" if plan.get("no_link") else ""
        new_st = done([plan["new"]], prev)
        later += _purge(plan["purge"], run, log, backup, where) if plan.get("purge") else ""
        return True, new_st, f"{where} 切换成功: {describe(plan['new'])}{later}"

    log(f"apply {where} {describe(plan['new'])}: 失败 {why}")
    left += _undo_run([plan["new"]], run)
    log(f"undo {where} {describe(plan['new'])}: apply 失败自动撤 ({_sh(plan['rollback'])})")
    prev = st.get("prev", {})
    ok, why2 = _run_until_fail(plan["restore"], run)
    if ok:
        log(f"apply {where} 恢复原来的 {before}: 成功")
        return False, done(plan["restore_recs"], prev), f"切换失败 ({why}), 已回到原来的 {before}"
    left += _undo_run(plan["restore_recs"], run)
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
        return f"有 {len(r['stale'])} 条推车建的东西上次没撤掉, 下次切换会再撤" if r.get("stale") else ""
    return f"记录是「{describe(r)}」, 但网口上实际生效的是「{active['name']}」" if active else \
        f"记录是「{describe(r)}」, 但网口上现在没有生效的连接"


def port_views(st, cons_text, ip_br, routes, links):
    """每个网口一行: 当前模式 (未托管就按现状认)、和记录对不对得上、实际地址、是不是默认出口."""
    cons, ips, defaults = _eth_cons(cons_text), npc.parse_ip_br(ip_br), parse_default_routes(routes)
    out = []
    for iface in PORTS:
        r = owner(st, iface)
        active = next((c for c in cons if c["device"] == iface and c["active"]), None)
        managed = r is not None and r["mode"] != LEGACY
        cur = r if managed else (legacy(iface, active["uuid"], active["name"]) if active else legacy(iface))
        metric = dict(reversed(defaults)).get(iface)
        egress = "" if metric is None else "默认出口" if defaults[0][0] == iface else f"备用出口 (metric {metric})"
        out.append({"iface": iface, "port": PORTS[iface], "mode": cur["mode"], "title": describe(cur),
                    "managed": managed, "drift": _drift(r, active) if r else "", "rec": cur,
                    "ip": ips.get(iface, "-"), "link": links.get(iface), "egress": egress})
    return out


# ------------------------------------------------------------ 确认框

def confirm_text(plan, ip="", backup_dir=BACKUP_DIR):
    """确认框: 原来/改为、要注意的、将删除的旧配置、将执行的命令、出问题时的 undo 命令 (接 #8 的回滚文案)."""
    where = " + ".join(f"{PORTS[p]} ({p})" for p in plan["ports"])
    before = " / ".join(describe(r) for r in plan["cur"]) + (f" · {ip}" if ip else "")
    lines = [f"{where} 切换网口模式", "", f"原来: {before}", f"改为: {describe(plan['new'])}", ""]
    if plan["warn_remote"]:
        lines.append("LAN2 是管理口, 切换时经它的远程连接会断 (无线和 tailscale 还在).")
    if plan["stop_pxe"]:
        lines.append("会先停 PXE 装机服务; 要用再去「装机」页打开.")
    if plan["no_link"]:
        lines.append(f"{_ports_text(plan['no_link'])} 没插线: 只把配置建好, 插上线 NetworkManager 自动连上.")
    if plan["purge"]:
        lines.append(f"将删除旧配置: {'、'.join(c['name'] for c in plan['purge'])} "
                     f"(新模式起来之后才删, 删之前备份到 {backup_dir}/<时间>/; 备份不成就不删).")
    if plan["pxe_rebuild"]:
        lines.append("装机口地址要用静态 IP 重建, PXE 服务会停.")
    if lines[-1]:
        lines.append("")
    lines.append("将执行的命令:")
    lines += ["  " + shlex.join(c) for c in plan["commands"]] or ["  (无)"]
    if plan["purge"]:
        lines.append("  成功之后, 备份再删旧配置:")
        lines += ["    " + shlex.join(["nmcli", "con", "delete", "uuid", c["uuid"]]) for c in plan["purge"]]
    lines.append("出问题时的 undo 命令 (切换失败会自动执行; 车边终端也能照抄):")
    lines.append("  撤掉新模式:")
    lines += ["    " + shlex.join(c) for c in plan["rollback"]] or ["    (不用撤)"]
    lines.append("  回到原来:")
    lines += ["    " + shlex.join(c) for c in plan["restore"]] or ["    (原来没有生效的连接)"]
    return "\n".join(lines + ["", "要继续吗?"])
