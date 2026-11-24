# ARM64 UEFI 网络引导和诊断收集

给 ARM64 UEFI 板子 (首个目标: 飞腾 D2000, 固件基于 EDK2) 用推车 PXE 起一个**整个跑在内存里**的诊断 Linux,
自动只读收集 ACPI / SMBIOS / PCI / USB / 网卡 / 盘 / EFI 变量等信息, 打成一个包传回推车,
用来摸底 "Windows on ARM 兼容性" 之类的问题. **不装系统; 收集阶段不写板子硬盘.**
唯一会碰本地盘的是 Alpine initramfs 启动时只读挂载本地文件系统找 `*.apkovl.tar.gz` (nlplug-findfs, `MS_RDONLY`):
不写文件, 但只读挂载 ext4/XFS 时内核仍可能回放日志 (见第 8 节第 5 步). 诊断系统关机时也不把时间写回板子 RTC.

> 状态: 代码和测试在仓库里; 引导文件在开发机的暂存目录里备好; **还没上车、没在实机上跑过.**
> 下面 "首次实机验证" 要人在板子前面做.

## 1. 原理

### 1.1 DHCP: 按架构精确分发

`pxeserver.arch_bootfile` 按 option 93 (RFC 4578) 的第一个架构号:

| option 93 | 架构 | 引导文件 |
|---|---|---|
| `0000` | BIOS x86 | `pxelinux.0` |
| `0007` / `0009` | UEFI x86-64 | `shimx64.efi` |
| `000b` | UEFI ARM64 | `aa64/grubaa64.efi` (`PXE_ARM64_FILE` 可改) |
| 没带 option 93 | (老行为) | `shimx64.efi` |
| 其它 (`0006` IA32、`000a` ARM32、`0010`/`0013` UEFI HTTP、RISC-V...) | 不支持 | **一个包都不回** |

修掉的老问题 (实机日志): ARM64 板子的 DISCOVER (`vendor=PXEClient:Arch:00011:UNDI:003000`, `arch=000b`)
在 67 口 OFFER 和 4011 口 proxyDHCP ACK 都拿到了 x86 的 `shimx64.efi`. 现在:

- 67 口 (自己发地址 / 限时到了只续租 / 代理模式) 和 4011 口都先过架构: 不认识的架构不发地址、不给引导文件, 日志记 "架构不支持, 不回".
- 代理模式的 `PROXY_ARCHES` = x86-64 UEFI + ARM64 UEFI. 4011 口**不按** `PROXY_ARCHES` 卡, 按上表映射卡:
  推车自己发地址时 BIOS 的 PXE ROM 也会去 4011 要文件, 按 `PROXY_ARCHES` 卡会把 BIOS 引导弄坏.
- iPXE (option 77 = iPXE) 拿 `boot.ipxe` 只限 x86-64 UEFI (或没报架构): `boot.ipxe` 是 x86 的 wimboot.

**`0x0013` (ARM64 UEFI HTTP boot) 不支持**: HTTP boot 客户端要 `option 60 = HTTPClient` 和一个 URL 形式的 bootfile,
现有 x86 也没做 HTTP boot (`0x0010`); 飞腾板子固件菜单里的网络启动走的是 PXE (实机报的就是 `000b`).
真要用 HTTP boot 时再单独做, 免得为一个没人用的路径改代理逻辑.

### 1.2 引导程序: Rocky 的 grubaa64.efi, 不过 shim

- 用 Rocky 9 官方树里的 `EFI/BOOT/grubaa64.efi` (和车上 x86 用的 GRUB 同一家、同一套补丁), 放 `aa64/`.
- **不走 shimaa64**: 飞腾这类板子出厂一般不开安全启动, 也不一定预置微软第三方 CA, shim 带来的 MokManager / fallback
  在这里只是多一层出错点. 不开安全启动时 GRUB 直接起, 也不校验内核签名 (Alpine 内核没有微软链签名, 本来就需要关安全启动).
  板子要是开着安全启动: GRUB 会被固件拒绝 (没有 shim 就没有可信链), 这时到固件里关掉安全启动; 或者把 `PXE_ARM64_FILE`
  改成 `aa64/BOOTAA64.EFI` (Rocky 的 shimaa64) —— 但 Alpine 内核照样过不了 shim, 所以只建议关安全启动.
- Rocky 9.9 自己的 aarch64 内核和 Alpine 的 `vmlinuz-lts` 都是 EFI zboot 格式 (PE 头 + `zimg` + gzip),
  Rocky 的 GRUB 能起自家内核, 推测也能起 Alpine 的 —— **推测, 要实机确认**.

### 1.3 菜单: aa64/ 单独前缀目录, 推车生成

选 "aa64 单独目录", 不在同一个 grub.cfg 里按 `$grub_cpu` 分支:

- Rocky 的 GRUB 网络启动时先按自己被下载的目录 (`fw_path`) 找菜单: 从 `aa64/grubaa64.efi` 起来就取 `aa64/grub.cfg-01-<mac>` ...
  `aa64/grub.cfg` (RHEL 文档里 UEFI PXE 也是把 grub.cfg 放在引导文件同目录). x86 的菜单和按 MAC 的解锁菜单都在根下, ARM 的 GRUB 碰不到.
- 根下 `grub.cfg` 要过 #70 的门禁白名单 (`rescue_default_menu` 只认 `set default/timeout` 和 `menuentry`),
  `if [ $grub_cpu = arm64 ]` 这种分支过不了, 改白名单比分目录风险大.
- `aa64/grub.cfg` **不读盘**: TFTP 收到这个名字就发 `pxeserver.grub_menu_arm64` 生成的 (同 `pxelinux.cfg/default`);
  `aa64/` 下其它 `grub.cfg*` (按 MAC / IP、子目录、备份) TFTP 一律找不到, HTTP 一律 404. ARM 板子没有任何装机 / 解锁用途.
- 万一 GRUB 没去 `aa64/` 找、而是取了根下 `grub.cfg`: 那份默认项也是本地盘, 只是救援项是 x86 内核起不来, 不危险.

生成的菜单 (地址为示例):

```
set default=0
set timeout=10

menuentry 'Boot from local disk' {
    exit
}

menuentry 'Diagnostic Linux in RAM (Alpine aarch64, console login root) (HTTP fast)' {
    linux (http,192.0.2.1)/aa64/alpine/vmlinuz-lts ip=dhcp alpine_repo=http://192.0.2.1/aa64/alpine/repo/main,http://192.0.2.1/aa64/alpine/repo/community modloop=http://192.0.2.1/aa64/alpine/modloop-lts pkgs=python3,acpica,iasl,dmidecode,pciutils,usbutils,lsblk,nvme-cli,smartmontools,efibootmgr,lshw,ethtool,iproute2 apkovl=http://192.0.2.1/aa64/alpine/ops-diag.apkovl.tar.gz ops_diag=http://192.0.2.1:8081/diag/ earlycon console=tty0 console=ttyAMA0,115200
    initrd (http,192.0.2.1)/aa64/alpine/initramfs-lts
}

menuentry '... (TFTP slow)' { 同上, linux/initrd 走 TFTP; modloop、包、apkovl 仍走 HTTP }

menuentry 'MemTest86 (PassMark Site)' {
    chainloader /mt86site/BOOTAA64.efi
}

menuentry 'Windows PE ARM64 23H2 (smoke test)' {
    chainloader /aa64/winpe/22621/bootaa64.efi
}

menuentry 'Windows PE ARM64 24H2 (expect fail: no LSE)' {
    chainloader /aa64/winpe/26100/bootaa64.efi
}
```

- 两个 WinPE 项是 Windows ARM64 内核的启动冒烟测试, 只能手选 (`_manual_only` 认 `aa64/winpe`), 见 [ARM64_WINPE.md](ARM64_WINPE.md).

- **默认 = 第 0 项本地盘** (`exit` 退回固件按 BootOrder 往下走), 10 秒超时. 诊断项只能手选:
  `_manual_only` 认 `aa64/alpine`, 默认项要是诊断项, TFTP 门禁换成默认菜单 (测试锁着).
- 不带 `ssh_key=`: 不开 sshd, 只有屏幕前 / 串口上的人能以 root 登录 (无密码, 跟 Alpine 安装盘一样).

### 1.4 串口控制台

写的是 `earlycon console=tty0 console=ttyAMA0,115200`:

- D2000 的 UART 是 ARM PL011 兼容 (Linux 下叫 `ttyAMA*`); 内核配置里 `SERIAL_AMBA_PL011_CONSOLE=y`、`ACPI_SPCR_TABLE=y` 都开着.
- 最后一个 `console=` 是 `/dev/console` (开机服务输出走串口), `tty0` 也有内核日志和登录 (推车采集卡看得到).
  Alpine initramfs 给每个 `console=` 设备都加 getty.
- `earlycon` 不带参数 = ACPI 模式按 SPCR 表、DT 模式按 `stdout-path` 自动找, 都没有就不起作用, 无害.
- **不确定**: 这块板 BIOS 里的 "COM0" 在 Linux 下是 `ttyAMA0` 还是 `ttyAMA1` (飞腾有的板子调试口是 UART1), 也可能被枚举成 `ttyS*`
  (如果走的是 8250/DesignWare 兼容). 实机时看 dmesg 里 `ttyAMA`/`ttyS` 和 SPCR 那几行; 不对就在 GRUB 菜单上按 `e` 改.
  固件自己的串口重定向 (COM0 115200 VT-UTF8) 只管到 GRUB 为止.

## 2. 诊断系统: Alpine aarch64 netboot (已定)

| | Alpine 3.24 netboot (lts) | Debian 13 arm64 netboot (d-i) | Rocky 9 aarch64 `inst.rescue` |
|---|---|---|---|
| 内核 | 6.18.60 (LTS) | 6.12 | 5.14 (RHEL 回移植) |
| 体积 | 内核 13 MB + initramfs 28 MB + modloop 264 MB + 包约 34 MB (83 个) | ~50 MB | stage2 `install.img` ~1 GB |
| 内存里跑 | 是 (tmpfs 根) | 是 (安装器) | 是 (stage2 下进内存) |
| 工具 | 本地子集仓库按需装 | 安装器 busybox, 没 lspci 之类 | 齐 (lspci/dmidecode...) |
| 离线 | 子集仓库 + 官方签名索引 | 难 | 要整个 stage2 |

推荐 Alpine: 内核最新、整体小、真 tmpfs 根、包管理能离线装工具、启动参数能直接带包名和 apkovl.
备选 Rocky 9 aarch64 rescue (和车上 x86 救援同一套路, 工具齐, 但内核 5.14 老、stage2 大).

**对 Phytium 支持的了解程度 (不确定的直说)**: 主线内核里没有 D2000 专门的 SoC 支持 (没有 `ARCH_PHYTIUM`);
D2000 走 ACPI 时 PCIe 是标准 ECAM (`PCI_HOST_GENERIC=y`), 一般通用发行版内核能起, 但这是印象, **没在这块板上验证过**.
板载网卡如果是飞腾自家 GMAC/MAC, 驱动多半只在厂商 (麒麟 / UOS 的 4.19、5.10) 内核里, 主线 Alpine 内核**可能认不到**;
插 PCIe 网卡 (Intel `igb`/`e1000e`、Realtek `r8169` 都是模块) 更稳. 诊断包里的 `lspci -nnvvv`、`dmesg`、`ip -d addr` 正好用来判断这一点.

**另: 内存测试**. 原先以为 Memtest86+ 不支持 ARM, 但 memtest.org 上 Memtest86+ v9.00 (2026-10-15 发布) 写着已支持 "ARM64 platforms supporting UEFI and ACPI"
(没下载、没试; 以后可以照 `memtest_core.py` 的路子固定版本 + 校验). 现在先用自备的 PassMark MemTest86 Site Edition, 见第 9 节.

## 3. 收集什么、怎么传回来

开机后 apkovl 里的 `/etc/local.d/ops-diag.start` 在后台调 `ops-diag-collect --auto` (= 仓库的 `arm64_diag_collect.py`):

- 只写 `/tmp` (先检查所在文件系统是 tmpfs/ramfs/rootfs, 不是就退出码 3, 一个文件都不建); 不挂载、不分区;
  命令都在只读白名单里 (`tests/test_arm64_diag_collect.py` 锁着, 以后加 `smartctl -t`、`efibootmgr -o/-b` 这类写操作测试会红).
- apkovl 里带 `/etc/conf.d/hwclock` (`clock_systohc="NO"`): 关机不写 RTC.
- 收: `uname -a`、`/proc/{cpuinfo,meminfo,cmdline,iomem,interrupts,version}` (cpuinfo 的 Features 看 atomics 等)、`ls -la /sys/firmware` (ACPI 还是 DT)、
  `dmidecode`、`lspci -nnvvv` / `-tvnn`、`lsusb -v` / `-t`、`lsblk -a -O`、`nvme list` / `id-ctrl` / `smart-log`、`smartctl -a`、`dmesg`、`lsmod`、
  `efibootmgr -v`、`ip -d addr` / `ip route`、每块网卡 `ethtool` / `ethtool -i`、`lshw`;
  ACPI: `acpidump` (文本) + `acpidump -b` (每张表 `<sig>.dat`) + 每张表 `iasl -d` 反编译成 `.dsl` (MCFG/MADT/GTDT/SPCR/DBG2/IORT/PPTT/FADT 都在里面, 有什么收什么);
  拷 `/sys/firmware/efi` 下所有文件 (含 efivars, 单个 >1 MB 跳过) 和 `/sys/class/dmi/id`. 每条的退出码记在 `MANIFEST.txt`, 缺工具记 `not found`.
- 包名: `ops-diag-<MAC>[-sn-<SMBIOS 序列号>]-<板子 UTC 时间>.tar.gz`. MAC = 起网那块网卡 (12 位十六进制);
  序列号取 `product_serial` 再 `board_serial`, "To be filled by O.E.M." 之类占位值当没有. 板子 RTC 可能不准, 时间只作区分.
- 上传: `PUT http://<PXE_IP>:8081/diag/<包名>` (地址来自内核参数 `ops_diag=`), 失败重试 5 次; 还失败就把包留在 `/tmp`,
  屏幕上提示 `ops-diag-collect --resend <包> --url ...`.
- 手动: 登录后 `ops-diag-collect` (再收一次并上传)、`ops-diag-collect --no-upload`、看 `/tmp/ops-diag.log`.

推车接收端 `diag_upload.py` (单元 `pxe-diag-upload.service`, **默认不开**):

- 只绑 `PXE_IP:8081` (`--bind` 不接受空、`0.0.0.0`、`::`), 只收 `PUT /diag/<名字>.tar.gz`: 名字白名单, 必须有非负的 `Content-Length`, ≤ 64 MB,
  内容得是 gzip, 推车剩余空间 (扣掉正在传的) < 1 GB 不收; 同名不覆盖 (加 `-1`...); 先写隐藏临时文件再改名.
  同时最多 2 个上传 (多了 503), 套接字 60 秒超时. GET/POST/DELETE 一律 405, 不列目录.
- 单元加固: DynamicUser、无能力 (`CapabilityBoundingSet=` 空)、`SystemCallFilter=@system-service`、`MemoryMax=256M`、`TasksMax=16`、`ProtectSystem=strict` 等.
- 存在 `/var/lib/ops-cart-arm64-diag/` (DynamicUser, 实际目录在 `/var/lib/private/` 下). 取包: `sudo ls /var/lib/ops-cart-arm64-diag/`.
- 选 HTTP PUT 而不是 tftp put / nc: 现有 TFTP 服务只读 (加 WRQ 等于给引导服务开写口), nc 要人每次手开监听;
  单独一个只能收文件的小服务、单独端口, 跟出引导文件的 80 口分开, 停掉它不影响引导.

## 4. 文件清单 (推车 tftp 根 = /opt/pxe/tftp)

```
aa64/grubaa64.efi                      Rocky 9.9 BaseOS aarch64 EFI/BOOT/grubaa64.efi (.treeinfo sha256)
aa64/alpine/vmlinuz-lts                Alpine 3.24.4 netboot tarball 里的 boot/ (tarball sha256 + GPG)
aa64/alpine/initramfs-lts
aa64/alpine/modloop-lts
aa64/alpine/repo/main/aarch64/APKINDEX.tar.gz + *.apk          官方索引 (签名在里面) + 依赖闭包
aa64/alpine/repo/community/aarch64/APKINDEX.tar.gz + *.apk
aa64/alpine/ops-diag.apkovl.tar.gz     arm64_diag_core.build_apkovl 生成 (收集脚本 + 开机自启)
(aa64/grub.cfg 不放文件: TFTP 生成)
```

仓库里: `pxeserver.py` (分发 / 菜单 / 门禁)、`httpserve.py` (aa64 菜单 404)、`arm64_diag_collect.py` (板子上跑)、
`arm64_diag_core.py` (备料)、`diag_upload.py` + `deploy/systemd/pxe-diag-upload.service` (收包)、`deploy/firewalld/pxe.xml` (开 8081/tcp).

## 5. 备料 (开发机, 要外网)

```sh
python3 arm64_diag_core.py fetch --staging ~/code/pxeserver-arm64-staging
```

- 下 Rocky `.treeinfo` 和 `grubaa64.efi` 并核对 sha256; 下 Alpine netboot tarball 核对官方 `.sha256` (另可用 `.asc` + ncopa 的公钥手工验 GPG), 取 lts 三件;
  下两个官方 `APKINDEX.tar.gz`, 按 `ARM64_DIAG_PKGS` + `alpine-base` + `openssl` 解依赖闭包, 下这些 `.apk`; 生成 apkovl;
  写 `<staging>/SOURCES.tsv` (路径 大小 sha256 来源 校验方式). 落盘布局 = tftp 根的相对路径, 部署直接拷 `tftp/aa64`.
- apk 包本身不在这里逐个校验: 板子上的 apk 用 initramfs 自带的 Alpine 公钥验 APKINDEX 签名, 再按索引里的校验和验每个包.
- 开发机上验过一次 (docker `alpine:3.24` arm64, 断网): 用这个子集仓库 `apk add --root ... --initdb --initramfs-diskless-boot alpine-base openssl <ARM64_DIAG_PKGS>`
  装齐 83 个包无报错; 收集脚本在 Python 3.14 下跑通、打包成功, 输出目录放在 overlay 上时拒绝运行 (退出码 3); apkovl 用 busybox tar 解开正常.
  (第一次验出来的问题: apk 按 provider_priority 选了 ifupdown-ng, 子集里没有 —— 解析器已改成同 apk 的规则.)
- 改了 `ARM64_DIAG_PKGS` 或 Alpine 版本要重新 fetch; 只改了收集脚本: `python3 arm64_diag_core.py apkovl --out <...>/ops-diag.apkovl.tar.gz`.

## 6. 上车部署 (要 sudo, 维护者在车上做)

```sh
# 0. 先备份
sudo cp -a /etc/firewalld/zones/pxe.xml /root/pxe.xml.bak-arm64
# 1. 代码 (照常规发布流程; 涉及 pxeserver.py httpserve.py diag_upload.py)
# 2. 引导文件 (开发机 -> 推车, 约 310 MB)
rsync -a ~/code/pxeserver-arm64-staging/tftp/aa64/ <推车>:/tmp/aa64/
sudo install -d -o root -g root -m 0755 /opt/pxe/tftp/aa64
sudo cp -a /tmp/aa64/. /opt/pxe/tftp/aa64/ && sudo chown -R root:root /opt/pxe/tftp/aa64
sudo restorecon -R /opt/pxe/tftp/aa64          # SELinux enforcing
# 3. 收包服务 + 防火墙
sudo install -m 0644 deploy/systemd/pxe-diag-upload.service /etc/systemd/system/
sudo install -m 0644 deploy/firewalld/pxe.xml /etc/firewalld/zones/pxe.xml && sudo firewall-cmd --reload
sudo systemctl daemon-reload && sudo systemctl restart pxe-server && sudo systemctl enable --now pxe-diag-upload
# 4. 确认
curl -s -o /dev/null -w '%{http_code}\n' http://<PXE_IP>/aa64/alpine/vmlinuz-lts   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://<PXE_IP>/aa64/grub.cfg             # 404 (菜单只走 TFTP)
```

`PXE_ARM64_FILE` 一般不用写 (默认 `aa64/grubaa64.efi`).

## 7. 回滚

- 只想让推车不再引导 ARM 板子: `sudo rm -rf /opt/pxe/tftp/aa64` —— 板子取不到 `aa64/grubaa64.efi`, 固件转下一个启动项.
- 收包: `sudo systemctl disable --now pxe-diag-upload`, 防火墙恢复 `/root/pxe.xml.bak-arm64` 后 `firewall-cmd --reload`.
- 代码: 回到上一个版本的 `pxeserver.py` / `httpserve.py`. 注意老代码会再把 `shimx64.efi` 发给 ARM 板子 (无害但起不来).

## 8. 首次实机验证 (维护者在板子前)

0. 推车上 `journalctl -u pxe-server -f` 开着; 板子串口接推车串口服务器看 115200.
1. 板子 F12 网络启动 -> 日志应是 `DHCP DISCOVER ... bootfile=aa64/grubaa64.efi` (或代理模式的 4011 ACK), 不再是 `shimx64.efi`;
   接着 `TFTP RRQ aa64/grubaa64.efi` 和 `TFTP RRQ aa64/grub.cfg (推车生成, ARM64)`.
   如果看到的是取根下 `grub.cfg` / `EFI/...`: 说明 GRUB 没按 `aa64/` 找菜单 (1.3 的推测不成立), 先停, 回来改.
2. **先确认菜单默认项是 "Boot from local disk"**, 让它倒计时走一次: 板子应退回固件、按启动顺序起本地盘 (或下一项). 这一步过了才往下.
3. 再进一次, 手选 "Diagnostic Linux ... (HTTP fast)". 看:
   - GRUB 能不能起 Alpine 的 zboot 内核 (起不来: 记下报错, 试 TFTP 项; 都不行要换 GRUB 或换 Debian 的 grubnetaa64);
   - 串口有没有输出 (没有: `e` 改 `ttyAMA1` 或去掉 `console=ttyAMA0` 只留 SPCR 自动);
   - initramfs 能否拿到地址、下 modloop、装包 (卡在 `Installing packages`: 子集仓库缺包, 看错误里的包名);
   - 进到系统后 ~1-3 分钟屏幕 / 串口出现 `ops-diag: 已传到推车, 存为 ...`.
4. 推车上 `sudo ls -l /var/lib/ops-cart-arm64-diag/`, 解开看 `MANIFEST.txt` 有没有 `not found`/`timeout`, `acpi/*.dsl` 齐不齐.
5. Alpine initramfs 起来时会按只读方式挂一下本地盘的文件系统找 `*.apkovl.tar.gz` (nlplug-findfs, `MS_RDONLY`), 不写文件;
   但 ext4/XFS 日志要回放的话只读挂载也可能回放日志. 第一次验证用盘里没有要紧数据的板子.

## 9. PassMark MemTest86 Site Edition (要自备)

需自备 MemTest86 Site Edition (商业软件): **它的任何文件都不进本仓库** (`tests/test_mt86_site.py` 锁着: 仓库里不许有 `.efi`、`unifont.bin`、`mt86.cfg` 等),
仓库里只有菜单项和这份说明.

**菜单**: x86 默认菜单 (`grub.cfg`, Memtest86+ 前面一项) 和 ARM64 菜单 (`aa64/grub.cfg`, 最后一项) 都有手动项
`MemTest86 (PassMark Site)`, 分别 `chainloader /mt86site/BOOTX64.efi` / `/mt86site/BOOTAA64.efi` (走 TFTP). 不是默认项;
`_manual_only` 认 `mt86site`, 手写成默认项的菜单门禁会拦. 文件没放上车时选它只会在 GRUB 里报找不到文件.

**它怎么找支持文件 (unifont.bin、mt86.cfg、blacklist.cfg...): 不确定.** 查到的:

- PassMark 官方 PXE 页只说 PXE 服务器按架构给 `BOOTX64.efi` / `BOOTIA32.efi` / `BOOTAA64.efi` 当引导文件, "把包里的文件解到 PXE 服务器相应目录",
  `mt86.cfg` 在 PXE 下也支持, 报告可以传回 PXE/TFTP 服务器; 没写具体目录, 没提 GRUB chainloader. 已知问题一条: 经 iPXE 链式加载时
  报告传不回去 (iPXE 不支持 TFTP 上传), 但 `mt86.cfg` 仍能从 TFTP 取 —— 说明它取配置走的是固件 PXE 的 TFTP.
- PassMark 论坛的旧回复说 PXE 时 `mt86.cfg` 要放 TFTP 根目录 (年代早, 版本不明).
- 二进制里有 `TFTPSERVERIP` (报告存到别的 TFTP 服务器)、`TFTPROOT=` (含义没查到)、`TFTPSTATUSSECS`、`RTCSYNC` (和 PXE 服务器对时)、`PMPDISABLE`/`TCPDISABLE`,
  以及 "TCP protocol not supported on this device, defaulting to TFTP mode" / "Warning - TFTP server IP not found" —— 推测它从固件 PXE 的 DHCP 应答里拿 TFTP 服务器地址.

两种方案, 实机验证时定:

- **A (先试): 经 GRUB chainloader**, 文件原样放 `mt86site/`. GRUB 用的是同一块网卡的 PXE, 固件里的 DHCP 应答还在, 推测它能找到 TFTP 服务器;
  它要是按 TFTP 根找支持文件, 就把 `unifont.bin`、`mt86.cfg`、`blacklist.cfg` (和 `customtests.cfg`/`report.css`/`mt86*.htm`/`mt86.png`/`SPD.spd`) 再拷一份到 tftp 根
  (ARM 那边 DHCP 的引导文件在 `aa64/`, 要是按引导文件所在目录找, 就拷到 `aa64/`). 看推车 `journalctl -u pxe-server` 里它 RRQ 的是哪个路径, 照着放.
- **B (官方做法): DHCP 直接给 `BOOTAA64.efi`**, 不过 GRUB. 推车现在每个架构只给一个引导文件, 要做成 "按 MAC 临时切到 MemTest86" 那样的开关 (没做).
  A 不行再做 B.

**推车 TFTP 只读**: 不收 TFTP 上传 (WRQ), 所以报告和管理控制台的 XML 状态都传不回推车, 结果看屏幕. 暂存目录 `mt86site/mt86.cart.cfg` 是建议配置
(原 `mt86.cfg` 不改, 部署时把它放成 `mt86site/mt86.cfg`): `SKIPSPLASH=1`、`AUTOMODE=2` (直接开测, 测完停在结果页)、`AUTOREPORT=0`、
`PMPDISABLE=1` (论坛上有 PXE 长跑卡住加这个解决的)、`TCPDISABLE=1`、`RTCSYNC=0` (不改板子 RTC)、`CPUSEL=PARALLEL`.
以后要收报告: 给 pxeserver 的 TFTP 加一个只允许写某个目录的 WRQ, 单独做.

**部署**: `sudo install -d /opt/pxe/tftp/mt86site`, 拷入 Site 包 `EFI/BOOT/` 下所有文件, `mt86.cart.cfg` 拷成 `mt86.cfg`, `chown -R root:root`、`restorecon -R`.
回滚: `sudo rm -rf /opt/pxe/tftp/mt86site` (菜单项留着, 选了只会报找不到文件).

**实机要记的**: MemTest86 识别到几个核 (屏幕上 CPU 一栏 / 结果页; 飞腾上 UEFI 工具可能只认到 1 核, 此前鲲鹏上就这样 —— 多核靠固件的 MP Services);
它 RRQ 了哪些文件、路径是什么; 能不能读到内存条 SPD. 跑之前一样先确认菜单默认项是本地盘.
