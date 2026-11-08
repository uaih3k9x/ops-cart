#!/usr/bin/env python3
"""cart_accept 的纯逻辑: 跑检查的框架、各项判定、"下一版必带什么" 的清单推导. 只用标准库, Python 3.9.

判定函数一律返回 (状态, 一行原因); 状态四种: PASS / FAIL / SKIP / 人工.
清单 (REQUIRED_PY / VENV_PY / ASSETS / SYSTEM_FILES) 是从仓库推出来的常量 —— 推车上没有仓库,
所以抄成常量; tests/test_accept_core.py 拿 derive(仓库) 比对, 改了 import 或 deploy/ 没同步就红.
同步: python3 scripts/cart_accept.py --print-manifest, 把输出抄回下面.
"""

import ast
import json
import os
import re
import stat
import threading
import time

PASS, FAIL, SKIP, MANUAL = "PASS", "FAIL", "SKIP", "人工"
STATUSES = (PASS, FAIL, SKIP, MANUAL)
DEFAULT_TIMEOUT = 20


# ============================================================ 跑检查

class Check:
    """name 给人看, issue 是对应的 issue 号 ("#9" / "-"), fn() -> (状态, 原因). timeout 不给用 run 的默认."""

    def __init__(self, name, issue, fn, timeout=None):
        self.name, self.issue, self.fn, self.timeout = name, issue, fn, timeout


def run_one(check, timeout=DEFAULT_TIMEOUT):
    """在线程里跑一项: 超时 / 抛异常 / 返回值不对都算 FAIL, 不往外抛. 超时的线程是 daemon, 不拖住退出."""
    box = {}

    def target():
        try:
            box["r"] = check.fn()
        except BaseException as e:      # noqa: BLE001  单项炸了不能连累后面
            box["e"] = e

    t0 = time.monotonic()
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(check.timeout or timeout)
    out = {"name": check.name, "issue": check.issue, "secs": round(time.monotonic() - t0, 1)}
    if th.is_alive():
        return dict(out, status=FAIL, why=f"超时 ({check.timeout or timeout} 秒没跑完)")
    if "e" in box:
        e = box["e"]
        return dict(out, status=FAIL, why=f"检查本身出错 {type(e).__name__}: {e}")
    r = box.get("r")
    if not (isinstance(r, tuple) and len(r) == 2 and r[0] in STATUSES):
        return dict(out, status=FAIL, why=f"检查返回值不对: {r!r}")
    return dict(out, status=r[0], why=str(r[1]))


def format_line(r):
    why = " ".join(str(r.get("why", "")).split())
    return f"[{r['status']:<4}] {r['issue']:<7} {r['name']}  —  {why}"


def run_all(checks, timeout=DEFAULT_TIMEOUT, out=print):
    results = []
    for c in checks:
        r = run_one(c, timeout)
        results.append(r)
        if out is not None:
            out(format_line(r))
    return results


def summary(results):
    """-> (汇总文字, 退出码). 有 FAIL 退出码 1. 失败和人工的项目再列一次, 方便抄进 issue."""
    n = {s: sum(1 for r in results if r["status"] == s) for s in STATUSES}
    lines = ["", "=" * 60, "汇总: " + "  ".join(f"{s} {n[s]}" for s in STATUSES)]
    for s, head in ((FAIL, "要处理的 FAIL:"), (MANUAL, "要人看的:")):
        rows = [r for r in results if r["status"] == s]
        if rows:
            lines.append(head)
            lines += ["  " + format_line(r) for r in rows]
    return "\n".join(lines), (1 if n[FAIL] else 0)


# ============================================================ 版本

def judge_version(text, expect):
    if text is None:
        return FAIL, "读不到 /opt/pxe/VERSION"
    v = text.strip()
    if v == expect:
        return PASS, v
    return FAIL, f"推车上是 {v}, 应为 {expect} (还没部署新版?)"


_BUILD = re.compile(r"^(\d+) ([0-9a-f]{7,40})(-dirty)?$")


def judge_build(text, expect=None):
    if text is None:
        return FAIL, "读不到 /opt/pxe/BUILD (部署时没跑 scripts/stamp-build.sh?)"
    b = text.strip()
    m = _BUILD.match(b)
    if not m:
        return FAIL, f"BUILD 格式不对: {b!r}"
    if m.group(3):
        return FAIL, f"{b}: 带 -dirty, 部署的是没提交的代码"
    if expect and b != expect.strip():
        return FAIL, f"推车上是 {b}, 清单里是 {expect.strip()}"
    return PASS, b + ("" if expect else " (没给预期值, 对一下 stamp-build.sh 当时的输出)")


# ============================================================ 清单推导

THIRD_PARTY = ("PIL", "usb")             # 只有副屏用, 装在 /opt/pxe/venv
OCR_THIRD_PARTY = ("rapidocr_openvino",)        # 只有 ocr_rapid_worker 用, 装在 /opt/pxe/ocr-venv (#47)
_PYREF = re.compile(r"\b(\w+)\.py\b")
_OPTREF = re.compile(r"/opt/pxe/(\w+)\.py\b")


def py_imports(src):
    """顶层名字: import x / from x import y (绝对) / __import__("x") / importlib.import_module("x")."""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            out.add(n.module.split(".")[0])
        elif isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("__import__", "import_module"):
                out.add(n.args[0].value.split(".")[0])
    return out


def _docstring_ids(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def py_file_refs(src, local):
    """代码里 (不算 docstring) 以 "xxx.py" 出现的本地模块: 子进程调用的 (recipe 调 cart_snap.py 那种)."""
    tree = ast.parse(src)
    skip = _docstring_ids(tree)
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            out |= {m for m in _PYREF.findall(n.value) if m in local}
    return out


def opt_pxe_refs(text):
    return set(_OPTREF.findall(text))


def closure(entries, graph):
    seen, todo = set(), list(entries)
    while todo:
        m = todo.pop()
        if m in seen or m not in graph:
            continue
        seen.add(m)
        todo += sorted(graph[m])
    return seen


# deploy/<子目录> -> 推车上的目录; 改名的单列. selinux 是装模块不是拷文件, pxe.env.example 要按现场改,
# srv-data/ 只在装了数据盘时由 setup-srv-data.sh / setup-samba.sh 装 (#11, 可选), 都不比对.
DEPLOY_DIRS = {"systemd": "/etc/systemd/system", "tmpfiles.d": "/etc/tmpfiles.d", "firewalld": "/etc/firewalld/zones",
               "logrotate.d": "/etc/logrotate.d", "sysctl.d": "/etc/sysctl.d",
               "journald.conf.d": "/etc/systemd/journald.conf.d", "bin": "/usr/local/bin"}
DEPLOY_RENAME = {"bin/kiosk-launcher": "kiosk"}
DEPLOY_SKIP = ("selinux/", "pxe.env.example", "srv-data/", "ocr-requirements.txt")
ASSET_DIRS = ("web", "icons")
FONT_DEST = "/usr/local/share/fonts/ops-cart"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def derive(root):
    """从仓库推 "推车上必须有什么":
    py      = 入口的 import 闭包. 入口 = deploy/ 和 scripts/*.sh 里写到的 /opt/pxe/<m>.py,
              加模块 docstring 里写的命令行用法 (sudo python3 /opt/pxe/recipe.py ...).
    venv    = 闭包里用到 PIL / usb 的 (只能用 /opt/pxe/venv/bin/python import).
    assets  = web/ icons/ 全部 (相对 /opt/pxe); 字体算 system (装到 FONT_DEST).
    system  = {推车上的路径: deploy 里的源文件}."""
    local = {f[:-3] for f in os.listdir(root) if f.endswith(".py")}
    srcs = {m: _read(os.path.join(root, m + ".py")) for m in local}
    imports = {m: py_imports(s) for m, s in srcs.items()}
    graph = {m: (imports[m] & local) | py_file_refs(s, local) for m, s in srcs.items()}
    graph = {m: g - {m} for m, g in graph.items()}

    entries, system, unmapped = set(), {}, []
    ddir = os.path.join(root, "deploy")
    for dp, _, files in os.walk(ddir):
        for f in files:
            rel = os.path.relpath(os.path.join(dp, f), ddir).replace(os.sep, "/")
            entries |= opt_pxe_refs(_read(os.path.join(dp, f)))
            if rel.startswith(DEPLOY_SKIP):
                continue
            sub = rel.split("/", 1)[0]
            if sub not in DEPLOY_DIRS or "/" not in rel:
                unmapped.append(rel)
                continue
            system[DEPLOY_DIRS[sub] + "/" + DEPLOY_RENAME.get(rel, f)] = "deploy/" + rel
    sdir = os.path.join(root, "scripts")
    for f in os.listdir(sdir):
        if f.endswith(".sh"):
            entries |= opt_pxe_refs(_read(os.path.join(sdir, f)))
    for s in srcs.values():
        entries |= opt_pxe_refs(s)
    py = closure(entries & local, graph)

    third = set()
    for m in py:
        third |= imports[m] - local
    venv = {m for m in py if any(imports[x] & set(THIRD_PARTY) for x in closure({m}, graph))}

    assets = []
    for d in ASSET_DIRS:
        for dp, _, files in os.walk(os.path.join(root, d)):
            assets += [os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/") for f in files]
    # 字体只装系统字体目录 (theme.UNIFONT_PATHS 先找那里); 仓库里的 fonts/ 是开发机预览用的, 不拷进 /opt/pxe
    fonts = sorted(f for f in os.listdir(os.path.join(root, "fonts")) if f.endswith(".otf"))
    for f in fonts:
        system[FONT_DEST + "/" + f] = "fonts/" + f

    return {"py": sorted(py), "venv": sorted(venv), "assets": sorted(assets),
            "system": dict(sorted(system.items())), "unmapped": sorted(unmapped),
            "third_party": _not_stdlib(third)}


def _not_stdlib(names):
    """3.10+ 才有标准库清单; 3.9 上返回空 (只给测试用)."""
    import sys
    std = getattr(sys, "stdlib_module_names", None)
    return set() if std is None else {n for n in names if n not in std}


# ---- 以下常量由 `python3 scripts/cart_accept.py --print-manifest` 生成, 测试比对仓库 ----
REQUIRED_PY = (
    "atomicfile", "cart_dream", "cart_label", "cart_mcp", "cart_ocr", "cart_snap",
    "cartlog", "ch9329", "clone_core", "confirm_core", "confirm_panel", "dash_core",
    "dash_render", "dash_service", "ocr_rapid_worker", "pet_core", "pet_render", "pet_sprites", "hidpdc", "httpserve", "kiosk", "kiosk_core",
    "kiosk_ui_core", "ks_panel", "kvm_broker", "kvm_broker_core", "kvm_core", "lamps_core",
    "lldp_core", "mcp_core", "mcp_kvm", "mcp_label", "mcp_pxe", "mcp_serial", "netmode_core",
    "netmode_panel", "netprof_core", "notify_core", "pxe_log_core", "pxe_req_core", "pxe_req_panel",
    "pxeserver", "recipe", "recipe_core", "ruijie_portal", "serial_core", "serial_gate_model",
    "serial_panel", "serial_write_core", "sun_core", "survey_core", "theme", "tspl_preview",
    "udisp", "udisp_usb", "ups_core", "web_core", "webkiosk", "wifi_core",
    "wifi_panel", "wifi_prefer", "wol_core", "wol_panel",
)
VENV_PY = (
    "dash_render", "dash_service", "pet_render", "udisp_usb",
)
ASSETS = (
    "icons/net-off.png",
    "icons/net-on.png", "icons/net-warn.png",
    "web/index.html", "web/vendor/LICENSE.xterm",
    "web/vendor/xterm.css", "web/vendor/xterm.js",
)
SYSTEM_FILES = {
    "/etc/firewalld/zones/pxe.xml": "deploy/firewalld/pxe.xml",
    "/etc/logrotate.d/ops-cart": "deploy/logrotate.d/ops-cart",
    "/etc/sysctl.d/90-wlan-rp-filter.conf": "deploy/sysctl.d/90-wlan-rp-filter.conf",
    "/etc/sysctl.d/91-powerloss.conf": "deploy/sysctl.d/91-powerloss.conf",
    "/etc/systemd/journald.conf.d/ops-cart.conf": "deploy/journald.conf.d/ops-cart.conf",
    "/etc/systemd/system/campus-auth.service": "deploy/systemd/campus-auth.service",
    "/etc/systemd/system/campus-auth.timer": "deploy/systemd/campus-auth.timer",
    "/etc/systemd/system/cart-boot-success.service": "deploy/systemd/cart-boot-success.service",
    "/etc/systemd/system/cart-boot-success.timer": "deploy/systemd/cart-boot-success.timer",
    "/etc/systemd/system/cart-dash.service": "deploy/systemd/cart-dash.service",
    "/etc/systemd/system/cart-dream.service": "deploy/systemd/cart-dream.service",
    "/etc/systemd/system/cart-dream.timer": "deploy/systemd/cart-dream.timer",
    "/etc/systemd/system/cart-fan-curve.service": "deploy/systemd/cart-fan-curve.service",
    "/etc/systemd/system/cart-mcp-ro.service": "deploy/systemd/cart-mcp-ro.service",
    "/etc/systemd/system/cart-mcp.service": "deploy/systemd/cart-mcp.service",
    "/etc/systemd/system/cart-ocr.service": "deploy/systemd/cart-ocr.service",
    "/etc/systemd/system/cart-notify.service": "deploy/systemd/cart-notify.service",
    "/etc/systemd/system/cart-revtunnel.service": "deploy/systemd/cart-revtunnel.service",
    "/etc/systemd/system/cart-notify.timer": "deploy/systemd/cart-notify.timer",
    "/etc/systemd/system/cart-serial-req.path": "deploy/systemd/cart-serial-req.path",
    "/etc/systemd/system/cart-serial-req.service": "deploy/systemd/cart-serial-req.service",
    "/etc/systemd/system/kiosk.service": "deploy/systemd/kiosk.service",
    "/etc/systemd/system/kvm-broker.service": "deploy/systemd/kvm-broker.service",
    "/etc/systemd/system/pxe-http-boot.service": "deploy/systemd/pxe-http-boot.service",
    "/etc/systemd/system/pxe-http-repo.service": "deploy/systemd/pxe-http-repo.service",
    "/etc/systemd/system/pxe-server.service": "deploy/systemd/pxe-server.service",
    "/etc/systemd/system/webkiosk.service": "deploy/systemd/webkiosk.service",
    "/etc/systemd/system/wifi-prefer.service": "deploy/systemd/wifi-prefer.service",
    "/etc/systemd/system/wifi-prefer.timer": "deploy/systemd/wifi-prefer.timer",
    "/etc/tmpfiles.d/ops-cart.conf": "deploy/tmpfiles.d/ops-cart.conf",
    "/usr/local/bin/cart-fan-curve": "deploy/bin/cart-fan-curve",
    "/usr/local/bin/kiosk": "deploy/bin/kiosk-launcher",
    "/usr/local/bin/kiosk-setpin": "deploy/bin/kiosk-setpin",
    "/usr/local/share/fonts/ops-cart/unifont-18.0.01.otf": "fonts/unifont-18.0.01.otf",
}
# ---- 生成段结束 ----


# ============================================================ systemd

def _u(enabled, active, issue, oneshot=False, note=""):
    return {"enabled": enabled, "active": active, "issue": issue, "oneshot": oneshot, "note": note}


# enabled / active: True = 必须; None = 按需 (没开自启算 PASS, 开了就得在跑). oneshot 只看上次跑成没成.
UNITS = {
    "kiosk.service": _u(True, True, "-", note="车机界面"),
    "cart-dash.service": _u(True, True, "#26", note="副屏 + 状态快照"),
    "kvm-broker.service": _u(True, True, "#9", note="键鼠代理"),
    "pxe-server.service": _u(None, None, "#11", note="装机口 DHCP/TFTP"),
    "pxe-http-boot.service": _u(None, None, "#11"),
    "pxe-http-repo.service": _u(None, None, "#11"),
    "webkiosk.service": _u(None, None, "#24", note="网页口 (含 PXE 日志页)"),
    "cart-mcp.service": _u(None, None, "#19", note="MCP HTTP; 默认不开, 只用 stdio"),
    "cart-mcp-ro.service": _u(None, None, "#49", note="MCP HTTP 只读档; 默认不开"),
    "cart-ocr.service": _u(None, None, "#47", note="OCR 常驻服务; 不开退回 Tesseract"),
    "cart-serial-req.path": _u(None, None, "#23", note="网页急停 / 拒绝"),
    "cart-serial-req.service": _u(None, None, "#23", oneshot=True),
    "cart-notify.timer": _u(True, True, "#4", note="关键事件推手机 (UPS 预告也走它)"),
    "cart-notify.service": _u(None, None, "#4", oneshot=True),
    "cart-boot-success.timer": _u(True, True, "-"),
    "cart-boot-success.service": _u(None, None, "-", oneshot=True),
    "campus-auth.timer": _u(True, True, "-"),
    "campus-auth.service": _u(None, None, "-", oneshot=True),
    "wifi-prefer.timer": _u(True, True, "-"),
    "wifi-prefer.service": _u(None, None, "-", oneshot=True),
    "cart-dream.timer": _u(None, None, "-"),
    "cart-dream.service": _u(None, None, "-", oneshot=True),
    "cart-fan-curve.service": _u(None, None, "-", oneshot=True),
    "cart-revtunnel.service": _u(None, None, "-", note="备用远程通道; 没 revtunnel.env 就不起"),
}
SHOW_PROPS = "Id,LoadState,ActiveState,SubState,UnitFileState,Result,ExecMainStatus,NeedDaemonReload"
_ENABLED = ("enabled", "enabled-runtime", "alias", "linked", "linked-runtime")


def parse_show(text):
    """`systemctl show -p ... a b c` -> {Id: {属性: 值}}; 段间空行."""
    out = {}
    for chunk in text.split("\n\n"):
        kv = dict(line.split("=", 1) for line in chunk.splitlines() if "=" in line)
        if kv.get("Id"):
            out[kv["Id"]] = kv
    return out


def judge_unit(spec, props):
    if not props:
        return FAIL, "systemctl 没回这个单元的信息"
    if props.get("LoadState") != "loaded":
        return FAIL, f"没装 (LoadState={props.get('LoadState')}, 没拷进 /etc/systemd/system 或没 daemon-reload)"
    act, en = props.get("ActiveState", ""), props.get("UnitFileState", "")
    enabled = en in _ENABLED
    state = f"{act}/{props.get('SubState', '?')}, 自启={en or '-'}"
    if props.get("NeedDaemonReload") == "yes":
        return FAIL, f"{state}: 单元文件改了还没 systemctl daemon-reload"
    if props.get("ExecMainStatus") == "226":
        return FAIL, f"{state}, 退出码 226/NAMESPACE: 多半是 SELinux 模块 ops_cart_mounton 没装"
    if act == "failed":
        return FAIL, f"{state}, Result={props.get('Result')} 退出码 {props.get('ExecMainStatus', '?')}"
    if spec["oneshot"]:
        if props.get("Result", "success") != "success":
            return FAIL, f"{state}, 上次 Result={props.get('Result')}"
        return PASS, f"一次性单元, 上次没失败 ({state})"
    if spec["enabled"] and not enabled:
        return FAIL, f"{state}: 应开机自启 (systemctl enable)"
    if spec["active"] and act != "active":
        return FAIL, f"{state}: 应在跑"
    if spec["enabled"] is None and not enabled and act != "active":
        return PASS, f"未启用 (按需开, {spec['note'] or '可选'})"
    if enabled and act not in ("active", "activating", "reloading"):
        return FAIL, f"{state}: 开了自启却没在跑"
    return PASS, state


# ============================================================ SELinux / 字体 / 权限 / 配置

SELINUX_MODULE = "ops_cart_mounton"


def parse_semodule(text):
    return {line.split()[0] for line in text.splitlines() if line.strip()}


def judge_selinux(enforce, modules):
    enforce = (enforce or "").strip()
    if enforce == "Disabled":
        return MANUAL, "SELinux 关着 (Disabled), 模块装不装都行; 推车平时应是 Enforcing, 问一下 维护者"
    if SELINUX_MODULE in modules:
        return PASS, f"{enforce or '?'}, 模块 {SELINUX_MODULE} 已加载"
    return FAIL, f"{enforce or '?'}, 模块 {SELINUX_MODULE} 没加载 (webkiosk 在 Enforcing 下会 226 起不来)"


def judge_fonts(fc_text, family):
    """family: 车机要的族名, 调用方从推车上装的 theme.py (theme.UNIFONT) 取, 这里不写字体名."""
    if not family:
        return FAIL, "读不到 theme.UNIFONT (theme.py 没装或是旧版)"
    fams = set()
    for line in (fc_text or "").splitlines():
        for part in line.split(":")[0].split(","):
            fams.add(part.strip())
    if family in fams:
        return PASS, f"fc-list 里有 {family}"
    return FAIL, f"fc-list 取不到 {family} (装到 {FONT_DEST}/ 再 fc-cache -f)"


def judge_perm(st, mode, uid=None, gid=None):
    """st: os.stat_result. 只比权限位低 12 位."""
    have = stat.S_IMODE(st.st_mode)
    bad = []
    if have != mode:
        bad.append(f"权限 {have:04o}, 应为 {mode:04o}")
    if uid is not None and st.st_uid != uid:
        bad.append(f"属主 uid {st.st_uid}, 应为 {uid}")
    if gid is not None and st.st_gid != gid:
        bad.append(f"属组 gid {st.st_gid}, 应为 {gid}")
    return (FAIL, "; ".join(bad)) if bad else (PASS, f"{have:04o} 属主 {st.st_uid}:{st.st_gid}")


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "", 1).strip()
        out[k] = v.strip().strip("'\"")
    return out


# ============================================================ 日志

def _age(s):
    if s < 120:
        return f"{int(s)} 秒前"
    if s < 7200:
        return f"{int(s // 60)} 分钟前"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} 小时前"
    return f"{int(s // 86400)} 天前"


def judge_log(age_s, must, fresh_s=86400):
    """age_s: 距最后写入的秒数, None = 文件不在. must: 正常运行就该有 (events.log、kvm.log);
    其余是有事才写的 (ups.log 断电才写、pxe-req.log 有申请才写), 不在算 SKIP."""
    if age_s is None:
        return (FAIL, "不存在") if must else (SKIP, "还没有 (相关功能还没用过)")
    if not must or age_s <= fresh_s:
        return PASS, "最后写入 " + _age(age_s)
    return MANUAL, f"最后写入 {_age(age_s)}, 看看是不是该有新内容"


# ============================================================ 快照 (/run/ops-cart/state.json) 里能看出来的

SNAP_FRESH_S = 10


def judge_snapshot(snap, now):
    if not isinstance(snap, dict) or not isinstance(snap.get("ts"), (int, float)):
        return FAIL, "读不到 /run/ops-cart/state.json (cart-dash 没在跑?)"
    age = now - snap["ts"]
    if age > SNAP_FRESH_S:
        return FAIL, f"快照 {int(age)} 秒没更新 (cart-dash 卡住?)"
    return PASS, f"{age:.1f} 秒前更新, seq {snap.get('seq')}"


def _nb(nb):
    nb = nb or {}
    dev = nb.get("sys_name") or nb.get("system_name") or nb.get("chassis_id") or "?"
    port = nb.get("port_desc") or nb.get("port_id") or "?"
    return f"{dev} {port}" + (f" VLAN {nb['vlan']}" if nb.get("vlan") else "")


def judge_lldp(lldp):
    if not isinstance(lldp, dict) or not lldp:
        return FAIL, "快照里没有 lldp 这一项 (cart-dash 不是新版, 或 LLDP 监听没起来)"
    seen, off = [], []
    for port, e in sorted(lldp.items()):
        e = e or {}
        if e.get("state") in ("fresh", "stale") and e.get("neighbor"):
            seen.append(f"{port}: {_nb(e['neighbor'])}" + (" (已过期)" if e["state"] == "stale" else ""))
        elif e.get("state") == "off":
            off.append(f"{port}: {e.get('error') or '不可用'}")
    if seen:
        return PASS, "; ".join(seen + off)
    if off:
        return FAIL, "LLDP 监听起不来 — " + "; ".join(off)
    return MANUAL, "两个口都没收到 LLDP: 插到开了 LLDP 的交换机上等 30 秒再跑"


def judge_theme(theme):
    if not isinstance(theme, dict) or theme.get("mode") not in ("dark", "light"):
        return FAIL, "快照里没有 theme (cart-dash 不是 1.11 的?)"
    if theme.get("setting") is None:
        return FAIL, f"/etc/ops-cart/theme.json 读不懂, 按深色 (mode={theme['mode']})"
    nxt = theme.get("next_switch")
    when = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if isinstance(nxt, (int, float)) else "不切换"
    return PASS, f"档位 {theme['setting']}, 现在 {theme['mode']}, 下次切换 {when}"


def judge_ups(ups):
    if not isinstance(ups, dict) or ups.get("percent") is None:
        return FAIL, "快照里没有 UPS 读数 (USB 线没插 / upower 和 hidraw 都读不到)"
    bits = [f"电量 {ups.get('percent')}%"]
    for k in ("state", "status", "runtime", "source"):
        if ups.get(k) not in (None, ""):
            bits.append(f"{k}={ups[k]}")
    if "hidraw" not in str(ups.get("source", "")):
        bits.append("没读到 hidraw (市电/电池切换会滞后 1-2 分钟)")
    return PASS, ", ".join(bits)


def judge_guard(doc, minutes, now):
    """#11 自己发地址限时: 守卫状态文件 window = 限时秒数 (新版才有这个键)."""
    if not isinstance(doc, dict) or not doc:
        return SKIP, "pxe-server 没在跑 (没有活的守卫状态), 装机页开 PXE 后再跑"
    if "window" not in doc:
        return FAIL, "守卫状态里没有 window: pxeserver.py 还是旧版"
    want = int(minutes) * 60
    if doc.get("window") != want:
        return FAIL, f"限时 {doc.get('window')} 秒, pxe.env 配的是 {minutes} 分钟 (改了 pxe.env 没重启 pxe-server?)"
    st = doc.get("state")
    if st == "serving" and isinstance(doc.get("until"), (int, float)):
        return PASS, f"自己在发地址, 限时 {minutes} 分钟, 剩 {int((doc['until'] - now) // 60)} 分钟"
    return PASS, f"状态 {st}, 限时 {minutes} 分钟" + ("" if want else " (0 = 不限时)")


# ============================================================ MCP / 键鼠代理

MCP_TOOLS = {
    "#19": ("list_threads", "read_output", "screenshot", "cart_status", "pxe_status"),
    "#20": ("label_preview", "label_print", "label_printer_status", "label_abbrev_propose"),
    "#21": ("lease_acquire", "lease_renew", "lease_release", "send_command", "send_key", "pending_list", "estop"),
    "#24": ("pxe_request", "pxe_requests", "pxe_retire"),
    "#41": ("kvm_status", "kvm_lease_acquire", "kvm_lease_renew", "kvm_lease_release", "kvm_key", "kvm_type",
            "screen_ocr", "wol_send", "pxe_clients"),
}


def mcp_requests():
    """stdio 里喂进去的: 握手 + 列工具. 不调任何工具 (tools/call 才进审计、才可能动东西)."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "cart_accept", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    return "".join(json.dumps(m) + "\n" for m in msgs)


def parse_mcp(stdout):
    info, names = {}, set()
    for line in (stdout or "").splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        res = msg.get("result") if isinstance(msg, dict) else None
        if not isinstance(res, dict):
            continue
        if msg.get("id") == 1:
            info = res.get("serverInfo") or {}
        elif msg.get("id") == 2:
            names = {t.get("name") for t in res.get("tools") or () if isinstance(t, dict)}
    return info, names


def judge_mcp_tools(names, issue):
    want = MCP_TOOLS[issue]
    miss = [t for t in want if t not in names]
    if not names:
        return FAIL, "stdio 没列出任何工具"
    if miss:
        return FAIL, "缺工具: " + ", ".join(miss)
    return PASS, ", ".join(want)


KVM_SOCK = "/run/ops-kvm/kvm.sock"
KVM_STATUS_REQ = json.dumps({"op": "status", "src": "agent", "who": "cart_accept"})


def judge_kvm_status(reply):
    try:
        r = json.loads(reply)
    except ValueError:
        return FAIL, f"代理回的不是 JSON: {reply[:80]!r}"
    if not isinstance(r, dict) or not r.get("ok"):
        return FAIL, f"代理拒绝 status: {r!r}"[:200]
    return PASS, "代理应答 status: " + json.dumps(r.get("status"), ensure_ascii=False)[:160]


UPS_EXPECT = (40, 15, 30)       # 预告 %, 倒计时 %, 倒计时秒 (#4)


def netmode_summary(doc):
    """netmode.json -> 一行 "托管: 口=模式, ..."; modes 是列表, 每项 {mode, ports, status}. 状态不是 ok 的带上状态."""
    parts = []
    for m in (doc or {}).get("modes") or []:
        st = m.get("status")
        tag = f"{'+'.join(m.get('ports') or ['?'])}={m.get('mode')}"
        parts.append(tag if st in (None, "ok") else f"{tag}({st})")
    return "托管: " + (", ".join(parts) or "无")


PXE_PORTS = ("67/udp", "68/udp", "69/udp", "4011/udp", "80/tcp", "8000/tcp")


def judge_pxe_zone(zone, ports):
    """LAN1 所在 firewalld 区放没放行 PXE (#38). zone=None = firewalld 没开; 没放行时 DHCP 请求到不了服务."""
    if zone is None:
        return SKIP, "firewalld 没在跑"
    fix = "修: sudo nmcli con mod <LAN1 配置> connection.zone pxe 后重新 up"
    if not zone:
        return FAIL, f"eno1 不在任何防火墙区. {fix}"
    miss = [p for p in PXE_PORTS if p not in (ports or "").split()]
    if miss:
        return FAIL, f"eno1 在 {zone} 区, 没放行 {' '.join(miss)}, 新机器拿不到地址. {fix}"
    return PASS, f"eno1 在 {zone} 区, PXE 端口都放行"


def judge_rapid(installed, rc, out):
    """OCR 常驻服务 (#47) 可选: 没装退回 Tesseract, 装了却跑不起来才算错."""
    if not installed:
        return SKIP, "没装 ocr-venv: screen_ocr 用 Tesseract, 彩色桌面读不出 (scripts/setup-ocr.sh 装, 再开 cart-ocr)"
    if rc != 0:
        return FAIL, f"ocr-venv 跑不起来, screen_ocr 退回 Tesseract: {(out or '').strip()[-120:]}"
    return PASS, f"rapidocr {(out or '').strip()}"

