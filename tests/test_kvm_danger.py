"""#69 键鼠高危规则按"模式"查: Windows 递归删 / 清盘, Linux 写块设备 / 递归改权限 / 截断 / 抹盘.
正例必须命中 (挂起); 反例是常见只读命令, 不该命中. 往多拦的方向错, 但别把只读的看盘命令拦光.
大小写、多空格、引号、反斜杠续行尽力而为; 变量拼接、编码拦不住 (减速带)."""

import json

import pytest

import mcp_kvm as mk

DANGEROUS = [
    # ---- issue 原例
    "rd /s /q C:\\",
    "cat /dev/zero >/dev/sda",
    "> /dev/sda",
    "chmod -R 000 /",
    "truncate -s0 /etc/fstab",
    # ---- Windows
    "RD /S /Q D:\\data",
    "rmdir /s c:\\windows",
    "del /s /q C:\\*",
    "erase /q /s c:\\users",
    "cipher /w:C:",
    "cipher   /W:c:\\",
    "bcdedit /delete {current}",
    "bcdedit /set {default} recoveryenabled no",
    "diskpart",
    "clean",
    "clean all",
    "  CLEAN ",
    "delete partition override",
    "delete volume",
    "format fs=ntfs quick",
    "convert gpt",
    "Format-Volume -DriveLetter D",
    "Clear-Disk -Number 0 -RemoveData",
    "Initialize-Disk 1 -PartitionStyle GPT",
    "Remove-Partition -DiskNumber 0 -PartitionNumber 2",
    "Remove-Item -Recurse -Force C:\\",
    "ri -r -fo C:\\",
    "takeown /f C:\\ /r /d y",
    "icacls C:\\ /reset /t",
    # ---- Linux 写块设备
    "echo x > /dev/nvme0n1",
    "cat img >>/dev/vda",
    "cat img >| /dev/sdb",
    "cat img | tee /dev/mmcblk0",
    "cat img | sudo tee -a /dev/md0 >/dev/null",
    "pv disk.img > /dev/xvda",
    "cat x > /dev/mapper/vg-root",
    "cat x > /dev/disk/by-id/ata-foo",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "dd  if=img  of=/dev/sdb",
    "dd of=/dev/sda",
    "sudo cp disk.img /dev/sda",
    'echo x > "/dev/sda"',
    "echo x > '/dev/sda'",
    # ---- Linux 递归改权限 / 属主
    "chmod -R 777 /",
    "chmod --recursive 000 /*",
    "sudo chown -R nobody /etc",
    "chown -Rh user:user /usr/",
    "chgrp -R users /",
    # ---- 截断 / 覆盖
    "truncate --size 0 /var/log/x",
    ": > /etc/fstab",
    "echo > /etc/passwd",
    "cat x > /boot/grub2/grub.cfg",
    # ---- 抹盘 / 分区 / 卷
    "shred -n1 /dev/sda",
    "blkdiscard /dev/nvme0n1",
    "wipefs -a /dev/sda",
    "sgdisk --zap-all /dev/sda",
    "parted /dev/sda mklabel gpt",
    "sfdisk /dev/sda < layout",
    "gdisk /dev/sda",
    "fdisk /dev/sda",
    "mkfs.xfs -f /dev/sdb1",
    "mke2fs /dev/sdb1",
    "mkswap /dev/sdb2",
    "mkntfs -f /dev/sdb1",
    "lvremove -f vg/root",
    "vgremove vg",
    "pvremove /dev/sdb",
    "lvreduce -L 1G vg/root",
    "zpool destroy tank",
    "zpool labelclear -f /dev/sdb",
    "zfs destroy -r tank/data",
    "mdadm --zero-superblock /dev/sdb",
    "cryptsetup luksFormat /dev/sdb",
    "cryptsetup erase /dev/sdb",
    "nvme sanitize /dev/nvme0 -a 2",
    "hdparm --security-erase p /dev/sda",
    "sg_format --format /dev/sg1",
    "find / -delete",
    "find /etc -name '*.conf' -delete",
    "echo b > /proc/sysrq-trigger",
    "efibootmgr -b 0001 -B",
    "efibootmgr -o 0002,0001",
    # ---- 写法变形
    "CHMOD  -R  000  /",
    "c\\hmod -R 000 /",
    "'rd' /s /q c:\\",
    "chmod -R 000 / \\",
]

HARMLESS = [
    "ls /dev/sda",
    "ls -l /dev/sd*",
    "lsblk",
    "lsblk -f /dev/nvme0n1",
    "blkid /dev/sda1",
    "smartctl -a /dev/sda",
    "hexdump -C /dev/sda | head",
    "cat /dev/sda | head -c 512 | xxd",
    "ls > /dev/null",
    "cmd 2>/dev/null",
    "echo hi > /tmp/x",
    "uname -a | tee /tmp/uname.txt",
    "chmod -R 755 ./build",
    "chown -R user /home/user/project",
    "chmod 644 /etc/hosts",
    "cat /etc/fstab",
    "grep sda /etc/fstab",
    "echo hi >> /var/log/test.log",
    "dir /s c:\\",
    "rd emptydir",
    "cipher /?",
    "Get-Disk",
    "Get-Volume",
    "Get-Partition -DiskNumber 0",
    "ipconfig /all",
    "git clean -n",
    "make clean",
    "echo clean",
    "systeminfo",
    "efibootmgr",
    "efibootmgr -v",
    "mount /dev/sda1 /mnt",
    "df -h",
    "cat /proc/partitions",
    "find / -name fstab",
    "ip addr",
    "lvs",
    "zpool status",
    "nvme list",
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_dangerous_patterns_hit(cmd):
    assert mk.danger_of(cmd), cmd


@pytest.mark.parametrize("cmd", HARMLESS)
def test_readonly_commands_do_not_hit(cmd):
    assert mk.danger_of(cmd) == [], (cmd, mk.danger_of(cmd))


def test_backslash_continuation_across_lines_is_joined():
    """bash 里行尾 \\ 续到下一行: 拆在两行里也要查出来."""
    hits, *_ = mk.advance("", "", "cat /dev/zero \\\n> /dev/sda")
    assert hits
    hits, *_ = mk.advance("", "", "chmod -R 000 \\\n/")
    assert hits


def test_pattern_labels_are_short_and_readable():
    for cmd in ("rd /s /q c:\\", "cat x > /dev/sda", "chmod -R 000 /"):
        for w in mk.danger_of(cmd):
            assert 0 < len(w) <= 40 and "\\" not in w and "(" not in w


def test_held_through_tool(tmp_path):
    """端到端: 经 kvm_type 发, 挂起不发."""
    import mcp_serial as ms
    sent = []
    store = mk.LeaseStore(str(tmp_path / "l.json"), clock=lambda: 1000.0, stopped=lambda: False,
                          log_path=str(tmp_path / "k.log"), event=lambda *a: None)
    tools = mk.build_tools(store, ports=lambda: {"S": "/dev/x"}, opener=lambda p, w: sent.append(p),
                           ocr=None, wol=None, broker_up=lambda p: True, who=lambda: "agent-a")
    srv = ms.Server(tools)

    def call(name, **a):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}},
                       "agent-a")
        return json.loads(r["result"]["content"][0]["text"])
    tok = call("kvm_lease_acquire")["lease"]
    assert call("kvm_type", text="rd /s /q C:\\", enter=True, lease=tok)["status"] == "pending"
    assert sent == []


def test_continuation_carried_over_enter():
    """行尾 \\ 再回车: shell 还在等下一行, 下一行跟它拼起来查."""
    _, _, after, blind = mk.press("chmod -R 000 \\", "", ["enter"])
    assert blind == mk.PARTIAL and mk.advance(after, blind, "/")[0]     # #71: 续行后下一行要人确认
    hits, _, after, _ = mk.advance("", "", "cat /dev/zero \\\n")
    assert mk.advance(after, "", "> /dev/sda")[0]


def test_plain_enter_still_resets():
    assert mk.press("ls", "", ["enter"])[2] == ""


# ---- #71 审查: 绕过 (每条先写成失败用例再修). 安全底线: 宁可误拦不可漏放.
BYPASSES = [
    # 绝对路径 / .exe 命令名
    "/usr/bin/truncate -s0 /etc/passwd",
    "/sbin/blkdiscard /dev/sda",
    "takeown.exe /f c:\\ /r",
    "C:\\Windows\\System32\\takeown.exe /f c:\\ /r /d y",
    "/usr/sbin/mkswap /dev/sdb2",
    "/sbin/zpool destroy tank",
    "icacls.exe C:\\ /reset /t",
    # Windows 开关紧贴命令名 / PowerShell 别名 / cmd /c
    "RD/S/Q C:\\",
    "rmdir/s c:\\windows",
    "del/s c:\\*",
    "erase/q c:\\users",
    "rd C:\\x -Recurse -Force",
    "rd C:\\ -r",
    "del C:\\x -Recurse",
    'cmd /c"rd /s /q c:\\"',
    'cmd /k"del /s /q c:\\*"',
    "cmd /crd /s /q c:\\",
    # 路径变形
    "cat x > /dev//sda",
    "cat x > ////dev/sda",
    "cat x > /dev/./sda",
    "chmod -R 000 /.",
    "chmod -R 000 /etc/..",
    "echo x > //etc/fstab",
    "cat x > /dev/../dev/sda",
    "cat x > /tmp/../etc/passwd",
    "dd if=/dev/zero of=/dev//sda",
    # 设备表: /dev/ 下除 null/zero/tty*/pts/stdout/stderr/fd 都算
    "cat x > /dev/rbd0",
    "cat x > /dev/nbd0",
    "cat x > /dev/mem",
    "cat x > /dev/port",
    "cat x > /dev/zram0",
    "cat x > /dev/vg0/root",
    "dd if=/dev/zero of=/dev/rbd0",
    # cryptsetup / efibootmgr / sysrq / cp 覆盖
    "cryptsetup luksErase /dev/sdb",
    "cryptsetup luksKillSlot /dev/sdb 0",
    "efibootmgr -Bb0001",
    "efibootmgr --delete-bootorder",
    "efibootmgr -v -o 0001",
    "echo b | tee /proc/sysrq-trigger",
    "cp /dev/null /etc/passwd",
    "cp -t /dev/sda x",
    "cp --target-directory=/dev/sda x",
    # Windows 引导 / 加密 / 磁盘
    "bootsect /nt60 c: /mbr",
    "bootrec /fixmbr",
    "manage-bde -off c:",
    "Set-Disk -Number 0 -IsOffline $true",
    "Reset-PhysicalDisk -FriendlyName x",
    # 递归改权限: 系统目录开头的任何路径, . * .., chattr
    "chmod -R 777 /usr/lib",
    "chown -R nobody /etc/ssh",
    "chmod -R 000 /boot/efi",
    "chmod -R 000 /run",
    "chown -R x /mnt",
    "chmod -R 000 /efi",
    "chmod -R 000 .",
    "chmod -R 000 *",
    "chmod -R 000 ..",
    "chattr -R +i /",
    "chattr -R +i /etc/ssh",
    # 绕过空白
    "truncate${IFS}-s0${IFS}/etc/passwd",
]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_bypass_variants_hit(cmd):
    assert mk.danger_of(cmd), cmd


STILL_HARMLESS = [
    "efibootmgr -v",
    "efibootmgr -vv",
    "efibootmgr -v | less",
    "cat x > /dev/null",
    "echo x > /dev/stderr",
    "echo x > /dev/tty1",
    "echo x > /dev/pts/0",
    "echo x >&2 > /dev/fd/2",
    "cat /dev/zero | head -c 10 > /tmp/z",
    "chmod -R 755 ./build",
    "chown -R user /home/user/project",
]


@pytest.mark.parametrize("cmd", STILL_HARMLESS)
def test_still_harmless(cmd):
    assert mk.danger_of(cmd) == [], (cmd, mk.danger_of(cmd))


@pytest.mark.parametrize("cont", ["^", "`"])
def test_cmd_caret_and_ps_backtick_continuation(cont):
    """cmd 行尾 ^ / PowerShell 行尾反引号续行: 跟下一行拼起来查, 下一行要人确认."""
    hits, *_ = mk.advance("", "", f"rd {cont}\n/s /q c:\\")
    assert hits
    hits, blocked, *_ = mk.advance("", "", f"echo {cont}\nhi")
    assert blocked
    _, _, after, blind = mk.press(f"rd {cont}", "", ["enter"])
    assert after and blind
    hits, blocked, *_ = mk.advance(after, blind, "/s /q c:\\")
    assert hits and blocked
    _, blocked, *_ = mk.advance(after, blind, "hi")
    assert blocked


def test_continuation_after_blind_line_stays_blind():
    """看不清的行再续行: 下一行也看不清 (#71)."""
    for cont in ("\\", "^", "`"):
        assert mk.after_enter("x " + cont, mk.UNKNOWN) == ("", mk.UNKNOWN)
        assert mk.after_enter(mk.SECRET_MARK + " " + cont, mk.PARTIAL) == ("", mk.UNKNOWN)
        assert mk.after_enter("a " + cont + "\nx " + cont, mk.PARTIAL)[1] == mk.PARTIAL
    assert mk.after_enter("ls", mk.UNKNOWN) == ("", "")


def test_harmless_with_continuation_char_inside_text_is_not_held():
    """一段 text 里续行符后面什么都没打: 这一段本身不挂起, 下一段才要人确认."""
    hits, blocked, after, blind = mk.advance("", "", "echo ^\n")
    assert not hits and not blocked and blind == mk.PARTIAL


# ---- 维护者拍板 (#71): .. 只在高危类命令里算; truncate 只拦文件截断
DOTDOT_DANGEROUS = [
    "cat x > /tmp/../etc/passwd",
    "cat x > /dev/../dev/sda",
    "cp x ../../etc/passwd",
    "cp x ../../../dev/sda",
    "echo x > ../../boot/grub/grub.cfg",
    "chmod -R 000 ..",
    "chmod -R 000 ../x",
    "chown -R x /etc/..",
]
DOTDOT_HARMLESS = [
    "cp a ../b",
    "echo x > ../y",
    "mv x ../",
    "cat x > ../out.txt",
    "cp -r build ../release",
]
TRUNCATE_DANGEROUS = [
    "truncate -s0 /etc/passwd",
    "truncate --size 0 x",
    "truncate -r ref x",
    "truncate --reference=ref x",
    "truncate x",
    "sudo truncate x",
    "ls; truncate x",
    "xargs truncate -s 0",
]
TRUNCATE_HARMLESS = [
    "mysql -e 'truncate table t'",
    'psql -c "TRUNCATE TABLE t"',
    "echo truncate table t",
    "TRUNCATE TABLE t;",
]


@pytest.mark.parametrize("cmd", DOTDOT_DANGEROUS + TRUNCATE_DANGEROUS)
def test_decided_dangerous(cmd):
    assert mk.danger_of(cmd), cmd


@pytest.mark.parametrize("cmd", DOTDOT_HARMLESS + TRUNCATE_HARMLESS)
def test_decided_harmless(cmd):
    assert mk.danger_of(cmd) == [], (cmd, mk.danger_of(cmd))
