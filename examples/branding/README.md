# 品牌目录模板

Kiosk 开机自检页（以及以后的 Plymouth 开机动画）从 `/opt/pxe/branding/` 读品牌素材：

| 文件 | 作用 | 缺省 |
| --- | --- | --- |
| `branding.json` | `title` 大标题、`subtitle` 副标题 | `OPS-CART` / `SYSTEM STARTUP` |
| `logo.png` | 标题上方的 Logo（宽度超过 480 px 会按整数倍缩小） | 不显示 |
| `logo-dark.png` | 深色界面用的 Logo（透明底 PNG，深色字改成浅色）；深色时优先用它，浅色时不用 | 用 `logo.png` |

```bash
sudo mkdir -p /opt/pxe/branding
sudo cp examples/branding/branding.json /opt/pxe/branding/   # 改成你自己的名字
sudo cp your-logo.png /opt/pxe/branding/logo.png
sudo systemctl restart kiosk
```

这个目录不进仓库，请只放你有权使用的素材。
