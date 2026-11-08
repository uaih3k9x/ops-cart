"""samba_core: 推车 Samba 配置生成 (#11). 只绑 LAN1、装机共享账号登录、克隆共享跟 clone.json 一致 (#18)."""

import json
import pathlib
import re
import shlex
import subprocess
import sys

import pytest

import samba_core as sb
import srv_data_core as sd

ROOT = pathlib.Path(__file__).resolve().parent.parent
IP, PREFIX = "192.0.2.1", "24"
CLONE = {"proto": "smb", "smb_version": "smb3", "save_share": "clone-rw", "user": "clonesave",
         "password": "Example-pass1", "restore_share": "clone-ro", "restore_user": "",
         "restore_password": "", "local_dir": "/srv/data/org/clone"}


def sections(conf):
    """smb.conf 文本 -> {节名: {键: 值}}."""
    out, cur = {}, None
    for line in conf.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        k, _, v = s.partition("=")
        cur[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- 网段

@pytest.mark.parametrize("ip,prefix,want", [
    ("192.0.2.1", "24", "192.0.2.0/255.255.255.0"),
    ("198.51.100.77", "25", "198.51.100.0/255.255.255.128"),
    ("10.20.30.40", 16, "10.20.0.0/255.255.0.0"),
])
def test_lan_net(ip, prefix, want):
    assert sb.lan_net(ip, prefix) == want


@pytest.mark.parametrize("ip,prefix", [("x", "24"), ("192.0.2.1", "33"), ("192.0.2.1", "7"),
                                       ("192.0.2.1", ""), ("2001:db8::1", "64"), ("0.0.0.0", "24")])
def test_lan_net_rejects(ip, prefix):
    with pytest.raises(ValueError):
        sb.lan_net(ip, prefix)


def test_firewall_rule_source_lan1_net_smb_only():
    r = sb.firewall_rich_rule(IP, PREFIX)
    assert r == 'rule family="ipv4" source address="192.0.2.0/24" port port="' + str(sb.SMB_PORT) + '" protocol="tcp" accept'


# ---------------------------------------------------------------- 全局

def test_global_binds_lan1_only():
    g = sections(sb.smb_conf(IP, PREFIX, None))["global"]
    assert g["interfaces"] == "lo eno1" and g["bind interfaces only"] == "yes"
    assert g["hosts allow"] == "127.0.0.1 192.0.2.0/255.255.255.0" and g["hosts deny"] == "ALL"
    assert g["smb ports"] == str(sb.SMB_PORT) and g["disable netbios"] == "yes"
    assert g["security"] == "user"


def test_no_guest_anywhere_without_clone():
    c = sections(sb.smb_conf(IP, PREFIX, None))
    assert c["global"]["map to guest"] == "Never"
    assert all(s.get("guest ok", "no") == "no" for s in c.values())


# ---------------------------------------------------------------- 装机共享

def test_install_shares_account_login():
    c = sections(sb.smb_conf(IP, PREFIX, None))
    assert set(c) == {"global", "win", "drivers", "tools"}
    for name in ("win", "drivers", "tools"):
        assert c[name]["valid users"] == sb.INSTALL_USER, name
        assert c[name].get("guest ok", "no") == "no"
    assert c["win"]["path"] == sd.LAYOUT["win"] and c["win"]["read only"] == "yes"
    assert c["tools"]["path"] == sd.LAYOUT["tools"] and c["tools"]["read only"] == "yes"
    d = c["drivers"]
    assert d["path"] == sd.LAYOUT["drivers"] and d["read only"] == "no"       # 驱动总裁在 PE 里直接跑, 要写
    assert d["force group"] == sd.SMB_GROUP


# ---------------------------------------------------------------- 克隆共享 (#18)

def test_clone_shares_follow_clone_json():
    c = sections(sb.smb_conf(IP, PREFIX, CLONE))
    rw, ro = c["clone-rw"], c["clone-ro"]
    assert rw["path"] == ro["path"] == "/srv/data/org/clone"
    assert rw["read only"] == "no" and rw["valid users"] == "clonesave"
    assert ro["read only"] == "yes"


def test_clone_ro_guest_when_no_restore_user():
    """clone.json 没写 restore_user: #18 当访客挂 (Clonezilla 是 Linux, 不受 Win11 拒来宾影响).
    只有 clone-ro 收访客, 别的共享照旧要账号."""
    c = sections(sb.smb_conf(IP, PREFIX, CLONE))
    assert c["clone-ro"]["guest ok"] == "yes" and "valid users" not in c["clone-ro"]
    assert c["global"]["map to guest"] == "Bad User"
    for name in ("win", "drivers", "tools", "clone-rw"):
        assert c[name].get("guest ok", "no") == "no", name


def test_clone_ro_account_when_restore_user():
    conf = dict(CLONE, restore_user="cloneread", restore_password="Example-ro1")
    c = sections(sb.smb_conf(IP, PREFIX, conf))
    assert c["clone-ro"]["valid users"] == "cloneread" and c["clone-ro"].get("guest ok", "no") == "no"
    assert c["global"]["map to guest"] == "Never"


def test_clone_not_smb_no_clone_shares():
    c = sections(sb.smb_conf(IP, PREFIX, dict(CLONE, proto="nfs")))
    assert "clone-rw" not in c and "clone-ro" not in c


@pytest.mark.parametrize("bad", [
    dict(CLONE, save_share="win"),                 # 撞装机共享
    dict(CLONE, restore_share="clone-rw"),         # 两个共享同名
    dict(CLONE, save_share="clone/sub"),           # 子目录: 这里只生成整个共享
    dict(CLONE, save_share=""),
    dict(CLONE, local_dir="relative"),
    dict(CLONE, local_dir=""),
    dict(CLONE, user=""),                          # 可写共享必须有账号
    dict(CLONE, user=sb.INSTALL_USER),             # 账号不和装机共用
    dict(CLONE, restore_user="clonesave", restore_password="Example-pass1"),   # 只读账号 = 可写账号
    "notadict",
])
def test_clone_conf_rejected(bad):
    with pytest.raises(ValueError):
        sb.smb_conf(IP, PREFIX, bad)


def test_examples_clone_json_renders():
    """仓库里的样例照抄能生成 (#18 的样例: clone-ro 访客)."""
    conf = json.loads((ROOT / "examples" / "clone.example.json").read_text())
    c = sections(sb.smb_conf(IP, PREFIX, conf))
    assert c[conf["save_share"]]["valid users"] == conf["user"]
    assert c[conf["restore_share"]]["path"] == conf["local_dir"]


# ---------------------------------------------------------------- 账号清单

def test_accounts_install_and_clone():
    assert sb.accounts(None) == [(sb.INSTALL_USER, True)]
    conf = dict(CLONE, restore_user="cloneread", restore_password="Example-ro1")
    assert sb.accounts(conf) == [(sb.INSTALL_USER, True), ("clonesave", True), ("cloneread", False)]
    assert sb.accounts(CLONE) == [(sb.INSTALL_USER, True), ("clonesave", True)]
    assert sb.accounts(dict(CLONE, proto="nfs")) == [(sb.INSTALL_USER, True)]


def test_clone_password():
    conf = dict(CLONE, restore_user="cloneread", restore_password="Example-ro1")
    assert sb.clone_password(conf, "clonesave") == "Example-pass1"
    assert sb.clone_password(conf, "cloneread") == "Example-ro1"
    with pytest.raises(ValueError):
        sb.clone_password(conf, sb.INSTALL_USER)


def test_no_password_in_rendered_conf():
    conf = dict(CLONE, restore_user="cloneread", restore_password="Example-ro1")
    text = sb.smb_conf(IP, PREFIX, conf)
    assert "Example-pass1" not in text and "Example-ro1" not in text


# ---------------------------------------------------------------- 部署件

def test_smb_dropin_requires_data_disk():
    u = (ROOT / "deploy" / "srv-data" / "smb-ops-cart.conf").read_text()
    assert "ConditionPathIsMountPoint=/srv/data" in u       # 没挂数据盘: smbd 不起, 不往根分区写
    assert "RequiresMountsFor=/srv/data" in u


LAN1_DROPIN = ROOT / "deploy" / "srv-data" / "smb-lan1.conf"


def _exec_condition():
    lines = [l for l in LAN1_DROPIN.read_text().splitlines() if l.startswith("ExecCondition=")]
    assert len(lines) == 1
    return shlex.split(lines[0].partition("=")[2])


@pytest.mark.parametrize("ip_out,ip_rc,want", [
    ("", 0, 1),                                                         # 没插线: NM 没给地址 -> 跳过
    ("", 1, 1),                                                         # 没有 eno1 这块网卡 -> 跳过
    ("2: eno1    inet 192.0.2.1/24 brd 192.0.2.255 scope global eno1\n", 0, 0),     # 有地址 -> 起
])
def test_smb_lan1_dropin_skips_cleanly_without_address(tmp_path, ip_out, ip_rc, want):
    """#95 smb.conf 只绑 LAN1: LAN1 没地址时 smbd 起来找不到接口, smb_panic 出 core.
    ExecCondition 不满足 = 单元跳过 (inactive, 不是 failed), 插线来地址时 NM 钩子再拉起."""
    fake = tmp_path / "ip"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> {tmp_path}/args\nprintf '%s' '{ip_out}'\nexit {ip_rc}\n")
    fake.chmod(0o755)
    r = subprocess.run(_exec_condition(), env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.returncode == want
    assert f"dev {sb.LAN1}" in (tmp_path / "args").read_text()


def test_smb_lan1_dropin_is_condition_not_wait():
    body = LAN1_DROPIN.read_text()
    assert "[Service]" in body and "ExecStartPre" not in body     # 不在开机时干等 (拖慢开机 / 超时又是 failed)


def test_nm_dispatcher_starts_smb_on_lan1_up():
    s = (ROOT / "deploy" / "srv-data" / "50-ops-cart-smb").read_text()
    # 开机没地址时 smbd 被跳过 (没在跑), try-restart 拉不起来; 要 restart, 但只对开了自启的 (没装 / 没启用不动)
    assert '"eno1"' in s and "try-restart" not in s
    assert ("{ systemctl is-enabled -q smb.service || systemctl is-active -q smb.service; } "
            "&& systemctl restart smb.service") in s


@pytest.mark.parametrize("enabled,active,want_restart", [
    (0, 1, True), (1, 0, True), (1, 1, False),          # 0 = 是 (systemctl 退出码); 手工起的没开自启也要重绑
])
def test_nm_dispatcher_restart_decision(tmp_path, enabled, active, want_restart):
    log = tmp_path / "calls"
    fake = tmp_path / "systemctl"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> {log}\n"
                    f"case \"$1\" in is-enabled) exit {enabled};; is-active) exit {active};; esac\nexit 0\n")
    fake.chmod(0o755)
    hook = ROOT / "deploy" / "srv-data" / "50-ops-cart-smb"
    r = subprocess.run(["bash", str(hook), "eno1", "up"], env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.returncode == 0
    assert ("restart smb.service" in log.read_text()) == want_restart
    r = subprocess.run(["bash", "-n", str(ROOT / "deploy" / "srv-data" / "50-ops-cart-smb")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- CLI

def _cli(*args, stdin=None):
    return subprocess.run([sys.executable, str(ROOT / "samba_core.py"), *args],
                          capture_output=True, text=True, input=stdin)


def _env(tmp_path):
    env = tmp_path / "pxe.env"
    env.write_text("PXE_IP=198.51.100.1\nPXE_PREFIX=24\nPXE_POOL=198.51.100.10,198.51.100.20\n")
    return env


def test_cli_render_with_and_without_clone(tmp_path):
    env = _env(tmp_path)
    r = _cli("render", "--env", str(env), "--clone", str(tmp_path / "missing.json"))
    assert r.returncode == 0 and "198.51.100.0/255.255.255.0" in r.stdout and "[clone-rw]" not in r.stdout
    cj = tmp_path / "clone.json"
    cj.write_text(json.dumps(CLONE))
    r = _cli("render", "--env", str(env), "--clone", str(cj))
    assert r.returncode == 0 and "[clone-rw]" in r.stdout


def test_cli_render_missing_env_exit_2(tmp_path):
    r = _cli("render", "--env", str(tmp_path / "nope.env"), "--clone", str(tmp_path / "x.json"))
    assert r.returncode == 2 and "pxe.env" in r.stderr


def test_cli_render_bad_clone_exit_2(tmp_path):
    cj = tmp_path / "clone.json"
    cj.write_text("{bad json")
    r = _cli("render", "--env", str(_env(tmp_path)), "--clone", str(cj))
    assert r.returncode == 2


def test_cli_rich_rule_and_users(tmp_path):
    env = _env(tmp_path)
    r = _cli("rich-rule", "--env", str(env))
    assert r.returncode == 0 and r.stdout.strip() == sb.firewall_rich_rule("198.51.100.1", "24")
    cj = tmp_path / "clone.json"
    cj.write_text(json.dumps(CLONE))
    r = _cli("users", "--clone", str(cj))
    assert r.stdout.split("\n")[:2] == [f"{sb.INSTALL_USER} rw", "clonesave rw"]


def test_cli_clone_password_twice_for_smbpasswd(tmp_path):
    cj = tmp_path / "clone.json"
    cj.write_text(json.dumps(CLONE))
    r = _cli("clone-password", "--clone", str(cj), "clonesave")
    assert r.returncode == 0 and r.stdout == "Example-pass1\nExample-pass1\n"     # smbpasswd -s 要两遍


def test_setup_samba_does_not_install_lan1_dropin():
    """仓库生成的 smb.conf 带 lo, LAN1 没地址时 smbd 也有 127.0.0.1 可绑, 不会崩; 再装 lan1 drop-in 反而没插线时整个跳过.
    lan1 drop-in 只给手工配置的推车 (docs/MIGRATE_95.md)."""
    assert "srv-data/smb-lan1.conf" not in (ROOT / "scripts" / "setup-samba.sh").read_text()
    assert sections(sb.smb_conf(IP, PREFIX, None))["global"]["interfaces"].split()[0] == "lo"
