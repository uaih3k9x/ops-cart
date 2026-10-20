# 开机画面（Plymouth）

```bash
sudo dnf install plymouth plymouth-scripts plymouth-plugin-script plymouth-plugin-label ImageMagick
sudo ./install.sh            # 重建当前内核 initramfs
sudo grubby --update-kernel=DEFAULT --args="rhgb quiet"
```

- 白底、大 Logo、基板名、`Step 1/3`（initramfs）→ `Step 2/3`（切到真根分区后），`Step 3/3` 是 Kiosk 自检页。
- 品牌素材放 `/opt/pxe/branding/`：`branding.json`（`title` / `board`）、`logo.png`、`spinner.gif`（转圈圈，安装时拆帧，按 GIF 帧间隔轮播）。这个目录不进仓库，只放你有权使用的素材。
- 改完素材要重跑 `install.sh`（素材在 initramfs 里）。
- 脚本只能写 ASCII（initramfs 里没有中文字体）；函数里访问全局变量要加 `global.` 前缀。
- 回滚：`grubby --remove-args="rhgb quiet"`，或从 GRUB 选旧内核。

`cart-boot-success.timer`（仓库根目录）：开机 60 s 后 Kiosk 还活着就写 `boot_success=1`，GRUB 下次藏菜单；某次没撑到就自动亮菜单。没有图形会话的机器上，系统自带的 grub-boot-success.timer 不会跑。
