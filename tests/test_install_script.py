"""scripts/install-1.16.00.sh: 假推车目录 + 假 systemctl / python3 / id 跑一遍, 看备份、不碰配置、新单元不开、出错停下给回滚."""
import os
import pathlib
import shutil
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-1.16.00.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")


def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


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
    for f in ("cart_accept.py", "accept_core.py", "accept-manifest.json"):
        (pkg / "accept" / f).write_text("x\n")

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
    # 假 python3: kiosk_core --write-rescue 像 pxe_disarm 那样写新菜单、删 EFI/BOOT 菜单、写 pxe-mode.json; 验收只记一笔
    _exe(bin_ / "python3", f"""echo "python3 $*" >> {calls}
case "$1" in
  */kiosk_core.py) printf "menuentry 'Boot from local disk' {{ exit }}\\n" > "$4/grub.cfg"
                   rm -f "$4/EFI/BOOT/grub.cfg"; echo '{{"new": 1}}' > "$4/../pxe-mode.json";;
esac
exit 0
""")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", OPT=str(opt), SYS=str(sysd),
               ETC=str(etc), LOG=str(log), SETTLE="0")
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
    assert f"sudo bash {b}/rollback.sh" in r.stderr
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
    assert f"sudo bash {b}/rollback.sh" in r.stderr


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
    r = run(cart)
    assert r.returncode != 0 and "1.16.01" in r.stderr
    assert backups(cart) == []


def test_force_hint_does_not_use_sudo_E(cart):
    (cart["opt"] / "VERSION").write_text("1.14.00\n")
    r = run(cart)
    assert "sudo FORCE=1 bash" in r.stderr and "sudo -E" not in r.stderr
