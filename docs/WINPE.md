# Windows PE 引导

推车给被测机起 Windows PE（比如微PE），再在 PE 里从 SMB 共享装 Windows。2026-10-30 手工跑通，#11 收进仓库。

## 链路

```
固件 PXE ─► shimx64.efi ─► GRUB ─► chainloader (http,<推车>)/win/ipxe.efi
        ─► iPXE 再要一次地址 (option 77 = iPXE) ─► pxeserver 回 http://<推车>/boot.ipxe
        ─► wimboot ─► bootmgfw.efi ─► boot.wim (PE)
```

- 不能让 GRUB 直接 `linux wimboot`：Rocky 的 GRUB（linuxefi）会报 kernel too old，所以中间要经 iPXE。
- `boot.ipxe` 由推车解锁时按模板生成（`pxeserver.ipxe_script()`），不要手写。

## 要放的文件

都放在 tftp 根目录（`/opt/pxe/tftp`）下的 `win/`，由 80 口的 HTTP 提供。清单是 `pxeserver.WINPE_FILES`：

| 文件 | 说明 |
|---|---|
| `win/ipxe.efi` | **完整版** `ipxe-x86_64.efi`。`snponly` 版被 GRUB 链式加载时认不到网卡 |
| `win/wimboot` | iPXE 官方的 wimboot |
| `win/bootmgfw.efi` | 微PE 的 wim 里没有，要单独放（用微PE 自带的 19041 版） |
| `win/bcd` | PE 的 BCD |
| `win/boot.sdi` | |
| `win/boot.wim` | PE 本体 |

文件名区分大小写，照上表用小写。空文件按"缺"算（拷到一半的不算数）。

## 菜单什么时候有 WinPE

- **只在车上解锁装机模式后、只在给选定 MAC 写的 `grub.cfg-01-<mac>` 里**出现，排在自动装机项后面，不是默认项。
  默认救援菜单（`grub.cfg`）里永远没有。
- 解锁时 `win/` 下少任何一个文件，这一项就**不放**进菜单，维护日志里那条"装机模式解锁"会写明缺哪些。
  不给走不通的路，是因为 iPXE 一失败就只能整机重启（见下）。
- 解锁菜单的默认项仍是 Rocky 自动装机，倒计时 5 秒：**要进 PE 得在 5 秒内选到最后一项**，
  不然被测机会被装 Rocky（清空整块盘）。
- 带 WinPE 项的菜单跟带 `inst.ks=` 的一样：TFTP / HTTP 只发给解锁的那台，上锁（到期、取走菜单、手动上锁）时
  整棵 tftp 树里这类菜单都会删掉。`boot.ipxe` 和 `win/` 不拦也不删：菜单一被取走就上锁，GRUB 那时还停在菜单上，
  再选 WinPE 还得取得到；PE 本身不动硬盘。

## 坑

- **被测机必须关 Secure Boot**：iPXE、wimboot 都没签名，开着会在 chainloader 那一步被拒。
- **iPXE 失败后要整机重启**：回到 GRUB 后网卡就不能用了（could not send packets），在 GRUB 里重试没用。
- iPXE 接管网卡时 LAN1 会断一下，装机口守卫会重新探测约 4 秒，属正常。
- 只支持 UEFI。BIOS 老机器暂不支持。
