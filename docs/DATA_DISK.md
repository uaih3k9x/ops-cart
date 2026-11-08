# 数据盘和装机库（/srv/data + Samba）

推车上一块 SATA 数据盘挂到 `/srv/data`，放 Windows ISO、驱动总裁、常用工具、克隆镜像，经 Samba 给 LAN1 上的
PE / Clonezilla 用。去掉对 U 盘的依赖。#11 的"数据盘 + 装机库"和"Samba 配置进仓库"两项。

- 按 **UUID** 挂，XFS，`nofail`：没装数据盘、盘坏了，开机照常，PXE 照常，只是没有共享。
- 仓库里**不写容量**。现在先用维护者自己的盘，以后换单位的盘：**换盘 = 数据拷过去 + 改 UUID**（见下面"换盘"）。
- 脚本**不格式化、不分区**。这两步只写在这份文档里，人看清楚盘再自己敲。

代码：`srv_data_core.py`（挂载单元、ISO 单元名转义）、`samba_core.py`（smb.conf、防火墙规则）；
脚本：`scripts/setup-srv-data.sh`、`scripts/setup-samba.sh`；部署件：`deploy/srv-data/`。

## 目录布局

| 目录 | 放什么 | 权限 | 共享 |
|---|---|---|---|
| `/srv/data/iso/` | Windows ISO 文件本身：`win10.iso`、`win11.iso` | root 写 | 不共享 |
| `/srv/data/win/<名>/` | 每个 ISO 开机自动**只读**挂在这（`win10.iso` → `win/win10/`） | 只读 | `\\推车\win`（账号） |
| `/srv/data/drivers/` | 驱动总裁（PE 里直接跑，不用先拷） | **可写** | `\\推车\drivers`（账号） |
| `/srv/data/tools/` | 常用工具 | root 写，共享只读 | `\\推车\tools`（账号） |
| `/srv/data/org/` | **单位的数据**（含单位机器内容的东西都放这下面） | | |
| `/srv/data/org/clone/` | #18 克隆镜像 | 抓镜像账号可写 | `clone-rw` / `clone-ro`（见 [CLONE.md](CLONE.md)） |

推车主控和这块盘是维护者私人的。**交接 / 离开时把 `/srv/data/org/` 整块拷走或清掉**，其余是装机工具。

目录由 `srv-data-layout.service` 在数据盘**挂上之后**建（没挂上就不建，免得建到根分区上）。

### 容量估算

| 东西 | 大约 |
|---|---|
| Windows ISO | 每个 5–7 GB |
| PE（微PE） | 约 1 GB（现在在系统盘 `/opt/pxe/tftp/win/`，经 HTTP 发，不在数据盘） |
| 驱动总裁离线包 | 几十 GB |
| 克隆镜像 | 每个十几到几十 GB，**最占地方** |

装机库（两个 ISO + 驱动 + 工具）一般不到 100 GB，剩下的都是给克隆镜像的。看用量：`df -h /srv/data`、
`du -sh /srv/data/*`。

## 明天上车：步骤

> 标 ⚠ 的是危险步骤，**人自己看清楚再敲**，别照抄。推车上 Python 是 3.9，下面的脚本都兼容。

### 0. 把东西拷到车上

在 Mac 上（仓库目录）：

```
ssh <推车> 'mkdir -p /tmp/ops-cart-11'
scp -r scripts deploy srv_data_core.py samba_core.py <推车>:/tmp/ops-cart-11/
scp srv_data_core.py samba_core.py <推车>:/tmp/      # 再 sudo install 进 /opt/pxe, 以后 --rescan 用
ssh <推车> 'sudo install -m 644 /tmp/srv_data_core.py /tmp/samba_core.py /opt/pxe/'
```

### 1. ⚠ 认盘

```
lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINTS
```

找到数据盘（看**型号、序列号、容量**对得上），记下设备名，下面写成 `/dev/sdX`。
**系统盘是哪块要先认出来**（`/`、`/boot` 挂在哪块上），千万别选它。设备名每次开机可能变，只在这一次用。

### 2. ⚠⚠ 分区 + 格式化（会清掉整块盘）

数据盘是维护者自己的盘，**先确认盘上没有要留的东西**。已经是 XFS 且想保留里面的数据，跳过这一步。

```
sudo wipefs /dev/sdX                       # 只看: 列出盘上现有的分区表/文件系统签名, 不改
sudo parted /dev/sdX -- print              # 只看: 现有分区

# 下面三条会清盘. 再看一眼 /dev/sdX 是不是数据盘
sudo parted -s /dev/sdX -- mklabel gpt mkpart ops-data xfs 1MiB 100%
sudo partprobe /dev/sdX
sudo mkfs.xfs -L ops-data /dev/sdX1
```

### 3. 拿 UUID

```
sudo blkid /dev/sdX1                       # 抄 UUID="..." 里那串 (不是 PARTUUID)
```

### 4. 装数据盘单元

```
sudo /tmp/ops-cart-11/scripts/setup-srv-data.sh <UUID>
```

脚本做的事：核对这个 UUID 的盘是 XFS（不是就停）、建 `ops-smb` 组、装 `srv-data.mount` 和
`srv-data-layout.service`、给 `/srv/data` 加 SELinux 标签 `samba_share_t`、挂上、建目录、挂 ISO。
看最后 `df -h /srv/data` 那行，容量对得上就对了。

### 5. 放 ISO、驱动总裁、工具

```
scp Win10.iso <推车>:/tmp/ && ssh <推车> 'sudo mv /tmp/Win10.iso /srv/data/iso/win10.iso'
# win11 同样. 文件名只许字母数字和 . _ - (PE 里要敲路径), 不合格的会跳过并提示改名
sudo /tmp/ops-cart-11/scripts/setup-srv-data.sh --rescan     # 加/删 ISO 后都跑一次
ls /srv/data/win/win10/                                     # 看到 setup.exe、sources/ 就对了
```

驱动总裁、工具：直接拷进 `/srv/data/drivers/`、`/srv/data/tools/`（`/tmp` 中转再 `sudo mv`，
驱动目录拷完 `sudo chgrp -R ops-smb /srv/data/drivers && sudo chmod -R g+w /srv/data/drivers`，PE 里才写得了）。

### 6. Samba

```
rpm -q samba || sudo dnf install samba        # 车上不出网就先连 LAN2 / 校园网
sudo /tmp/ops-cart-11/scripts/setup-samba.sh
```

- 会问**装机账号 `winpe` 的密码**（PE 里 `net use` 要敲，挑个好敲的）。不想每次重跑都问，就写进
  `/etc/ops-cart/smb-winpe.pass`（一行，`sudo chmod 600`），密码不进仓库。
- 有 `/opt/pxe/clone.json`（`proto: smb`）就顺带生成克隆的两个共享、建克隆账号，密码照 `clone.json`。
- firewalld：在 `eno1` 所在的区加一条规则，只放 LAN1 网段来的 SMB 端口（TCP，号码见 `samba_core.SMB_PORT`）。

### 7. 验证

在推车上：

```
systemctl is-active srv-data.mount srv-data-layout smb pxe-server     # 都 active
testparm -s 2>/dev/null | grep -E 'hosts allow|interfaces'            # 只有 lo eno1 和装机网段
smbclient -L //127.0.0.1 -U winpe                                     # 列出 win drivers tools (克隆的两个)
sudo firewall-cmd --get-zone-of-interface=eno1
sudo firewall-cmd --zone=<上面的区> --list-rich-rules                  # source address="<装机网段>" port <SMB 端口>
```

在被测机 PE 里（命令提示符）：

```
net use W: \\<推车装机地址>\win /user:winpe <密码>
W:\win10\setup.exe
net use D: \\<推车装机地址>\drivers /user:winpe <密码>     # 驱动总裁直接从 D: 跑
```

从 LAN2 / 无线连推车的 SMB 端口应该连不上（规则只放 LAN1 网段）。

### 8. （可选）没数据盘也照常

`sudo systemctl stop smb srv-data-layout` 后 `sudo umount -R /srv/data`，PXE 救援照常能起；
`sudo systemctl start srv-data.mount smb` 恢复。真拔盘重启也一样：`srv-data.mount` 失败，开机不等它，`smb` 不起。

## 换盘（比如换成单位的盘）

1. 新盘照上面 1–3 步认盘、格式化（⚠）、拿新 UUID，先手动挂到别处：
   `sudo mkdir -p /mnt/new && sudo mount /dev/sdY1 /mnt/new`
2. 停共享，拷数据：
   `sudo systemctl stop smb` → `sudo rsync -aHAX --info=progress2 /srv/data/ /mnt/new/`
   （ISO 挂载点是只读挂上去的，rsync 会顺带把挂着的内容拷一份；先 `sudo systemctl stop 'srv-data-win-*.mount'` 再拷）
3. `sudo umount /mnt/new`，重跑 `sudo .../setup-srv-data.sh <新 UUID>`，再 `--rescan`、`sudo systemctl start smb`。
4. 旧盘拿走前确认 `/srv/data/org/` 要不要清（单位的数据）。

## 留给人定的点（不回就按默认做法）

| 点 | 默认做法 |
|---|---|
| #18 克隆的只读共享 `clone-ro` 用访客（`clone.example.json` 的 `restore_user` 空着），#11 要求账号登录 | **克隆和装机分开**：装机共享（win/drivers/tools）一律账号；`clone-ro` 照 `clone.json` 走——没写 `restore_user` 就收访客（Clonezilla 是 Linux，不受 Win11 拒来宾影响，且只读），写了就按账号。想全推车不收访客：`clone.json` 加 `restore_user` / `restore_password`（如 `cloneread`），重跑 `setup-samba.sh` |
| 克隆镜像目录从 `/srv/data/clone` 挪到 `/srv/data/org/clone`（单位数据单独一个顶层） | 样例 `clone.example.json` 已改；车上已有 `clone.json` 的话 `local_dir` 也改成 `/srv/data/org/clone` |
| `eno1` 不在任何 firewalld 区，或和 LAN2（`enp3s0`）在同一个区 | 脚本不动区的归属（动了可能把现在通的 DHCP/TFTP/HTTP 关掉）。不在区里就不加规则并提示；同区就照加（规则带源网段限制）并提示 |
| 装机账号名 | `winpe`（写死在 `samba_core.INSTALL_USER`） |
| 驱动总裁共享可写，PE 里任何拿到 `winpe` 密码的人都能改驱动目录 | 接受（只在 LAN1 网段，账号登录）；不想要就把 `[drivers]` 改只读，驱动总裁先拷到 PE 的内存盘再跑 |
| 代理 DHCP 模式下（LAN1 接交换机），被测机拿的是现场 DHCP 的地址 | `hosts allow` 和防火墙按 `pxe.env` 的装机网段放；现场网段和装机网段不一样时连不上（HTTP/TFTP 也一样），要人改 `pxe.env` 后重跑 `setup-samba.sh` |

## 坑

- **Win11 内核的 PE 默认拒绝来宾访问**，所以装机共享一律账号登录。`net use` 要带 `/user:winpe`。
- smbd 只听 `lo` 和 `eno1`（`bind interfaces only`）。LAN1 没插线就没地址，smbd 起来找不到接口会崩出 core（#95），
  所以 `smb.service.d/ops-cart-lan1.conf` 在 LAN1 没地址时直接跳过 smbd（单元是 inactive，不是 failed）；插线来地址时
  NetworkManager 的 `50-ops-cart-smb` 把 smbd（重）启起来。还不行就 `sudo systemctl restart smb`。
  LAN1 没插线时 `smbclient -L //127.0.0.1` 也连不上，是正常的。
- ISO 文件名只许 ASCII 字母数字和 `. _ -`；单元名按 `systemd-escape --path` 转义（`win11-24h2` →
  `srv-data-win-win11\x2d24h2.mount`），`systemctl status` 时要加引号。
- ISO 按 UDF 挂（Windows 的 ISO 都是 UDF）。挂载带 `context=...samba_share_t`，SELinux enforcing 下 smbd 才读得到。
- 数据盘没挂上时 smbd 根本不起（`smb.service.d/ops-cart.conf`），免得共享出根分区上的空目录、抓镜像写满系统盘。
- 改了 `pxe.env` 的装机网段或 `clone.json`，重跑 `setup-samba.sh`（会问密码，回车 = 不改）。
