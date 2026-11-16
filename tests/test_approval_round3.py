"""远程审批第一阶段第三轮审查回归 (#50): 分级不信调用方、变形写法升 L3、打码不藏命令、口令和密码提示符漏网.
这些命令只当字符串送进分级 / 打码判定, 不执行、不连任何设备."""

import os

import pytest

import approval_core as ac
from test_approval_core import ACCOUNTS, NOW, req as _req, seeds, serial


def req(item, **kw):
    return _req(id=item["id"], digest=ac.digest(item), **kw)


def feishu_one_click(item):
    c = ac.card(item, "feishu", NOW)
    return "approve" in c["buttons"] and not c["totp"]


# ------------------------------------------------------------ H1: 变形写法不能落到 L2 一键批

H1_CMDS = [
    # Linux 绝对路径 / 换写法
    "/sbin/reboot", "/usr/sbin/shutdown -h now", "/bin/rm -rf /", "find / -delete", "$'\\x72m' -rf /etc",
    "blkdiscard /dev/sda", "echo b > /proc/sysrq-trigger", "telinit 6", "systemctl isolate rescue.target",
    "iptables -F", "ip link set eth0 down", "ifdown eth0", "nft flush ruleset", "wipefs -a /dev/sdb",
    "shred /dev/sda", "kill -9 1", "sudo /sbin/reboot", "env X=1 reboot", "true; /sbin/halt",
    "nmcli connection down eth0",
    # 思科
    "do wr", "no ip address", "no vlan 2-4094", "clear ip bgp *", "crypto key zeroize rsa", "test crash",
    "default interface gi0/1", "debug all",
    # 华为
    "undo ip route-static all", "undo vlan batch 2 to 4094", "clear configuration interface GE0/0/1",
    "rollback configuration to label x", "commit",
]


@pytest.mark.parametrize("cmd", H1_CMDS)
def test_h1_disguised_destructive_commands_are_L3(cmd):
    it = serial(cmd)                         # 调用方没传 danger: 审批模块自己现算
    assert it["tier"] == ac.L3, cmd
    assert ac.tier_of(it) == ac.L3
    assert not feishu_one_click(it), cmd
    assert it["danger"], "卡片标题要能看到为什么是高危"


def test_h1_tier_of_does_not_trust_item_danger():
    it = serial("/sbin/reboot")
    it["danger"] = []
    assert ac.tier_of(it) == ac.L3


@pytest.mark.parametrize("cmd", ["display version", "show ip interface brief", "display interface brief",
                                 "show running-config | include hostname", "ls -l /tmp", "cat /etc/hostname",
                                 "screen-length 0 temporary", "terminal length 0", "interface GE0/0/1",
                                 "description uplink-to-core"])
def test_h1_ordinary_commands_stay_L2(cmd):
    assert serial(cmd)["tier"] == ac.L2, cmd


# ------------------------------------------------------------ H2: 打码不能把命令藏起来

H2_CMDS = ["KEY= iptables -F", "PASS= /sbin/reboot", "TOKEN= ifdown eth0", "strace -o key iptables -F",
           "env -C key ifdown eth0", "sudo -u auth ifdown eth0", "echo token; iptables -F",
           "set key x; ip link set eth0 down"]


@pytest.mark.parametrize("cmd", H2_CMDS)
def test_h2_masking_never_hides_a_command_behind_one_click(cmd):
    it = serial(cmd)
    assert not feishu_one_click(it), cmd
    shown = ac.card(it, "feishu", NOW)["text"]
    # 要么能直接看到后面的命令词, 要么根本不给批准
    if "approve" in ac.card(it, "feishu", NOW)["buttons"]:
        for word in ("iptables", "reboot", "ifdown", "link"):
            if word in cmd:
                assert word in shown, (cmd, shown)


@pytest.mark.parametrize("cmd", ["strace -o key iptables -F", "env -C key ifdown eth0",
                                 "sudo -u auth ifdown eth0"])
def test_h2_value_that_is_followed_by_more_words_is_unclear(cmd):
    assert ac.truncated(serial(cmd)), cmd


def test_h2_value_rule_requires_value_glued_to_equals():
    assert "iptables" in ac.mask_command("KEY= iptables -F")


@pytest.mark.parametrize("cmd", ["username admin password 7 S3cretPw", "snmp-server community S3cret RO 2000",
                                 "tacacs-server host 192.0.2.1 key S3cr3tKey", "export DB_PASSWORD=S3cret"])
def test_h2_masked_serial_is_L3_not_one_click(cmd):
    it = serial(cmd)
    assert "S3cr" not in ac.card(it, "feishu", NOW)["text"]
    assert it["tier"] == ac.L3 and not feishu_one_click(it)


# ------------------------------------------------------------ M1: 漏网口令写法

M1_CMDS = [
    ("ipmitool -I lanplus -H 192.0.2.9 -U admin -P S3cretPw chassis status", "S3cretPw"),
    ("redis-cli -a S3cretPw ping", "S3cretPw"),
    ("unzip -P S3cretPw a.zip", "S3cretPw"),
    ("zip -P S3cretPw a.zip f", "S3cretPw"),
    ("ssh-keygen -N S3cretPw -f k", "S3cretPw"),
    ("curl -H 'Authorization: Bearer S3cretTok' https://example.com", "S3cretTok"),
    ("snmp-server user u g v3 auth sha AuthPw1 priv aes 128 PrivPw1", "PrivPw1"),
    ("snmp-server user u g v3 auth sha AuthPw1 priv aes 128 PrivPw1", "AuthPw1"),
    ("security wpa2 psk pass-phrase S3cretPw aes", "S3cretPw"),
    ("wpa-psk ascii 0 S3cretPw", "S3cretPw"),
    ("key config-key password-encrypt S3cretPw", "S3cretPw"),
    ("username admin password 12", "12"),
]


@pytest.mark.parametrize("cmd,secret", M1_CMDS)
def test_m1_credentials_are_masked(cmd, secret):
    shown = ac.mask_command(cmd)
    assert secret not in shown.replace("<已打码", ""), (cmd, shown)


def test_m1_two_digit_password_does_not_eat_following_words():
    assert ac.mask_command("username admin password 12").startswith("username admin password <已打码")


# ------------------------------------------------------------ M2: 密码提示符

@pytest.mark.parametrize("last", ["Enter passphrase for key '/root/.ssh/id_rsa':", "Enter enable secret:",
                                  "Password: \x1b[0m", "口令：", "Enter PIN:"])
def test_m2_password_prompt_variants(last):
    it = serial("S3cretPw", evidence=["login ok", last])
    assert ac.at_password_prompt(it), last
    assert "S3cretPw" not in ac.card(it, "feishu", NOW)["text"]
    assert "approve" not in ac.card(it, "feishu", NOW)["buttons"]


def test_m2_prompt_followed_by_control_only_line():
    it = serial("S3cretPw", evidence=["Password:", "\x1b[K"])
    assert ac.at_password_prompt(it)


def test_m2_normal_prompt_is_not_password_prompt():
    it = serial("display version", evidence=["<HUAWEI>"])
    assert not ac.at_password_prompt(it)


# ------------------------------------------------------------ 低危

def test_l1_record_uses_recomputed_digest():
    it = serial("display version")
    it2 = dict(it, digest="0" * 16)
    out, st, _ = ac.decide(ac.fresh_state(), it2, req(it, op="reject"), "feishu", seeds, ACCOUNTS, NOW)
    assert out == "rejected"
    assert st["decisions"][it["id"]]["digest"] == ac.digest(it)


def test_l2_audit_failure_still_counts_totp_failure(tmp_path, monkeypatch):
    store = ac.ApprovalStore(str(tmp_path / "a.json"), audit_path=str(tmp_path / "audit.log"))
    it = serial("reload")                     # L3: 飞书要 TOTP
    r = req(it, totp="000000")

    def boom(path, rec):
        raise OSError("disk full")
    monkeypatch.setattr(ac, "append_audit", boom)
    assert store.decide(it, r, "feishu", seeds, ACCOUNTS, now=NOW) == "audit_fail"
    import json
    st = json.load(open(tmp_path / "a.json"))
    assert st["accounts"]["alice"]["fails"] >= 1          # 失败计数照样落盘
    assert r["nonce"] in st["nonces"]
    assert st["decisions"] == {}                           # 但不登记批准 / 拒绝


def test_l2_audit_failure_never_registers_authorized(tmp_path, monkeypatch):
    store = ac.ApprovalStore(str(tmp_path / "a.json"), audit_path=str(tmp_path / "audit.log"))
    it = serial("display version")
    monkeypatch.setattr(ac, "append_audit", lambda p, r: (_ for _ in ()).throw(OSError("x")))
    assert store.decide(it, req(it), "feishu", seeds, ACCOUNTS, now=NOW) == "audit_fail"
    import json
    assert json.load(open(tmp_path / "a.json"))["decisions"] == {}     # 文件必须写出来 (计数落盘), 但不登记批准


def test_l3_longest_open_id_fits_by():
    oid = "ou_" + "a" * 54
    assert ac._OPEN_ID_RE.fullmatch(oid)
    assert ac._BY_RE.fullmatch("feishu:" + oid)
    assert not ac._OPEN_ID_RE.fullmatch("ou_" + "a" * 55)


def test_l4_intent_and_model_reason_are_masked():
    it = serial("display version", intent="run ipmitool -P S3cretPw", model_reason="password S3cretPw seen")
    text = ac.render(it, NOW)["text"]
    assert "S3cretPw" not in text


def test_snmpv3_auth_and_priv_both_masked_and_clear():
    t = "snmp-server user u g v3 auth sha AuthPw1 priv aes 128 PrivPw1"
    it = serial(t)
    assert not ac.truncated(it)               # 两个口令都打了, 其余看得清: L3 带 TOTP 能批
    c = ac.card(it, "feishu", NOW)
    assert "approve" in c["buttons"] and c["totp"]


# ------------------------------------------------------------ 第四轮审查回归

@pytest.mark.parametrize("cmd", ["password: S3cret", "DB_PASSWORD = S3cret", "echo \"api_key: S3cret\" >> app.yml",
                                 "pass: S3cret", "token : S3cret"])
def test_r4_m1_spaced_value_still_masked(cmd):
    it = serial(cmd)
    assert "S3cret" not in ac.card(it, "feishu", NOW)["text"], cmd
    assert not feishu_one_click(it)


def test_r4_m1_shell_assignment_then_command_not_masked():
    assert "iptables" in ac.mask_command("KEY= iptables -F")


@pytest.mark.parametrize("cmd", ["sudo -u root mount /dev/sda1 /mnt", "sudo -E -H chmod 777 /",
                                 "nice -n 10 chmod 777 /", "sudo -u root systemctl stop sshd"])
def test_r4_m2_prefix_options_with_args(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


@pytest.mark.parametrize("cmd", ["ls -la 2>/dev/null", "grep foo /var/log/syslog 2>/dev/null", "do show run",
                                 "mount", "sysctl -a", "find /etc -name '*.conf'", "cat /tmp/sa", "ls /srv/de",
                                 "iptables -L -n -v -x", "iptables -A INPUT -p tcp --dport 22 -j ACCEPT"])
def test_r4_l1_read_only_not_l3(cmd):
    assert serial(cmd)["tier"] == ac.L2, cmd


@pytest.mark.parametrize("cmd", ["iptables -P INPUT DROP", "iptables -F", "sysctl -w net.ipv4.ip_forward=0",
                                 "mount /dev/sdb1 /mnt", "find / -delete", "find / -exec rm {} ;"])
def test_r4_l1_still_l3(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


def test_r4_l2_too_many_modifiers_is_unclear():
    assert ac.truncated(serial("password 7 7 7 7 7 Hunter2"))


@pytest.mark.parametrize("last", ["Info: The password needs to be changed. Change now? [Y/N]:",
                                  "Warning: The password will expire in 3 days. Continue? [Y/N]:",
                                  "Configure the secret key of TACACS? [Y/N]:"])
def test_r4_l3_yes_no_question_is_not_password_prompt(last):
    assert not ac.at_password_prompt(serial("Y", evidence=[last]))


def test_r4_l3_evidence_with_embedded_newlines_uses_last_line():
    assert not ac.at_password_prompt(serial("display version", evidence=["Password:\r\n<HUAWEI>"]))


def test_r4_l4_audit_failure_keeps_existing_decisions_and_persists_reject(tmp_path, monkeypatch):
    import json
    store = ac.ApprovalStore(str(tmp_path / "a.json"), audit_path=str(tmp_path / "audit.log"))
    old = serial("display version")
    assert store.decide(old, req(old, nonce="ab" * 8), "feishu", seeds, ACCOUNTS, now=NOW) == "authorized"
    monkeypatch.setattr(ac, "append_audit", lambda p, r: (_ for _ in ()).throw(OSError("x")))
    other = ac.make_item("serial", 18, "1a2b3c4d", target="slot:3", created=NOW - 60,
                         action={"type": "line", "text": "display clock"})
    assert store.decide(other, req(other, op="reject", nonce="cd" * 8), "feishu", seeds, ACCOUNTS,
                        now=NOW + 1) == "audit_fail"
    st = json.load(open(tmp_path / "a.json"))
    assert st["decisions"][old["id"]]["op"] == "approve"         # 已有决定不被清掉
    assert st["decisions"][other["id"]]["op"] == "reject"        # 拒绝是停的方向, 照样落盘


# ------------------------------------------------------------ 第五轮审查回归

@pytest.mark.parametrize("cmd", ["sudo ip route show default", "sudo cat /var/log/debug", "sudo ls /etc/default",
                                 "sudo grep -r test /etc", "sudo journalctl -u test", "sudo docker logs commit",
                                 "mount -l", "mount -t ext4"])
def test_r5_m1_read_only_with_prefix_stays_l2(cmd):
    assert serial(cmd)["tier"] == ac.L2, cmd


@pytest.mark.parametrize("cmd", ["sudo -u root mount /dev/sda1 /mnt", "sudo -E -H chmod 777 /",
                                 "nice -n 10 chmod 777 /", "sudo -u root systemctl stop sshd",
                                 "sudo nice -n 5 env A=1 chmod 777 /", "sudo -u root -g wheel -E chmod 777 /",
                                 "mount -t ext4 /dev/sdb1 /mnt"])
def test_r5_m1_prefixed_commands_still_l3(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


@pytest.mark.parametrize("last", ["Enter new password [8-16 chars]:", "Enter password [default: none]:"])
def test_r5_m2_bracketed_password_prompt(last):
    assert ac.at_password_prompt(serial("S3cretPw", evidence=[last]))


@pytest.mark.parametrize("last", ["Change the password now? [Y/N]:", "Save the secret key [Y/N]:"])
def test_r5_m2_yes_no_is_not_prompt(last):
    assert not ac.at_password_prompt(serial("Y", evidence=[last]))


def test_r5_l4_long_serial_text_short_circuits():
    import time
    t0 = time.time()
    it = ac.make_item("serial", 19, "1a2b3c4d", target="slot:2", created=NOW - 60,
                      action={"type": "line", "text": "-" * 20000 + ":"})
    assert it["tier"] == ac.L3 and ac.truncated(it)
    assert time.time() - t0 < 1.0


# ------------------------------------------------------------ 第六轮审查回归

@pytest.mark.parametrize("cmd", ["sudo -n iptables -F", "sudo -s iptables -F", "sudo -i systemctl stop sshd",
                                 "sudo -S sysctl -w a=1", "sudo -k nft flush ruleset", "sudo -n chmod 777 /",
                                 "sudo -n mount /dev/sda1 /mnt", "env -i kill 1", "sudo -u root -i kill 1",
                                 "timeout 10 iptables -F", "timeout 5 systemctl stop sshd", "chroot /mnt iptables -F",
                                 "timeout -s KILL 5 kill 1", "mount -a", "mount --all"])
def test_r6_prefix_flags_without_args_still_l3(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


@pytest.mark.parametrize("cmd", ["sudo grep mount /etc/fstab", "sudo -n cat /etc/hosts", "sudo -i", "sudo dmesg",
                                 "sudo iptables -nL", "sudo journalctl -u sshd", "timeout 5 ping -c 1 192.0.2.1"])
def test_r6_read_only_with_prefix_flags_l2(cmd):
    assert serial(cmd)["tier"] == ac.L2, cmd


# ------------------------------------------------------------ 第七轮审查回归

@pytest.mark.parametrize("cmd", ["sudo -iu root kill 1", "sudo -nu root kill 1", "sudo -Eu root chmod 777 /",
                                 "sudo -iu root umount /mnt", "doas -nu root kill 1", "env -iu FOO kill 1",
                                 "sudo -u root nice -n 1 sudo -nu root systemctl stop sshd"])
def test_r7_combined_short_options(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


@pytest.mark.parametrize("cmd", ["sudo -iu root ls", "sudo -nu root cat /etc/hosts"])
def test_r7_combined_short_options_read_only(cmd):
    assert serial(cmd)["tier"] == ac.L2, cmd


# ------------------------------------------------------------ 第八轮审查回归

@pytest.mark.parametrize("cmd", ["sudo -uroot kill 1", "sudo -uroot chmod 600 /etc/x", "sudo -uroot mv a b",
                                 "sudo -groot kill 1", "strace -ofile kill 1", "sudo -ugit kill 1",
                                 "strace -fo log kill 1"])
def test_r8_glued_option_argument(cmd):
    assert serial(cmd)["tier"] == ac.L3, cmd


def test_r9_double_dash_ends_prefix_options():
    assert serial("sudo -nu root -- kill 1")["tier"] == ac.L3
    assert serial("sudo -- ls")["tier"] == ac.L2
