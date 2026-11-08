"""scripts/install-1.16.00.sh: 假推车目录 + 假 systemctl / python3 / id 跑一遍, 看备份、不碰配置、新单元不开、出错停下给回滚."""
import hashlib
import json
import os
import pathlib
import signal
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-1.16.00.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")


def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_manifest(pkg, build=None):
    """照 cart_accept --make-manifest 的格式: /opt/pxe/<相对路径> 和装进去的单元."""
    files = {}
    for f in sorted((pkg / "opt").rglob("*")):
        if f.is_file() and f.name != "BUILD":                # 真 manifest 不列 BUILD, 单独记在 build 字段
            files["/opt/pxe/" + f.relative_to(pkg / "opt").as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    for f in sorted((pkg / "systemd").iterdir()):
        files["/etc/systemd/system/" + f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    files["/usr/local/bin/kiosk"] = "0" * 64          # 包里不带的系统件: 不核对
    m = {"version": "1.16.00", "build": build or (pkg / "opt" / "BUILD").read_text().strip(), "files": files}
    (pkg / "accept" / "accept-manifest.json").write_text(json.dumps(m))


@pytest.fixture
def cart(tmp_path):
    opt, sysd, etc, log, pkg, bin_ = (tmp_path / n for n in ("opt", "sys", "etc", "log", "pkg", "bin"))
    for d in (opt / "web", opt / "icons", opt / "tftp", sysd, etc, log, bin_,
              pkg / "opt" / "web", pkg / "opt" / "icons", pkg / "systemd", pkg / "accept"):
        d.mkdir(parents=True)
    (opt / "VERSION").write_text("1.15.01\n")
    (opt / "BUILD").write_text("304 f6541fd\n")
    (opt / "kiosk.py").write_text("old\n")
    (opt / "web" / "index.html").write_text("old\n")
    (opt / "tftp" / "grub.cfg").write_text("Rescue default\n")
    (sysd / "cart-mcp.service").write_text("old unit\n")
    (etc / "pxe.env").write_text("PXE_IP=x\n")
    (etc / "serial.conf").write_text("keep\n")

    (pkg / "opt" / "VERSION").write_text("1.16.00\n")
    (pkg / "opt" / "BUILD").write_text("320 abc1234\n")
    (pkg / "opt" / "kiosk.py").write_text("new\n")
    (pkg / "opt" / "kiosk_core.py").write_text("new\n")
    (pkg / "opt" / "ime_core.py").write_text("new\n")
    (pkg / "opt" / "web" / "index.html").write_text("new\n")
    for u in ("cart-mcp.service", "cart-mcp-ro.service", "cart-ble-input.service"):
        (pkg / "systemd" / u).write_text("new " + u + "\n")
    shutil.copy(SCRIPT, pkg / "install.sh")

    (opt / "tftp" / "EFI" / "BOOT").mkdir(parents=True)
    (opt / "tftp" / "EFI" / "BOOT" / "grub.cfg").write_text("old efi menu\n")
    (opt / "pxe-mode.json").write_text('{"old": 1}\n')
    for f in ("cart_accept.py", "accept_core.py"):
        (pkg / "accept" / f).write_text("x\n")
    _write_manifest(pkg)

    calls = tmp_path / "calls.log"
    fail = tmp_path / "fail-restart"      # 列在这里的服务: restart 返回非 0
    brk = tmp_path / "break-on-restart"   # 列在这里的服务: restart 返回 0, 之后 is-active 为假 (新代码起不来)
    active = tmp_path / "active"          # 当前 active 的服务 (同时当作 enabled)
    active.write_text("kiosk\ncart-dash\ncart-mcp\n")
    _exe(bin_ / "id", 'echo 0\n')
    _exe(bin_ / "systemctl", f"""echo "$*" >> {calls}
svc="${{@: -1}}"
case "$1" in
  is-active|is-enabled) grep -qx "$svc" {active} && exit 0; exit 3;;
  restart)
    [ -f {fail} ] && grep -qx "$2" {fail} && exit 1
    if [ -f {brk} ] && grep -qx "$2" {brk}; then grep -vx "$2" {active} > {active}.n || true; mv {active}.n {active}
    else grep -qx "$2" {active} || echo "$2" >> {active}; fi
    exit 0;;
esac
exit 0
""")
    # 假 python3: kiosk_core --write-rescue 像 pxe_disarm 那样写新菜单、删 EFI/BOOT 菜单、写 --state;
    # 验收只记一笔; 别的 (脚本里内联的 manifest 核对) 交给真 python
    _exe(bin_ / "python3", f"""echo "python3 $*" >> {calls}
case "$1" in
  */kiosk_core.py) printf "menuentry 'Boot from local disk' {{ exit }}\\n" > "$4/grub.cfg"
                   rm -f "$4/EFI/BOOT/grub.cfg"; echo '{{"new": 1}}' > "$6";;
  */cart_accept.py) [ -f {tmp_path}/term-in-accept ] && kill -TERM $PPID; sleep 0.2;;
  *) exec {sys.executable} "$@";;
esac
exit 0
""")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", OPT=str(opt), SYS=str(sysd),
               ETC=str(etc), LOG=str(log), SETTLE="0", MIN_OPT_FILES="1")
    return dict(tmp=tmp_path, opt=opt, sys=sysd, etc=etc, pkg=pkg, env=env, calls=calls, fail=fail,
                brk=brk, active=active)


def run(cart, extra_env=None):
    env = dict(cart["env"], **(extra_env or {}))
    return subprocess.run(["bash", str(cart["pkg"] / "install.sh")], env=env, capture_output=True, text=True)


def backups(cart):
    return sorted(cart["opt"].glob(".bak-*"))


def test_happy_path_installs_backs_up_and_leaves_config(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    opt = cart["opt"]
    assert (opt / "VERSION").read_text() == "1.16.00\n"
    assert (opt / "kiosk.py").read_text() == "new\n"
    assert (opt / "ime_core.py").exists()
    assert (opt / "web" / "index.html").read_text() == "new\n"
    assert "Boot from local disk" in (opt / "tftp" / "grub.cfg").read_text()
    assert (cart["sys"] / "cart-mcp.service").read_text() == "new cart-mcp.service\n"
    assert (cart["sys"] / "cart-mcp-ro.service").exists()
    # 配置原样, 没新建
    assert sorted(p.name for p in cart["etc"].iterdir()) == ["pxe.env", "serial.conf"]
    assert (cart["etc"] / "serial.conf").read_text() == "keep\n"
    # 备份齐
    (b,) = backups(cart)
    assert (b / "VERSION").read_text() == "1.15.01\n"
    assert (b / "kiosk.py").read_text() == "old\n"
    assert (b / "tftp" / "grub.cfg").read_text() == "Rescue default\n"
    assert (b / "etc" / "systemd" / "cart-mcp.service").read_text() == "old unit\n"
    assert (b / "etc" / "ops-cart" / "serial.conf").exists()
    calls = cart["calls"].read_text()
    # 新单元只装不开
    assert not any(l.startswith(("enable", "start")) for l in calls.splitlines())
    assert "restart kiosk" in calls and "restart pxe-server" not in calls   # 没开着的不重启
    assert "cart_accept.py" in calls
    assert "rollback.sh" in r.stdout


def test_refuses_wrong_version_without_touching_anything(cart):
    (cart["opt"] / "VERSION").write_text("1.14.00\n")
    r = run(cart)
    assert r.returncode != 0
    assert "1.14.00" in r.stderr
    assert backups(cart) == []
    assert (cart["opt"] / "kiosk.py").read_text() == "old\n"


def test_refuses_dirty_build(cart):
    (cart["pkg"] / "opt" / "BUILD").write_text("320 abc1234-dirty\n")
    _write_manifest(cart["pkg"])
    r = run(cart)
    assert r.returncode != 0 and "dirty" in r.stderr
    assert backups(cart) == []


def test_refuses_while_a_mac_is_armed(cart):
    (cart["opt"] / "tftp" / "grub.cfg-01-aa-bb-cc-dd-ee-ff").write_text("x")
    r = run(cart)
    assert r.returncode != 0 and "上锁" in r.stderr
    assert backups(cart) == []


def test_failure_stops_and_rollback_restores(cart):
    cart["fail"].write_text("kiosk\n")
    r = run(cart)
    assert r.returncode != 0
    (b,) = backups(cart)
    assert f"bash {b}/rollback.sh" in r.stderr
    assert "cart_accept.py" not in cart["calls"].read_text()       # 出错后不往下走

    cart["fail"].unlink()
    rr = subprocess.run(["bash", str(b / "rollback.sh")], env=cart["env"], capture_output=True, text=True)
    assert rr.returncode == 0, rr.stderr
    opt = cart["opt"]
    assert (opt / "VERSION").read_text() == "1.15.01\n"
    assert (opt / "kiosk.py").read_text() == "old\n"
    assert (opt / "web" / "index.html").read_text() == "old\n"
    assert (opt / "tftp" / "grub.cfg").read_text() == "Rescue default\n"
    assert (cart["sys"] / "cart-mcp.service").read_text() == "old unit\n"
    assert not (cart["sys"] / "cart-mcp-ro.service").exists()
    assert not (cart["sys"] / "cart-ble-input.service").exists()


def _rollback(cart, b):
    return subprocess.run(["bash", str(b / "rollback.sh")], env=cart["env"], capture_output=True, text=True)


def test_die_after_backup_prints_rollback(cart):
    """restart 返回 0 但随后没起来: 走 die 而不是 ERR, 也要打印回滚命令."""
    cart["brk"].write_text("kiosk\n")
    r = run(cart)
    assert r.returncode != 0
    (b,) = backups(cart)
    assert "kiosk" in r.stderr
    assert f"bash {b}/rollback.sh" in r.stderr


def test_rollback_restarts_services_active_before_deploy_even_if_down_now(cart):
    cart["brk"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    assert "kiosk" not in cart["active"].read_text().split()     # 新代码把 kiosk 弄停了
    cart["brk"].unlink()
    before = len(cart["calls"].read_text())
    rr = _rollback(cart, b)
    assert rr.returncode == 0, rr.stderr
    after = cart["calls"].read_text()[before:].splitlines()
    for s in ("kiosk", "cart-dash", "cart-mcp"):
        assert f"restart {s}" in after
    assert "restart pxe-server" not in after                     # 部署前没开的不拉
    assert "kiosk" in cart["active"].read_text().split()


def test_rollback_reports_services_that_fail_to_restart(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    rr = _rollback(cart, b)
    assert rr.returncode != 0 and "cart-mcp" in rr.stderr
    assert (cart["opt"] / "VERSION").read_text() == "1.15.01\n"   # 文件照样换回去了


def test_menus_and_pxe_mode_backed_up_and_restored_as_a_set(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    assert (b / "tftp" / "EFI" / "BOOT" / "grub.cfg").read_text() == "old efi menu\n"
    assert (b / "pxe-mode.json").exists()
    cart["fail"].unlink()
    assert _rollback(cart, b).returncode == 0
    opt = cart["opt"]
    assert (opt / "tftp" / "grub.cfg").read_text() == "Rescue default\n"
    assert (opt / "tftp" / "EFI" / "BOOT" / "grub.cfg").read_text() == "old efi menu\n"
    assert (opt / "pxe-mode.json").read_text() == '{"old": 1}\n'


def test_rollback_removes_pxe_mode_that_did_not_exist_before(cart):
    (cart["opt"] / "pxe-mode.json").unlink()
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    cart["fail"].unlink()
    assert _rollback(cart, b).returncode == 0
    assert not (cart["opt"] / "pxe-mode.json").exists()


@pytest.mark.parametrize("missing", ["systemd/cart-mcp-ro.service", "systemd/cart-ble-input.service",
                                     "systemd/cart-mcp.service", "accept/cart_accept.py"])
def test_refuses_incomplete_package(cart, missing):
    (cart["pkg"] / missing).unlink()
    r = run(cart)
    assert r.returncode != 0 and missing in r.stderr
    assert backups(cart) == []


def test_refuses_package_with_wrong_target_version(cart):
    (cart["pkg"] / "opt" / "VERSION").write_text("1.16.01\n")
    _write_manifest(cart["pkg"])
    r = run(cart)
    assert r.returncode != 0 and "1.16.01" in r.stderr
    assert backups(cart) == []


def test_force_hint_does_not_use_sudo_E(cart):
    (cart["opt"] / "VERSION").write_text("1.14.00\n")
    r = run(cart)
    assert "--setenv=FORCE=1" in r.stderr and "sudo -E" not in r.stderr


def test_install_hint_and_rollback_use_systemd_run(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    (b,) = backups(cart)
    assert f"sudo systemd-run --unit=ops-cart-rollback --collect bash {b}/rollback.sh" in r.stdout
    head = (cart["pkg"] / "install.sh").read_text().splitlines()[:6]
    assert any("systemd-run --unit=ops-cart-deploy --collect" in l for l in head)


def test_rewrites_menu_with_state_path(cart):
    run(cart)
    assert f"--state {cart['opt']}/pxe-mode.json" in cart["calls"].read_text()
    assert (cart["opt"] / "pxe-mode.json").read_text() == '{"new": 1}\n'


def test_services_not_running_before_deploy_are_reported_not_failed(cart):
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "pxe-server 部署前就没在跑" in r.stdout


def _hup_on_restart(cart, svc="kiosk"):
    """假 systemctl 在 restart <svc> 时给安装脚本发 SIGHUP, 当作 ssh 断线."""
    sc = cart["tmp"] / "bin" / "systemctl"
    body = sc.read_text().replace("  restart)\n", f'  restart)\n    [ "$2" = {svc} ] && [ ! -f {cart["tmp"]}/hup-done ] && touch {cart["tmp"]}/hup-done && kill -HUP $PPID\n', 1)
    sc.write_text(body)


def test_sighup_prints_rollback_and_rerun_refuses(cart):
    _hup_on_restart(cart)
    r = run(cart)
    assert r.returncode == 130
    (b,) = backups(cart)
    assert "信号" in r.stderr and f"bash {b}/rollback.sh" in r.stderr
    # 重跑: 版本已经是 1.16.00, 指向上次的 rollback, 不再说"什么都没改"之外的假话, 也不再建备份
    r2 = run(cart)
    assert r2.returncode != 0
    assert "上次可能没装完" in r2.stderr and f"bash {b}/rollback.sh" in r2.stderr
    assert backups(cart) == [b]


def test_rerun_refused_by_marker_even_if_version_not_yet_written(cart):
    cart["fail"].write_text("kiosk\n")
    run(cart)
    (b,) = backups(cart)
    (cart["opt"] / "VERSION").write_text("1.15.01\n")            # 假装断在拷 VERSION 之前
    r = run(cart)
    assert r.returncode != 0 and "上次可能没装完" in r.stderr and str(b) in r.stderr
    assert backups(cart) == [b]


def test_after_rollback_can_install_again(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    cart["fail"].unlink()
    assert _rollback(cart, b).returncode == 0
    assert (b / "deploy.marker.rolled-back").exists()
    import time; time.sleep(1.1)                                  # 备份目录按秒命名
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert len(backups(cart)) == 2


def test_refuses_when_already_target_version(cart):
    (cart["opt"] / "VERSION").write_text("1.16.00\n")
    r = run(cart, {"FORCE": "1"})
    assert r.returncode != 0 and "上次可能没装完" in r.stderr
    assert backups(cart) == []


def test_refuses_when_package_differs_from_manifest(cart):
    (cart["pkg"] / "opt" / "kiosk.py").write_text("tampered\n")
    r = run(cart)
    assert r.returncode != 0 and "kiosk.py" in r.stderr
    assert backups(cart) == []


def test_refuses_when_manifest_build_differs(cart):
    _write_manifest(cart["pkg"], build="999 deadbee")
    r = run(cart)
    assert r.returncode != 0 and "999 deadbee" in r.stderr
    assert backups(cart) == []


@pytest.mark.parametrize("unit", ["cart-mcp-ro.service", "cart-ble-input.service"])
def test_refuses_when_new_unit_already_present(cart, unit):
    (cart["sys"] / unit).write_text("someone\n")
    r = run(cart)
    assert r.returncode != 0 and unit in r.stderr
    assert backups(cart) == []


def test_rollback_err_trap_says_rerunnable(cart):
    cart["fail"].write_text("cart-mcp\n")
    run(cart)
    (b,) = backups(cart)
    (b / "VERSION").unlink()
    rr = _rollback(cart, b)
    assert rr.returncode != 0 and "回滚第" in rr.stderr and "重跑" in rr.stderr


def test_half_made_backup_without_rollback_is_removed(cart):
    """rollback.sh 还没写好就出错: 推车文件没动, 半截备份删掉, 也不留 marker 挡下次."""
    _exe(cart["tmp"] / "bin" / "chmod", "exit 1\n")
    r = run(cart)
    assert r.returncode != 0
    assert backups(cart) == []
    assert "什么都没改" in r.stderr
    assert (cart["opt"] / "VERSION").read_text() == "1.15.01\n"
    (cart["tmp"] / "bin" / "chmod").unlink()
    assert run(cart).returncode == 0                             # 下次照常装


def test_marker_written_only_with_rollback(cart):
    run(cart)
    (b,) = backups(cart)
    marker = (b / "deploy.marker").read_text().splitlines()
    assert marker[:2] == ["from=1.15.01", "to=1.16.00"] and "done=1" in marker
    assert (b / "rollback.sh").exists()


def test_interrupted_during_accept_says_installed(cart):
    (cart["tmp"] / "term-in-accept").write_text("")
    r = run(cart)
    assert r.returncode == 130
    assert "已经装完, 只是验收被打断" in r.stderr and "装了一半" not in r.stderr
    assert (cart["opt"] / "VERSION").read_text() == "1.16.00\n"
    r2 = run(cart)
    assert r2.returncode != 0 and "已经装完了" in r2.stderr and "没装完" not in r2.stderr


def test_force_hint_uses_systemd_run_setenv(cart):
    (cart["opt"] / "VERSION").write_text("1.14.00\n")
    r = run(cart)
    assert "sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash" in r.stderr


def test_refuses_empty_manifest_files(cart):
    m = json.loads((cart["pkg"] / "accept" / "accept-manifest.json").read_text())
    m["files"] = {}
    (cart["pkg"] / "accept" / "accept-manifest.json").write_text(json.dumps(m))
    r = run(cart)
    assert r.returncode != 0 and "files 是空的" in r.stderr
    assert backups(cart) == []


def test_refuses_manifest_with_too_few_opt_files(cart):
    r = run(cart, {"MIN_OPT_FILES": "60"})
    assert r.returncode != 0 and "少于 60" in r.stderr
    assert backups(cart) == []


def test_refuses_manifest_missing_a_packaged_unit(cart):
    m = json.loads((cart["pkg"] / "accept" / "accept-manifest.json").read_text())
    del m["files"]["/etc/systemd/system/cart-mcp-ro.service"]
    (cart["pkg"] / "accept" / "accept-manifest.json").write_text(json.dumps(m))
    r = run(cart)
    assert r.returncode != 0 and "manifest 只列了 2 个" in r.stderr
    assert backups(cart) == []


def test_warns_on_packaged_file_not_in_manifest_and_prints_manifest_sha(cart):
    m_raw = None
    (cart["pkg"] / "opt" / "approval_core.py").write_text("not deployed yet\n")   # 包里有、manifest 没列
    m_raw = (cart["pkg"] / "accept" / "accept-manifest.json").read_bytes()
    r = run(cart)
    assert r.returncode == 0, r.stderr
    assert "警告" in r.stderr and "opt/approval_core.py" in r.stderr
    assert "opt/BUILD" not in r.stderr
    assert hashlib.sha256(m_raw).hexdigest() in r.stdout


def test_inline_python_runs_isolated():
    assert 'python3 -I - "$PKG"' in SCRIPT.read_text()


def test_pack_script_only_ships_tracked_files():
    code = [l for l in (ROOT / "scripts" / "pack-1.16.00.sh").read_text().splitlines() if not l.lstrip().startswith("#")]
    assert any("git ls-files" in l for l in code) and not any("*.py " in l and l.lstrip().startswith("cp") for l in code)
