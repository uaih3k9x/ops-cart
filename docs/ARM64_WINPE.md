# ARM64 Windows PE 启动冒烟测试

目的只有一个：看 **Windows ARM64 内核能不能在一块 ARM64 UEFI 板子上起来**。WinPE 整个跑在内存里（ramdisk），
不碰硬盘，也不需要板子上的网卡有 Windows 驱动（启动阶段用的是固件自己的 UEFI 网络栈）。

拿两个版本对照：

| 菜单项 | build | 预期 |
|---|---|---|
| `Windows PE ARM64 23H2 (smoke test)` | 22621（22H2/23H2 共用的内核） | 应能进 PE |
| `Windows PE ARM64 24H2 (expect fail: no LSE)` | 26100 | 预计失败：24H2 起内核要 ARMv8.1 LSE 原子指令 |

第一块用它的板子是飞腾 D2000（FTC663 核，`/proc/cpuinfo` 没有 `atomics`）。

## 1. 原理

### 1.1 链路（两条路，先试 A，不通走 B）

```
A. 固件 PXE ─► aa64/grubaa64.efi ─► 菜单手选 ─► chainloader /aa64/winpe/<build>/bootaa64.efi
B. arm64-winpe 开关 ─► 固件 PXE 直接拿 aa64/winpe/<build>/bootaa64.efi (DHCP 带 option 252 = BCD 路径)

   Windows 启动管理器 ─TFTP─► BCD ─► boot.sdi + sources/boot.wim (ramdisk) ─► winload.efi ─► 内核
```

`bootaa64.efi` 就是 ADK 里 `Media\EFI\Boot\bootaa64.efi`（ARM64 版 bootmgfw）。不装 WDS：WDS 那套只是把
启动管理器和 BCD 用 TFTP 发出去，推车的 TFTP 就能干。

**A 路（GRUB 链式加载）能不能走通，没有把握，要实机看：**

- 确定的：Rocky 的 GRUB 2.06 链式加载 TFTP 上的文件时，会把被加载程序的 `DeviceHandle` 设成网卡的句柄
  （`chainloader.c` 里 `dev->net` 分支 `grub_efinet_get_device_handle`），启动管理器有机会认出自己是网络启动。
- 确定的：GRUB 用网卡时以 `BY_EXCLUSIVE` 打开 SNP（`efinet.c` `open_card`），链式加载前不关。
- 推测（不利）：独占打开 SNP 会把固件挂在上面的 MNP/IP4/UDP4/PXE Base Code 那一串驱动顶掉；启动管理器经
  PXE Base Code 走 TFTP，很可能找不到协议而报错。旁证：x86 那条 WinPE 路（[WINPE.md](WINPE.md)）里，
  `snponly` 版 iPXE 被 GRUB 链式加载后认不到网卡，只能用自带驱动的完整版。公开资料里 GRUB 链式加载
  bootmgfw/wdsmgfw 有成有败，没找到 ARM64 的先例。
- 推测：就算认出网络启动，没有 option 252 时启动管理器去取 `\Boot\BCD`（它的二进制里有这个字符串，跟微软
  BIOS PXE 文档的约定一致）。所以 A 路要么开关同时打开（DHCP 会带 252），要么临时把 `Boot/BCD` 链到
  对应 build 的 `boot/bcd`（§5 第 3 步）。

**B 路（按 MAC 一次性开关）**是 WDS / 微软 PXE 文档的标准做法：固件自己 TFTP 拿启动管理器，PXE Base Code
完好；BCD 路径用 option 252 告诉它（WDS 就是靠 252 指 BCD；ARM64 上没实测过）。

### 1.2 开关怎么工作

```
sudo python3 /opt/pxe/pxeserver.py arm64-winpe --mac <板子 MAC> --build 22621 [--minutes 15]
sudo python3 /opt/pxe/pxeserver.py arm64-winpe --off
```

- 写 `/opt/pxe/arm64-winpe.json`（`mac`、`build`、`until`）。有效期 1–60 分钟，默认 15；`until` 不是有限数、
  或比"现在 + 60 分钟"还远（时钟回跳、手改的文件）都当没开。
- **只有一个状态文件：同时只能对一块板开开关，再开会覆盖前一块。**
- 开着时，**只有这台 MAC、且报 ARM64 UEFI（option 93 = 000b）** 的 DHCP（自己发地址、代理模式 67 口、4011 口）
  拿到 `aa64/winpe/<build>/bootaa64.efi` 和 option 252 `\aa64\winpe\<build>\boot\bcd`（结尾带 NUL，同 WDS）。每个回包只读一次状态文件，引导文件和 252 一致。别的机器、
  同一 MAC 报 x86 的，一律照旧。
- 一次性：TFTP 把某个 build 的 `bootaa64.efi` **完整传完**一次就删状态文件（只问 tsize 就断开的那次不算），
  events.log 记一笔。过期、坏文件也当没开。之后板子再 PXE 回到 GRUB 菜单（默认本地盘）。
- 菜单里的两个 WinPE 项只能手选：`_manual_only` 认 `aa64/winpe`，默认项指到它们的菜单会被 TFTP 换成默认菜单。

### 1.3 BCD 怎么来的

不用 bcdedit（要 Windows）。ADK 自带的 `arm64\Media\EFI\Microsoft\Boot\BCD` 本来就是 ramdisk 写法
（`device/osdevice = ramdisk=[boot]\sources\boot.wim,{7619dcc8-…}`，`winpe Yes`，`detecthal Yes`），
`[boot]` = 启动管理器自己来的设备，网络启动时就是 TFTP 根。[`scripts/arm64_winpe_bcd.py`](../scripts/arm64_winpe_bcd.py)
用 hivex 在它上面改：

| 对象 | 元素 | 改成 |
|---|---|---|
| ramdisk 选项 `{7619dcc8-…}` | `32000004` ramdisksdipath | `\aa64\winpe\<build>\boot\boot.sdi` |
| WinPE 项 `{7619dcc9-…}` | `11000001`/`21000001` device/osdevice | `ramdisk=[boot]\aa64\winpe\<build>\sources\boot.wim,{7619dcc8-…}` |
| 同上 | `260000b0` ems | Yes（内核 EMS / SAC） |
| 同上 | `12000004` description | `Windows PE ARM64 <build> (cart smoke test)` |
| `{emssettings}` | `16000020` bootems | Yes（启动管理器、winload 经 `{globalsettings}` 继承） |

- `{7619dcc8-…}` 是 ADK 模板自己建的 ramdisk 选项对象，不是保留名 `{ramdiskoptions}`（那个是 `{ae5534e0-…}`），沿用即可。
- 设备元素是二进制：脚本从头拼（`ramdisk_device`），两个长度字段随路径变。改写前先拿模板原有的 device 跟自己拼的
  `\sources\boot.wim` 比，逐字节相同才往下写（`check_template`），不同就报错退出。
- EMS 口：默认**不写口号**，让 Windows 按 ACPI SPCR 找口（D2000：PL011 @ 0x28000000，115200，ANSI）。
  `boot/bcd.com1` 是备用版，强写 EMS port 1 / 115200；ARM 上"COM1"怎么映射不确定，默认版不出字再换它。
- 不开 `testsigning` / `nointegritychecks`：PE 和启动管理器都是微软签名的，用不着。板子没开安全启动。

生成（开发机，docker 里的 Debian 带 `python3-hivex`；镜像一次性做：
`docker run --name hx debian:bookworm-slim sh -c 'apt-get update && apt-get install -y python3-hivex' && docker commit hx local/hivex:bookworm`）：

```
docker run --rm -v ~/code/pxeserver/scripts:/s:ro -v <staging>/winpe/22621/boot:/d local/hivex:bookworm \
    python3 -I /s/arm64_winpe_bcd.py /d/bcd.adk /d/bcd 22621            # 末尾加 "1 115200" 出 com1 版
```

### 1.4 TFTP 速度

- 推车 TFTP：支持 `blksize`（上限 1428，`TftpServer.blksize`）和 `tsize`，**不支持 `windowsize`**（RFC 7440），
  一问一答。块号 16 位回绕（300 MB / 1428 ≈ 21 万块，会绕三圈多）。
- Windows 启动管理器：BCD 有 `ramdisktftpblocksize` / `ramdisktftpwindowsize` / `ramdisktftpvarwindow`
  （WDS 的大块、多窗口），这里**都没设**，走默认值。客户端带了推车不认的选项，OACK 里不回，按 RFC 2347 客户端
  应退回默认（窗口 1）。
- 估计：千兆直连、一问一答，固件网络栈慢的话 1–3 MB/s，boot.wim 300 MB 约 2–5 分钟。实测太慢再考虑给推车
  TFTP 加 windowsize（先写测试），不建议把 blksize 抬过 MTU（会分片）。
- 未确认：板子固件 / 启动管理器对块号回绕的处理。传到 ~93 MB（65535 × 1428）附近卡住就是它。

## 2. 文件清单（推车 tftp 根 = /opt/pxe/tftp）

微软的文件**不进 git**，在开发机 `~/code/pxeserver-arm64-staging/winpe/`（来源、版本、sha256 见那里的
`SOURCES.tsv`、`SHA256SUMS`）。上车后的布局：

```
aa64/winpe/<build>/bootaa64.efi            Windows 启动管理器 (ARM64)
aa64/winpe/<build>/boot/bcd                本仓库脚本生成 (ADK 模板改的)
aa64/winpe/<build>/boot/bcd.com1           备用: EMS 强写 port 1
aa64/winpe/<build>/boot/bcd.adk            ADK 原样 (留底)
aa64/winpe/<build>/boot/boot.sdi
aa64/winpe/<build>/boot/fonts/*.ttf        启动管理器的字体 (缺了只是不好看)
aa64/winpe/<build>/boot/en-us/bootmgr.efi.mui
aa64/winpe/<build>/sources/boot.wim        ADK 的 arm64 winpe.wim 原样改名
```

| build | 来源 | boot.wim | bootaa64.efi |
|---|---|---|---|
| 22621 | ADK for Windows 11 22H2 的 WinPE add-on（2025-05 重发） | 10.0.22621.1 | 10.0.22621.5337 |
| 26100 | ADK 10.1.26100.9457（2026-09）的 WinPE add-on | 10.0.26100.1 | 10.0.28000.367 |

取法：`adkwinpesetup.exe` 是 WiX Burn 包，`7z x` 解出 Burn 清单（各 msi/cab 的下载名、大小、SHA1），
从 `download.microsoft.com/.../adkwinpeaddons/Installers/` 只下 arm64 要用的几个 cab 和 msi，SHA1 对上清单；
msi 的 File/Component/Directory 表（`msiinfo export`）给出 cab 里每个文件的真实路径。

**LSE 旁证**（开发机静态扫描，不是实测）：按 LSE 原子指令（LDADD/SWP/CAS 一族）的编码数可执行节里的字：
26100 的 `ntoskrnl.exe` 约 1.1 万处，22621 的约 170 处（跟随机数据的背景值同一量级）；两版的
`winload.efi`、`bootaa64.efi` 都在背景值。也就是说 26100 预计**启动管理器和 winload 都能跑，死在内核里**。

## 3. 上车（要 sudo，维护者在车上做）

```
# 0. 代码: 照常规发布流程 (pxeserver.py); 不改默认项, 菜单只多两个手动项
# 1. 文件 (开发机 -> 推车, 约 640 MB)
rsync -a --exclude dl ~/code/pxeserver-arm64-staging/winpe/22621 ~/code/pxeserver-arm64-staging/winpe/26100 <推车>:/tmp/winpe/
sudo install -d -o root -g root -m 0755 /opt/pxe/tftp/aa64/winpe
sudo cp -a /tmp/winpe/. /opt/pxe/tftp/aa64/winpe/ && sudo chown -R root:root /opt/pxe/tftp/aa64/winpe
sudo restorecon -R /opt/pxe/tftp/aa64/winpe          # SELinux enforcing
(cd /opt/pxe/tftp/aa64/winpe && sha256sum -c /tmp/winpe/SHA256SUMS)    # SHA256SUMS 一起拷过去
# 2. 确认: 菜单多两项, 默认仍是本地盘
tftp <推车装机地址> -c get aa64/grub.cfg /dev/stdout | head
```

## 4. 回滚

- 拿掉 WinPE：`sudo rm -rf /opt/pxe/tftp/aa64/winpe`（菜单项留着，选了只会报找不到文件）；
  `sudo python3 /opt/pxe/pxeserver.py arm64-winpe --off`。
- 代码回到上一版即可，开关文件没人读就不起作用。

## 5. 实机验证（维护者在板子前）

准备：采集卡接 HDMI 看画面；串口接 SPCR 那个口（BIOS 串口重定向 COM0，115200 8N1），终端开日志；推车上
`tail -f` pxeserver 日志看 TFTP 请求了哪些名字。

1. **A 路**：板子 PXE → GRUB 菜单手选 23H2 项。
   - 看推车日志有没有 `aa64/winpe/22621/bootaa64.efi … 传输完成`，之后有没有 BCD 的请求、请求的是什么名字。
   - 画面 / 串口出 Windows 启动管理器错误（如 0xc000000f、"BCD … from the PXE server"、找不到网络设备）
     或直接回到固件：A 路不通，做 B 路。
2. **B 路**：`arm64-winpe --mac <MAC> --build 22621`，板子重启 PXE（固件里选网络启动）。日志应依次看到
   bootaa64.efi → `\aa64\winpe\22621\boot\bcd` → boot.sdi → boot.wim（几分钟）。
3. 启动管理器请求的名字跟文件对不上（推车 TFTP 区分大小写）：按日志里的名字补软链接，试完删掉。A 路没有 252 时
   大概率取 `\Boot\BCD`：`sudo install -d /opt/pxe/tftp/Boot && sudo ln -s ../aa64/winpe/22621/boot/bcd /opt/pxe/tftp/Boot/BCD`
   （名字按日志里的大小写来）。
4. 进内核后看什么：
   - 串口 EMS / SAC 出 `SAC>` 提示符或 `Computer is booting, SAC started and initialized` = 内核起来了。
     PE 正常的话画面出 `wpeinit` 的命令行窗口。**23H2 走到这里冒烟测试就算过。**
   - 蓝屏停止码（画面和 EMS 都会出）：
     - `0x0000001E KMODE_EXCEPTION_NOT_HANDLED`，参数 1 = `0xC000001D`（STATUS_ILLEGAL_INSTRUCTION）：
       内核执行了 CPU 不支持的指令，26100 缺 LSE 预期就是这类。
     - `0x0000005D UNSUPPORTED_PROCESSOR`：内核启动早期自己查 CPU 特性不过（可能性同样有，未确认 ARM64 上是否这样报）。
     - `0x0000007B INACCESSIBLE_BOOT_DEVICE`：ramdisk 没挂上（BCD 设备 / boot.sdi 有问题），不是 CPU 问题。
     - `0x000000A5 ACPI_BIOS_ERROR`、`0x00000035 / 0x0000007E` 一类：ACPI 表或驱动问题，记下参数。
   - 黑屏无输出、也没 EMS：太早死（显示还没起来）或 EMS 口不对。换 `bcd.com1`（拷成 `bcd`）再试一次；
     还不行就当"内核没起来"记，配合 26100/22621 对照判断。
5. 26100 同样做一遍（开关 `--build 26100`，或手选菜单 24H2 项），记现象和停止码。
6. 结果、日志、停止码和参数写回板子的兼容性报告；用完 `arm64-winpe --off`。
