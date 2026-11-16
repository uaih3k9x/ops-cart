"""scripts/install-1.16.01.sh: 假推车目录 + 假 systemctl / python3 / id / restorecon 跑一遍.
照 test_install_script.py (1.16.00) 的写法; 这版多 #95 迁移 (pxeserver.log、logrotate) 和 smb disable --now."""
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-1.16.01.sh"
PACK = ROOT / "scripts" / "pack-1.16.01.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")

def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _write_manifest(pkg, build=None):
    """照 cart_accept --make-manifest 的格式: /opt/pxe/<相对路径>、单元、/etc/logrotate.d/ops-cart."""
    files = {}
    for f in sorted((pkg / "opt").rglob("*")):
        if f.is_file() and f.name != "BUILD":                # 真 manifest 不列 BUILD, 单独记在 build 字段
            files["/opt/pxe/" + f.relative_to(pkg / "opt").as_posix()] = _sha(f)
    for f in sorted((pkg / "systemd").iterdir()):
        files["/etc/systemd/system/" + f.name] = _sha(f)
    files["/etc/logrotate.d/ops-cart"] = _sha(pkg / "etc" / "logrotate.d" / "ops-cart")
    files["/usr/local/bin/kiosk"] = "0" * 64          # 包里不带的系统件: 不核对
    m = {"version": "1.16.01", "build": build or (pkg / "opt" / "BUILD").read_text().strip(), "files": files}
    (pkg / "accept" / "accept-manifest.json").write_text(json.dumps(m))


@pytest.fixture
def cart(tmp_path):
    opt, sysd, etc, log, lrd, pkg, bin_ = (
        tmp_path / n for n in ("opt", "sys", "etc", "log", "logrotate.d", "pkg", "bin"))
    for d in (opt / "web", opt / "icons", opt / "tftp", sysd, etc, log, lrd, bin_,
              pkg / "opt" / "web", pkg / "opt" / "icons", pkg / "systemd", pkg / "etc" / "logrotate.d",
              pkg / "accept"):
        d.mkdir(parents=True)
    (opt / "VERSION").write_text("1.16.00\n")
    (opt / "BUILD").write_text("323 2b28f24\n")
    (opt / "kiosk.py").write_text("old\n")
    (opt / "web" / "index.html").write_text("old\n")
    (opt / "pxeserver.log").write_text("old pxe log\n")
    (sysd / "pxe-server.service").write_text("old unit --log /opt/pxe/pxeserver.log\n")
    (lrd / "ops-cart").write_text("/var/log/ops-cart/*.log /opt/pxe/pxeserver.log {}\n")
    (etc / "pxe.env").write_text("PXE_IP=x\n")
    (etc / "serial.conf").write_text("keep\n")

    (pkg / "opt" / "VERSION").write_text("1.16.01\n")
    (pkg / "opt" / "BUILD").write_text("330 abc1234\n")
    for f in ("kiosk.py", "bigfish_core.py", "observe_core.py"):
        (pkg / "opt" / f).write_text("new\n")
    (pkg / "opt" / "web" / "index.html").write_text("new\n")
    (pkg / "systemd" / "pxe-server.service").write_text("new unit --log /var/log/ops-cart/pxeserver.log\n")
    (pkg / "etc" / "logrotate.d" / "ops-cart").write_text("/var/log/ops-cart/*.log {}\n")
    shutil.copy(SCRIPT, pkg / "install.sh")
    for f in ("cart_accept.py", "accept_core.py"):
        (pkg / "accept" / f).write_text("x\n")
    _write_manifest(pkg)

    calls = tmp_path / "calls.log"
    fail = tmp_path / "fail-restart"      # 列在这里的服务: restart 返回非 0
    brk = tmp_path / "break-on-restart"   # 列在这里的服务: restart 返回 0, 之后 is-active 为假 (新代码起不来)
    active = tmp_path / "active"          # 当前 active 的服务
    enabled = tmp_path / "enabled"
    failed = tmp_path / "failed"          # is-failed 为真的单元
    active.write_text("kiosk\ncart-dash\ncart-mcp\npxe-server\nkvm-broker\n")
    enabled.write_text("smb.service\n")
    failed.write_text("smb.service\nlogrotate.service\n")
    _exe(bin_ / "id", 'echo 0\n')
    # stop pxe-server 时顺手记下旧日志还在不在 (要先停再挪)
    _exe(bin_ / "systemctl", f"""echo "$*" >> {calls}
[ "$*" = "stop pxe-server" ] && {{ [ -f {opt}/pxeserver.log ] && echo "  (stop 时旧日志还在)" >> {calls}; }}
svc="${{@: -1}}"
case "$1" in
  is-active) grep -qx "$svc" {active} && exit 0; exit 3;;
  is-enabled) grep -qx "$svc" {enabled} && exit 0; exit 1;;
  is-failed) grep -qx "$svc" {failed} && exit 0; exit 1;;
  show) [ -f {tmp_path}/no-smb ] && echo not-found || echo loaded; exit 0;;
  disable) grep -vx "$svc" {enabled} > {enabled}.n || true; mv {enabled}.n {enabled}
           grep -vx "$svc" {active} > {active}.n || true; mv {active}.n {active}; exit 0;;
  enable) grep -qx "$svc" {enabled} || echo "$svc" >> {enabled}; exit 0;;
  reset-failed) grep -vx "$svc" {failed} > {failed}.n || true; mv {failed}.n {failed}; exit 0;;
  stop) grep -vx "$svc" {active} > {active}.n || true; mv {active}.n {active}; exit 0;;
  start|restart)
    [ -f {fail} ] && grep -qx "$svc" {fail} && exit 1
    [ "$svc" = logrotate.service ] && {{ grep -vx "$svc" {failed} > {failed}.n || true; mv {failed}.n {failed}; exit 0; }}
    if [ -f {brk} ] && grep -qx "$svc" {brk}; then grep -vx "$svc" {active} > {active}.n || true; mv {active}.n {active}
    else grep -qx "$svc" {active} || echo "$svc" >> {active}; fi
    exit 0;;
esac
exit 0
""")
    _exe(bin_ / "restorecon", f'echo "restorecon $*" >> {calls}\n')
    # 假 python3: 验收只记一笔; 别的 (内联的 manifest 核对) 交给真 python
    _exe(bin_ / "python3", f"""echo "python3 $*" >> {calls}
case "$1" in
  */cart_accept.py) [ -f {tmp_path}/term-in-accept ] && kill -TERM $PPID; sleep 0.2;;
  *) exec {sys.executable} "$@";;
esac
exit 0
""")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", OPT=str(opt), SYS=str(sysd), ETC=str(etc),
               LOG=str(log), LRD=str(lrd), SETTLE="0", MIN_OPT_FILES="1")
    return dict(tmp=tmp_path, opt=opt, sys=sysd, etc=etc, log=log, lrd=lrd, pkg=pkg,
                env=env, calls=calls, fail=fail, brk=brk, active=active, enabled=enabled, failed=failed, bin=bin_)


def run(cart, extra_env=None):
    env = dict(cart["env"], **(extra_env or {}))
    return subprocess.run(["bash", str(cart["pkg"] / "install.sh")], env=env, capture_output=True, text=True)


def _rollback(cart, b):
    return subprocess.run(["bash", str(b / "rollback.sh")], env=cart["env"], capture_output=True, text=True)


def backups(cart):
    return sorted(cart["opt"].glob(".bak-*"))


def calls(cart):
    return cart["calls"].read_text().splitlines()


# ------------------------------------------------------------ 正常路径

def test_happy_path_installs_code_and_leaves_config(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    opt = cart["opt"]
    assert (opt / "VERSION").read_text() == "1.16.01\n"
    assert (opt / "kiosk.py").read_text() == "new\n"
    assert (opt / "bigfish_core.py").exists() and (opt / "observe_core.py").exists()
    assert (opt / "web" / "index.html").read_text() == "new\n"
    assert sorted(p.name for p in cart["etc"].iterdir()) == ["pxe.env", "serial.conf"]
    assert (cart["etc"] / "serial.conf").read_text() == "keep\n"
    (b,) = backups(cart)
    assert (b / "VERSION").read_text() == "1.16.00\n"
    assert (b / "etc" / "systemd" / "pxe-server.service").read_text().startswith("old unit")
    assert (b / "etc" / "logrotate.d" / "ops-cart").read_text().startswith("/var/log/ops-cart/*.log /opt/pxe")
    assert (b / "etc" / "ops-cart" / "serial.conf").exists()
    assert "cart_accept.py" in cart["calls"].read_text()
    assert "rollback.sh" in r.stdout
    assert not any(l.startswith(("enable", "start smb", "restart smb")) for l in calls(cart))


def test_restart_scope_follows_changed_code(cart):
    (cart["tmp"] / "active").write_text("kiosk\ncart-dash\ncart-mcp\ncart-mcp-ro\nwebkiosk\npxe-server\n"
                                        "kvm-broker\npxe-http-boot\npxe-http-repo\n")
    r = run(cart)
    assert r.returncode == 0, r.stderr
    c = calls(cart)
    for s in ("kiosk", "cart-dash", "cart-mcp", "cart-mcp-ro", "webkiosk"):
        assert f"restart {s}" in c
    for s in ("kvm-broker", "pxe-http-boot", "pxe-http-repo", "cart-ocr"):     # 这版没改它们的代码
        assert f"restart {s}" not in c and f"stop {s}" not in c
    assert "restart pxe-server" not in c                                     # pxe-server 是停 / 起, 不是 restart


def test_pxeserver_log_moved_after_stop_and_relabelled(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    c = calls(cart)
    i_stop, i_start = c.index("stop pxe-server"), c.index("start pxe-server")
    assert c[i_stop + 1] == "  (stop 时旧日志还在)" and i_stop < i_start   # 先停, 再挪, 再起
    assert not (cart["opt"] / "pxeserver.log").exists()
    assert (cart["log"] / "pxeserver.log").read_text() == "old pxe log\n"
    assert f"restorecon -v {cart['log']}/pxeserver.log" in c
    assert "new unit" in (cart["sys"] / "pxe-server.service").read_text()
    daemon = [i for i, l in enumerate(c) if l == "daemon-reload"]
    assert any(i_stop < i < i_start for i in daemon)                         # 新单元生效后才起


def test_pxe_server_not_started_if_it_was_not_running(cart):
    cart["active"].write_text("kiosk\n")
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "start pxe-server" not in calls(cart)
    assert "pxe-server 部署前就没在跑" in r.stdout


def test_existing_new_log_is_not_overwritten(cart):
    (cart["log"] / "pxeserver.log").write_text("already here\n")
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert (cart["log"] / "pxeserver.log").read_text() == "already here\n"
    assert (cart["opt"] / "pxeserver.log").read_text() == "old pxe log\n"
    assert "没挪" in r.stdout


def test_logrotate_config_replaced_and_run_once(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert (cart["lrd"] / "ops-cart").read_text() == "/var/log/ops-cart/*.log {}\n"
    assert "start logrotate.service" in calls(cart)
    assert "logrotate 跑过一次, 没失败" in r.stdout


def test_logrotate_failure_is_reported_not_rolled_back(cart):
    cart["fail"].write_text("logrotate.service\n")
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "logrotate 还是失败" in r.stdout
    assert (cart["opt"] / "VERSION").read_text() == "1.16.01\n"


# ------------------------------------------------------------ smb: 没在用, disable --now (维护者定)

def test_smb_disabled_now_and_reset_failed(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    c = calls(cart)
    assert "disable --now smb.service" in c and "reset-failed smb.service" in c
    assert c.index("disable --now smb.service") < c.index("reset-failed smb.service")
    assert "smb.service" not in cart["enabled"].read_text().split()
    assert "smb.service" not in cart["failed"].read_text().split()
    assert "已停用" in r.stdout and "MIGRATE_95" in r.stdout
    assert not any(l.startswith(("restart smb", "start smb", "enable")) for l in c)


def test_script_does_not_touch_smb_config():
    code = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
    for bad in ("smb.conf", "setup-samba", "smb-ops-cart.conf", "smb-lan1", "ops-cart-lan1", "50-ops-cart-smb",
                "testparm", "dispatcher.d", "smb.service.d"):
        assert bad not in code, bad


def test_no_samba_skips_smb_part(cart):
    (cart["tmp"] / "no-smb").write_text("")
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert not any("smb.service" in l and not l.startswith("show") for l in calls(cart))
    assert "没装 Samba" in r.stdout


def test_smb_left_enabled_is_reported(cart):
    sc = cart["bin"] / "systemctl"
    sc.write_text(sc.read_text().replace("  disable)", "  disable) exit 0;;\n  disable-orig)", 1))
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "smb.service 没停干净" in r.stdout


def test_disable_failure_stops_with_rollback(cart):
    sc = cart["bin"] / "systemctl"
    sc.write_text(sc.read_text().replace("  disable)", "  disable) exit 1;;\n  disable-orig)", 1))
    r = run(cart)
    assert r.returncode != 0
    (b,) = backups(cart)
    assert f"bash {b}/rollback.sh" in r.stderr


@pytest.mark.parametrize("enabled, active", [(True, False), (True, True), (False, False), (False, True)])
def test_rollback_restores_smb_as_it_was(cart, enabled, active):
    """部署前 enabled 才 enable, active 才 start (推车上是 enabled + failed: 只 enable, 不拉起)."""
    cart["enabled"].write_text("smb.service\n" if enabled else "")
    act = [l for l in cart["active"].read_text().split() if l != "smb.service"] + (["smb.service"] if active else [])
    cart["active"].write_text("\n".join(act) + "\n")
    cart["fail"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    cart["fail"].unlink()
    before = len(calls(cart))
    assert _rollback(cart, b).returncode == 0
    after = calls(cart)[before:]
    assert ("enable smb.service" in after) is enabled
    assert ("start smb.service" in after) is active
    assert ("smb.service" in cart["enabled"].read_text().split()) is enabled


# ------------------------------------------------------------ 回滚

def test_rollback_restores_code_unit_smb_state_but_keeps_new_logrotate(cart):
    cart["fail"].write_text("kiosk\n")
    r = run(cart)
    assert r.returncode != 0
    (b,) = backups(cart)
    assert f"bash {b}/rollback.sh" in r.stderr
    assert "cart_accept.py" not in cart["calls"].read_text()       # 出错后不往下走
    cart["fail"].unlink()
    before = len(calls(cart))
    rr = _rollback(cart, b)
    assert rr.returncode == 0, rr.stderr
    opt = cart["opt"]
    assert (opt / "VERSION").read_text() == "1.16.00\n"
    assert (opt / "kiosk.py").read_text() == "old\n"
    assert (opt / "web" / "index.html").read_text() == "old\n"
    assert (cart["sys"] / "pxe-server.service").read_text().startswith("old unit")
    assert (cart["lrd"] / "ops-cart").read_text() == "/var/log/ops-cart/*.log {}\n"   # 新 logrotate 留着
    assert (cart["log"] / "pxeserver.log").exists()                                  # 挪过去的日志留着
    after = calls(cart)[before:]
    for s in ("kiosk", "cart-dash", "cart-mcp", "pxe-server"):
        assert f"restart {s}" in after
    assert "restart kvm-broker" not in after
    assert "enable smb.service" in after and "start smb.service" not in after   # 部署前 enabled + failed
    assert (b / "deploy.marker.rolled-back").exists()


def test_rollback_without_samba_does_not_touch_smb(cart):
    (cart["tmp"] / "no-smb").write_text("")
    cart["fail"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    cart["fail"].unlink()
    before = len(calls(cart))
    assert _rollback(cart, b).returncode == 0
    assert not any("smb" in l for l in calls(cart)[before:])


def test_die_after_backup_prints_rollback(cart):
    """restart 返回 0 但随后没起来: 走 die 而不是 ERR, 也要打印回滚命令."""
    cart["brk"].write_text("kiosk\n")
    r = run(cart)
    assert r.returncode != 0
    (b,) = backups(cart)
    assert "kiosk" in r.stderr and f"bash {b}/rollback.sh" in r.stderr


def test_rollback_restarts_services_active_before_deploy_even_if_down_now(cart):
    cart["brk"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    assert "kiosk" not in cart["active"].read_text().split()
    cart["brk"].unlink()
    assert _rollback(cart, b).returncode == 0
    assert "kiosk" in cart["active"].read_text().split()


def test_rollback_reports_services_that_fail_to_restart(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    rr = _rollback(cart, b)
    assert rr.returncode != 0 and "cart-mcp" in rr.stderr
    assert (cart["opt"] / "VERSION").read_text() == "1.16.00\n"


def test_rollback_err_trap_says_rerunnable(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    (b / "VERSION").unlink()
    rr = _rollback(cart, b)
    assert rr.returncode != 0 and "回滚第" in rr.stderr and "重跑" in rr.stderr


def test_after_rollback_can_install_again(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    cart["fail"].unlink()
    assert _rollback(cart, b).returncode == 0
    time.sleep(1.1)                                  # 备份目录按秒命名
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert len(backups(cart)) == 2


# ------------------------------------------------------------ 拒绝 / 防重跑 / 信号 (照 1.16.00)

def test_refuses_wrong_version_without_touching_anything(cart):
    (cart["opt"] / "VERSION").write_text("1.15.01\n")
    r = run(cart)
    assert r.returncode != 0 and "1.15.01" in r.stderr
    assert backups(cart) == []
    assert "sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash" in r.stderr
    assert "sudo -E" not in r.stderr


def test_old_1_16_00_marker_does_not_block(cart):
    old = cart["opt"] / ".bak-20261108-181843"
    old.mkdir()
    (old / "deploy.marker").write_text("from=1.15.01\nto=1.16.00\nbuild=323 2b28f24\n")
    r = run(cart)
    assert r.returncode == 0, r.stderr


def test_refuses_dirty_build(cart):
    (cart["pkg"] / "opt" / "BUILD").write_text("330 abc1234-dirty\n")
    _write_manifest(cart["pkg"])
    r = run(cart)
    assert r.returncode != 0 and "dirty" in r.stderr
    assert backups(cart) == []


def test_refuses_while_a_mac_is_armed(cart):
    (cart["opt"] / "tftp" / "grub.cfg-01-aa-bb-cc-dd-ee-ff").write_text("x")
    r = run(cart)
    assert r.returncode != 0 and "上锁" in r.stderr
    assert backups(cart) == []
    assert "stop pxe-server" not in calls(cart)


@pytest.mark.parametrize("missing", ["systemd/pxe-server.service", "etc/logrotate.d/ops-cart", "accept/cart_accept.py",
                                     "opt/bigfish_core.py", "opt/observe_core.py"])
def test_refuses_incomplete_package(cart, missing):
    (cart["pkg"] / missing).unlink()
    r = run(cart)
    assert r.returncode != 0 and missing in r.stderr
    assert backups(cart) == []


def test_refuses_package_with_wrong_target_version(cart):
    (cart["pkg"] / "opt" / "VERSION").write_text("1.16.00\n")
    _write_manifest(cart["pkg"])
    r = run(cart)
    assert r.returncode != 0 and "不是 1.16.01" in r.stderr
    assert backups(cart) == []


def test_refuses_when_package_differs_from_manifest(cart):
    (cart["pkg"] / "opt" / "kiosk.py").write_text("tampered\n")
    r = run(cart)
    assert r.returncode != 0 and "kiosk.py" in r.stderr
    assert backups(cart) == []


def test_refuses_when_logrotate_config_differs_from_manifest(cart):
    (cart["pkg"] / "etc" / "logrotate.d" / "ops-cart").write_text("tampered\n")
    r = run(cart)
    assert r.returncode != 0 and "logrotate.d/ops-cart" in r.stderr
    assert backups(cart) == []


def test_refuses_when_logrotate_config_not_in_manifest(cart):
    m = json.loads((cart["pkg"] / "accept" / "accept-manifest.json").read_text())
    del m["files"]["/etc/logrotate.d/ops-cart"]
    (cart["pkg"] / "accept" / "accept-manifest.json").write_text(json.dumps(m))
    r = run(cart)
    assert r.returncode != 0 and "manifest 没列 /etc/logrotate.d/ops-cart" in r.stderr


def test_refuses_when_manifest_build_differs(cart):
    _write_manifest(cart["pkg"], build="999 deadbee")
    r = run(cart)
    assert r.returncode != 0 and "999 deadbee" in r.stderr
    assert backups(cart) == []


def test_refuses_empty_manifest_files(cart):
    m = json.loads((cart["pkg"] / "accept" / "accept-manifest.json").read_text())
    m["files"] = {}
    (cart["pkg"] / "accept" / "accept-manifest.json").write_text(json.dumps(m))
    r = run(cart)
    assert r.returncode != 0 and "files 是空的" in r.stderr


def test_refuses_manifest_with_too_few_opt_files(cart):
    r = run(cart, {"MIN_OPT_FILES": "60"})
    assert r.returncode != 0 and "少于 60" in r.stderr
    assert backups(cart) == []


def test_warns_on_packaged_file_not_in_manifest(cart):
    (cart["pkg"] / "opt" / "approval_core.py").write_text("not deployed yet\n")
    raw = (cart["pkg"] / "accept" / "accept-manifest.json").read_bytes()
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "警告" in r.stderr and "opt/approval_core.py" in r.stderr and "opt/BUILD" not in r.stderr
    assert hashlib.sha256(raw).hexdigest() in r.stdout


def test_sighup_prints_rollback_and_rerun_refuses(cart):
    sc = cart["bin"] / "systemctl"
    sc.write_text(sc.read_text().replace(
        "  start|restart)\n",
        f'  start|restart)\n    [ "$2" = kiosk ] && [ ! -f {cart["tmp"]}/hup-done ] && touch {cart["tmp"]}/hup-done && kill -HUP $PPID\n', 1))
    r = run(cart)
    assert r.returncode == 130
    (b,) = backups(cart)
    assert "信号" in r.stderr and f"bash {b}/rollback.sh" in r.stderr
    r2 = run(cart)
    assert r2.returncode != 0
    assert "上次可能没装完" in r2.stderr and f"bash {b}/rollback.sh" in r2.stderr
    assert backups(cart) == [b]


def test_rerun_refused_by_marker_even_if_version_not_yet_written(cart):
    cart["fail"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    (cart["opt"] / "VERSION").write_text("1.16.00\n")
    r = run(cart)
    assert r.returncode != 0 and "上次可能没装完" in r.stderr and str(b) in r.stderr
    assert backups(cart) == [b]


def test_refuses_when_already_target_version(cart):
    (cart["opt"] / "VERSION").write_text("1.16.01\n")
    r = run(cart, {"FORCE": "1"})
    assert r.returncode != 0 and "上次可能没装完" in r.stderr
    assert backups(cart) == []


def test_half_made_backup_without_rollback_is_removed(cart):
    _exe(cart["bin"] / "chmod", "exit 1\n")
    r = run(cart)
    assert r.returncode != 0
    assert backups(cart) == [] and "什么都没改" in r.stderr
    assert (cart["opt"] / "VERSION").read_text() == "1.16.00\n"
    (cart["bin"] / "chmod").unlink()
    assert run(cart).returncode == 0


def test_marker_written_only_with_rollback(cart):
    run(cart)
    (b,) = backups(cart)
    marker = (b / "deploy.marker").read_text().splitlines()
    assert marker[:2] == ["from=1.16.00", "to=1.16.01"] and "done=1" in marker
    assert (b / "rollback.sh").exists()


def test_interrupted_during_accept_says_installed(cart):
    (cart["tmp"] / "term-in-accept").write_text("")
    r = run(cart)
    assert r.returncode == 130
    assert "已经装完, 只是验收被打断" in r.stderr and "装了一半" not in r.stderr
    r2 = run(cart)
    assert r2.returncode != 0 and "已经装完了" in r2.stderr


def test_install_hint_and_rollback_use_systemd_run(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    (b,) = backups(cart)
    assert f"sudo systemd-run --unit=ops-cart-rollback --collect bash {b}/rollback.sh" in r.stdout
    head = (cart["pkg"] / "install.sh").read_text().splitlines()[:6]
    assert any("systemd-run --unit=ops-cart-deploy --collect" in l for l in head)


def test_inline_python_runs_isolated():
    s = SCRIPT.read_text()
    assert 'python3 -I - "$PKG"' in s


def test_never_touches_bigfish_dirs():
    s = SCRIPT.read_text()
    code = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
    assert "/home/bigfish" not in code and "bigfish-ctl" not in code


# ------------------------------------------------------------ 打包脚本

def test_pack_script_only_ships_tracked_files_and_migration_parts():
    code = [l for l in PACK.read_text().splitlines() if not l.lstrip().startswith("#")]
    assert any("git ls-files" in l for l in code) and not any("*.py " in l and l.lstrip().startswith("cp") for l in code)
    text = "\n".join(code)
    for part in ("deploy/systemd/pxe-server.service", "deploy/logrotate.d/ops-cart", "tests/test_bigfish_panel.py",
                 "tests/test_confirm_panel.py", "scripts/install-1.16.01.sh"):
        assert part in text
    assert "srv-data" not in text                     # smb 这版直接停用, 不带 drop-in / 钩子


def test_pack_ships_files_install_requires():
    """install.sh 要的每个包内路径, pack 都会放 (防两边对不上)."""
    s = SCRIPT.read_text()
    p = PACK.read_text()
    assert "systemd/$u" in s and '"$P/systemd/"' in p
    for need, made in (("etc/logrotate.d/ops-cart", '"$P/etc/logrotate.d/"'),
                       ("accept/accept-manifest.json", '"$P/accept/"')):
        assert need in s and made in p
