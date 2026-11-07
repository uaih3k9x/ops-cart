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
| L2 | 串口不带高危词、`send_key ctrl_c`、键鼠 `alt+f4`、`label_print` 批量 | 直接点批准 | TOTP |
| L3 | 串口带高危词、键鼠挂起（默认）、远程解除急停 | TOTP；键鼠只能拒绝 | 带外码上线（A3）之前不开 |
| L4 | `pxe_request` 全部用途 | 不开放 | 不开放 |

拒绝不要因素，但审批目录里的拒绝也要带 digest。同一账号 30 秒内只能批一条 L3 及以上。

打码：命令里出现任一敏感词（`password`、`secret`、`key`、`passphrase`、`community`、`cipher`、`simple`、`md5`、`sha`、`auth`、`authentication`、`priv`、`encrypted` 等；`encrypted-password`、`shared-key`、`-secret` 这类复合词也认），从第一个敏感词后面起整段换成 `<已打码 N 字符>`（往多打的方向错）；键鼠 secret 只显示长度；设备输出和提示符上的名字也先打码再清洗。digest 仍按原文算。动作行里的控制字符、方向控制、零宽字符、行/段分隔符一律显示成 `\xNN` / `\u{..}`，不会另起一行。串口命令（最长 256 字符）永远完整显示；键鼠长文本超过 256 字符只显示头尾，这种条目只能拒绝、不能远程批准。

条目：`make_item` 按 kind 校验 action 的字段名和类型，多一个少一个都不收；`created` 必须是有限数。`decide` 不信条目里带的 `tier` 和 `expires`，按 `created` 现算。

审计行字段：`t ev id digest kind tier channel account by factor summary result ts_login ts_node nonce`。`ts_login` / `ts_node` 是 tailscale 登录名和设备名（拍板 8），这一阶段由调用方传入、只校验格式，格式不对按 `forbidden/identity` 拒。永远不记 TOTP 码和种子，nonce 只记前 4 位。

审批人身份：

- 网页：`by` 必须是 `web:...`，并且必须带 tailscale 登录名，缺了就拒。
- 飞书：白名单里这个账号必须绑了 open_id（`{"alice": {"feishu": "ou_..."}}`），`by` 必须正好是 `feishu:<那个 open_id>`。没绑或对不上就拒。

决定记录先写审计、后提交：审计写不进去就返回 `audit_fail`，不登记批准。决定记录逐项校验，任何一项读不懂，整份就按读不懂处理。

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
