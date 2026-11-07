# 远程审批：第一阶段（服务端核心）

设计稿见 PR #59 的 `docs/DESIGN_remote_approval.md`（issue #50）。这一阶段只做推车服务端的纯逻辑和测试，**不接原生 Book、不接真飞书、不改任何单元文件**，推车上的行为不变。

## 已实现

| 模块 | 内容 |
|---|---|
| [`totp_core.py`](../totp_core.py) | RFC 6238，只用标准库（SHA-1、30 秒、6 位，±1 步）。每个账号记 `last_step`，只增不减：同一个码第二次用、跨通道再用、时钟回拨到已用过的步，都拒。输错按 `PinGate` 的方式指数退避，封顶 1 小时，按账号算、跨通道共享。种子文件读取（`load_seed`）：不是普通文件、是链接、属主不对、组或其他人有权限、内容不对，一律当没有种子 |
| [`approval_core.py`](../approval_core.py) | 统一条目 `make_item`，分级 `tier_of`，各通道因素 `requirement`，digest v1，摘要 `render` 和卡片 `card`（命令打码、不可信文字清洗限长），请求解析 `parse_request`，决定 `decide`（纯函数），审计行 `audit`，决定记录 `ApprovalStore`（`/var/lib/ops-cart/approvals.json`，root 0600，flock，原子写；读不懂就一律拒，不擅自重置） |
| [`approval_channel.py`](../approval_channel.py) | 推送通道接口 `Channel` 和测试用的 `FakeChannel`；`notify` 把卡片推给白名单账号，每个账号一行审计，送不到记 `receipt_fail` |

分级和因素（拍板 2、4、7、11）：

| 级别 | 什么 | 飞书 | 网页 |
|---|---|---|---|
| L2 | 串口不带高危词、`send_key ctrl_c`、键鼠 `alt+f4`、`label_print` 批量（2–200 张） | 直接点批准 | TOTP |
| L3 | 串口带高危词、键鼠挂起（默认）、远程解除急停（范围只收 `all`、`kvm`、`slot:1`–`slot:4`） | TOTP；键鼠只能拒绝 | 带外码上线（A3）之前不开 |
| L4 | `pxe_request` 全部用途 | 不开放 | 不开放 |

拒绝不要因素，但审批目录里的拒绝也要带 digest。同一账号 30 秒内只能批一条 L3 及以上。

打码（第二轮审查后改过）：**只打口令的值，不打后面的命令**。

- 关键词写法：`password`、`secret`、`key`、`community`、`cipher`、`md5`、`auth`、`priv` 等，后面最多跳过 4 个修饰词（`0`/`7`/`cipher`/`simple`/`irreversible-cipher`/`md5`/`sha`/`aes`/`level 15` 等），只打紧跟的那一个值。`crypto key zeroize` 这类后面跟的是命令词的，不打。
- 固定写法：`XX_PASSWORD=`、`--password=`、`pass:`，URL 里的 `user:pass@`，`curl -u u:p`，`smbclient -U u%p`，`mysql -pX`，`ldapsearch -w X`，`wpa_passphrase SSID X`，`snmp-server host H version 2c X`。
- `chpasswd`、`passwd`、`htpasswd`、`net user`、`sshpass`：整条命令只显示长度，**只能拒**。
- 设备最后一行输出是密码提示符（`Password:`、`密码：` 等）时，这条动作本身就是密码：只显示长度，**只能拒**。
- **看不清就只能拒**：动作太长被截断、含控制类字符、整条只显示长度、打掉的值里有空白或 shell 元字符（`; & | < > \` $(`），这几种情况都没有批准按钮，`decide` 也会拒。
- 设备输出和提示符上的名字先去掉零宽字符并做 NFKC 归一化，再打码、再清洗；`<` `>` 换成全角，防止飞书 `<at>` 标签和 lark_md 生效。
- digest 仍按原文算。键鼠 secret 只显示长度，它的 prefix 照样检查。

条目：`make_item` 按 kind 校验 action 的字段名和类型，多一个少一个都不收；`created` 必须是有限数。`decide` 不信条目里带的 `tier` 和 `expires`，按 `created` 现算。

审计行字段：`t ev id digest kind tier channel account by factor summary result ts_login ts_node nonce`。`ts_login` / `ts_node` 是 tailscale 登录名和设备名（拍板 8），这一阶段由调用方传入、只校验格式，格式不对按 `forbidden/identity` 拒。永远不记 TOTP 码和种子，nonce 只记前 4 位。

审批人身份：

- 白名单：`{"alice": {"feishu": "ou_…", "ts_login": "alice@example.com", "ts_nodes": ["phone-1"]}}`，三项都可选。open_id 和登录名在账号之间必须唯一，白名单不合格时一律拒。
- 网页：tailscale 登录名必须等于账号绑定的登录名；`ts_nodes` 不为空时，设备名也要在列表里。`by` 由服务端拼成 `web:<登录名>/<设备>`，请求里自报的不算。
- 飞书：账号必须绑了 open_id，`by` 必须正好是 `feishu:<那个 open_id>`。
- 执行前的拒绝覆盖批准，停的方向优先：先批后拒时记 `rejected/override`，执行器（下一阶段）在原生锁里看到这条就不发；先拒后批记 `conflict`，拒绝不能被翻。

决定记录先写审计、后提交：审计写不进去就返回 `audit_fail`，不登记批准；审计写进去了但状态落盘失败，就补记一行 `commit_fail`，并返回失败。决定记录逐项校验，任何一项读不懂，整份就按读不懂处理。

## 残余风险

- **TOTP 冷却可以被用来锁人**：冷却按账号算、跨通道共享。谁能往请求目录投件，谁就能故意输错，把这个账号锁住，最长 1 小时（设计稿 T11）。这一阶段不处理，缓解办法：车机确认不受影响；急停和拒绝不要 TOTP；以后每次进入冷却都推通知。
- 飞书 L2 按拍板 2 不要 TOTP：飞书账号被盗就能直接批 L2（设计稿 T2 需要同步）。

## 待维护者对稿确认（拍板 9–11，按设计稿现有取值实现）

- 期限：L2 15 分钟，L3 10 分钟，L4 10 分钟（`EXPIRES_S`）；`AUTH_TTL_S` 120 秒（常量先定，执行器是下一阶段）。
- 审批会话 15 分钟、不滑动，签发和第一次批准合并（网页，下一阶段）。
- 分级：`pxe_request` 全部 L4；不带高危词的串口命令 L2；键鼠 L2 白名单只有 `alt+f4`。

## 部署说明（这一阶段不部署，写给下一阶段）

- **种子每人一个**（拍板 12）：`/etc/ops-cart/approve-secrets/totp-<账号>`，目录 root 0700，文件 root 0600，内容是 base32。只经 `LoadCredential=` 交给取件单元 `cart-approve.service`，例如 `LoadCredential=totp-alice:/etc/ops-cart/approve-secrets/totp-alice`；程序从 `$CREDENTIALS_DIRECTORY` 读（`totp_core.load_seed`）。其他单元的 `InaccessiblePaths` 见设计稿 4.1。
- 签发：`totp_core.new_seed()` 生成，`otpauth_uri()` 给验证器 App 扫；只在签发时显示一次。`totp-enroll` 命令是下一阶段。
- **飞书**（拍板 6）：专用应用，长连接，官方 SDK 装在**独立的 venv** 里，不进系统 Python、不和推车其他服务共用；只发白名单账号的单聊；回调用 `card.action.trigger`，字段和签名实现前对照官方文档核对。真实客户端实现成 `approval_channel.Channel` 的子类，卡片按纯文本发。

## 接入点（下一阶段）

为了不和正在审的 #68、#74（`send_confirmed`、`Book.begin/confirm/finish`）冲突，这一阶段没有改原生入口。下一阶段要接的位置：

1. 串口 `serial_write_core.Book.confirm(iid, by, now, expect=None)`：在原生 flock 里用 `approval_core.digest` 重算并比对 `ctx`（`send_seq`）。
2. 键鼠 `mcp_kvm.LeaseStore.take(iid, by, expect=None)`；挂起时写定 `serial` 和 `prefix`。
3. PXE `pxe_req_core` 的 `take` 和新增的 `remote_confirm`：L4 不开放远程，只用于拒绝。
4. 三处 Book 加 `epoch`（缺 `epoch` 的旧状态文件就地补上，不触发全部急停）。
5. 取件单元 `cart-approve`：照 `mcp_serial._take_request` 的加固读 `/run/ops-cart/approve-req/<通道>/`，`parse_request` → 按原生状态现算条目 → `ApprovalStore.decide`。
6. 远程解除急停：取件单元批准后调现有的解除路径；`label_print` 批量：`mcp_label` 里 `confirmed` 之外再要一条已批准的 `label_batch` 条目。

## 下一阶段

网页审批和网页登录加 TOTP（拍板 3）：ops-web 读不到种子（设计稿要求遮掉），网页登录的 TOTP 也要经 root 一侧校验，跟审批取件走同一套请求目录，所以放到下一阶段和执行器一起做。另外还有：执行器 `cart-approve-exec@`、`approval-check`、`approve-res` 结果文件、带外码、撤回窗口、`lockdown`、`kvm_stop`。
