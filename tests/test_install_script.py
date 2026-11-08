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

    calls = tmp_path / "calls.log"
    fail = tmp_path / "fail-restart"
    _exe(bin_ / "id", 'echo 0\n')
    # 假 systemctl: kiosk / cart-dash / cart-mcp 开着; fail-restart 文件里写的服务重启失败
    _exe(bin_ / "systemctl", f'''echo "$*" >> {calls}
case "$1" in
  is-active|is-enabled) for a; do case "$a" in kiosk|cart-dash|cart-mcp) exit 0;; esac; done; exit 3;;
  restart) [ -f {fail} ] && grep -qx "$2" {fail} && exit 1; exit 0;;
esac
exit 0
''')
    # 假 python3: kiosk_core --write-rescue 写新菜单; 验收只记一笔
    _exe(bin_ / "python3", f'''echo "python3 $*" >> {calls}
case "$1" in
  */kiosk_core.py) printf "menuentry 'Boot from local disk' {{ exit }}\\n" > "$4/grub.cfg";;
esac
exit 0
''')
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", OPT=str(opt), SYS=str(sysd),
               ETC=str(etc), LOG=str(log), SETTLE="0")
    return dict(tmp=tmp_path, opt=opt, sys=sysd, etc=etc, pkg=pkg, env=env, calls=calls, fail=fail)


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
