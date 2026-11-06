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
    assert blind == "" and mk.advance(after, blind, "/")[0]
    hits, _, after, _ = mk.advance("", "", "cat /dev/zero \\\n")
    assert mk.advance(after, "", "> /dev/sda")[0]


def test_plain_enter_still_resets():
    assert mk.press("ls", "", ["enter"])[2] == ""
