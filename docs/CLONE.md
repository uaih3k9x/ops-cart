# 整盘克隆（Clonezilla）

推车经 PXE 给被测机起 Clonezilla Live，**抓镜像**（被测机整盘存到推车）或**恢复镜像**（推车上的镜像整盘写到
一批被测机）。#18 第一版：只做单播，组播留了接口。代码在 `clone_core.py`（参数生成、盘匹配、批量清单），
菜单模板仍只在 `pxeserver.py` 一处。

> 恢复会**覆盖整块盘**。这里的守卫都是防"误写"的：未解锁的机器拿不到克隆菜单，目标盘按序列号唯一匹配，
> 车机上人看过整批清单才放行。和装机模式一样，MAC 是客户端自己报的，真正的边界是物理的（LAN1 接的是谁）。

## 链路

```
固件 PXE ─► shimx64.efi ─► GRUB (只给解锁的 MAC: grub.cfg-01-<mac>)
        ─► linux (http,<推车>)/clonezilla/vmlinuz + initrd.img
        ─► live-boot 按 fetch= 经 HTTP 取 clonezilla/filesystem.squashfs
        ─► 挂 ocs_repository (推车 Samba / NFS) ─► ocs_live_run 里的 ocs-sr 做 savedisk / restoredisk ─► 关机
```

## 要放的文件

从 Clonezilla Live 的 zip（amd64 版）`live/` 下取三个文件，放到 tftp 根目录（`/opt/pxe/tftp`）下的
`clonezilla/`，由 80 口 HTTP 提供。清单是 `pxeserver.CLONE_FILES`：

```
unzip -j clonezilla-live-*-amd64.zip live/vmlinuz live/initrd.img live/filesystem.squashfs -d /opt/pxe/tftp/clonezilla/
```

| 文件 | 说明 |
|---|---|
| `clonezilla/vmlinuz` | 内核，GRUB 经 HTTP 取 |
| `clonezilla/initrd.img` | |
| `clonezilla/filesystem.squashfs` | 几百 MB，live-boot 经 HTTP 取（不走 TFTP） |

- 文件不进仓库（体积/许可）。空文件按"缺"算。缺任何一个，克隆用途**拒绝解锁**，对话框写明缺哪些。
- 版本要支持 `SERIALNO=` 指定目标盘（ocs-sr 手册写了这种形式，据发布记录约 2021 年起支持，以实机验证第 1 步为准）。
- 被测机要关 Secure Boot（链路上是 Rocky 的 shim/GRUB，Clonezilla 内核的签名它不认，会被拒）。只支持 UEFI。
- 代码部署时多一个 `clone_core.py`，和 `kiosk_core.py` 放在一起（`/opt/pxe/`）。

## 镜像仓库

镜像放推车数据盘（#11 的 `/srv/data`），比如 `/srv/data/clone/<镜像名>/`。**抓镜像只写推车，恢复只读挂**：
导出两个共享，指向同一个目录。

`/opt/pxe/clone.json`（不进仓库，样例 `examples/clone.example.json`）：

| 键 | 说明 |
|---|---|
| `proto` | `smb` / `nfs` / `nfs4` |
| `server` | 不写就用装机口地址（`pxe.env` 的 `PXE_IP`） |
| `save_share` | 抓镜像用的**可写**共享：SMB 共享名，或 NFS 导出的绝对路径 |
| `restore_share` | 恢复用的**只读**共享 |
| `user` / `password` | SMB 可写账号（只给抓镜像） |
| `restore_user` / `restore_password` | SMB 只读账号；不写就当访客挂，**不会**回退到可写账号 |
| `smb_version` | `smb3` 之类，不写用 `smb` |
| `local_dir` | 推车上镜像目录的绝对路径，用来核对：抓镜像时镜像已存在就拒绝（不覆盖），恢复时不存在那台不放行 |

Samba 例（推车上，按现场改）：

```
[clone-rw]
   path = /srv/data/clone
   read only = no
   valid users = clonesave
[clone-ro]
   path = /srv/data/clone
   read only = yes
   guest ok = yes
```

NFS 例：`/srv/data/clone <装机网段>(rw,no_root_squash)` 给抓镜像，另一个路径（bind mount）导出成 `ro` 给恢复。

**账号密码会写进 GRUB 菜单（内核命令行）**：菜单只发给解锁的 MAC、上锁就删，但 LAN1 上能冒充那个 MAC 的
机器拿得到。可写账号只给这一个共享的权限，别和别处共用。用户名密码只许字母数字和 `. _ -`（要进 URL 和命令行）。

## 这一批的清单

`/opt/pxe/clone-batch.json`（不进仓库，样例 `examples/clone-batch.example.json`）：

```json
{"image": "镜像名",
 "rule": {"model": "型号", "size_min": 字节, "size_max": 字节},
 "machines": [{"mac": "...", "image": "可选, 覆盖上面的", "disks": <lsblk 输出>}]}
```

每台的磁盘在被测机上（救援 shell 就行）跑这条，把输出整个贴进 `disks`：

```
lsblk -dbJ -o NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,RM
```

### 盘匹配规则（`clone_core.match_disk`）

- 规则键只认 `model`、`serial`、`size_min`、`size_max`（字节，闭区间）、`tran`。拼错一个键整条作废（免得规则悄悄变宽）。
- `model` / `serial` 至少写一个；只按容量/接口不行。`model` 忽略大小写和多余空格后**整串相等**，不做子串。
- 只看整盘（`type` 是 `disk`）；可移动盘和 USB 盘不算候选，除非规则写 `"tran": "usb"`。
- 恰好一块盘符合，且它有序列号、序列号只含安全字符、和同机别的盘不重复，才给 `SERIALNO=<序列号>`（空格换 `_`，
  这是 Clonezilla 的规矩）。零块、多块、没序列号都**不放行这台**，对话框里写原因。**绝不默认 sda / 第一块盘**；
  设备名每次开机顺序会变，也不用。

## 车机上怎么用

1. 放好 Clonezilla 文件、`clone.json`、`clone-batch.json`。
2. "解锁装机模式"对话框选 **抓镜像** 或 **恢复镜像**：
   - 抓镜像：在上面选（或手填）源机器的 MAC，它要在清单里；镜像名用清单的 `image`，仓库里已有就拒绝。
   - 恢复镜像：不看上面选的 MAC，按清单来。
3. 对话框下方列出这一批：放行的写 `MAC 镜像 -> SERIALNO=...`，不放行的写原因；整批不行写原因。
   **人看清楚再照抄验证码解锁**。解锁时推车重算一遍，和显示的不一样（清单/配置被改过）就拒绝。
4. 只给放行的 MAC 写 `grub.cfg-01-<mac>`：第 0 项是 Clonezilla，后面是救援，倒计时 15 秒，没有自动装机项。抓镜像默认第 0 项；**恢复默认救援**，要人在被测机屏幕前手选恢复项才写盘。
5. 上锁：30 分钟到期；一台（抓镜像）取走菜单就上锁；一批（恢复）全部取走才上锁，先取走的那几台先删掉它们的菜单。
   上锁时整棵 tftp 树里带克隆项的菜单都删掉（和 WinPE 一样）；`clonezilla/` 不删。
6. ocs-sr 做完**关机**（不重启，免得 PXE 在前时再来一遍）。

## 启动参数（`clone_core`）

每个参数为什么加、为什么不加，`clone_core.py` 里逐条写了手册原话。要点：

- `ocs_live_run` 直接跑 `ocs-sr`；`ocs_live_extra_param=""`（只对 ocs-live-restore 有用）；`ocs_live_batch="yes"`；
  `locales` / `keyboard-layouts` 预设，开机不问。
- 只有 `-batch` 跳过确认（启动参数里必须写 `-batch` 不能写 `-b`）。不加 `-scr`（恢复前检查镜像）、`-icds`
  （检查目标盘容量）、`-k` / `-k1`（分区表照镜像，显式 `-k0`）、`-c`（确认在推车上做）。
- 抓镜像：`-q2 -j2 -z9p -sfsck -senc -p poweroff -batch savedisk <镜像> SERIALNO=...`（`-sfsck` 不让 fsck 改源盘）。
- 恢复：`-g auto -e1 auto -e2 -r -j2 -k0 -p poweroff -batch restoredisk <镜像> SERIALNO=...`。
- GRUB 菜单里双引号写成 `\"`（Clonezilla 自己的 grub.cfg 也这样）；参数里有 `$ \ ' ; { } #` 之类的直接拒绝。

出处：
[启动参数](https://clonezilla.org/fine-print-live-doc.php?path=./clonezilla-live/doc/99_Misc/00_live-boot-parameters.doc)、
[ocs-sr 手册](https://clonezilla.org/fine-print-live-doc.php?path=clonezilla-live/doc/98_ocs_related_command_manpages/01-ocs-sr.doc)、
[保留字 / SERIALNO](https://clonezilla.org/advanced/reserved-word-ocs-sr.php)、
[PXE 启动](https://clonezilla.org/livepxe.php)。

## 组播（没做）

第一版只生成单播。`clone_core.ocs_sr_cmd(..., multicast=...)` 留了位置，传了就报 `NotImplementedError`，
不会悄悄退成单播。要做时：客户端改 `--mcast-port <port> multicast_restoredisk`，推车另起 udpcast 发送端；
等几台（`clients_to_wait`，= 这一批台数）、等不齐最多等多久（`max_wait_s`）、限速（`max_bitrate_mbps`）
三个参数在发送端；组播不通退 BitTorrent。

## 实机验证（另记结果）

1. **单播恢复一台**：先抓一台的镜像（抓镜像用途），确认 `/srv/data/clone/<镜像>/` 生成且被测机关机；
   再用另一台同型号机恢复。看：菜单里的 `SERIALNO=` 对、Clonezilla 认得 `SERIALNO=`（不认的老版本会报找不到盘并停）、
   只读共享真挂成只读、做完关机、上锁后再 PXE 只有救援菜单。故意把规则写成匹配两块盘，确认对话框不放行那台。
2. **组播两台**（组播做完后）：两台同时 PXE，看等齐才开始、超时行为、限速。
3. **交换机 IGMP snooping**：推车经交换机（代理模式）时，分别在 IGMP snooping 开/关、有无 querier 的情况下跑组播，
   记下哪种组播流被交换机丢掉、哪种泛洪；不通时确认退 BitTorrent 能走。

## 坑

- 被测机要关 Secure Boot；只支持 UEFI。
- 清单里的盘信息是登记时的快照：登记后换了盘，序列号对不上就不放行（这是想要的）。
- 恢复不会无人值守写盘：倒计时 15 秒落到救援，每台都要有人在屏幕前选恢复项。批量恢复时一台台过去选；以后要无人值守再加开关。
