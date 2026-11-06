# 设计：远程审批（issue #50）

推车的定位是"人在办公室，车在机房"。#23、#42、#24 定下的"确认只在车机本机、网页只能拒绝、Agent 没有确认工具"是早期的保守做法：人不在车旁，挂起的操作只能干等。本文把**远程审批定为主路径**，车机确认保留为现场路径，**急停在任何通道都能按、优先级最高**。

本文只是设计，不改代码。引用的函数、文件和单元配置都是写作时 `main` 上的实际实现。

**本期范围**：车机、webkiosk 网页、飞书卡片、Claude 会话转达，以及大肥鱼经 ACP 接入时怎么挂进来（第 4.6 节）。issue 里提到的"大肥鱼网页"前端**不在本期范围**，以后要做也只能当本设计的又一个前端：同样的请求目录、同样的因素要求。

## 〇、结论先说

1. **不另起一套挂起状态。** 串口 `serial_write_core.Book`、键鼠 `mcp_kvm.LeaseStore`、PXE `pxe_req_core.Book` 仍然是各自挂起项的唯一事实来源。新增的 `approval_core` 只做两件事：把三处挂起项映射成统一视图（ID、服务端生成的摘要、风险级、摘要哈希），以及记录"谁、在哪个通道、用什么因素、做了什么决定"。
2. **网页会话不能证明"是人"。** tailnet 来源在没设 PIN 时，不输 PIN 就能登录网页（`kiosk_core.PinGate.attempt` 在 `required` 为假时直接返回 `ok`）。跟人同在 tailnet 上的 Agent 用命令行也能拿到会话；同源检查只防得住浏览器。所以**网页上的每次批准都要输 TOTP**，由 root 校验。
3. **第二因素推荐 TOTP（RFC 6238）。** 种子放在单独的目录里，只有 root 执行器能读：`webkiosk` 和飞书服务的单元都用 `InaccessiblePaths` 把这个目录遮掉（`webkiosk.service` 有 `CAP_DAC_READ_SEARCH`，单靠文件权限挡不住它，见 H1）。
4. **说清楚边界：TOTP 只能证明"有人在场"，不能证明"这个人批的是哪一条"。** 被拿下的 ops-web 或飞书服务，可以把人刚输的码改投到另一条挂起项上。另外 ops-web 本来就在 `dialout` 组里，能直接写 CH9329 和 conbroker 的人用通道。所以"ops-web 被拿下，最多只能叫停"这句话**不成立**，第六节 T3 如实写出残余风险。对策有三条：执行后经另一个通道推送回执；审批链接一律不由 Agent 提供；L4 的长期方案是做挑战和 digest 绑定的签名（WebAuthn 一类）。
5. **批准由 root 执行，请求按通道分目录投递，root 按目录认通道。** **急停每个通道都有自己的目录和单元**，不和可能阻塞的审批执行器排队。
6. **闸门只是减速带。** 键鼠闸门目前可以绕过（#64），串口挂起期间还能继续直发、改掉上下文（#65）。所以分级和威胁模型**不能建立在"闸门拦得住"上**：分级看的是"这一条一旦执行，后果是什么"，以及"人批的东西和实际执行的东西是不是同一件事"。
7. **部署前提：跑 Agent 的机器上不能有到推车的 root 通道。** `cart-mcp` 以 root 运行，`cart_mcp.py` 文档串还给了 `ssh cart sudo python3 /opt/pxe/cart_mcp.py stdio` 这种接法。持有这种通道的 Agent 能直接读种子、写请求目录，本设计的一切都挡不住它。长期要把 `cart-mcp` 降为非 root（关联 #49 的只读、动手分拆）。在那之前，"AI 不能审批 AI"**只靠代码不变量**，不是靠权限隔离（T9）。
8. **Claude 只转达。** 它能看到挂起项的服务端摘要和编号，**拿不到可点的审批链接，也没有任何能批准的工具**。

## 一、现状（读代码确认）

| 挂起源 | 状态文件 | 编号 | 过期 | 人确认入口 | 网页 |
|---|---|---|---|---|---|
| 串口 `send_command` / `send_key ctrl_c` | `/run/ops-cart/serial-write.json`（`mcp_serial.Store`，flock） | `doc["seq"]`，一次开机内递增 | `PENDING_MAX_S = 900`；租约过期、释放或急停时作废 | `mcp_serial.human_action("confirm")` → `send_confirmed` → `Book.confirm`（再查急停、租约、在途） | 只能 `estop` / `reject`（`confirm_core.WEB_OPS`） |
| 键鼠 `kvm_key` / `kvm_type` 命中高危词或高危组合键 | `/run/ops-cart/kvm-lease.json`（`LeaseStore`） | `st["next"]`；`read_state` 读不懂时**回退到 1**，所以同一次开机里编号**可能重复** | 没有自己的期限，跟着租约走（`TTL_MAX_S = 300`，Agent 续期期间一直有效）；全部急停时作废 | `mcp_kvm.human_action` → `LeaseStore.take` | 只能 `kvm_reject` |
| PXE `pxe_request` | `/run/ops-cart/pxe-req.json` | 一次开机内递增 | `REQ_TTL_S = 600`，最多 3 条 | `mcp_pxe.human_confirm(iid, typed, shown_digest)`：要照抄车机屏幕上的 6 位码；再重算清单，跟申请时不一样就拒 | 不显示，也不能确认或拒绝 |

跟远程审批直接相关的事实：

- **进程权限**：`deploy/systemd/webkiosk.service` 以 `ops-web` 运行，`SupplementaryGroups=dialout video`，`AmbientCapabilities=CAP_DAC_READ_SEARCH`（为了读 root 0600 的 PIN 文件和状态文件）。`InaccessiblePaths` 现在只遮了 `/etc/shadow`、`/etc/gshadow`、`/root`。这意味着 ops-web **读得到推车上几乎所有文件**；它又在 `dialout` 组里，能连 `kvm_broker` 的 socket（`kvm_broker.GROUP = "dialout"`）和 conbroker。`deploy/systemd/cart-mcp.service` 以 root 运行。
- **身份**：车机确认的身份是 `confirm_core.BY_KIOSK = "human:kiosk@local"`；命令行的 `confirm`、`resume` 要过 `mcp_serial.local_human`。网页的身份是 `web:<ip>/<session_tag>`（`webkiosk.App._serial_write_op`），PIN 全车共用，**分不出是哪个人**。
- **会话**：`web_core.Sessions(ttl_s=12*3600, max_age_s=7*24*3600)`，空闲 12 小时滑动过期，最长 7 天；cookie 是 `HttpOnly; SameSite=Strict`。
- **网页会话本来就有"人"的权限**：`/api/serial/send` 往 conbroker 的人用通道写**原始字节**，`/api/hid`、`/api/type` 直接打键，都不过闸门。
- `webkiosk` 没有发 `X-Frame-Options`，也没有 CSP 的 `frame-ancestors`。
- 飞书目前只能往外推（`notify_core.KINDS`，用 `urllib`），没有任何回调入口。
- `label_print` 批量打印时要带 `confirmed`（"人看过样张后确认的原话"），这句话是 **Agent 自己填的**。
- **闸门已知的缺口**：#64，键鼠闸门只查最后一行，`kvm_key` 逐个按键不计入，secret 的前半截不计入；#65，串口挂起期间 `Book.begin` 不查同槽位有没有挂起项，Agent 能继续直发、改掉设备的上下文。

## 二、挂起项的统一模型

### 2.1 字段

`approval_core.collect(serial_view, kvm_view, pxe_view, boot_id, now) -> [Pending]`。这是纯函数，输入是三处现有 `view()` 或 `peek()` 的结果（PXE 用不带确认码的视图）。

| 字段 | 来源 | 说明 |
|---|---|---|
| `id` | `f"{kind}:{boot_id[:8]}:{native_id}"` | 三处的编号每次开机都从头数，带上 boot_id，重启前的卡片就批不中重启后同号的新挂起项。键鼠编号在同一次开机里也可能重复（见第一节），所以**任何决定（含拒绝）都必须同时带 digest** |
| `kind` / `tool` | `serial` / `kvm` / `pxe`；条目自己的 kind | |
| `agent` | 条目的 `who` | MCP 令牌名，只是个名字 |
| `target` | 串口：槽位号；键鼠：键鼠序列号；PXE：MAC 列表 | 以槽位号或 MAC 为准；提示符上认出的设备名来自设备输出，算不可信，单独放在 `target_label` |
| `action` | 串口：`cmd` 原文；键鼠：从上次回车起的**整段**输入（`line` 加上本次的 `text`/`keys`/`enter`）；PXE：`purpose` 加上 `plan_lines` 算出的清单 | 由服务端从真实参数生成，见 2.3 |
| `context` | 串口：挂起时该槽位的**发送序号** `ctx_seq`，以及设备最近 20 行输出；键鼠：挂起时的截图哈希 `shot_sha` 和截图本身 | 用来把批准绑定到"当时的设备状态"，见 2.2；设备输出和截图都标不可信 |
| `danger` / `gate_reason` | 条目的 `danger`、`reason` | 闸门自己的结论（只是减速带的判断） |
| `model_reason` / `intent` | `gate.model.reason`、`gate.intent` | 分别是模型意见、Agent 自述（**没有核实过**），只作补充展示 |
| `tier` | 第三节 | |
| `created` / `expires` | `ts`；`expires = min(原生过期时间, created + TIER_TTL[tier])` | |
| `digest` | 2.2 | |
| `status` | 原生状态映射过来 | 2.4 |

### 2.2 摘要哈希：看到的就是批的，批的时候现场没变

```
digest = sha256(canonical_json({id, kind, target, action 原始字段, danger, created, ctx_seq | shot_sha}))[:16]
```

- **批准和拒绝都必须带 `(id, digest)`。** root 执行器在锁里重新 `collect` 一遍，对不上就拒（`refused: changed`）。PXE 原有的二次核对（`human_confirm` 重算清单再比 digest）照留。
- **串口上下文绑定（对应 #65）**：`Book` 给每个槽位加一个 `send_seq`，每发出一条（直发或者人确认后发）就加 1，挂起时记下 `ctx_seq`。`Book.confirm` 时如果 `send_seq != ctx_seq`，就作废这一条（`dropped: context changed`）。**两种修法取一种**：一是 #65 那样在有挂起项时禁止直发，二是在挂起项上绑定 `ctx_seq`。前一种更简单，本设计倾向前一种；两种都能保证"批准时设备停在挂起时的那个位置"。不管选哪种，摘要里都附上挂起时设备最近 20 行输出，**标为设备输出，不可信**，给人自己核对当前的视图或接口。
- **键鼠截图绑定**：挂起时截一张图，存下哈希，摘要里附上这张图。L3 在批准前还要看**实时截图**（网页直接显示；飞书卡片里放一张执行器在推送时截的图，并标明截图时间）。键鼠没法像串口那样数"发送序号"，人用通道（车机、网页）也会动键鼠，所以截图是唯一的现场证据。
- 这个 digest **不是凭据**：知道它也批不了，还要过通道认证和第二因素。它和 `pxe_req_core` 里那份秘密的清单 digest 是两回事，互不混用。

### 2.3 摘要必须由服务端生成

各通道展示的内容只能来自 `approval_core.render(pending)`，**不接受 Agent 传进来的任何描述性文字**：

```
标题:  [L3] 槽位 2 串口 · 高危词 save
动作:  save            ← 原文, 等宽, 不截断 (超过 200 字符: 卡片显示头 80 + 尾 80 + 总长, 要去网页看全文)
目标:  槽位 2 (提示符上的名字: <Core-SW>, 来自设备输出)
现场:  挂起时设备最近输出 (设备输出, 不可信) ……20 行……
来源:  Agent claude-laptop · 2 分 13 秒前 · 剩 7 分 47 秒
闸门:  带高危词 save, 要人确认
模型意见: (没开)
Agent 自述(未核实): 保存刚才的 VLAN 修改
digest: 3f9a1c0e5b7d2a46
```

- 命令和文字在入口已经限定成可打印 ASCII（`serial_write_core.check_line`；键鼠只收 ASCII）。渲染时空格显示成 `␠`，回车和换行都明确写出来：键鼠的多行输入按行展开，每个 `\n` 标成"↵ 回车"。
- 秘密内容只显示长度（沿用 `mcp_kvm._shown`）。按现在的代码，secret 内容一旦带高危词就直接拒绝，不会挂起；但 #64 指出 secret 的前半截不计入闸门，所以**渲染时也不假设挂起项里一定没有 secret**。
- 标题和"动作"行只由 `kind`、`target`、`action`、`danger` 拼出来。`intent` 和 `model_reason` 放在最后，带前缀，各自截到 200 字。

### 2.4 状态机（统一视图）

```
              ┌─────────── 原生状态 (Book / LeaseStore / pxe Book) 是唯一事实 ────────────┐
pending ──批准(通道认证 + 本级因素 + digest 一致 + 未过期 + 上下文没变)──► executing ──► done(sent / refused / timeout / error / armed)
   ├──拒绝(任一已认证通道, 要带 digest, 不要第二因素)──► rejected
   ├──到 expires──────────────────────────► expired   (远程不能再批; 车机照原生规则)
   ├──急停 / 租约没了 / 原生作废 / 上下文变了──► dropped
   └──被另一个通道先处理──────────────────► 按原生状态显示, 附上"由 <账号> 经 <通道> 处理"
```

`approval_core` 不存状态，只存决定：`/run/ops-cart/approvals.json`（root 0600，flock）里放每条的决定记录（通道、账号、因素、nonce、结果），以及每个 TOTP 账号的 `last_step`、输错计数和冷却时间。种子不放在这里。

### 2.5 幂等、并发、急停

- **一个执行器，一把锁。** 远程的批准和拒绝都由 root 的 `cart-approve.service` 执行（oneshot，由 `cart-approve.path` 盯着请求目录拉起，写法照 `cart-serial-req`）。执行器先拿 `approvals.json` 的 flock，再调原生入口：串口 `mcp_serial.send_confirmed`；键鼠走 `LeaseStore.take`，再经代理发送（抽一个跟 `mcp_kvm.human_action` 共用的函数）；PXE 走新增的 `mcp_pxe.remote_confirm`。原生入口自己在 flock 里会再查一遍。车机确认和远程批准同时到达时，**先拿到原生锁的生效**，后到的记 `lost_race`。
- **nonce 去重**：同一个 nonce 只执行一次。同一条挂起项上重复同一个决定，返回 `already`；先后两个相反的决定，后到的返回 `conflict`。
- **急停：每个通道有自己的目录，各由一个独立的单元处理。** 网页仍走 `/run/ops-cart/serial-req/`（ops-web 0700）→ `cart-serial-req`；飞书新增 `/run/ops-cart/estop-req/feishu/`（飞书服务用户 0700）→ `cart-estop-feishu.path/.service`。两者都用 `confirm_core.parse_request` 解析，只认 `estop`、`reject`、`kvm_reject`。审批执行器可能正卡在一条 30 秒的串口发送上，急停**不经过它**，也就不用等它。
- **最后一道**：串口靠 conbroker 写串口前查 `serial-estop.json`，键鼠靠 `kvm_broker` 每发一步前读标记。所以批准已经生效、字节还没写出去的那段时间里按急停，照样能拦下来。
- 执行器每一轮先处理拒绝，再处理批准；同一轮里同一条既有批准又有拒绝，拒绝优先。

## 三、风险分级

### 3.1 级别和各通道要求

| 级别 | 含义 | 车机（现场） | 飞书卡片（只在手机上点） | 网页（tailnet） |
|---|---|---|---|---|
| L0 | 只读；或者往"停"的方向（急停、拒绝、收紧） | 不用审批 | 不用审批 | 不用审批 |
| L1 | 不碰被测设备配置，最坏是废张标签、白唤醒一台机器 | 直通 | 直通 | 直通 |
| L2 | 改设备状态，但一般能撤回 | 点"确认" | 白名单账号点卡片 | **每次都要 TOTP**（提权窗口默认 0，见 4.2） |
| L3 | 可能中断业务、丢配置 | 点"确认"，高危词再问一次 | 白名单账号点卡片，并填 **TOTP**；卡片上带挂起时截图（键鼠） | 新的 **TOTP**；键鼠要看实时截图 |
| L4 | 清盘、整批覆盖、进一个"之后什么都能做"的环境 | 照抄屏幕确认码 | **不给批准**，卡片上只有拒绝和急停 | 新的 **TOTP**，再**复述目标**（MAC 末 4 位或台数） |

- 同一个账号 30 秒内只能批一条 L3 或 L4。界面上写明原因："防连点：30 秒后才能批下一条高风险操作"。
- 远程解除急停建议按 L3 处理（待拍板第 4 条）。

### 3.2 现有工具逐个定级

只读工具（L0）：`list_threads`、`read_output`、`screenshot`、`cart_status`、`pxe_status`、`pending_list`、`kvm_status`、`screen_ocr`、`pxe_clients`、`pxe_requests`、`label_preview`、`label_printer_status`、`label_abbrev_propose`。

动手工具：

| 工具 / 情形 | 现状 | 建议级别 | 理由 |
|---|---|---|---|
| `estop`、`pxe_retire` | 直通 | L0 | 停、收紧 |
| `lease_*`、`kvm_lease_*` | 直通 | L1 | 不发字节 |
| `wol_send` | 直通，只从 LAN1 发 | L1 | 拿到装机菜单还得另外过 PXE 解锁 |
| `label_print`（单张或批量） | 直通；批量时 `confirmed` 由 Agent 自填 | L1 | AI 替人作证，因为风险低才接受（待拍板第 7 条） |
| `send_command` 第 1、2 层放行 / `send_key enter,space` | 直通 | L1 | 不变 |
| `send_command` 挂起、不带高危词 | 车机确认 | L2 | 配置命令多数能 `undo` 回去；靠 2.2 的上下文绑定保证"批的就是这个视图下的这条" |
| `send_command` 挂起、带高危词（`sw.DANGER`） | 车机确认，再问一次 | L3 | 写启动配置、重启、擦除 |
| `send_key ctrl_c` | 车机确认 | L2 | 打断 |
| **键鼠：凡是挂起的，默认 L3** | 车机确认 | **L3** | 推车看不懂屏幕，闸门能绕过（#64），焦点在哪也不知道。所以不按"词有多危险"往下降级，只把**明确无害**的单独列出来降到 L2 |
| 键鼠：明确无害、降到 L2 的 | — | L2 | 白名单 `KVM_L2 = {组合键 alt+f4}`，此外暂时为空。**不按 `sw.danger_words` 降级**：通用词表里本来就有 format、rm、dd、mkfs、fdisk、wipefs、shutdown、reboot、poweroff、init，跟 `mcp_kvm.DANGER_SUB` 有重叠，"只命中通用词"这个条件既判断不出来，也不安全 |
| `kvm_key ctrl+alt+delete`、`alt+sysrq` | 车机确认 | L3 | 重启、打到内核 |
| `pxe_request winpe` | 车机照抄码 | **L4（倾向）** | 按后果看：进了 WinPE，之后用键鼠什么都能做（diskpart 清盘），而键鼠闸门拦不住（#64）。批准 WinPE 实际上就是批准"之后可能清盘" |
| `pxe_request clone-save` | 车机照抄码 | L3 | 只读目标盘，写推车，被测机会重启 |
| `pxe_request rocky`、`clone-restore` | 车机照抄码 | L4 | 清盘、整批覆盖 |
| 解除急停 | 只认车机本机 | 远程 L3 | 待拍板第 4 条 |

分级放在代码常量 `approval_core.TIER` 里，**只增不减**；现场配置 `/etc/ops-cart/approval.json` 的 `raise` 只能提级。

### 3.3 第二因素选型

| 方案 | 人不在现场能不能用 | 跟通道独立吗 | 谁校验 | 能不能绑定到某一条挂起项 | 结论 |
|---|---|---|---|---|---|
| 车机屏幕一次性码 | 不能（网页上看到的是被测机画面，不是车机屏幕） | — | root | 能 | 只留给现场 |
| 飞书二次确认 | 能 | 不独立（同一账号、同一部手机） | 飞书服务 | 不能 | 只防误点，不算因素 |
| 网页 PIN 重输 | 能 | 半独立 | ops-web（能离线爆破哈希） | 不能 | 弱 |
| **TOTP** | 能 | 独立 | **root** | **不能**：码只代表"这 30 秒里有人在场" | **推荐**（L2、L3） |
| WebAuthn / 通行密钥 | 能 | 独立，而且防钓鱼 | root | **能**：挑战里带上 digest，签名只对这一条有效 | 最强；要先给网页上 HTTPS。**L4 的长期方案** |

TOTP 的实现约束：

- 只用标准库（`hmac`、`hashlib.sha1`），30 秒一步，容许 ±1 步。种子按人分开。
- **每个码只能用一次，而且跨通道也只能用一次**：`last_step` 按账号记，不按通道记。同一个码在网页上用过，再拿到飞书上用会被拒。
- **时钟回拨**：`last_step` 只增不减。时钟往回调以后，码对应的步数小于等于 `last_step` 一律不认，直到时钟追上来为止。这样做宁可暂时用不了，也不放宽。执行器发现当前步数比 `last_step` 小 2 步以上时，往 `events.log` 记"时钟可能回拨"。
- **输错退避**：参照 `kiosk_core.PinGate`，连续错 5 次锁 60 秒，之后每次翻倍，封顶 1 小时。**按账号算，跨通道共享。** Agent 想故意输错把人锁在外面，也只能锁到冷却时间为止；而且**车机上的确认不受 TOTP 冷却影响**。每次进入冷却都往飞书推一条通知。
- 缺点：码不绑定具体挂起项（H2），手机丢了两个因素一起丢（T1）。

## 四、认证与防伪

### 4.1 共同规则

- **种子单独放**：放在 `/etc/ops-cart/approve-secrets/`（root 0700，里面的文件是 0600）。`webkiosk.service`、飞书服务的单元、`cart-estop-*` 单元**都加上** `InaccessiblePaths=/etc/ops-cart/approve-secrets`（`webkiosk` 有 `CAP_DAC_READ_SEARCH`，单靠文件权限挡不住它）。飞书的 app_secret 放在飞书服务自己的 `LoadCredential=` 里，不和种子放在一起。部署文档写明这几条，A2 用测试读单元文件来断言。
- **审批请求按通道分目录**：`/run/ops-cart/approve-req/web/`（ops-web 0700）、`/run/ops-cart/approve-req/feishu/`（飞书服务用户 0700）。root 按**所在目录**认通道，请求里写的 `channel` 一律忽略。写入、读取的加固照抄 `mcp_serial.write_request` 和 `_take_request`。急停目录见 2.5。
- 请求格式（`approval_core.parse_request`，字段多一个或少一个都不认）：
  ```json
  {"op": "approve|reject|elevate|lockdown", "id": "serial:1a2b3c4d:17", "digest": "3f9a1c0e5b7d2a46",
   "account": "alice", "by": "web:192.0.2.20/ab12cd", "totp": "123456", "retype": "a1b2",
   "nonce": "9c2e…(16 hex)", "t": 1700000000.0}
  ```
  `reject` 也必须带 `digest`。
- `serial-req` 目录维持原样，只认急停和拒绝，"网页请求里出现 confirm 就当坏请求"这条老不变量不变。
- **执行回执走另一个通道**：每次远程批准执行完，root 都把"实际执行的摘要、digest、结果、经哪个通道、哪个账号批的"推送到**另一个**通道（网页批的推飞书，飞书批的在网页审批页常驻显示，并写进 `events.log`）。这样被拿下的服务偷偷换了批准对象（H2），本人能在另一处发现。
- **审批链接不由 Agent 提供**：人从自己收藏的网页地址进审批页，或者用飞书卡片（卡片是推车直接推给飞书的）。`approval_list` 只返回编号，不返回可点的链接。
- secret 一律只显示长度：`cart_mcp.py approval-check` 打印 `app_secret: <38 字符>`、`totp seed[alice]: <32 字符>`。日志、审计、卡片里都不出现码、种子、cookie、卡片 token。

### 4.2 网页（webkiosk，tailnet）

- 身份：会话，加登录时选的账号名，加 TOTP。光有会话什么也批不了。推车没设 PIN 时审批页直接 403。
- **提权窗口默认 0**：每次批准都要输码。可以配成 N 分钟内的 L2 免码（`op=elevate` 由 root 校验，把**（会话标签, 账号）**写进 `approvals.json`，网页只读这张表）。窗口里只允许用当初提权的那个账号批。默认关掉的理由见 T12：Agent 可能驱动维护者已经登录的浏览器。
- 会话时长现在是空闲 12 小时滑动、最长 7 天。登录加 TOTP（待拍板第 3 条）能挡住"Agent 用 curl 自己登录"，**挡不住 Agent 驱动一个已经登录的浏览器**。后一种只能靠"每次批准都要新码"。
- 防 CSRF：保留现有的同源检查和 `SameSite=Strict`；批准表单带一个服务端签发、只能用一次的 nonce（绑定会话和挂起项 id，2 分钟过期）；跨站请求本来也伪造不出 TOTP。
- 防点击劫持：加 `X-Frame-Options: DENY` 和 `frame-ancestors 'none'`，**作为纵深防御**。cookie 本来就是 `SameSite=Strict`，被跨站嵌进 iframe 时浏览器不带 cookie，所以主要的防线仍然是 cookie 和 TOTP。
- tailscale 的身份（whois）认不出"人还是这台设备上的 Agent"，不当认证，最多作为审计的附加字段（待拍板第 8 条）。

### 4.3 飞书卡片

- **专用应用**：审批用的飞书应用只给推车用，**不能和任何其他机器人或桥接服务共用凭据**。长连接是集群模式：同一个应用开着几个客户端时，回调会随机分给其中一个，共用凭据就等于把审批回调交给了别人。
- **长连接接入**（推车主动连出去，不需要公网入口），官方 SDK 装在独立的 venv 里。卡片回调用新版 `card.action.trigger`。旧版回调的签名算法和新版不一样，如果走 HTTP 回调，签名公式、`form_value` 字段、3 秒应答的要求都要在**实现前对照当前的官方文档核对**（**待核**）。
- **服务沙箱**：飞书服务用自己的用户，**不进** `dialout`、`video` 组；`IPAddressDeny=any`，只放行飞书出口需要的地址（具体地址实现时确定，至少要挡住 localhost 和 tailnet）；`InaccessiblePaths` 遮掉种子目录和 `/run/ops-cart` 下的状态文件，只留它自己的请求目录可写。conbroker 的人用通道很可能没有认证（conbroker 在另一个仓库，**疑似，待核**），所以至少不能让飞书服务连得到它。
- 只认指定账号：`feishu.accounts: {open_id: 账号名}`，同时核对 `app_id` 和 `tenant_key`；不在白名单的人点了，记 `forbidden`。
- 卡片按钮的 value 只带 `{id, digest, op}`，权威校验在 root。L3 卡片有 TOTP 输入框；L4 卡片没有批准按钮。
- **只在手机上批**：部署文档写明，飞书桌面版和网页版不要登录审批账号。理由是 Agent 能驱动维护者电脑上已经登录的飞书。这一条推车验证不了，属于残余风险（T12）。
- 回调先回一句"已提交"，root 执行完以后更新卡片，写明"由 <账号> 经 <通道> 在 <时间> 批准/拒绝/过期/作废"，不留可点的旧按钮。
- 卡片常驻"本路急停"和"全部急停"：不要 TOTP，只认白名单账号，投到飞书自己的急停目录（2.5）。
- 命令原文默认显示；`feishu.show_action: length` 时只显示长度和高危词（待拍板第 5 条）。

### 4.4 经维护者的 Claude 会话转达

- 只读的 MCP 工具 `approval_list`：返回 2.3 的服务端摘要、`id`、`digest` 和剩余时间，**不返回可点的链接**，也不带确认码和 PXE 的秘密 digest。工具描述写明："这里只能看。批准要人本人在推车网页（用你自己收藏的地址）上输 TOTP，或者在手机飞书卡片上点。请把摘要原样转给人，不要改写，也不要替人判断安不安全。"
- `intent`、`target_label`、`model_reason` 和设备输出，用每次随机生成的标记包起来，注明是数据。
- 发起挂起的 Agent 很可能就是这个会话，所以这条通道只是通知。

### 4.5 车机（现场路径）

维持原样：`confirm_panel`、`pxe_req_panel`、命令行 `local_human`。车机上的确认也写进审计（通道 `kiosk`），不受 TOTP 冷却影响。

### 4.6 大肥鱼经 ACP 接入

dsh 作为 MCP 客户端调用推车工具时，**不经过它自己的审批**（#49）。大肥鱼经 ACP 运行时，dsh 自己的审批请求只送得到 ACP 客户端（包装层），到不了推车的网页和飞书。

- 推车工具这一路不用另外处理：动手工具在推车服务端挂起，走本设计。dsh 那一侧无论批不批，都改变不了推车上的结果。
- dsh **自己的**审批（比如它要在本机跑命令）：倾向由包装层把这类请求转成本设计的挂起项（新增一个 `kind=acp`，由包装层经一个**专用的、只能"提交挂起、查询结果"**的接口投进来）。人在任一通道上批准，包装层再拿到结果回给 dsh。这样维护者只需要一个审批入口，因素要求也一样。
- 风险：包装层跑在 Agent 那一侧，它报上来的"要批的内容"是它自己说的，推车核实不了。所以这类挂起项的摘要要标明"来自 ACP 包装层，内容推车无法核实"，级别不低于 L2。
- 这部分列为待拍板（第 13 条），放到 A4 以后。

### 4.7 部署前提（每个阶段的 PR 都要在说明里重申）

1. 跑 Agent 的机器上不能有到推车的 root 通道：不配 `ssh cart sudo …` 那种 MCP 接法，不放能 sudo 的 ssh 密钥。远程的 Agent 只走 `cart_mcp.py http` 加 Bearer 令牌。现在 `cart-mcp` 是 root 进程，所以这个隔离**只靠代码不变量**（MCP 工具里没有审批写路径）。长期把它降为非 root，跟 #49 的只读、动手分拆一起做。
2. 推车设了 PIN；PIN、TOTP 种子、飞书凭据都不放在跑 Agent 的机器上。
3. 审批用的飞书账号只在手机上登录，审批应用专用。

## 五、分阶段落地（每阶段一个可以单独合并的 PR）

| 阶段 | 内容 | 前置 |
|---|---|---|
| **A0** | 本设计稿 | — |
| **A1 统一视图（只读）** | `approval_core.py`（`collect`、`TIER`、`render`、`digest`）；`cart_mcp.py approvals`；网页只读的审批页；`approval_list` | #64、#65 合并（分级和上下文绑定要建立在修过的闸门和 Book 上） |
| **A2 执行器 + TOTP + 网页批准** | `totp_core.py`；`totp-enroll`（只认 `local_human`）；种子目录和各单元的 `InaccessiblePaths`；`mcp_approve.py`；`mcp_pxe.remote_confirm`；`cart-approve.*`；串口 `send_seq` / `ctx_seq`（如果 #65 没有采用"禁止直发"）；键鼠挂起时截图；网页批准、nonce、`X-Frame-Options`；执行回执 | A1 |
| **A3 飞书** | 通知器、长连接回调服务（专用应用、沙箱）、白名单、卡片 TOTP、截图、飞书急停目录和单元、`lockdown` | A2 |
| **A4 防疲劳 + 远程解除急停 + ACP** | 频率限制、30 秒间隔、L4 复述、每日汇总；remote resume；`kind=acp` | A3，按拍板结果 |
| **A5 文档收尾** | 更新 `DESIGN_agent_mcp.md` 第十一、十二、十五节，`webkiosk.py`、`confirm_core.py`、`mcp_kvm.py` 文档串，README | — |
| **长期** | `cart-mcp` 降为非 root（#49）；网页上 HTTPS 和 WebAuthn，L4 用挑战绑定 digest | — |

测试清单（先写红灯）：

**A1**
- `collect`：三处视图的样例（空的、读不懂的、急停中的）。读不懂一律当"没有可批的"。
- `id` 带 boot_id；键鼠编号重复（模拟 `read_state` 回退）时，两条的 digest 不同。
- `digest`：字段顺序不影响；改一个字符、换槽位、换 MAC、换 `ctx_seq`、换 `shot_sha`，digest 都变。
- `render`：标题和"动作"行里没有 intent；多行键鼠输入每个 `\n` 都显示成回车；空格可见；超长时显示头尾和总长；设备输出标不可信；secret 只显示长度。
- `TIER`：3.2 表逐行一条用例，**特别覆盖**键鼠的 `format c:`、`rm -rf /`、`dd if=`、`mkfs`、`fdisk`、`wipefs`、`shutdown`、`reboot` 都是 L3，`alt+f4` 是 L2，`winpe` 是 L4；配置里的 `raise` 只能提级。
- 不变量：整套 MCP 工具配上各种参数（偷来的 id、digest，多余参数 `totp` `approve`），断言原生状态里没有一条变成 sending、armed 或 confirmed；`approval_list` 的返回里没有 `http` 链接。

**A2**
- `totp_core`：RFC 6238 附录 B 的向量；±1 步能过，±2 步不过；同一个码第二次被拒；**同一个码网页用过后在飞书上被拒**；**时钟回拨**以后，小于等于 `last_step` 的码都被拒，并记日志；退避 60、120、240 秒……封顶 1 小时，冷却按账号跨通道共享；冷却期间车机确认照常能用；种子读不懂一律拒。
- **服务读不到种子**：读 `deploy/systemd/webkiosk.service`、飞书服务、`cart-estop-*` 的单元文件，断言 `InaccessiblePaths` 里有种子目录；飞书服务单元里没有 `dialout`、`video`，有 `IPAddressDeny`。
- `parse_request`：多字段、少字段、类型不对、超长、超时、未来时间、链接、目录，一律不认；`channel` 以目录为准；`reject` 不带 digest 不认。
- 因素矩阵：L2/L3/L4 × 网页（无码、错码、旧码、提权关、提权开但换了账号）× 车机 × 飞书。
- 30 秒内同一个账号的第二条 L3 被拒。
- **上下文变化作废**：串口挂起后直发了一条（或者被 #65 拒掉），批准时 `dropped: context changed`，一个字节都不发；键鼠挂起后截图哈希变了，L3 批准被拒，要重新看实时截图。
- 并发：车机确认和远程批准交错，只发一次，后到的记 `lost_race`；同一个 nonce 只执行一次；先批后拒得到 `conflict`。
- **执行器阻塞时急停照常生效**：假的 `send` 阻塞在执行器里，这时网页投急停，由 `cart-serial-req` 独立处理并写好 `serial-estop.json`，断言不用等执行器。
- 键鼠拆分（跟 #64 的修复对齐）：多行文本、逐个按键拼出高危词、secret 拆成几段，审批摘要里显示的都是整段。
- `remote_confirm`：TOTP 没过就拒；清单变了就作废；更新 `test_pxe_invariant.py`，改成"没有照抄确认码，也没有 root 校验过的 TOTP，就不能解锁"。
- 网页：响应头里有防点击劫持的头；nonce 只能用一次，也不能跨会话用；执行回执写进了另一个通道的队列。

**A3**
- 卡片：L4 卡片没有批准按钮；L3 卡片有 TOTP 输入框和截图；`show_action=length` 时找不到原文。
- 回调：白名单之外的账号、`app_id` 或 `tenant_key` 不对、事件重复，都按预期处理。
- 被别的通道处理掉、过期、作废之后，卡片上没有能点的按钮。
- 飞书急停投到自己的目录，由独立单元用 `confirm_core.parse_request` 处理；审批执行器阻塞时照样生效。
- `lockdown` 只能远程关，开回来只认 `local_human`。

**待实机验证**：path 单元能不能被拉起；`InaccessiblePaths` 生效后 webkiosk 原有功能（读 PIN、读状态）是否正常；飞书长连接断线重连、卡片在手机上的显示；车机 NTP；飞书回调字段和签名（对照官方文档）；conbroker 人用通道的认证情况（另一个仓库）。

## 六、威胁模型

| 编号 | 威胁 | 后果 | 对策 / 残余风险 |
|---|---|---|---|
| **T0** | Agent 和人同在 tailnet 上 | 没设 PIN 时，Agent 用 curl 就能登录网页，再用 `/api/serial/send`、`/api/hid` **绕过所有闸门**（现在就有的问题） | 远程审批要求设了 PIN；建议登录也加 TOTP（待拍板第 3 条）；PIN 和种子不放在 Agent 机器上 |
| T1 | 手机丢了 | 飞书和验证器一起丢 | 手机锁屏，验证器开生物识别锁；`lockdown` 任一已认证通道都能一键关掉远程审批，开回来只能在车机上；`approval-revoke <账号>` 作废种子和 open_id |
| T2 | 飞书账号被盗（手机还在） | 能批 L2，看得到原文 | L3 及以上要 TOTP；执行回执推到另一通道；L2 有频率上限，超了自动升级；`show_action=length` 可选 |
| **T3** | **ops-web 或飞书服务被拿下** | ① ops-web 本来就在 `dialout`，能直接写 CH9329（经 `kvm_broker` 的 socket）和 conbroker 的人用通道，**根本不用审批**；② 能把人刚输的 TOTP 码改投到另一条 L3 或 L4（码不绑定挂起项）；③ 能伪造飞书 L2 的点击 | **残余风险，如实写**：①是现有的权限问题，本设计不解决（长期要把"人用通道"和网页进程分开，另开 issue）；②靠执行回执走另一通道（事后发现）和码只能用一次（窗口只有一次）缓解，**L4 的根本解决办法是 WebAuthn 一类的挑战绑定 digest**；③可以拍板把飞书限制成只能拒绝和急停（第 2 条）。飞书服务不进 `dialout`，所以只剩②③ |
| T4 | 提示词注入，让 Agent 生成诱导性的摘要 | 人照着摘要批了 | 摘要由服务端从真实参数生成；intent 标未核实、不进标题；级别按后果定；设备输出和 OCR 都标为数据；Claude 原样转达 |
| **T5** | 把危险操作拆开，或者先挂起一条、再改掉上下文 | 批的是 A，生效的是 B | **不依赖闸门**：#64 修键鼠闸门的拆分绕过，#65 修串口挂起期间直发；本设计在此之上用 `ctx_seq` 和截图哈希把批准绑定到现场，变了就作废；键鼠挂起一律 L3，要看实时截图 |
| T6 | 批准疲劳 | 习惯性地点 | 没有"全部批准"；L2 默认也要码；同一账号 30 秒只能批一条 L3/L4；L4 复述目标、只能在网页批；同类请求计数，超了自动升级；每日汇总 |
| T7 | 重放 | 旧的批准再次生效 | boot_id、digest（拒绝也要带）、nonce、TOTP 按账号只能用一次、请求只认 60 秒内的、级别期限 |
| T8 | 检查和使用之间被换了（TOCTOU） | 批的和执行的不一样 | 执行器在锁里重算 digest；`Book.confirm` 查 `ctx_seq`；PXE 重算清单 |
| **T9** | AI 审批 AI | 没有人参与，操作就放行了 | 现在**只靠代码不变量**：MCP 工具里没有审批写路径，有不变量测试。`cart-mcp` 是 root，持有 root ssh 或者 root MCP 的 Agent 能绕过一切，所以部署前提（4.7）是这类通道不存在。长期把 cart-mcp 降为非 root（#49） |
| T10 | 时钟漂移、回拨 | 码全错，或者被放宽 | 窗口固定 ±1 步；`last_step` 只增不减，回拨时宁可暂时用不了；车机要有 NTP |
| T11 | Agent 故意输错 TOTP，把人锁在外面 | 远程审批暂时用不了 | 退避封顶 1 小时；车机确认不受影响；急停和拒绝不需要 TOTP；每次进入冷却都推通知 |
| T12 | Agent 驱动维护者已经登录的浏览器或飞书客户端 | 会话、提权窗口、飞书桌面版的点击都能被它用掉 | 提权窗口默认 0；飞书审批只在手机上做（推车验证不了，属于残余风险）；L3 及以上每次都要手机上的码 |

## 七、需要维护者拍板

| # | 问题 | 选项 | 我的倾向 |
|---|---|---|---|
| 1 | 第二因素 | a. TOTP；b. PIN 重输；c. 飞书二次确认；d. WebAuthn | **a**；L4 长期上 d |
| 2 | 飞书能批到哪一级 | a. L2 直点、L3 加 TOTP、L4 不给；b. 只能拒绝和急停；c. 全都能批 | **a**；担心 T3③ 选 b |
| 3 | 网页登录加 TOTP（堵 T0） | a. 登录就要 PIN + TOTP；b. 只在审批时要；c. 只要求设 PIN | **a**（挡不住 T12，T12 靠每次输码） |
| 4 | 远程解除急停 | a. 远程按 L3；b. 只在车机上解除 | **a** |
| 5 | 飞书卡片上显示原文 | a. 显示原文；b. 只显示长度和高危词 | **a** |
| 6 | 飞书接入方式 | a. 长连接 + 专用应用 + 独立 venv；b. HTTP 回调 + 公网中转；c. 不接飞书 | **a** |
| 7 | `label_print` 批量的 `confirmed` | a. 维持现状；b. 改成 L2 挂起 | **a** |
| 8 | 审计带 tailscale 身份 | a. 带；b. 不带 | **b** |
| 9 | 各级远程批准期限 | L2 15 分钟 / L3 10 分钟 / L4 10 分钟，或者统一 10 分钟 | **15 / 10 / 10** |
| 10 | 网页提权窗口 | 0 / 10 分钟 / 30 分钟 | **0**（T12） |
| 11 | 有争议的分级 | WinPE 定 L3 还是 L4；不带高危词的串口配置命令定 L2 还是 L3；键鼠 L2 白名单里除了 `alt+f4` 还放不放别的 | **WinPE L4；串口配置命令 L2；白名单只放 `alt+f4`** |
| 12 | TOTP 种子每人一个还是全车一个 | a. 每人一个；b. 全车一个 | **a** |
| 13 | 大肥鱼经 ACP 时 dsh 自己的审批 | a. 包装层转成本设计的挂起项（`kind=acp`，内容标不可核实，不低于 L2）；b. 只在包装层本地确认；c. 不接 | **a**，放到 A4 以后 |
| 14 | #65 的修法 | a. 有挂起项时禁止直发；b. 挂起项绑定 `ctx_seq`，变了就作废 | **a**（更简单）；b 作为兜底 |
| 15 | ops-web 的 `dialout` 权限（T3①） | a. 另开 issue，把网页的人用键鼠和串口改走需要认证的代理；b. 维持现状，接受残余风险 | **a** |
