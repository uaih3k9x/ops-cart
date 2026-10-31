# 字体

| 文件 | 来源 | 授权 |
|---|---|---|
| `unifont-18.0.01.otf` | GNU Unifont 18.0.01 OTF 原样 (<https://unifoundry.com/unifont/>), sha256 `88d0a14d4aa9a96419720b39ba5da921a59560d76d4ec7d523acc06ed85cd3c2` | GPLv2+ 带字体嵌入例外 / SIL OFL 1.1 双授权, 原文见 [`unifont-LICENSE.txt`](unifont-LICENSE.txt)、[`unifont-OFL-1.1.txt`](unifont-OFL-1.1.txt) |

车机界面和副屏用 (#28)。部署装到 `/usr/local/share/fonts/ops-cart/` 再 `fc-cache -f`;
取不到时车机退回 Noto, 自检多一行 `UI FONT ... WARN FALLBACK NOTO`。

- 只用 OTF: Rocky 9 的 FreeType 2.10.4 解析不了 OTB。
- 不要装 `bitmap-fangsongti-fonts`: ASCII 全是全角, 还会把自己设成全系统默认 monospace。
