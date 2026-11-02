#!/usr/bin/env python3
"""推车验收 (只读): 在推车上跑, 逐项打印 PASS / FAIL / SKIP / 人工 + 一行原因, 最后汇总.

    scp scripts/cart_accept.py scripts/accept_core.py [scripts/accept-manifest.json] <推车>:/tmp/accept/
    ssh <推车> sudo python3 /tmp/accept/cart_accept.py            # 全部
    ssh <推车> sudo python3 /tmp/accept/cart_accept.py --only '#9'  # 只跑名字或 issue 号里带 #9 的

只读: 不 start/stop/enable 任何单元、不写文件、不调任何 MCP 工具 (只握手 + 列工具, 审计写 /dev/null)、
键鼠代理只问 status (不发键、不进日志); python 一律 -B 不写 __pycache__. 每项有超时, 一项炸了不影响后面.
退出码: 有 FAIL = 1, 否则 0.

开发机上:
    python3 scripts/cart_accept.py --make-manifest scripts/accept-manifest.json   # 先 stamp-build.sh; 带上它可比对每个文件的 sha256
    python3 scripts/cart_accept.py --print-manifest                               # 重新生成 accept_core 里的清单常量
"""

import argparse
import functools
import glob
import grp
import hashlib
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True      # 拷到推车上跑, 也不在旁边留 __pycache__
sys.path.insert(0, HERE)
import accept_core as ac  # noqa: E402

OPT_DEFAULT = "/opt/pxe"
OPT = OPT_DEFAULT                   # --opt 可换 (开发机上拿仓库目录冒烟)
ETC = "/etc/ops-cart"
RUN = "/run/ops-cart"
LOG = "/var/log/ops-cart"
EXPECT_VERSION = "1.12.04"          # 没有仓库的 VERSION 可读时用 (推车上单独拷过去跑)
MANIFEST = os.path.join(HERE, "accept-manifest.json")
PY_ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
PY_ENV.pop("PYTHONSTARTUP", None)


# ============================================================ 小工具

def sh(cmd, timeout=10, inp=None):
    """-> (rc, stdout, stderr). 命令不在 127, 超时 124. 不抛."""
    try:
        r = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "超时"
    except OSError as e:
        return 127, "", str(e)


def venv_py():
    return OPT + "/venv/bin/python"


def sys_py():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def py(code, venv=False, timeout=15):
    """在 /opt/pxe 下用推车的 python 跑一段 (import 已装的模块做判定). -I 不从当前目录 / 环境变量捡模块,
    -B 不写字节码. /opt/pxe 不在就不跑 (免得 import 到别处的同名模块)."""
    if not os.path.isdir(OPT):
        return 127, "", f"没有 {OPT}"
    exe = venv_py() if venv else sys_py()
    try:
        r = subprocess.run([exe, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {OPT!r})\n" + code],
                           cwd=OPT, capture_output=True, text=True, timeout=timeout, env=PY_ENV)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "超时"
    except OSError as e:
        return 127, "", str(e)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def load_json(path):
    """-> (doc, 错误). 文件不在: (None, None)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return None, f"{path} 读不懂: {e}"


def age(path):
    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except OSError:
        return None


def gid_of(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def uid_of(name):
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def last_err(text, n=160):
    lines = [x for x in (text or "").strip().splitlines() if x.strip()]
    return lines[-1][:n] if lines else ""


def few(items, n=12):
    items = list(items)
    return " ".join(items[:n]) + (f" ... 等 {len(items)} 个" if len(items) > n else "")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@functools.lru_cache(maxsize=None)
def units_show():
    rc, out, err = sh(["systemctl", "show", "-p", ac.SHOW_PROPS] + sorted(ac.UNITS))
    if rc == 127:
        return None
    return ac.parse_show(out)


@functools.lru_cache(maxsize=None)
def snapshot():
    doc, _ = load_json(RUN + "/state.json")
    return doc


@functools.lru_cache(maxsize=None)
def pxe_env():
    t = read(ETC + "/pxe.env")
    return None if t is None else ac.parse_env(t)


@functools.lru_cache(maxsize=None)
def mcp_list():
    """stdio 握手 + 列工具 -> (serverInfo, 工具名集合, 出错说明)."""
    if not os.path.exists(OPT + "/cart_mcp.py"):
        return {}, set(), "没有 /opt/pxe/cart_mcp.py"
    try:
        r = subprocess.run([sys_py(), "-B", OPT + "/cart_mcp.py", "stdio", "--audit", "/dev/null"],
                           input=ac.mcp_requests(), capture_output=True, text=True, timeout=30, cwd=OPT, env=PY_ENV)
    except subprocess.TimeoutExpired:
        return {}, set(), "stdio 30 秒没回完"
    except OSError as e:
        return {}, set(), str(e)
    info, names = ac.parse_mcp(r.stdout)
    return info, names, ("" if names else f"rc={r.returncode} {last_err(r.stderr)}")


def unit_enabled(name):
    props = (units_show() or {}).get(name) or {}
    return props.get("UnitFileState") in ac._ENABLED


# ============================================================ 检查 (每个返回 (状态, 原因))

def chk_root():
    if os.geteuid() == 0:
        return ac.PASS, "root"
    return ac.MANUAL, "不是 root: semodule、0600 的配置读不了, 会多出 SKIP/FAIL; 用 sudo 再跑一遍"


def chk_platform():
    v = sys.version.split()[0]
    if not os.path.isdir(OPT):
        return ac.MANUAL, f"Python {v}; 这台没有 {OPT} —— 不是推车? 下面大部分会 FAIL/SKIP"
    return ac.PASS, f"Python {v} ({sys.platform})"


def make_version_checks(expect_version, expect_build, manifest):
    def version():
        return ac.judge_version(read(OPT + "/VERSION"), expect_version)

    def build():
        return ac.judge_build(read(OPT + "/BUILD"), expect_build or (manifest or {}).get("build"))

    def py_files():
        miss = [m for m in ac.REQUIRED_PY if not os.path.isfile(f"{OPT}/{m}.py")]
        if miss:
            return ac.FAIL, f"缺 {len(miss)} 个: " + few(m + ".py" for m in miss)
        return ac.PASS, f"{len(ac.REQUIRED_PY)} 个模块都在"

    def assets():
        miss = [a for a in ac.ASSETS if not os.path.isfile(f"{OPT}/{a}")]
        if miss:
            return ac.FAIL, f"缺 {len(miss)} 个: " + few(miss)
        return ac.PASS, f"web/ icons/ fonts/ 共 {len(ac.ASSETS)} 个文件都在"

    def system_files():
        miss = [p for p in ac.SYSTEM_FILES if not os.path.exists(p)]
        if miss:
            return ac.FAIL, f"缺 {len(miss)} 个: " + few(miss)
        return ac.PASS, f"单元 / tmpfiles / logrotate / sysctl / journald / bin / 字体 共 {len(ac.SYSTEM_FILES)} 个都在"

    def hashes():
        if not manifest:
            return ac.SKIP, f"没带清单 ({os.path.basename(MANIFEST)}): 开发机上 --make-manifest 生成后一起拷来, 可逐个比 sha256"
        diff, miss = [], []
        for path, want in sorted(manifest.get("files", {}).items()):
            if path.startswith(OPT_DEFAULT + "/"):
                path = OPT + path[len(OPT_DEFAULT):]
            if not os.path.exists(path):
                miss.append(path)
            elif sha256(path) != want:
                diff.append(path)
        if diff or miss:
            return ac.FAIL, (f"内容不一致 {len(diff)} 个: " + few(diff) if diff else "") + \
                (f"  缺 {len(miss)} 个: " + few(miss) if miss else "")
        return ac.PASS, f"{len(manifest['files'])} 个文件和 {manifest.get('version')} 清单逐个一致"

    return [ac.Check("VERSION", "-", version), ac.Check("BUILD", "-", build),
            ac.Check("必带的 py 模块都在 /opt/pxe", "-", py_files),
            ac.Check("静态资源 (网页 / 图标 / 字体)", "-", assets),
            ac.Check("系统文件 (单元 / tmpfiles / logrotate ...)", "-", system_files),
            ac.Check("文件内容和清单 sha256 一致", "-", hashes, timeout=60)]


def unit_checks():
    def one(name):
        def f():
            show = units_show()
            if show is None:
                return ac.SKIP, "没有 systemctl (不是推车)"
            return ac.judge_unit(ac.UNITS[name], show.get(name))
        return f
    return [ac.Check(f"单元 {n}", spec["issue"], one(n)) for n, spec in ac.UNITS.items()]


def chk_selinux():
    rc, out, _ = sh(["getenforce"])
    if rc == 127:
        return ac.SKIP, "没有 getenforce (不是推车)"
    rc2, mods, err = sh(["semodule", "-l"], timeout=60)
    if rc2 != 0:
        return ac.SKIP if os.geteuid() else ac.FAIL, f"{out.strip()}; semodule -l 跑不了 (要 root): {last_err(err)}"
    return ac.judge_selinux(out, ac.parse_semodule(mods))


def log_checks():
    def logdir():
        try:
            st = os.stat(LOG)
        except OSError:
            return ac.FAIL, f"{LOG} 不存在 (tmpfiles 没装 / 没 systemd-tmpfiles --create)"
        return ac.judge_perm(st, 0o750, uid=0, gid=gid_of("wheel"))

    def one(path, must):
        return lambda: ac.judge_log(age(path), must() if callable(must) else must)

    def slots():
        logs = glob.glob(LOG + "/serial-slot*.log")
        if not logs:
            return ac.SKIP, "还没有 (Agent 还没写过串口)"
        return ac.PASS, ", ".join(f"{os.path.basename(p)} {ac._age(age(p) or 0)}" for p in sorted(logs))

    def survey():
        d = LOG + "/survey"
        if not os.path.isdir(d):
            return ac.SKIP, "还没有巡检存档 (串口面板「巡检」还没按过)"
        n = sum(len(fs) for _, _, fs in os.walk(d))
        return ac.PASS, f"{n} 份存档"

    def journal():
        return (ac.PASS, "/var/log/journal 在 (断电前的日志能看)") if os.path.isdir("/var/log/journal") \
            else (ac.FAIL, "/var/log/journal 不在: journald 没落盘")

    broker = lambda: unit_enabled("kvm-broker.service")      # noqa: E731
    pxe = lambda: unit_enabled("pxe-server.service")        # noqa: E731
    return [ac.Check("日志目录 /var/log/ops-cart 0750 root:wheel", "-", logdir),
            ac.Check("events.log", "-", one(LOG + "/events.log", True)),
            ac.Check("kvm.log (键鼠代理)", "#9", one(LOG + "/kvm.log", broker)),
            ac.Check("ups.log (断电才写)", "#4", one(LOG + "/ups.log", False)),
            ac.Check("mcp-audit.log", "#19", one(LOG + "/mcp-audit.log", False)),
            ac.Check("pxe-req.log (PXE 申请)", "#24", one(LOG + "/pxe-req.log", False)),
            ac.Check("serial-slot*.log (Agent 写串口)", "#21", slots),
            ac.Check("巡检存档 survey/", "#15", survey),
            ac.Check("pxeserver.log", "#11", one(OPT + "/pxeserver.log", pxe)),
            ac.Check("journald 落盘", "-", journal)]


def font_checks():
    def fclist():
        rc, out, err = sh(["fc-list", ":", "family"], timeout=30)
        if rc == 127:
            return ac.SKIP, "没有 fc-list"
        rc2, fam, _ = py("import theme; print(theme.UNIFONT)")
        return ac.judge_fonts(out, fam if rc2 == 0 else None)

    def file_():
        fonts = [p for p in ac.SYSTEM_FILES if p.startswith(ac.FONT_DEST + "/")]
        miss = [p for p in fonts if not os.path.isfile(p)]
        return (ac.FAIL, "没有 " + " ".join(miss)) if miss else (ac.PASS, " ".join(fonts))

    def fangsong():
        rc, out, _ = sh(["rpm", "-q", "bitmap-fangsongti-fonts"])
        if rc == 127:
            return ac.SKIP, "没有 rpm"
        if rc == 0:
            return ac.FAIL, f"装了 {out.strip()}: 会抢全系统默认 monospace, 应卸掉"
        return ac.PASS, "没装 bitmap-fangsongti-fonts"

    def uifont_row():
        for src, doc in (("state.json", snapshot()), ("kiosk.json", load_json(RUN + "/kiosk.json")[0])):
            for row in (doc or {}).get("rows") or ():
                if isinstance(row, dict) and row.get("label") == "UI FONT":
                    st = row.get("state")
                    return (ac.PASS if st == "OK" else ac.FAIL), f"{src}: UI FONT {st} {row.get('detail', '')}"
        return ac.MANUAL, "快照里找不到 UI FONT 一行: 看车机开机自检里 UI FONT 是不是 OK UNIFONT 16PX"

    return [ac.Check("fc-list 能取到 Unifont", "#28", fclist),
            ac.Check("Unifont 字体文件", "#28", file_),
            ac.Check("没装 bitmap-fangsongti-fonts", "#28", fangsong),
            ac.Check("自检 UI FONT 行", "#28", uifont_row)]


def config_checks():
    def pxeenv():
        env = pxe_env()
        if env is None:
            return ac.FAIL, f"没有 {ETC}/pxe.env (pxe-* 三个单元起不来)"
        miss = [k for k in ("PXE_IP", "PXE_PREFIX", "PXE_POOL") if not env.get(k)]
        if miss:
            return ac.FAIL, "缺 " + ", ".join(miss)
        m = env.get("PXE_SERVE_MINUTES", "30")
        if not m.isdigit():
            return ac.FAIL, f"PXE_SERVE_MINUTES={m!r} 不是整数 (会退回 30)"
        return ac.PASS, f"装机网段 {env['PXE_IP']}/{env['PXE_PREFIX']}, 自己发地址限时 {m} 分钟"

    def theme():
        path = ETC + "/theme.json"
        doc, err = load_json(path)
        if err:
            return ac.FAIL, err
        if doc is None:
            return ac.MANUAL, "没有 theme.json: 一直深色. 要日出日落切换, 照 examples/theme.example.json 填推车经纬度"
        rc, out, e = py("import sun_core, json; print(json.dumps(sun_core.read_conf(%r)))" % path)
        if rc != 0:
            return ac.FAIL, f"sun_core 读不了: {last_err(e)}"
        conf = json.loads(out or "null")
        if conf is None:
            return ac.FAIL, "theme.json 内容不合规 (档位 / 经纬度 / 缓冲分钟), cart-dash 会按深色"
        return ac.PASS, f"档位 {conf['mode']}, 经纬度 {conf['lat']},{conf['lon']}, 缓冲 {conf['buffer_min']} 分钟"

    def gate():
        path = ETC + "/serial-gate.json"
        doc, err = load_json(path)
        if err:
            return ac.FAIL, err + " (闸门会按内置默认)"
        if doc is None:
            return ac.PASS, "没放规则文件 = 内置默认 (华为提示符自动认, 只放行 display / show, 决策模型不开)"
        rc, out, e = py("import serial_write_core as s, json; r = s.load_rules(%r); "
                        "print(json.dumps({'slots': r['slots'], 'model': r['model'].get('enabled')}))" % path)
        if rc != 0:
            return ac.FAIL, f"serial_write_core 读不了: {last_err(e)}"
        return ac.PASS, "能解析: " + out

    def notify():
        path = ETC + "/notify.json"
        if not os.path.exists(path):
            return ac.SKIP, "没有 notify.json: 事件只写 events.log, 不推手机"
        perm = ac.judge_perm(os.stat(path), 0o600, uid=0)
        if perm[0] != ac.PASS:
            return ac.FAIL, "notify.json 里可能有令牌: " + perm[1]
        rc, out, e = py("import notify_core as n; c = n.load_conf(%r); print(c and c.get('kind'))" % path)
        if rc != 0:
            return ac.FAIL, f"notify_core 不认: {last_err(e)}"
        return ac.PASS, f"0600 root, 渠道 {out}" + (" (enabled=false, 不外发)" if out == "None" else "")

    def mcp():
        path = ETC + "/mcp.json"
        if not os.path.exists(path):
            if unit_enabled("cart-mcp.service"):
                return ac.FAIL, "cart-mcp 开了自启却没有 mcp.json (谁都进不来)"
            return ac.SKIP, "没有 mcp.json (只用 stdio 时不需要)"
        perm = ac.judge_perm(os.stat(path), 0o600, uid=0)
        if perm[0] != ac.PASS:
            return ac.FAIL, "mcp.json 里是令牌: " + perm[1]
        doc, err = load_json(path)
        if err:
            return ac.FAIL, err
        toks = (doc or {}).get("tokens") or {}
        if not toks:
            return ac.FAIL, "没配令牌"
        if any("CHANGE" in str(k) or "CHANGE" in str(v) for k, v in toks.items()):
            return ac.FAIL, "令牌还是样例里的 CHANGE-ME"
        return ac.PASS, f"0600 root, {len(toks)} 个令牌, 端口 {(doc or {}).get('port', 8765)}"

    def abbrev():
        path = ETC + "/label_abbrev.json"
        doc, err = load_json(path)
        if err:
            return ac.FAIL, err
        if doc is None:
            return ac.MANUAL, "没有推车本地缩写表: 标签工具用仓库样例; 照 README 装一份"
        return ac.PASS, f"能解析, {len(doc) if hasattr(doc, '__len__') else '?'} 项"

    def simple(path, issue_hint):
        def f():
            doc, err = load_json(path)
            if err:
                return ac.FAIL, err
            if doc is None:
                return ac.SKIP, f"没有 {path} ({issue_hint})"
            return ac.PASS, "能解析"
        return f

    def webconf():
        path = OPT + "/web.conf"
        if not os.path.exists(path):
            return ac.SKIP, "没有 web.conf (网页口没装)"
        return ac.judge_perm(os.stat(path), 0o600, uid=uid_of("ops-web"))

    def serial_req():
        d = RUN + "/serial-req"
        if uid_of("ops-web") is None:
            return ac.SKIP, "没有 ops-web 用户 (网页口没装)"
        if not os.path.isdir(d):
            return ac.FAIL, f"没有 {d} (tmpfiles 是旧版? systemd-tmpfiles --create)"
        return ac.judge_perm(os.stat(d), 0o700, uid=uid_of("ops-web"))

    return [ac.Check("pxe.env", "#11", pxeenv),
            ac.Check("theme.json (日出日落)", "#31", theme),
            ac.Check("serial-gate.json (串口写闸门)", "#21", gate),
            ac.Check("notify.json 0600 且能解析", "#4", notify),
            ac.Check("mcp.json 0600 且有令牌", "#19", mcp),
            ac.Check("label_abbrev.json (标签缩写表)", "#20", abbrev),
            ac.Check("campus-auth.json", "-", simple(ETC + "/campus-auth.json", "校园网认证没配")),
            ac.Check("selftest.json", "-", simple(OPT + "/selftest.json", "用内置自检表")),
            ac.Check("clone.json (克隆共享)", "#18", simple(OPT + "/clone.json", "克隆还没配")),
            ac.Check("web.conf ops-web 0600", "#24", webconf),
            ac.Check("/run/ops-cart/serial-req ops-web 0700", "#23", serial_req)]


def import_checks():
    code = ("import importlib, json\nbad = {}\nfor m in %r:\n    try:\n        importlib.import_module(m)\n"
            "    except BaseException as e:\n        bad[m] = '%%s: %%s' %% (type(e).__name__, e)\nprint(json.dumps(bad))")

    def run(mods, venv):
        def f():
            if not os.path.isdir(OPT):
                return ac.SKIP, f"没有 {OPT}"
            if venv and not os.path.exists(venv_py()):
                return ac.FAIL, f"没有 {venv_py()} (副屏的 venv 没建)"
            rc, out, err = py(code % (list(mods),), venv=venv, timeout=90)
            if rc != 0:
                return ac.FAIL, f"rc={rc} {last_err(err)}"
            bad = json.loads(out.splitlines()[-1])
            if bad:
                return ac.FAIL, "; ".join(f"{m}: {e}" for m, e in sorted(bad.items()))[:600]
            return ac.PASS, f"{len(mods)} 个模块都能 import"
        return f
    sys_mods = [m for m in ac.REQUIRED_PY if m not in ac.VENV_PY]
    return [ac.Check("系统 python3 import 全部模块", "-", run(sys_mods, False), timeout=120),
            ac.Check("venv import 副屏模块 (Pillow / pyusb)", "#26", run(ac.VENV_PY, True), timeout=120)]


def issue_checks(expect_version):
    def snap_fresh():
        return ac.judge_snapshot(snapshot(), time.time())

    def snap_version():
        v = (snapshot() or {}).get("version") or ""
        if not v:
            return ac.FAIL, "快照里没有版本"
        return (ac.PASS if expect_version in v else ac.FAIL), f"快照写的是 {v!r} (cart-dash 读到的; 不对就是没重启 cart-dash)"

    def ups():
        return ac.judge_ups((snapshot() or {}).get("ups"))

    def ups_consts():
        rc, out, e = py("import ups_core as u; print(u.WARN_PCT, u.LOW_PCT, u.COUNTDOWN_S)")
        if rc != 0:
            return ac.FAIL, f"ups_core 读不了: {last_err(e)}"
        got = tuple(int(x) for x in out.split())
        want = ac.UPS_EXPECT
        if got != want:
            return ac.FAIL, f"推车上 ups_core 是 {got}, 应为 预告 {want[0]}% / 倒计时 {want[1]}% / {want[2]} 秒"
        return ac.PASS, f"两段式: {want[0]}% 预告, {want[1]}% 起 {want[2]} 秒倒计时 (真拔市电在验收单里)"

    def lldp():
        return ac.judge_lldp((snapshot() or {}).get("lldp"))

    def theme():
        return ac.judge_theme((snapshot() or {}).get("theme"))

    def guard():
        env = pxe_env() or {}
        m = env.get("PXE_SERVE_MINUTES", "30")
        rc, out, e = py("import kiosk_core as k, json; print(json.dumps(k.pxe_guard_live()))")
        if rc != 0:
            return ac.FAIL, f"kiosk_core 读不了守卫状态: {last_err(e)}"
        return ac.judge_guard(json.loads(out or "{}"), int(m) if m.isdigit() else 30, time.time())

    def kvm_sock():
        try:
            st = os.stat(ac.KVM_SOCK)
        except OSError:
            return ac.FAIL, f"没有 {ac.KVM_SOCK} (kvm-broker 没在跑)"
        if not stat.S_ISSOCK(st.st_mode):
            return ac.FAIL, f"{ac.KVM_SOCK} 不是 socket"
        return ac.judge_perm(st, 0o660, uid=0, gid=gid_of("dialout"))

    def kvm_status():
        if not os.path.exists(ac.KVM_SOCK):
            return ac.SKIP, "socket 不在, 见上一行"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(ac.KVM_SOCK)
            s.sendall(ac.KVM_STATUS_REQ.encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as e:
            return ac.FAIL, f"连不上代理: {e}"
        finally:
            s.close()
        return ac.judge_kvm_status(buf.decode("utf-8", "replace").strip())

    def ch9329_present():
        devs = sorted(glob.glob("/dev/serial/by-id/usb-1a86*"))
        if not devs:
            return ac.MANUAL, "没看到 CH9329 (/dev/serial/by-id/usb-1a86*): 键鼠线插了吗"
        return ac.PASS, f"{len(devs)} 块: " + ", ".join(os.path.basename(d)[:40] for d in devs)

    def mcp_handshake():
        info, names, err = mcp_list()
        if not names:
            return ac.FAIL, f"stdio 列不出工具: {err}"
        v = info.get("version", "?")
        st = ac.PASS if v == expect_version else ac.FAIL
        return st, f"服务 {info.get('name')} {v}, 共 {len(names)} 个工具"

    def mcp_tools(issue):
        def f():
            _, names, err = mcp_list()
            if not names:
                return ac.FAIL, f"stdio 列不出工具: {err}"
            return ac.judge_mcp_tools(names, issue)
        return f

    def printer():
        if os.path.exists("/dev/usb/lp0"):
            return ac.PASS, "/dev/usb/lp0 在 (TSC 标签机连着)"
        return ac.MANUAL, "没有 /dev/usb/lp0: 标签机没开 / 没插; 打标签那步前接好"

    def private_state(path, issue_word):
        def f():
            if not os.path.exists(path):
                return ac.SKIP, f"没有 {os.path.basename(path)} ({issue_word}, 重启推车会清掉)"
            perm = ac.judge_perm(os.stat(path), 0o600, uid=0)
            doc, err = load_json(path)
            if err:
                return ac.FAIL, err + " (读不懂 = 按最保守处理)"
            return perm[0], perm[1] + ", 能解析"
        return f

    def pxe_log_page():
        html, wk = read(OPT + "/web/index.html") or "", read(OPT + "/webkiosk.py") or ""
        ok = "PXE 日志" in html and "/api/pxe-log" in html and "/api/pxe-log" in wk
        return (ac.PASS, "网页和 webkiosk 都有 PXE 日志页 (/api/pxe-log)") if ok \
            else (ac.FAIL, "装的 web/index.html 或 webkiosk.py 没有 PXE 日志页: 是旧版")

    def conbroker():
        rc, out, e = py("import serial_core as s, json; c = s.serial_conf_read(); "
                        "print(json.dumps([c['user'], c['bin'], c['script']]))")
        if rc != 0:
            return ac.FAIL, f"serial_core 读 serial.conf 出错: {last_err(e)}"
        user, exe, script = json.loads(out)
        miss = [p for p in (exe, script) if not os.path.exists(p)]
        src = "serial.conf" if os.path.exists(OPT + "/serial.conf") else "内置默认 (没有 serial.conf)"
        if miss:
            return ac.FAIL, f"{src}: 找不到 {' '.join(miss)} (串口面板 / 巡检 / MCP 读输出都靠 conbroker)"
        return ac.PASS, f"{src}: 用户 {user}, {script}"

    def con_heads():
        heads = sorted(glob.glob("/dev/con-*"))
        if not heads:
            return ac.MANUAL, "没有 /dev/con-*: 串口线没插, 接串口那步再跑一遍 --only '#15'"
        return ac.PASS, " ".join(heads)

    def tesseract():
        rc, out, err = sh(["tesseract", "--list-langs"])
        if rc == 127:
            return ac.FAIL, "没装 tesseract: 截图识字和 boot-pxe 配方用不了"
        langs = set((out + err).split())
        return (ac.PASS if "eng" in langs else ac.FAIL), "语言: " + " ".join(sorted(langs - {"List", "of", "available", "languages"}))[:120]

    def recipe_builtin():
        rc, out, e = py("import recipe_core as r; x = r.builtin('boot-pxe'); print(len(x['steps']))")
        if rc != 0:
            return ac.FAIL, f"内置配方 boot-pxe 加载失败: {last_err(e)}"
        return ac.PASS, f"内置 boot-pxe 能加载 ({out} 步); 真跑在验收单里"

    def capture():
        vids = sorted(glob.glob("/dev/video*"))
        return (ac.PASS, " ".join(vids)) if vids else (ac.FAIL, "没有 /dev/video*: 采集卡没认到, 截图 / 配方都不行")

    def clone_files():
        rc, out, e = py("import pxeserver as p; print(p.CLONE_DIR, *p.CLONE_FILES)")
        if rc != 0:
            return ac.FAIL, f"pxeserver 读不了: {last_err(e)}"
        d, *files = out.split()
        miss = [f for f in files if not (os.path.isfile(f"{OPT}/tftp/{d}/{f}") and os.path.getsize(f"{OPT}/tftp/{d}/{f}") > 0)]
        if miss:
            return ac.MANUAL, f"tftp/{d}/ 缺 {', '.join(miss)}: 克隆解锁会被拒 (见 docs/CLONE.md)"
        return ac.PASS, f"tftp/{d}/ 三个文件都在"

    def netmode():
        doc, err = load_json("/var/lib/ops-cart/netmode.json")
        if err:
            return ac.FAIL, err
        if doc is None:
            return ac.SKIP, "还没用网口模式面板切过 (没有 netmode.json)"
        bad = [m for m in doc.get("modes") or [] if m.get("status") not in (None, "ok")]
        return (ac.FAIL if bad else ac.PASS), ac.netmode_summary(doc)

    def kiosk_alive():
        doc, _ = load_json(RUN + "/kiosk.json")
        pid = (doc or {}).get("pid")
        if not isinstance(pid, int):
            return ac.FAIL, "没有 kiosk.json 或里面没 pid (车机界面没起来)"
        return (ac.PASS, f"pid {pid} 在跑, 锁屏={doc.get('locked')}") if os.path.exists(f"/proc/{pid}") \
            else (ac.FAIL, f"kiosk.json 里的 pid {pid} 不在了")

    return [
        ac.Check("车机 kiosk 回写 kiosk.json", "-", kiosk_alive),
        ac.Check("状态快照 state.json 在更新", "#26", snap_fresh),
        ac.Check("快照里的版本号", "-", snap_version),
        ac.Check("UPS 读数", "#4", ups),
        ac.Check("UPS 两段式阈值 40% / 15% / 30 秒", "#4", ups_consts),
        ac.Check("LLDP 对端", "#14", lldp),
        ac.Check("深浅模式进快照", "#31", theme),
        ac.Check("自己发地址限时", "#11", guard),
        ac.Check("键鼠代理 socket 0660 root:dialout", "#9", kvm_sock),
        ac.Check("键鼠代理应答 status", "#9", kvm_status),
        ac.Check("CH9329 键鼠线", "#9", ch9329_present),
        ac.Check("MCP stdio 握手", "#19", mcp_handshake, timeout=40),
        ac.Check("MCP 只读工具", "#19", mcp_tools("#19"), timeout=40),
        ac.Check("MCP 标签工具", "#20", mcp_tools("#20"), timeout=40),
        ac.Check("MCP 串口写工具", "#21", mcp_tools("#21"), timeout=40),
        ac.Check("MCP PXE 申请工具", "#24", mcp_tools("#24"), timeout=40),
        ac.Check("标签机 /dev/usb/lp0", "#20", printer),
        ac.Check("串口写状态 serial-write.json", "#21", private_state(RUN + "/serial-write.json", "还没人拿过写租约")),
        ac.Check("PXE 申请状态 pxe-req.json", "#24", private_state(RUN + "/pxe-req.json", "还没有过 PXE 申请")),
        ac.Check("网页 PXE 日志页已装", "#24", pxe_log_page),
        ac.Check("conbroker (serial.conf 指的)", "#15", conbroker),
        ac.Check("串口操作头 /dev/con-*", "#15", con_heads),
        ac.Check("tesseract (截图识字)", "#17", tesseract),
        ac.Check("内置配方 boot-pxe", "#17", recipe_builtin),
        ac.Check("采集卡 /dev/video*", "#17", capture),
        ac.Check("Clonezilla 文件", "#18", clone_files),
        ac.Check("网口模式状态 netmode.json", "#13", netmode),
    ]


# ============================================================ 清单 (开发机上)

def repo_root():
    return os.path.dirname(HERE)


def print_manifest():
    d = ac.derive(repo_root())
    if d["unmapped"]:
        print("# deploy/ 里没映射到推车路径的:", d["unmapped"], file=sys.stderr)

    def tup(name, items, per=6):
        rows = [", ".join(json.dumps(x) for x in items[i:i + per]) + "," for i in range(0, len(items), per)]
        return f"{name} = (\n" + "".join(f"    {r}\n" for r in rows) + ")"
    sysf = "SYSTEM_FILES = {\n" + "".join(f"    {json.dumps(k)}: {json.dumps(v)},\n" for k, v in d["system"].items()) + "}"
    print("\n".join([tup("REQUIRED_PY", d["py"]), tup("VENV_PY", d["venv"]), tup("ASSETS", d["assets"], 2), sysf]))


def make_manifest(out):
    root = repo_root()
    files = {}
    for m in ac.REQUIRED_PY:
        files[f"{OPT_DEFAULT}/{m}.py"] = sha256(os.path.join(root, m + ".py"))
    for a in ac.ASSETS + ("VERSION",):
        files[f"{OPT_DEFAULT}/{a}"] = sha256(os.path.join(root, a))
    for dest, src in ac.SYSTEM_FILES.items():
        files[dest] = sha256(os.path.join(root, src))
    build = (read(os.path.join(root, "BUILD")) or "").strip() or None
    doc = {"version": (read(os.path.join(root, "VERSION")) or "").strip(), "build": build, "files": files}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    print(f"写好 {out}: {doc['version']} BUILD {build or '(没有, 先跑 scripts/stamp-build.sh)'}, {len(files)} 个文件")


# ============================================================ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="推车只读验收 (1.11)")
    ap.add_argument("--expect-version", help="默认取仓库 VERSION, 没有就是 " + EXPECT_VERSION)
    ap.add_argument("--expect-build", help='如 "312 47ea7fa" (stamp-build.sh 的输出); 默认取清单里的')
    ap.add_argument("--manifest", default=MANIFEST, help="--make-manifest 生成的 sha256 清单")
    ap.add_argument("--only", help="只跑名字或 issue 号里含这个字符串的项, 如 '#9'、'MCP'")
    ap.add_argument("--opt", default=OPT_DEFAULT, help="代码目录 (开发机上冒烟可指向仓库)")
    ap.add_argument("--timeout", type=float, default=ac.DEFAULT_TIMEOUT, help="每项默认超时秒数")
    ap.add_argument("--make-manifest", metavar="OUT", help="开发机上: 生成 sha256 清单")
    ap.add_argument("--print-manifest", action="store_true", help="开发机上: 打印 accept_core 的清单常量")
    a = ap.parse_args(argv)
    if a.print_manifest:
        print_manifest()
        return 0
    if a.make_manifest:
        make_manifest(a.make_manifest)
        return 0

    global OPT
    OPT = a.opt.rstrip("/") or "/"
    manifest, err = load_json(a.manifest)
    expect = a.expect_version or (manifest or {}).get("version") or \
        (read(os.path.join(repo_root(), "VERSION")) or "").strip() or EXPECT_VERSION
    print(f"推车验收 {expect}  {time.strftime('%Y-%m-%d %H:%M:%S')}  (只读; 每项默认超时 {a.timeout:g} 秒)")
    if err:
        print("清单读不懂, 不比对 sha256:", err)
    checks = ([ac.Check("以 root 运行", "-", chk_root), ac.Check("平台", "-", chk_platform)]
              + make_version_checks(expect, a.expect_build, manifest)
              + unit_checks()
              + [ac.Check("SELinux 模块 ops_cart_mounton (webkiosk 要)", "-", chk_selinux, timeout=70)]
              + log_checks() + font_checks() + config_checks() + import_checks()
              + issue_checks(expect))
    if a.only:
        checks = [c for c in checks if a.only in c.name or a.only in c.issue]
    results = ac.run_all(checks, timeout=a.timeout)
    text, code = ac.summary(results)
    print(text)
    print("下一步: 照 docs/ACCEPT_1.11.md 做人工那部分.")
    return code


if __name__ == "__main__":
    sys.exit(main())
