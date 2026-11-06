# Memtest86+（PXE 测内存，#55）

测内存不用插 U 盘：救援菜单最后一项就是 Memtest86+。版本固定为 **8.10**（GPL，官方 memtest.org 发布）。

## 链路

```
UEFI: 固件 PXE ─► shimx64.efi ─► GRUB (grub.cfg 救援菜单) ─► chainloader (http,<推车>)/memtest/memtest64.efi
BIOS: 固件 PXE ─► pxelinux.0 ─► pxelinux.cfg/default ─► 在 boot: 提示符敲 memtest ─► LINUX memtest/memtest64.efi (TFTP)
```

- 8.10 官方二进制包里每个架构只有一个文件（`mt86p_810_x86_64` / `_i586` / `_la64`），没有单独的 `.bin`。
  x86_64 文件既是 EFI 程序（`MZ`/PE）又是 Linux bzImage：头里有 `HdrS`、启动协议 2.12、7 个实模式 setup 扇区，
  所以 pxelinux 的 `LINUX` 能按 16 位启动协议加载（不按扩展名猜类型）。**BIOS 下能否实际跑起来尚待实机验证**
  （只核对了文件头，没找到官方对 pxelinux 的明确说明）；起不来的话改用官方 `mt86plus_8.10_x86_64.iso.zip`
  里的 ISO 经 memdisk 或换 i586 文件，另钉 sha256。
- 菜单模板在 `pxeserver.grub_menu`（救援菜单最后一项，**不是默认项**）和 `pxeserver.pxelinux_menu`
  （默认救援，10 秒）。自动装机菜单里没有这一项。
- Memtest 不动硬盘，所以放在人人能取的救援菜单里，不算解锁菜单。

## 安全启动：要先关

**Memtest86+ 8.10 的 EFI 文件没有签名**，被测机开着安全启动（Secure Boot）时会被 shim/GRUB 拒绝
（chainloader 报签名 / 权限错误后回到菜单）。测内存前进 BIOS 设置**临时关闭安全启动**，测完再打开。
菜单项文字里也写了 `turn Secure Boot OFF first`。

（官方 9.00 起用自己的 CA 签名，但这个 CA 默认不在固件 db 里，不手工导入证书照样被拒，对推车没区别。）
理论上可以经 shim 的 MOK（`mokutil --import`）导入 9.00 的 CA，之后不关安全启动也能起；但每台被测机都要导入一次
（重启时在 MokManager 里人工确认），而且**没有实机验证过**，推车这边不做。

## 部署

二进制不进仓库。推车上（能上外网时）：

```
sudo bash scripts/setup-memtest.sh       # 在拷到推车上的仓库目录里跑; memtest_core.py 要先部署到 /opt/pxe/
```

- 下载 `https://memtest.org/download/v8.10/mt86plus_8.10.binaries.zip`。**只走 https**（重定向到非 https 直接失败），
  **完整性靠钉死的 sha256**：zip 的值抄自官网同目录 `sha256sum.txt`，解出的 `mt86p_810_x86_64` 再对一次（第二个值是
  从校验过的 zip 里算出来的，不是另一个独立来源）。都对上才原子写入 `/opt/pxe/tftp/memtest/memtest64.efi`，
  **任何一步不符就中止，旧文件不动**。官方发布包没有签名，这是能做到的上限：防得住传输损坏和下载被换，防不住官网本身被换包后我们抄了错的值。
- 下载和本地包都限 16 MB（官方包几百 KB），超了中止。
- 推车上不了外网：在别处下载同一个包拷上去，`setup-memtest.sh --zip <包>`，校验照做。
- 版本、地址、两个 sha256 都在 `memtest_core.py` 顶部。升级时都要换成真值；没填（占位）一律当不符。

菜单不用另写：车机开机 / 上锁时（`pxe_disarm`）按当前 `pxe.env` 的 `PXE_IP` 重写救援 `grub.cfg` 和
`pxelinux.cfg/default`，都已带这一项，改了地址也不会留旧的。文件还没放时选这一项，GRUB 报找不到文件，回菜单即可；
验收脚本 `cart_accept.py` 的"Memtest86+ 文件"一项会报"人工"。

### BIOS 机器

仓库只管菜单：`pxelinux.cfg/default` 由 `pxe_disarm` 写（车机开机 / 上锁，以及 `setup-cart.sh` 调的
`kiosk_core.py --write-rescue`）。**BIOS 菜单只用 `pxelinux.cfg/default`，推车不生成按 MAC / IP 命名的 pxelinux 配置**，
解锁用途（装机、WinPE、克隆）都只给 UEFI 的 GRUB。pxelinux 路径是**白名单**（`pxeserver.pxelinux_cfg_gate`）：
- TFTP 收到 `pxelinux.cfg/default`（大小写、反斜杠、`./`、`../` 归一后）**不读磁盘**，直接发 `pxeserver.pxelinux_menu` 按当前地址生成的内容；
  磁盘上那份被改成什么（`INCLUDE`、`CONFIG`、自带 ks 的 initrd、memdisk……）都不会发出去；
- `pxelinux.cfg` 下别的（按 MAC / IP 命名、任意深度子目录、`bios/pxelinux.cfg/…`、符号链接别名）TFTP 一律回找不到，pxelinux 落到 `default`；
- HTTP（`pxe-http-boot` 出的就是 tftp 根）对 `pxelinux.cfg` 下任何东西一律 404（`pxelinux.0` 只走 TFTP，用不着），也不列任何目录；
- 生成的菜单带 `ALLOWOPTIONS 0` 和 `IMPLICIT 0`：`boot:` 下只能敲 `rescue` / `memtest`，不能加内核参数、不能敲别的内核或配置文件名。
  要临时加参数（如串口 `console=ttyS0`）请走 UEFI 的 GRUB 菜单（按 `e` 编辑），或改 `pxelinux_menu` 加一项；
- 上锁时先删状态文件，再把 `pxelinux.cfg` 下根 `default` 以外的全删（含子目录、非常规文件；符号链接只删链接本身），
  删不掉 / `default` 写不进去都只记 pxe 事件，不拖住上锁。
**`pxelinux.0` 和 `ldlinux.c32` 要另放**到 tftp 根（Rocky 9 的 `syslinux-nonlinux` 包，`/usr/share/syslinux/` 下），
还没收进部署脚本。注意：

- `pxelinux.0` 只会 TFTP，Memtest 文件小（约 160 KB），没关系；救援项的 initrd 大，走 TFTP 慢。
- 推车自己发地址时才答 BIOS 机器；代理 DHCP 模式只答 UEFI x86-64（`PROXY_ARCHES`），BIOS 机器起不来。
- 手工跑 `--write-rescue` 会顺带上锁装机模式（和 `setup-cart.sh` 一样）。
