# Memtest86+（PXE 测内存，#55）

测内存不用插 U 盘：救援菜单最后一项就是 Memtest86+。版本固定为 **8.10**（GPL，官方 memtest.org 发布）。

## 链路

```
UEFI: 固件 PXE ─► shimx64.efi ─► GRUB (grub.cfg 救援菜单) ─► chainloader (http,<推车>)/memtest/memtest64.efi
BIOS: 固件 PXE ─► pxelinux.0 ─► pxelinux.cfg/default ─► 在 boot: 提示符敲 memtest ─► LINUX memtest/memtest64.efi (TFTP)
```

- 官方 x86_64 文件既是 EFI 程序又是 Linux bzImage，所以 UEFI / BIOS 用同一个文件。pxelinux 里用 `LINUX`
  指令加载，不按扩展名猜类型。
- 菜单模板在 `pxeserver.grub_menu`（救援菜单最后一项，**不是默认项**）和 `pxeserver.pxelinux_menu`
  （默认救援，10 秒）。自动装机菜单里没有这一项。
- Memtest 不动硬盘，所以放在人人能取的救援菜单里，不算解锁菜单。

## 安全启动：要先关

**Memtest86+ 8.10 的 EFI 文件没有签名**，被测机开着安全启动（Secure Boot）时会被 shim/GRUB 拒绝
（chainloader 报签名 / 权限错误后回到菜单）。测内存前进 BIOS 设置**临时关闭安全启动**，测完再打开。
菜单项文字里也写了 `turn Secure Boot OFF first`。

（官方 9.00 起用自己的 CA 签名，但这个 CA 默认不在固件 db 里，不手工导入证书照样被拒，对推车没区别。）

## 部署

二进制不进仓库。推车上（能上外网时）：

```
sudo bash scripts/setup-memtest.sh       # 在拷到推车上的仓库目录里跑; memtest_core.py 要先部署到 /opt/pxe/
```

- 下载 `https://memtest.org/download/v8.10/mt86plus_8.10.binaries.zip`，先校验 zip 的 sha256
  （和官网同目录 `sha256sum.txt` 一致），再校验解出的 `mt86p_810_x86_64` 的 sha256，两道都过才原子写入
  `/opt/pxe/tftp/memtest/memtest64.efi`。**任何一步不符就中止，旧文件不动**。
- 推车上不了外网：在别处下载同一个包拷上去，`setup-memtest.sh --zip <包>`，校验照做。
- 版本、地址、两个 sha256 都在 `memtest_core.py` 顶部。升级时都要换成真值；没填（占位）一律当不符。

GRUB 菜单不用另写：车机开机 / 上锁时重写的救援 `grub.cfg` 已带这一项。文件还没放时选这一项，GRUB 报找不到文件，
回菜单即可。

### BIOS 机器

仓库只管菜单：`kiosk_core.py --write-rescue`（`setup-cart.sh` 会调）写 `pxelinux.cfg/default`。
**`pxelinux.0` 和 `ldlinux.c32` 要另放**到 tftp 根（Rocky 9 的 `syslinux-nonlinux` 包，`/usr/share/syslinux/` 下），
还没收进部署脚本。注意：

- `pxelinux.0` 只会 TFTP，Memtest 文件小（约 160 KB），没关系；救援项的 initrd 大，走 TFTP 慢。
- 推车自己发地址时才答 BIOS 机器；代理 DHCP 模式只答 UEFI x86-64（`PROXY_ARCHES`），BIOS 机器起不来。
- 手工跑 `--write-rescue` 会顺带上锁装机模式（和 `setup-cart.sh` 一样）。
