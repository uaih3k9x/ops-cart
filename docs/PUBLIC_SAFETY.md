# 公开仓库脱敏约定

这是公开仓库（MIT）。代码、测试、文档、示例里都不放现场信息。

## 不能进仓库的

- 真实地址：公网 IPv4/IPv6、tailnet（CGNAT 段）地址、现场在用的私网段
- 真实 MAC、序列号、主机名、内部 SSID、人名
- 本机路径（`/Users/` 或 `/home/` 下带真实用户名的路径）、密码、PIN、令牌
- 推车上的运行时文件：维护日志 CART-LOG.md、kiosk.conf、pxe-mode.json、pxeserver.log、各种 `.bak`
  （已写进 `.gitignore`）

## 示例值

| 类型 | 用这些 |
|---|---|
| IPv4 | 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24（RFC 5737 文档段） |
| IPv6 | 2001:db8::/32 |
| MAC | 02:00:00:xx:xx:xx（本地管理位） |
| 路径 | /opt/pxe、/home/ops |
| SSID | CAMPUS-NET、LabNet |

## 检查工具

```
python3 scripts/check_public_safe.py [仓库目录]
```

只用标准库，扫 `git ls-files` 列出的文件（文件名本身也查），有发现时退出码为 1。

内置规则：

- IPv4：公网地址和 CGNAT 段算发现；文档段、本机、广播、掩码放过；私网段太常见，不在内置规则里
- IPv6：全局单播地址，文档段除外
- MAC：全球唯一的单播地址；本地管理位、组播、全 0、全 f 放过；`grub.cfg-01-<mac>` 也查
- 路径：`/Users/` 或 `/home/` 下带用户名的路径，通用账号名（ops、user 等）放过

豁免和本地词表：

- 行内写 `public-safe-ignore` 豁免整行，只用在确实需要写死的公共地址上（例如探测外网用的公共 DNS）
- 仓库根目录的 `.public-safe.local`（不进仓库）放现场词表：每行一个词，不分大小写子串匹配；
  以 `re:` 开头的按正则匹配。现场私网段写成 `re:\b<前两段>\.<第三段>\.` 这样的形式

`tests/test_public_safe.py` 测试规则本身，并对整个仓库跑一遍，要求零发现。

## 已知问题

Git 历史里有过真实地址等信息，HEAD 已清理，但历史没有重写：重写会影响已有的 clone 和 fork，
是否重写由维护者决定。
