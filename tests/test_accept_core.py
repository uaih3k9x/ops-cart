"""验收脚本 (scripts/cart_accept.py) 的纯逻辑: 判定函数 + 清单推导 + 跑检查的框架. 不碰推车."""

import importlib.util
import os
import stat
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("accept_core", os.path.join(ROOT, "scripts", "accept_core.py"))
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

P, F, S, M = ac.PASS, ac.FAIL, ac.SKIP, ac.MANUAL


# ------------------------------------------------------------ 跑检查的框架

def test_run_one_pass_and_tag():
    r = ac.run_one(ac.Check("版本", "-", lambda: (P, "1.11.00")), timeout=2)
    assert (r["status"], r["why"], r["issue"], r["name"]) == (P, "1.11.00", "-", "版本")


def test_run_one_exception_is_fail_and_does_not_raise():
    def boom():
        raise RuntimeError("坏了")
    r = ac.run_one(ac.Check("x", "#9", boom), timeout=2)
    assert r["status"] == F and "RuntimeError" in r["why"] and "坏了" in r["why"]


def test_run_one_timeout_is_fail():
    r = ac.run_one(ac.Check("慢", "#14", lambda: time.sleep(5)), timeout=0.2)
    assert r["status"] == F and "超时" in r["why"]


def test_run_one_bad_return_is_fail():
    r = ac.run_one(ac.Check("x", "-", lambda: "PASS"), timeout=1)
    assert r["status"] == F


def test_run_one_check_timeout_overrides_default():
    r = ac.run_one(ac.Check("慢但给够", "-", lambda: (time.sleep(0.3), (P, "ok"))[1], timeout=2), timeout=0.1)
    assert r["status"] == P


def test_run_all_continues_after_failure_and_prints():
    lines = []
    checks = [ac.Check("a", "-", lambda: 1 / 0), ac.Check("b", "#4", lambda: (S, "没接 UPS"))]
    rs = ac.run_all(checks, timeout=1, out=lines.append)
    assert [r["status"] for r in rs] == [F, S]
    assert len(lines) == 2 and "#4" in lines[1] and "没接 UPS" in lines[1]


def test_format_line_one_line_even_if_reason_multiline():
    s = ac.format_line({"status": M, "issue": "#28", "name": "看主屏", "why": "第一行\n第二行"})
    assert "\n" not in s and "人工" in s and "#28" in s


def test_summary_counts_and_exit_code():
    rs = [{"status": P, "issue": "-", "name": "a", "why": ""},
          {"status": F, "issue": "#9", "name": "b", "why": "x"},
          {"status": M, "issue": "#4", "name": "c", "why": "y"}]
    text, code = ac.summary(rs)
    assert "PASS 1" in text and "FAIL 1" in text and "人工 1" in text and code == 1
    assert "#9" in text                    # 失败的项目在汇总里再列一次
    assert ac.summary(rs[:1])[1] == 0


# ------------------------------------------------------------ 版本

def test_judge_version():
    assert ac.judge_version("1.11.00\n", "1.11.00")[0] == P
    st, why = ac.judge_version("1.09.01", "1.11.00")
    assert st == F and "1.09.01" in why
    assert ac.judge_version(None, "1.11.00")[0] == F


@pytest.mark.parametrize("text,expect,want", [
    ("312 47ea7fa\n", None, P),
    ("312 47ea7fa-dirty", None, F),
    ("garbage", None, F),
    (None, None, F),
    ("312 47ea7fa", "312 47ea7fa", P),
    ("311 1234567", "312 47ea7fa", F),
])
def test_judge_build(text, expect, want):
    assert ac.judge_build(text, expect)[0] == want


# ------------------------------------------------------------ 清单推导 (别手写漏)

def test_py_imports_sees_plain_from_and_dunder_import():
    src = ("import os, json\nfrom a import b\nfrom .x import y\n"
           "def f():\n    import c\n    return __import__('d'), importlib.import_module('e')\n")
    assert ac.py_imports(src) >= {"os", "json", "a", "c", "d", "e"}
    assert "x" not in ac.py_imports(src)


def test_py_file_refs_skip_docstrings():
    src = '"""用 serial_panel.py 的那个."""\nCMD = ["python3", "cart_snap.py"]\n'
    assert ac.py_file_refs(src, {"serial_panel", "cart_snap"}) == {"cart_snap"}


def test_opt_pxe_refs():
    assert ac.opt_pxe_refs("ExecStart=/usr/bin/python3 /opt/pxe/kiosk.py -- x\n# /opt/pxe/web.conf") == {"kiosk"}


def test_closure():
    g = {"a": {"b"}, "b": {"c"}, "c": set(), "d": {"a"}}
    assert ac.closure({"a"}, g) == {"a", "b", "c"}


def test_constants_match_repo_derivation():
    """常量是从仓库推出来的: 改了 import / deploy 没同步, 这里就红 (提示跑 --print-manifest 抄过去)."""
    d = ac.derive(ROOT)
    assert d["unmapped"] == []
    assert sorted(ac.REQUIRED_PY) == d["py"]
    assert sorted(ac.VENV_PY) == d["venv"]
    assert sorted(ac.ASSETS) == d["assets"]
    assert ac.SYSTEM_FILES == d["system"]


def test_derived_list_contains_known_entry_points_and_lazy_imports():
    d = ac.derive(ROOT)
    for m in ("kiosk", "pxeserver", "webkiosk", "kvm_broker", "cart_mcp", "notify_core", "dash_service",
              "dash_render", "udisp_usb", "recipe", "cart_snap", "tspl_preview", "sun_core", "theme"):
        assert m in d["py"], m
    assert {"dash_render", "udisp_usb", "dash_service"} <= set(d["venv"])
    assert "kiosk" not in d["venv"]


def test_third_party_imports_are_only_venv_ones():
    if not hasattr(sys, "stdlib_module_names"):
        pytest.skip("要 3.10+ 才有标准库清单")
    d = ac.derive(ROOT)
    assert d["third_party"] <= set(ac.THIRD_PARTY) | set(ac.OCR_THIRD_PARTY)


def test_system_files_cover_every_unit():
    units = os.listdir(os.path.join(ROOT, "deploy", "systemd"))
    for u in units:
        assert "/etc/systemd/system/" + u in ac.SYSTEM_FILES
    assert ac.SYSTEM_FILES["/usr/local/bin/kiosk"] == "deploy/bin/kiosk-launcher"


# ------------------------------------------------------------ systemd

def test_units_table_matches_deploy():
    udir = os.path.join(ROOT, "deploy", "systemd")
    assert set(ac.UNITS) == set(os.listdir(udir))
    for name, spec in ac.UNITS.items():
        text = open(os.path.join(udir, name)).read()
        assert spec["oneshot"] == ("Type=oneshot" in text), name
        assert spec["issue"], name
    assert ac.UNITS["cart-mcp.service"]["enabled"] is None          # 默认不开, 开了也行


SHOW = """Id=kiosk.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
Result=success
ExecMainStatus=0

Id=cart-mcp.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=disabled
Result=success
ExecMainStatus=0

Id=webkiosk.service
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled
Result=exit-code
ExecMainStatus=226

Id=kvm-broker.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=
Result=success
ExecMainStatus=0
"""


def test_parse_show():
    d = ac.parse_show(SHOW)
    assert d["kiosk.service"]["ActiveState"] == "active"
    assert d["webkiosk.service"]["ExecMainStatus"] == "226"


def test_judge_unit_required_running():
    d = ac.parse_show(SHOW)
    assert ac.judge_unit(ac.UNITS["kiosk.service"], d["kiosk.service"])[0] == P


def test_judge_unit_mcp_default_off_is_pass():
    d = ac.parse_show(SHOW)
    st, why = ac.judge_unit(ac.UNITS["cart-mcp.service"], d["cart-mcp.service"])
    assert st == P and "未启用" in why


def test_judge_unit_226_hints_selinux():
    d = ac.parse_show(SHOW)
    st, why = ac.judge_unit(ac.UNITS["webkiosk.service"], d["webkiosk.service"])
    assert st == F and "226" in why and "SELinux" in why


def test_judge_unit_not_installed():
    d = ac.parse_show(SHOW)
    st, why = ac.judge_unit(ac.UNITS["kvm-broker.service"], d["kvm-broker.service"])
    assert st == F and "没装" in why


def test_judge_unit_missing_block():
    assert ac.judge_unit(ac.UNITS["kiosk.service"], None)[0] == F


def test_judge_unit_required_but_disabled():
    props = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "disabled", "Result": "success"}
    st, why = ac.judge_unit(ac.UNITS["kvm-broker.service"], props)
    assert st == F and "自启" in why


def test_judge_unit_optional_enabled_but_dead():
    props = {"LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "enabled", "Result": "success"}
    assert ac.judge_unit(ac.UNITS["webkiosk.service"], props)[0] == F


def test_judge_unit_needs_daemon_reload():
    props = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled", "Result": "success",
             "NeedDaemonReload": "yes"}
    st, why = ac.judge_unit(ac.UNITS["kiosk.service"], props)
    assert st == F and "daemon-reload" in why


def test_show_props_ask_for_daemon_reload():
    assert "NeedDaemonReload" in ac.SHOW_PROPS.split(",")


def test_judge_unit_oneshot():
    ok = {"LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "static", "Result": "success"}
    bad = dict(ok, ActiveState="failed", Result="exit-code", ExecMainStatus="1")
    spec = ac.UNITS["cart-notify.service"]
    assert ac.judge_unit(spec, ok)[0] == P
    assert ac.judge_unit(spec, bad)[0] == F


# ------------------------------------------------------------ SELinux / 字体 / 权限 / 配置

def test_parse_semodule_and_judge():
    mods = ac.parse_semodule("abrt\t1.4.1\nops_cart_mounton\t1.0\nzabbix\n")
    assert "ops_cart_mounton" in mods and "zabbix" in mods
    assert ac.judge_selinux("Enforcing", mods)[0] == P
    st, why = ac.judge_selinux("Enforcing", {"abrt"})
    assert st == F and "ops_cart_mounton" in why
    assert ac.judge_selinux("Disabled", set())[0] == M


def test_judge_fonts():
    """族名从推车上装的 theme.py 取 (theme.UNIFONT), 这里不写死."""
    assert ac.judge_fonts("Unifont:style=Regular\nNoto Sans CJK SC\n", "Unifont")[0] == P
    assert ac.judge_fonts("Unifont Upper\nDejaVu Sans\n", "Unifont")[0] == F     # 只认这个族名
    assert ac.judge_fonts("", "Unifont")[0] == F
    st, why = ac.judge_fonts("Unifont\n", None)
    assert st == F and "theme" in why


def _st(mode, uid=0, gid=0):
    return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, gid, 10, 0, 0, 0))


def test_judge_perm():
    assert ac.judge_perm(_st(0o600), 0o600)[0] == P
    st, why = ac.judge_perm(_st(0o644), 0o600)
    assert st == F and "0644" in why and "0600" in why
    st, why = ac.judge_perm(_st(0o600, uid=1000), 0o600, uid=0)
    assert st == F and "属主" in why


def test_parse_env():
    env = ac.parse_env("# x\nPXE_IP=192.0.2.1\nPXE_PREFIX=24\n#PXE_SERVE_MINUTES=30\nexport A='b'\n")
    assert env == {"PXE_IP": "192.0.2.1", "PXE_PREFIX": "24", "A": "b"}


# ------------------------------------------------------------ 日志

def test_judge_log():
    assert ac.judge_log(None, must=False)[0] == S
    assert ac.judge_log(None, must=True)[0] == F
    assert ac.judge_log(120, must=True)[0] == P
    assert ac.judge_log(3 * 86400, must=True)[0] == M
    st, why = ac.judge_log(3 * 86400, must=False)
    assert st == P and "天前" in why


# ------------------------------------------------------------ 快照里能看出来的

def test_judge_snapshot():
    now = 1000.0
    assert ac.judge_snapshot({"ts": 999.0}, now)[0] == P
    assert ac.judge_snapshot({"ts": 900.0}, now)[0] == F
    assert ac.judge_snapshot(None, now)[0] == F


def test_judge_lldp():
    fresh = {"LAN1": {"state": "fresh", "neighbor": {"system_name": "SW-1", "port_id": "GE0/0/1"}, "error": ""},
             "LAN2": {"state": "none", "neighbor": None, "error": ""}}
    st, why = ac.judge_lldp(fresh)
    assert st == P and "LAN1" in why
    st, why = ac.judge_lldp({"LAN1": {"state": "none", "neighbor": None, "error": ""}})
    assert st == M and "交换机" in why
    st, why = ac.judge_lldp({"LAN1": {"state": "off", "neighbor": None, "error": "没权限"}})
    assert st == F and "没权限" in why
    assert ac.judge_lldp({})[0] == F            # 快照里没 lldp 这一项 = cart-dash 不是新版


def test_judge_theme():
    assert ac.judge_theme({"mode": "dark", "setting": "auto", "next_switch": 123.0})[0] == P
    st, why = ac.judge_theme({"mode": "dark", "setting": None, "next_switch": None})
    assert st == F and "theme.json" in why
    assert ac.judge_theme(None)[0] == F


def test_judge_ups():
    assert ac.judge_ups(None)[0] == F
    st, why = ac.judge_ups({"percent": 100, "on_battery": False, "source": "hidraw"})
    assert st == P and "100" in why


def test_judge_guard():
    now = 1000.0
    serving = {"state": "serving", "until": now + 600, "window": 1800, "pid": 1}
    st, why = ac.judge_guard(serving, 30, now)
    assert st == P and "30" in why
    st, why = ac.judge_guard(dict(serving, window=0, until=None), 30, now)
    assert st == F
    assert ac.judge_guard({"state": "proxy", "window": 1800, "pid": 1}, 30, now)[0] == P
    assert ac.judge_guard({"state": "serving", "pid": 1}, 30, now)[0] == F       # 旧版守卫没有 window
    assert ac.judge_guard({}, 30, now)[0] == S                                    # pxe-server 没在跑


def test_judge_guard_unlimited_configured():
    assert ac.judge_guard({"state": "serving", "until": None, "window": 0, "pid": 1}, 0, 0.0)[0] == P


# ------------------------------------------------------------ MCP / 键鼠代理

def test_mcp_tools_table_matches_cart_mcp():
    import cart_mcp
    names = {t.name for t in cart_mcp.real_tools()}
    want = set()
    for v in ac.MCP_TOOLS.values():
        want |= set(v)
    assert want == names


def test_parse_mcp_and_judge():
    out = ('{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"ops-cart","version":"1.11.00"}}}\n'
           '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"list_threads"},{"name":"estop"}]}}\n')
    info, names = ac.parse_mcp(out)
    assert info["version"] == "1.11.00" and names == {"list_threads", "estop"}
    st, why = ac.judge_mcp_tools(names, "#21")
    assert st == F and "lease_acquire" in why
    assert ac.judge_mcp_tools(set(ac.MCP_TOOLS["#19"]), "#19")[0] == P
    assert ac.parse_mcp("not json\n") == ({}, set())


def test_mcp_requests_are_read_only():
    reqs = ac.mcp_requests()
    assert '"tools/call"' not in reqs and '"tools/list"' in reqs and '"initialize"' in reqs


def test_judge_kvm_status():
    assert ac.judge_kvm_status('{"ok": true, "status": {"estop": []}}')[0] == P
    assert ac.judge_kvm_status('{"ok": false, "err": "bad"}')[0] == F
    assert ac.judge_kvm_status("")[0] == F


def test_kvm_status_request_is_control_op():
    import json
    import kvm_broker_core as kbc
    r = kbc.parse_request(ac.KVM_STATUS_REQ)
    assert r["op"] == "status" and json.loads(ac.KVM_STATUS_REQ)["op"] in kbc.CONTROL_OPS


def test_ups_constants_expected_match_code():
    import ups_core
    assert (ups_core.WARN_PCT, ups_core.LOW_PCT, ups_core.COUNTDOWN_S) == ac.UPS_EXPECT


def test_fonts_are_system_files_not_opt_pxe_assets():
    """字体只装系统字体目录 (theme.UNIFONT_PATHS 第一条); /opt/pxe/fonts 是开发机预览用的, 车上没有不算缺."""
    d = ac.derive(ROOT)
    assert not any(a.startswith("fonts/") for a in d["assets"])
    assert any(k.startswith(ac.FONT_DEST + "/") for k in d["system"])


def test_netmode_summary_reads_modes_list():
    """netmode.json 的 modes 是列表 (每项 mode + ports), 不是 dict."""
    doc = {"version": 1, "modes": [
        {"mode": "dhcp", "ports": ["enp3s0"], "status": "ok"},
        {"mode": "static", "ports": ["eno1"], "params": {"addr": "192.0.2.1/24"}, "status": "ok"}]}
    assert ac.netmode_summary(doc) == "托管: enp3s0=dhcp, eno1=static"
    assert ac.netmode_summary({"modes": []}) == "托管: 无"
    assert "failed" in ac.netmode_summary({"modes": [{"mode": "static", "ports": ["eno1"], "status": "failed"}]})


def test_default_expect_version_follows_repo_version():
    """推车上单独拷脚本跑时没仓库 VERSION 可读, 用 EXPECT_VERSION; 改版本号忘了改它, 验收就会把新版判成没部署."""
    src = open(os.path.join(ROOT, "scripts", "cart_accept.py"), encoding="utf-8").read()
    want = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
    assert f'EXPECT_VERSION = "{want}"' in src


# ---- LAN1 所在防火墙区要放行 PXE 端口 (#38)

PXE_PORTS = "80/tcp 8000/tcp 67/udp 68/udp 69/udp 4011/udp"


def test_pxe_zone_ok():
    assert ac.judge_pxe_zone("pxe", PXE_PORTS)[0] == ac.PASS


def test_pxe_zone_missing_ports_fails_and_says_which():
    st, why = ac.judge_pxe_zone("public", "")
    assert st == ac.FAIL and "public" in why and "67/udp" in why and "nmcli" in why


def test_pxe_zone_no_zone_is_fail():
    assert ac.judge_pxe_zone("", "")[0] == ac.FAIL


def test_pxe_zone_firewalld_off_is_skip():
    assert ac.judge_pxe_zone(None, None)[0] == ac.SKIP


# ---- RapidOCR (#46): 可选, 没装不算错

def test_judge_rapid():
    assert ac.judge_rapid(False, 0, "")[0] == ac.SKIP
    assert ac.judge_rapid(True, 0, "1.4.4")[0] == ac.PASS
    st, why = ac.judge_rapid(True, 1, "ImportError: libGL")
    assert st == ac.FAIL and "libGL" in why


# ------------------------------------------------------------ #55 Memtest86+ 文件

def test_judge_memtest_missing_is_manual():
    st, why = ac.judge_memtest(None, "a" * 64)
    assert st == M and "setup-memtest.sh" in why


def test_judge_memtest_match_is_pass_and_mismatch_manual():
    assert ac.judge_memtest("a" * 64, "A" * 64)[0] == P
    st, why = ac.judge_memtest("b" * 64, "a" * 64)
    assert st == M and "setup-memtest.sh" in why
