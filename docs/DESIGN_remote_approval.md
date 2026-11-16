# 设计：远程审批（issue #50）

推车的定位是"人在办公室，车在机房"。#23、#42、#24 定下的"确认只在车机本机、网页只能拒绝、Agent 没有确认工具"是早期的保守做法：人不在车旁，挂起的操作只能干等。本文把**远程审批定为主路径**，车机确认保留为现场路径，**急停在任何通道都能按、优先级最高**。

本文只是设计，不改代码。**以 `main` 上的代码和 [`docs/REMOTE_APPROVAL.md`](REMOTE_APPROVAL.md) 为准**：维护者 2026-11-07 对第七节拍板以后，`main` 合进了 #77（远程审批第一阶段：`approval_core.py`、`totp_core.py`、`approval_channel.py`），以及 #64、#65、#70、#49（部分）的实现。本稿第三次修订以后按拍板结论和这些实现同步过；两边对不上时，以代码为准，并回来改本稿。

**本期范围**：车机、webkiosk 网页、飞书（与专用应用的单聊）、Claude 会话转达，以及大肥鱼经 ACP 接入时怎么挂进来（4.6）。issue 里提到的"大肥鱼网页"前端**不在本期范围**，以后要做也只能当本设计的又一个前端：同样的请求目录、同样的因素要求。

**相关 issue**：#64（键鼠闸门的拆分绕过，已修 #67）、#65（串口挂起期间能继续直发，已修 #68）、#69（键鼠高危词按模式补全）、#70（PXE 默认菜单自动进救援，已修 #76）、#49（MCP 拆成只读和动手两个服务：按档位启动已做 #60，降为非 root 未做）。

## 〇、结论先说

1. **不另起一套挂起状态。** 串口 `serial_write_core.Book`、键鼠 `mcp_kvm.LeaseStore`、PXE `pxe_req_core.Book` 仍然是各自挂起项的唯一事实来源。`approval_core`（#77 已在 `main`）负责：把挂起项规整成统一条目（`make_item`）、分级（`tier_of`）、各通道要什么因素（`requirement`）、纯函数 `digest(item)`、服务端摘要和卡片（`render`、`card`，含打码）、请求解析和决定（`parse_request`、`decide`）、决定记录和审计。原生入口在**自己的 flock 里**用 `digest` 比对。
2. **分级和因素按拍板 2**：飞书 L2 **直接点批准**；L3 要 TOTP（键鼠 L3 在飞书上只能拒绝）；**L4 不开放远程批准，远程只能拒绝**，只能车机照抄屏幕码确认。网页 L2 要 TOTP；网页 L3 要等带外码（A3）上线才开。见 3.1。
3. **人要在手机上看清楚，不能只看电脑。** 维护者和 Agent 往往在同一台电脑上，被 Agent 驱动的浏览器可以让页面显示 A、实际提交 B。所以网页上的 L3 批准要用**带外码**：推车把服务端生成的摘要和一个跟（账号, id, digest, nonce）绑定的短码，推到维护者手机上，发在与审批专用应用的飞书单聊里。
   带外码**能挡住**的情况：页面为 A 要码，却把码拿去交给 B。
   带外码**挡不住**的情况：页面显示 A，后台直接为 B 发起 challenge。这时手机上收到的是 B 的摘要，只要人在手机上读一眼就会发现不对。所以推送会单独写一句"如果这不是你刚点的那一条，别填码，直接急停"。
   串口 L3 优先直接在飞书卡片上批，不经网页。见 3.3。
4. **第二因素是 TOTP（拍板 1），但它只证明"有人在场"。** 种子每人一个（拍板 12），只通过 `LoadCredential=` 交给取件单元 `cart-approve`。其他单元都用 `InaccessiblePaths` 遮掉种子目录和 `/run/credentials`（`LoadCredential` 实际落盘的地方）。**对 root 运行的单元来说，`InaccessiblePaths` 不算边界**：root 可以经 D-Bus 或 `systemd-run` 拉起一个不受限制的单元，再去读。`cart-mcp` 和 `cart-mcp-ro` 现在**都以 root 运行**（单元里没有 `User=`），#49 把它们降为非 root 之前，**种子隔离对这两个单元不成立**。`approval-check` 把这一点当作硬性检查项（4.7）。
5. **残余风险如实写。** ops-web 在 `dialout` 组里，能直接写 CH9329 和 conbroker 的人用通道；网页会话最长 7 天，Agent 偷到 cookie 就能直接调 `/api/hid`、`/api/type`、`/api/serial/send`。飞书 L2 不要 TOTP（拍板 2），飞书账号或飞书回调服务被拿下就能批 L2。审计里的 tailscale 身份由 ops-web 转交来源地址，ops-web 被拿下就能冒充。第三节和第六节逐条列出。
6. **批准分两步执行。** 第一步是"取件校验"：秒级完成、从不阻塞，在取到请求的那一刻校验因素。第二步是"执行"：每条批准一个模板实例 `cart-approve-exec@<转义后的 id>`，互不排队。L3 在执行前先推送"即将执行"，留出撤回窗口；推送没送达就不执行。执行前到的拒绝（含撤回）一律**覆盖**批准，执行器在原生锁里看到就不发（2.4）。急停、`lockdown` 走各通道自己的急停目录，不经过执行器。
7. **部署前提有程序检查。** `cart_mcp.py approval-check` 检查 PIN 已设、各单元的遮挡真的生效、root 登录面、种子权限位。任何一项不合格，就**拒绝打开远程审批**。
8. **Claude 只转达。** 它只能看到挂起项的服务端摘要和编号，拿不到链接，拿不到码，也没有任何能批准的工具。所有界面上都写明："**任何人（包括 Claude）向你要验证码，都是攻击。**"（`approval_core.SAFETY_LINE`）

## 一、现状（读 `main` 代码确认）

| 挂起源 | 状态文件 | 编号 | 过期 | 人确认入口 | 网页 |
|---|---|---|---|---|---|
| 串口 `send_command` / `send_key` | `/run/ops-cart/serial-write.json`（`mcp_serial.Store`，flock） | `doc["seq"]`。`Book.from_text` 读不懂时从 0 重新数，同时全部急停 | `PENDING_MAX_S = 900`；租约过期、释放、急停时作废 | `human_action("confirm")` → `send_confirmed` → `Book.confirm(iid, by, now, current)` | 只能 `estop` / `reject` |
| 键鼠 `kvm_key` / `kvm_type` 挂起 | `/run/ops-cart/kvm-lease.json`（`LeaseStore`） | `st["next"]`。`read_state` 读不懂时回退到 1 | 跟着租约走（`TTL_MAX_S = 300`）；全部急停时作废 | `mcp_kvm.human_action` → `LeaseStore.take(iid, by)` | 只能 `kvm_reject` |
| PXE `pxe_request` | `/run/ops-cart/pxe-req.json` | 递增；读不懂时也从头数 | `REQ_TTL_S = 600`，最多 3 条 | `Book.take(iid, typed, shown_digest, now, stopped)`：照抄车机屏幕上的码，再重算清单比对 | 不显示 |

其他事实：

- **远程审批第一阶段（#77）**：`approval_core`、`totp_core`、`approval_channel` 是纯逻辑和测试，**没有接原生 Book、没有接飞书、没有改单元文件**。`OPS = ("approve", "reject")`；挂起项的 `epoch` 还没加进三处 Book。
- **串口现场绑定（#65，#68）**：`Book.submit` 挂起时记下 `sent_mark`（槽位的发送序号）和 `tail_hash`（挂起时设备输出最后几行有效行的整体指纹，删转义、NFKC、滤掉 conbroker 标记行）。`Book.confirm(iid, by, now, current)` 要调用方传入**确认前刚读的设备输出** `current`：`sent_mark` 变了，或者 `_tail_hash(current)` 跟挂起时不一样（人用通道敲过键、迟到的字节、设备自己吐的日志），这条就作废；`current` 读不到就不发、继续挂着。另外，槽位有挂起项时 `Book.begin` 一律拒绝直发，`submit` 也只收打断键。
- **键鼠现场绑定（#64，#67）**：`kvm_broker` 在内存里按键鼠记 `human_n`（人用通道有效输入的次数），`status` 一次报出全部，带一个启动 id `boot`（代理重启后 `human_n` 从 0 重数）。`mcp_kvm.human_seq(port)` 返回 `[boot, human_n]`，代理不在时返回 `None`（`mcp_kvm.py` 764 行附近）。`LeaseStore.gate` 发现跟上次看到的不一样就把这一行当成"看不清"。闸门按行、逐键、secret 都计入了。
- **PXE 默认菜单（#70，#76，已关闭）**：默认菜单超时后从本地盘启动，救援（`inst.rescue inst.sshd`，root）只能手选；`pxeserver.rescue_default_menu` 拒发"默认项是救援"的菜单。GRUB 菜单没有设密码。
- **进程权限**：`webkiosk.service` 以 `ops-web` 运行，`SupplementaryGroups=dialout video`，带 `AmbientCapabilities=CAP_DAC_READ_SEARCH`，`ProtectSystem=strict`。`cart-ocr.service` 是 `DynamicUser=yes`。**其余单元都以 root 运行**，包括 `cart-mcp`、`cart-mcp-ro`（#60 按档位启动，只读档不注册动手工具，但进程仍是 root）、`kvm-broker`、`kiosk`、`pxe-*`、`cart-dash`、`cart-ble-input`、`cart-fan-curve`、`cart-revtunnel` 等。
- **会话**：`web_core.Sessions(ttl_s=12*3600, max_age_s=7*24*3600)`，空闲 12 小时滑动过期，最长 7 天；cookie 是 `HttpOnly; SameSite=Strict`。tailnet 来源在**没设 PIN 时不用输 PIN**。
- **网页会话本身就有人的权限**：`/api/serial/send` 往 conbroker 人用通道写原始字节，`/api/hid`、`/api/type` 直接打键，都不过闸门。
- **键鼠没有"本路急停"**：`kvm_broker` 只读 `serial-estop.json` 里的 `kvm` / `all` 字段，`kvm` 只在串口全部急停时为真。
- **PXE digest 不是秘密**：`pxe_req_core._digest` 是 `sha256(json)[:16]`，简单清单能从 `[purpose, mac]` 直接推算出来。
- **决策模型**：串口第 2 层（`serial_gate_model`）默认关。打开以后，模型能放行改配置的命令，这就是"模型替人审批 Agent"。
- **还没补的闸门缺口**：#69（`danger_of` 认不出 `rd /s /q`、`> /dev/sda`、`chmod -R 000 /` 这类写法）。

## 二、挂起项的统一模型

### 2.1 字段

`approval_core.make_item(kind, native_id, epoch, target, created, action, danger, ctx, agent, intent, model_reason, target_label, evidence)`（`main` 已有）。`KINDS = ("serial", "kvm", "pxe", "estop_release", "label_batch")`。

| 字段 | 说明 |
|---|---|
| `id` | `f"{kind}:{epoch}:{native_id}"`，格式由 `_ID_RE` 校验。`epoch` 是**状态文件新建时生成的随机 8 位十六进制**（三处 Book 都要加 `doc["epoch"]`，A1 做）。开机、读坏重置都会换一个新的 epoch，重号以后的旧 id 也对不上 |
| `kind` / `agent` | `agent` 是条目的 `who`（MCP 令牌名，只是名字） |
| `target` | 槽位号、键鼠序列号、MAC 或急停范围。为空的 `make_item` 不收，也就不许远程批准。键鼠在**挂起时**就解析出序列号写进条目，不留 `serial=None`。提示符上认出的设备名放在 `target_label`，标为不可信 |
| `action` | 按 kind 的字段白名单校验（`_SCHEMA`，多一个少一个都不收）。键鼠 `type` 带 `prefix`（挂起前已经敲在屏幕上的行前缀），渲染时分开显示"已经敲在屏幕上的"和"批准后才会敲的" |
| `ctx` | 进 digest 的现场绑定，见 2.2 |
| `evidence` | 串口：挂起项里已打码的设备上下文（`Book.submit` 存的 `context`）；键鼠：挂起时的截图。**只给人看，不进 digest** |
| `danger` / `intent` / `model_reason` | `danger` 由 `approval_core.serial_danger` 现算并取并集，不只信调用方；`intent` 是 Agent 自述，没有核实过；`model_reason` 是模型意见 |
| `tier`、`expires`、`digest` | 由 `make_item` 现算；`decide` 也不信条目里带的，按 `created` 重算 |

### 2.2 digest 和现场绑定

```
digest = sha256(canonical_json({"v": 1, id, kind, target, action, danger, created, ctx}))[:16]
```

公式在 `approval_core.digest`，定了以后不改。现场绑定**沿用 #65 和 #64 已有的机制**，不另做计数：

- **串口**：`ctx = {"sent_mark": it["sent_mark"], "tail_hash": it["tail_hash"]}`。人用通道的写入（网页终端、车机终端、其他 conbroker 客户端）不计发送序号，但会有回显，`tail_hash` 兜住；同槽位其他条目发出会让 `sent_mark` 变。没有回显的人用写入兜不住，跟车机确认是同一个残余。
- **键鼠**：挂起时把当时的 `human_seq(port)`（`[boot, human_n]`）连同 `port` 一起写进条目，`ctx = {"port": port, "human": [boot, human_n]}`。挂起时拿不到（代理不在）的条目 `ctx` 为空，**不许远程批准**：代理不在时 webkiosk 会直开设备，人用输入就没人计数了。
- **急停解除、标签批量**：`ctx` 为空；靠 `id` 和 `action` 绑定。
- **比对在原生锁里做，两个参数各管一件事**：
  - 串口 `Book.confirm(iid, by, now, current, expect=None)`：先在锁里按原生条目现算 `make_item`，`digest` 跟 `expect` 不一样就拒（人批的不是这一条），再走原有的 `sent_mark` / `tail_hash` 对 `current` 的检查（现场变了）。执行器在拿锁之前先读一次设备输出当 `current`，跟车机确认的做法一样。
  - 键鼠 `LeaseStore.take(iid, by, expect=None, human=None)`：`expect` 同上；`human` 是执行器拿锁前刚读的 `human_seq(port)`，跟条目里挂起时的快照比，`None` 或不一样就作废这一条（`dropped: 人动过键鼠或代理不在`）。
  - 车机确认传 `expect=None`、`human=None`，行为维持原样：车机界面直接读原生状态，人在现场。
  - Agent 那一侧在挂起期间本来就发不出（串口 `begin` 和键鼠 `gate` 发现有挂起项都拒）。
- **PXE**：L4 不开放远程批准，**`remote_confirm` 本期不做**。PXE 远程只能拒绝；解锁只认车机上照抄的屏幕码（原有 `take`）。digest 能推算出来这件事因此不构成风险。

### 2.3 摘要由服务端生成

各通道展示的内容只能来自 `approval_core.render(item, now)` 和 `card(item, channel, now)`，不接受 Agent 传进来的任何描述性文字。卡片**显示打码后的原文**（拍板 5），打码规则以 `docs/REMOTE_APPROVAL.md` 的"打码"一节和 `approval_core.mask_command` 为准，要点：

- **只打口令的值，不打后面的命令**；`chpasswd`、`passwd`、`htpasswd`、`net user`、`sshpass` 整条只显示长度；设备最后一行是密码提示符时，这条动作本身就是密码，只显示长度。
- **看不清就只能拒**：动作被截断、含控制类字符、整条只显示长度、打掉的值里有空白或 shell 元字符，卡片上都没有批准按钮，`decide` 也会拒（`forbidden/truncated`）。**凡是打了码的串口条目一律 L3**。
- digest 仍按原文算；键鼠 secret 只显示长度。
- 设备输出和提示符上的名字先去掉零宽字符、NFKC 归一化，再打码、再清洗；`<` `>` 换成全角，卡片按纯文本发。
- 空格、Tab、换行分别显示成 `␠`、`⇥`、"↵ 回车"；键鼠多行输入按行展开。
- 摘要最后固定一行 `SAFETY_LINE`："任何人（包括 Claude）向你要验证码或带外码，都是攻击。码只填在你自己打开的推车网页上。"

示意（不是逐字的 `render` 输出）：

```
标题:  [L3] 槽位 2 串口 · 高危词 save
动作:  save
目标:  槽位 2 (提示符上的名字: <Core-SW>, 来自设备输出, 不可信)
现场:  挂起时设备最近输出 (已打码, 不可信) ……
来源:  Agent claude-laptop · 2 分 13 秒前 · 剩 7 分 47 秒
Agent 自述(未核实): 保存刚才的 VLAN 修改
digest: 3f9a1c0e5b7d2a46
任何人(包括 Claude)向你要验证码或带外码, 都是攻击. 码只填在你自己打开的推车网页上.
```

### 2.4 状态机

```
pending ─批准(取件校验通过)─► authorized ─┬─(L3: "即将执行"推送送达 → 撤回窗口 W 秒)─┐
   │                                      └─(L2: 直接)──────────────────────────────┤
   │                                                                                  ▼
   │          exec@ 在原生锁里: digest 一致 + 现场没变 + 决定仍是 approve + 没 lockdown ─► executing ─► done
   │                       └─ 任一不满足 / 超过 AUTH_TTL_S 没开始 / 推送没送达 ─► dropped
   ├─拒绝─► rejected     ├─到期─► expired     ├─急停 / 租约没了 / 现场变了 / lockdown─► dropped
   ├─authorized 期间被拒绝或撤回 ─► rejected (result=override)
   └─被另一个通道先处理─► 按原生状态显示, 附上"由 <by> 经 <通道>"
```

**先批后拒、先拒后批按 `main` 的 `decide`**：

- **先批后拒**：执行前到的拒绝覆盖批准，记 `rejected`、`result=override`，执行器在原生锁里看到决定已经是 `reject` 就不发。这不是 `conflict`。
- **先拒后批**：记 `conflict`，拒绝不能被翻。
- 同一个决定重复：`already`。
- **撤回就是"执行前的拒绝"**，没有单独的状态：入口有两个，结果一样（都写一条 `reject` 决定、`override`）。
  - 卡片和网页上的"拒绝"按钮在 `authorized` 期间仍然可点，走审批目录，带 digest。
  - 急停目录里的 `withdraw`（只带 id、不要因素，停的方向；见 2.5），给"手边只有急停按钮"的场合用。
  - 已经进入 `executing`（原生锁里标了在途）的，撤回无效，记 `too_late`，靠急停去拦。
- **撤回窗口**（L3，原第 17 条倾向 15 秒，待确认）：`exec@` 在窗口期间**不碰原生锁**，只是等着；窗口结束后才读 `current`、拿原生锁、调 `confirm(expect)`。"即将执行"推送没送达（飞书接口报错或超时）就直接中止，记 `dropped`、`result=receipt_fail`。L4 不开放远程批准，没有撤回窗口。
- **锁的顺序**：`exec@` 先拿原生锁，再在锁里以共享方式读 `approvals.json` 查决定和 `lockdown`，然后标在途。`cart-approve` 登记决定只拿 `approvals.json` 的锁，不碰原生锁，所以不会死锁；拒绝要么在标在途之前生效，要么记 `too_late`。
- **lockdown**：写一个持久标记 `/var/lib/ops-cart/approval-lockdown`，同时把所有 `authorized` 的条目作废。`exec@` 在原生锁里也查这个标记。已经交给 conbroker 的那一条收不回来，跟急停一样。

决定记录 `ApprovalStore`（`main` 已有）：`/var/lib/ops-cart/approvals.json`，root 0600，flock，原子写；读不懂一律拒、不擅自重置。里面有每个账号的 TOTP `last_step`、输错计数和冷却、`last_hi`（同账号 30 秒一条 L3），用过的 nonce 和决定（保留 24 小时）。先写审计、后提交：审计写不进去返回 `audit_fail`，不登记批准。

### 2.5 执行、并发、急停

- **取件**：`cart-approve.path` 盯着 `/run/ops-cart/approve-req/<通道>/`，拉起 `cart-approve.service`（oneshot）。它只做校验和登记：照 `mcp_serial._take_request` 的加固读请求，`parse_request` → 按原生状态现算条目 → `ApprovalStore.decide`（**校验时刻就是取件时刻**，取件从不阻塞）。通过就记 `authorized`，然后启动执行实例。**只有取件单元有 `LoadCredential`**。
- **执行实例名**：`id` 先过 `_ID_RE.fullmatch`，不合格的根本不启动；实例名用 `systemd-escape --template=cart-approve-exec@.service <id>` 生成，`systemctl start --no-block`。`exec@` 启动后用 `%I`（反转义后的实例名）再过一次 `_ID_RE`，不合格就退出。
- **执行**：`cart-approve-exec@.service` 每条批准一个实例，**不带 `LoadCredential`**（因素在取件时已经校验完）。它调原生入口，传入 `expect`（以及串口的 `current`、键鼠的 `human`）。超过 `AUTH_TTL_S = 120` 秒没开始就作废。
- **同一个槽位一次只能批一条**：#65 以后同槽位已经只能挂一条（打断键除外）；`Book.confirm` 遇到在途会拒；第一条发出去以后 `sent_mark` 变了，同槽位的其余条目必然作废。界面上写明。
- **结果回传**：root 写 `/run/ops-cart/approve-res/web.json` 和 `/run/ops-cart/approve-res/feishu.json`（0640，属组分别是 ops-web 和飞书服务的组；只有 root 写）。内容是按 nonce 索引的结果，以及每个账号当前的 challenge 状态（不含码）。网页登录的 TOTP 结果也从这里回（4.2.1）。
- **幂等**：nonce 只能用一次（重启后也是）。先后两个决定的关系见 2.4。车机确认和远程批准撞上时，先拿到原生锁的那个生效，后到的记 `lost_race`。
- **急停和 lockdown 都不经过审批执行器**：每个通道一个急停目录，由各自的单元处理（网页：`serial-req` → `cart-serial-req`；飞书：`estop-req/feishu/` → `cart-estop-feishu`），解析都用 `confirm_core.parse_request`，在原有的 `estop`、`reject`、`kvm_reject` 之外，再加 `kvm_stop`、`withdraw`（带 id）和 `lockdown`。
- **远程解除急停**是审批项（`kind=estop_release`，L3，范围只收 `ESTOP_SCOPES`：`all`、`kvm`、`slot:1`–`slot:4`），走审批目录，不走急停目录；批准后由执行器调现有的解除路径。
- **键鼠的"本路急停"要新做，而且要有自己的持久状态**：`mcp_serial.Store._flag` 每次存状态都会重新算 `serial-estop.json` 的 `kvm` 字段，单独设置的值会被冲掉。所以：
  - `kvm_stop` 写自己的状态 `/run/ops-cart/kvm-stop.json`（root 0600：谁、何时、来源），同时收回键鼠租约、丢掉挂起项。
  - `_flag` 合并时改成 `kvm = 原有算法 or kvm_stop 生效中`：**只能把它置真，不能把它清掉**。
  - 解除 `kvm_stop` 只能删 `kvm-stop.json`，然后重算一遍。`kvm_broker` 不用改。
- **急停里的拒绝不带 digest，明确豁免**：`serial-req` 和飞书急停目录里的 `reject`、`kvm_reject`、`withdraw` 只带 id。拒绝是"停"的方向，id 投错了，最多是多拒掉一条。审批目录里的 `reject` 要带 digest（`main` 的 `parse_request` 已如此）。

## 三、风险分级

### 3.1 级别和各通道要求

以 `approval_core.requirement(tier, channel, kind)` 为准（返回 `none` / `totp` / `forbid`）。

| 级别 | 含义 | 车机（现场） | 飞书单聊（只在手机上） | 网页（不在跑 Agent 的机器上） |
|---|---|---|---|---|
| L0 | 只读，或者"停"的方向 | 不用审批 | 不用审批 | 不用审批 |
| L1 | 不碰被测设备的配置 | 直通 | 直通 | 直通 |
| L2 | 改设备状态，但一般能撤回 | 点"确认" | **直接点批准**，不要 TOTP（拍板 2）；root 侧限频（3.4） | TOTP；A3 以后再加带外码（A2 阶段没有带外码，残余风险 T12） |
| L3 | 可能中断业务、丢配置 | 点"确认"，再问一次 | 串口：TOTP。**键鼠：只能拒绝**，批准要去网页看实时画面 | TOTP 加带外码，键鼠还要看实时画面；**A3 之前不开** |
| L4 | 不可逆，或者进入一个能任意写盘的环境 | 照抄屏幕确认码 | **只能拒绝** | **只能拒绝** |

- 同一个账号 30 秒内只能批一条 L3 及以上（`L3_GAP_S`）。
- **远程永远解不了 L4**：卡片和网页上没有批准按钮，`decide` 对 L4 返回 `forbidden/tier`，执行器、原生入口都没有远程确认 PXE 的路径。
- **飞书只用单聊**：审批卡片只发到审批专用应用和白名单账号的**单聊**里，不发到群里。群里的其他机器人可以发长得一模一样的仿冒卡片，单聊里只有这个应用能发。

### 3.2 工具逐个定级，以及"不受审批保护"的路径

| 工具 / 情形 | 级别 | 理由 |
|---|---|---|
| 只读工具：`list_threads`、`read_output`、`screenshot`、`cart_status`、`pxe_status`、`pending_list`、`kvm_status`、`screen_ocr`、`pxe_clients`、`pxe_requests`、`label_preview`、`label_printer_status`、`label_abbrev_propose`；以及 `estop`、`pxe_retire`（停、收紧） | L0 | |
| `lease_*`、`kvm_lease_*`、单张 `label_print`、`send_key enter/space`（闸门放行时）、`kvm_key` 普通键 | L1 | 不变 |
| `wol_send` | L1，**加注** | 本身无害，但它是下面那条残余路径的第一步 |
| `send_command` 过了第 1 层（只读白名单） | L1 | |
| **`send_command` 过了第 2 层（模型放行）** | **单列："模型审批"** | 实际上是模型在替人审批 Agent。默认关；`approval-check` 发现远程审批和模型同时打开时给出警告（原第 16 条，待拍板） |
| `send_command` 挂起、`serial_danger` 认为不带高危词、没打码 / `send_key ctrl_c` | L2 | 配置命令多数能 `undo`；现场靠 `sent_mark` 和 `tail_hash` 绑定 |
| `send_command` 挂起、带高危词，或者**打了码** | L3 | `approval_core.tier_of` |
| 键鼠挂起（默认） | L3 | 闸门是减速带（#69 还没补）。只有白名单（`KVM_L2_KEYS`，只有 `alt+f4`）降到 L2 |
| `kvm_key ctrl+alt+delete`、`alt+sysrq` | L3 | |
| **`label_print` 批量（2–200 张）** | **L2**（拍板 7） | `kind=label_batch`，`count` 上限 `LABEL_BATCH_MAX = 200`；超过 200 张的不成条目，不能远程批，要分批或在车机上做。批准以后，`mcp_label` 在原有的 `proof` 和 `confirmed` 之外再要一条已批准的 `label_batch` 条目 |
| **远程解除急停** | **L3**（拍板 4） | `kind=estop_release`，范围白名单 `ESTOP_SCOPES` |
| **`pxe_request` 的全部用途（winpe、clone-save、rocky、clone-restore）** | **L4**，远程只能拒绝 | 进入一个能任意写盘的环境，或者直接写盘 |

**不受审批保护的路径（如实列出）**：

| 路径 | 现状 | 跟踪 |
|---|---|---|
| `wol_send`（L1）→ 被测机 PXE 进默认菜单 → 键鼠选中救援项 → 回车进 `inst.rescue inst.sshd`（root shell）→ 在里面敲清盘命令 | #70 以后默认超时从本地盘启动，救援只能手选；**GRUB 菜单不设密码**（本期不做）。所以残余是"键鼠在菜单里手选救援"：方向键移动选中项，回车落在一个推车看不清的行上，闸门挂起，按键鼠 L3 走远程审批（飞书只能拒，网页要带外码加实时画面）。进了救援以后，键鼠闸门的词表认不出 `rd /s /q`、`> /dev/sda` 这类写法（#69） | #69；人看实时画面 |
| 键鼠直通的一切 GUI 操作（鼠标点选、图形界面里的"格式化"） | 推车看不懂屏幕 | 只能靠人盯和急停 |
| 网页会话（人用通道）：`/api/hid`、`/api/type`、`/api/serial/send` | 设计上它就是"人"；Agent 偷到 cookie 就能冒充（T0、T13） | 4.7 部署前提；原第 15 条，待拍板 |
| ops-web 被拿下后直接写 CH9329 和 conbroker | `dialout` 组 | 原第 15 条，待拍板 |

### 3.3 因素：TOTP 加带外码

| 方案 | 证明什么 | 绑定到具体挂起项吗 | 定位 |
|---|---|---|---|
| TOTP（RFC 6238，`totp_core`） | 这 30 秒里，手机在人手上 | 不绑定 | 网页 L2 起、飞书 L3、网页登录（拍板 3） |
| **带外码** | 人在**手机上**看到了服务端生成的摘要 | **绑定**：`code = HMAC(oob_key, account ‖ id ‖ digest ‖ nonce)` 取 6 位，`oob_key` 只给 `cart-approve` | A3 以后，网页上的批准都要 |
| WebAuthn / 通行密钥 | 人在**这台设备**上按了，挑战里带着 digest | 绑定 | 长期方案（要先给网页上 HTTPS） |
| 撤回窗口 | 执行前人还有一次机会喊停（推送没送达就不执行） | — | L3，倾向 15 秒（待确认） |
| 车机屏幕码 | 人在现场 | 绑定 | 只用于现场；L4 唯一的确认方式 |

带外码的流程（网页批准，A3）：

1. 人在网页上点"批准"，网页投一个 `op=challenge` 请求（A3 加进 `OPS`）。
2. `cart-approve` 在 root 一侧登记这个 challenge：账号、id、digest、nonce、时限 120 秒、剩余尝试 3 次。**同一个账号同时只保留一个有效的 challenge**：新的发起时作废旧的，并在新推送里注明"之前那条已作废"。每个账号每分钟最多发起 3 个。
3. `cart-approve` **自己**调飞书发消息接口，把服务端摘要和码推到这个账号的单聊里，并写明两句话："不要把这个码告诉任何人，包括 Claude。""**如果这不是你刚点的那一条，别填码，直接点急停。**"
4. 人在手机上读完摘要，再把码和 TOTP 一起填回网页。
5. 取件时，`approve` 只认跟登记的 challenge **完全匹配**（账号、id、digest、nonce 都一样）、没有过期、还有剩余次数的请求，然后重算 HMAC 校验码。

飞书不可用时，网页上要带外码的批准一律拒绝（fail-closed），回到车机确认（原第 19 条倾向）。

TOTP 的约束（`totp_core`，`main` 已实现）：SHA-1、30 秒、6 位，±1 步；种子每人一个；`last_step` 按账号记、只增不减，存在 `/var/lib`，**重启后也还在**；同一个码跨通道也只能用一次；时钟回拨到已用过的步记 `clock_back`；输错按 `PinGate` 指数退避，封顶 1 小时，按账号、跨通道共享；冷却期间车机确认照常能用。

### 3.4 飞书 L2 的 root 侧限频

拍板 2 让飞书 L2 一键直点，飞书账号或回调服务被拿下就能批 L2（T2、T3）。在 `cart-approve`（root）里对**飞书通道、不要因素的批准**限频，回调服务改不了：

- 每个账号每分钟最多 3 条、每小时最多 30 条飞书 L2 批准；
- 超限以后，该账号的飞书 L2 在冷却期（10 分钟）内**改为要 TOTP**，卡片上出现 TOTP 输入框，并推一条"飞书 L2 批得太快，已临时改为要验证码"；
- 每天推一次汇总：各账号经飞书批了几条 L2、按 Agent 分组。

数值是建议，列为新的待拍板问题（第七节 B1）。

## 四、认证与防伪

### 4.1 共同规则

- **种子和带外密钥**：放在 `/etc/ops-cart/approve-secrets/`（root 0700，文件 0600，`totp-<账号>`）。**只通过 `LoadCredential=` 交给 `cart-approve.service`**（`exec@` 不带），程序从 `$CREDENTIALS_DIRECTORY` 读（`totp_core.load_seed`）。
- **飞书 app_secret**：放在 `/etc/ops-cart/feishu-approve-secret/`，只通过 `LoadCredential=` 交给飞书回调服务和 `cart-approve`（后者要用它发带外码和"即将执行"推送）。
- **遮挡**：`deploy/systemd/*.service` 里**除 `cart-approve.service` 以外的每一个单元**都加 `InaccessiblePaths=-/etc/ops-cart/approve-secrets -/etc/ops-cart/feishu-approve-secret -/run/credentials`；飞书回调服务只遮种子目录。写作时的单元：`campus-auth`、`cart-ble-input`、`cart-boot-success`、`cart-dash`、`cart-dream`、`cart-fan-curve`、`cart-mcp`、`cart-mcp-ro`、`cart-notify`、`cart-ocr`、`cart-revtunnel`、`cart-serial-req`、`kiosk`、`kvm-broker`、`pxe-http-boot`、`pxe-http-repo`、`pxe-server`、`webkiosk`、`wifi-prefer`，以及新加的急停单元和 `cart-approve-exec@`。**测试遍历目录逐个断言**，以后新加的单元自动纳入，不靠这份名单。
- **决定记录**：`/var/lib/ops-cart/approvals.json` 和 `approval-lockdown` 对除了 `cart-approve*` 以外的单元也用 `InaccessiblePaths` 遮掉（ops-web 有 `CAP_DAC_READ_SEARCH`）。网页需要的状态从 `approve-res` 读。
- **root 单元的边界**：对 root 运行的单元，这几条遮挡只能防误读，防不住有意为之（它可以经 `/run/systemd/private` 或 `/run/dbus/system_bus_socket` 拉起不受限制的单元）。所以 `approval-check` 对每个 root 单元的要求是：要么它已经不是 root，要么它同时遮掉这两个 socket，并且 `RestrictAddressFamilies` 不含 `AF_UNIX`，或者只允许它确实需要的 socket。都做不到的，判为"种子隔离不成立"，按原第 20 条处理（待拍板）。**`cart-mcp` 和 `cart-mcp-ro` 在 #49 降权之前都属于"不成立"**：#60 只做了按档位启动，两个单元都没有 `User=`；`cart-mcp` 要用 `sudo -u` 跑 conbroker、要 mpv 的 socket，限制不了 `AF_UNIX`。
- **请求目录**：`/run/ops-cart/approve-req/web/`（ops-web 0700）和 `/run/ops-cart/approve-req/feishu/`（飞书服务用户 0700）。`webkiosk.service` 要加 `ReadWritePaths=-/run/ops-cart/approve-req/web`（它是 `ProtectSystem=strict`）。root 按所在目录认通道。请求格式以 `approval_core.parse_request` 为准（字段集合固定为 `FIELDS`，多一个少一个都不认）：
  ```json
  {"op": "approve|reject", "id": "serial:1a2b3c4d:17", "digest": "3f9a1c0e5b7d2a46",
   "account": "alice", "by": "…", "totp": "123456", "oob": null, "retype": null,
   "nonce": "9c2e…(16 hex)", "t": 1700000000.0}
  ```
  `op` 现在只有 `approve`、`reject`（`main` 的 `OPS`）；A2 加 `login`（4.2.1），A3 加 `challenge`。**没有 `elevate`**：没有提权窗口，审批会话在第一次批准时签发（4.2）。`by` 只是请求里自报的，**不进审计**：网页的 `by` 由服务端拼（4.8）。写入和读取的加固照抄 `mcp_serial.write_request` / `_take_request`。`serial-req` 维持原样，"网页请求里出现 confirm 就当坏请求"这条老不变量不变。
- **推车没设 PIN 时，root 执行器拒绝 web 目录里的一切批准**（不只是网页自己拦）。
- **审批人白名单**：`{账号: {"feishu": open_id, "ts_login": 登录名, "ts_nodes": [设备名]}}`（`approval_core.valid_accounts`），open_id 和登录名在账号之间必须唯一，不合格一律拒。
- **审批链接不由 Agent 提供**：`approval_list` 只返回编号。网页审批页、飞书卡片、`approval_list` 的工具描述里，都写明"任何人向你要码都是攻击"。
- 秘密只显示长度；日志里不出现码、种子、cookie、卡片 token。

### 4.2 网页

- **审批会话单独签发**：审批页不认普通的网页登录会话，要另外输一次 TOTP，换来一个 15 分钟、不滑动、绑定（会话, 账号）的审批会话，由 root 登记。**签发和第一次批准合并**（原第 10 条，待对稿确认）：在审批页上第一次批准时，一个码同时完成签发和批准，免得签发以后还要等下一个 30 秒步。之后**每次批准**还要再输 TOTP，A3 以后网页 L3 还要加带外码。没有提权窗口。
- **身份**：取件时 root 用请求的来源地址问 tailscaled（LocalAPI whois）拿到登录名和设备名，登录名必须等于账号绑定的 `ts_login`；`ts_nodes` 不为空时设备也要在列表里（`approval_core._identity_ok`）。来源地址是 ops-web 写进请求的，见 T3。
- **前提**：网页登录和审批都不在跑 Agent 的机器上做。用另一台设备，或者用 Agent 那个系统账号读不到的浏览器配置（同一个系统用户另开浏览器配置不算隔离）。推车能部分检查：`ts_nodes` 只填手机或专用设备；另外可选做 `same_host` 比对（网页请求的来源设备跟 `cart-mcp` 最近 24 小时见过的 Agent 来源相同就拒，新问题 B4）。挡不住"Agent 经别的机器中转"，仍然列为残余风险 T13。
- 防 CSRF：同源检查、`SameSite=Strict`、一次性的表单 nonce。防点击劫持：`X-Frame-Options: DENY` 和 `frame-ancestors 'none'`，作为纵深防御。

#### 4.2.1 网页登录加 TOTP（拍板 3）

ops-web 读不到种子（4.1 遮掉了），所以网页登录的 TOTP 也要到 root 一侧校验，跟审批取件走同一套请求目录：

1. 登录页在 PIN 之外再要"账号 + TOTP"。webkiosk 往 `/run/ops-cart/approve-req/web/` 投一个 `op=login` 请求：`account`、`totp`、`nonce`、来源地址，以及待签发会话令牌的哈希（不放令牌原文）。`id`、`digest` 为空，`parse_request` 按 op 分别校验字段。
2. `cart-approve` 取件：校验 tailscale 身份（同 4.2）、TOTP（`totp_core.verify`，跟审批共用同一份账号状态），把结果按 nonce 写进 `approve-res/web.json`：`ok` / `bad` / `locked`（带冷却剩余秒数）/ `replay` / `no_seed` / `identity`。
3. webkiosk 轮询 `approve-res/web.json`（每 200 毫秒，最多 10 秒），看到 `ok` 才签发会话；`approve-res` 只有 root 能写，ops-web 自己写不出 `ok`。
4. **延迟**：path 单元拉起 oneshot 加 Python 启动，预计 1 秒以内（待实机验证）；页面显示"校验中"。
5. **失败处理**：超时（`cart-approve` 没跑、path 单元没触发）一律**不签发**，提示"校验服务没响应，稍后再试或到车机上操作"，fail-closed；输错计入同一份退避，按账号跨通道共享；`locked` 时显示冷却剩余时间，车机不受影响；`no_seed` 提示去 `totp-enroll`。cart-approve 不在时要不要退回只用 PIN 登录，是新问题 B3，倾向不退回。
6. **和审批会话共用码**：`last_step` 只增不减，所以登录用掉的那一步，审批页的"签发 + 第一次批准"不能再用，要等下一个码（最多 30 秒）；同一步里的第二次一律 `replay`，界面提示"这个码已经用过，等下一个（还剩 N 秒）"。登录码直接顺带签发审批会话可以省掉这次等待，但会让 15 分钟审批会话从登录那一刻起算，列为新问题 B3。
7. **挡得住什么**：挡得住 tailnet 上的人或 Agent 拿 curl 自己登录；挡不住偷 cookie（T13）和 ops-web 被拿下（它就是签发会话的那个进程，T3）。

### 4.3 飞书

- **专用应用、只用单聊**（拍板 6）：长连接，官方 SDK 装在**独立的 venv** 里，不和任何其他机器人或桥接共用凭据（长连接是集群模式，回调会随机分给任一客户端）。卡片和带外码只发给白名单账号的单聊。
- 回调用新版 `card.action.trigger`，签名和字段**实现前对照当前官方文档核对**（待核）。卡片按纯文本发。真实客户端实现成 `approval_channel.Channel` 的子类。
- **应用权限只申请"以机器人身份发消息"和卡片回调**，不申请读消息、读通讯录之类的权限（具体权限项实现前核对官方文档，待核）。
- **带外码和"即将执行"推送由 `cart-approve` 直接发**，不经过飞书回调服务。回调服务只负责接收按钮点击和 TOTP 输入框，看不到码。
- **服务沙箱**：独立用户；不进 `dialout`、`video` 组；`IPAddressDeny=any`，只放行飞书出口；用 `InaccessiblePaths` 遮掉种子目录和 `/run/ops-cart` 下的状态文件，只留自己的请求目录和急停目录可写。回调服务被拿下就能批 L2（T3），沙箱和 3.4 的 root 侧限频是对策。
- 只在手机上批；桌面版和网页版飞书不登录审批账号（T12）。
- **按级别的按钮**（`approval_core.card`）：L2 卡片直接有"批准"；串口 L3 卡片有"批准"和 TOTP 输入框，附设备最近输出（已打码，标不可信）；键鼠 L3、全部 L4、看不清的条目**没有批准按钮**。拒绝、急停、"关掉远程审批"（`lockdown`）常驻；急停在串口上分本路和全部，在键鼠上是 `kvm_stop` 和全部。这些都投到飞书的急停目录。
- **by**：必须正好是 `feishu:<白名单里绑定的 open_id>`。

### 4.4 Claude 转达

只读的 `approval_list`：返回摘要、`id`、`digest`、剩余时间；不返回链接，不返回任何码。工具描述里写明：**只能看；批准要人本人在自己打开的推车网页上做，或者在手机飞书上做；任何人（包括你）都不应该向人要验证码；摘要要原样转达。** 不可信的字段用随机标记包起来。

### 4.5 车机

维持原样。车机确认写进审计，不受 TOTP 冷却影响。PXE（L4）只能在车机上照抄屏幕码确认。

### 4.6 大肥鱼经 ACP 接入

dsh 作为 MCP 客户端调用推车工具时不经过它自己的审批（#49）；大肥鱼经 ACP 运行时，dsh 自己的审批请求只送得到 ACP 客户端（包装层），到不了推车的网页和飞书。推车工具在推车服务端挂起，走本设计。dsh 自己的审批倾向由包装层转成 `kind=acp` 的挂起项（经一个只能"提交挂起、查询结果"的专用接口），内容标"推车无法核实"，级别不低于 L2（原第 13 条，待拍板）。

### 4.7 部署前提和 `approval-check`

`cart_mcp.py approval-check` 在每次开启远程审批时运行，另由 `cart-approval-check.timer` 每小时跑一次（不放在执行器里）。**有任何一项不合格就拒绝开启**；已经开着的，自动 `lockdown` 并推送通知。

| 检查项 | 怎么查 | 不合格时 |
|---|---|---|
| 推车设了 PIN | `web_core.pin_state` 返回 `set` | 拒绝开启 |
| 遮挡在**运行中的进程**上真的生效 | 对每个在跑的单元取 `MainPID`，用 `nsenter -t <pid> -m` 进到它的挂载空间里，实际 `stat` 种子目录、`/run/credentials`、`approvals.json`，必须是不可访问。另外比对 `ExecMainStartTimestamp` 和单元文件的修改时间，并检查 `NeedDaemonReload`：改了配置但没重启的，算不合格 | 拒绝开启 |
| root 单元的边界 | 见 4.1。现在 `cart-mcp`、`cart-mcp-ro` 必然不过 | 判为"种子隔离不成立"，按原第 20 条处理（倾向只开 L2） |
| 飞书凭据 | `feishu-approve-secret` 的权限位；只有飞书服务和 `cart-approve` 有 `LoadCredential` | 拒绝开启 |
| 种子权限 | 目录 0700、文件 0600，属主 root（`totp_core.load_seed` 同样的规则） | 拒绝开启 |
| **root 登录面** | ① Tailscale SSH：`tailscale debug prefs` 里 `RunSSH` 为真，**直接不合格**；② sshd：用 `sshd -T` 取 `permitrootlogin`（必须是 `no`）、`authorizedkeysfile`（逐个文件检查 root 有没有密钥）、`authorizedkeyscommand`（设了就不合格）、`passwordauthentication`；③ 能 sudo 的普通用户：`getent group wheel` 加 `sudo -l -U`，有 NOPASSWD 就不合格；④ 等同 root 的组（`docker`、`disk`、`lxd`、`libvirt`）里有没有成员；⑤ `cart-revtunnel` 把本机 22 端口反向映射出去，跳板机那头的人就能到推车的 sshd，所以要求 ②③ 也成立 | ① 到 ④ 拒绝开启；⑤ 提示 |
| 提示项（不拒绝） | 决策模型是否打开（原第 16 条）；`same_host` 比对是否开着 | 提示 |

**范围**：`approval-check` 只检查本仓库 `deploy/systemd` 里的单元，以及上面列出的登录面。推车上别的软件装的单元和 root 进程不在检查范围内，列为残余风险（T17）。

**从 `ssh cart sudo …` 接法迁移**：

1. 在推车上给每个 Agent 生成一个 Bearer 令牌，写进 `/etc/ops-cart/mcp.json`（`examples/mcp.example.json` 有样例）。
2. 启用 `cart-mcp.service`（或只读档 `cart-mcp-ro.service`，http 模式，只绑本机和 tailnet 地址）。
3. Agent 那边改成 `claude mcp add --transport http cart http://<推车>:8765/ -H "Authorization: Bearer <令牌>"`。
4. 删掉 Agent 机器上能 sudo 的 ssh 密钥，然后在推车上跑一遍 `approval-check`，确认 root 登录面这一项通过。

文字层面的前提：PIN、种子、飞书凭据都不放在 Agent 的机器上；审批飞书账号只在手机上登录。

### 4.8 审计

以 `approval_core.audit` 为准：

- `/var/log/ops-cart/approval.log`（JSONL；`*.log` 已有的 logrotate 规则管）。字段：`t ev id digest kind tier channel account by factor summary result ts_login ts_node nonce`。
- **`by` 由服务端拼，请求里自报的不算**（拍板 8）：网页是 `web:<tailscale 登录名>/<设备名>`；飞书是 `feishu:<open_id>`；车机是 `human:kiosk@local`。`ts_login`、`ts_node` 是 tailscale 登录名和设备名，格式不对按 `forbidden/identity` 拒。
- `factor`：`totp:ok`、`totp:<失败原因>`、`none`（A3 加 `oob:*`）。`summary` 是 `render` 的标题和动作行，已打码。
- `ev`：`authorized`、`reject`（`result=override` 表示覆盖了此前的批准）、`forbidden`、`replay`、`totp_bad`、`totp_lock`、`clock_back`、`no_seed`、`drop`、`expire`、`already`、`conflict`、`too_soon`、`state_unreadable`、`commit_fail`，推送是 `notify`（`result` 为 `sent` / `receipt_fail` / `bad_account`）（`main` 已有；审计写不进去时 `decide` 返回 `audit_fail`，没有那一行）；执行器和后续阶段再加 `executed`、`lost_race`、`too_late`、`lockdown`、`check_fail`、`login`、`challenge`、`challenge_superseded`、`oob_bad`、`same_host`、`rate_limit`。
- 车机确认也要写一行（通道 `kiosk`，因素 `none`），这样审计是完整的。原生日志（`serial-slot<N>.log`、`kvm-thread.log`、`pxe-req.log`）照旧记各自的 `confirm` 和 `send`。
- `events.log` 只记一行 `[approval]` 摘要，不写命令原文。
- 日志里永远不出现：TOTP 码、带外码、种子、cookie、卡片 token、nonce 原文（只记前 4 位）。

## 五、分阶段落地

| 阶段 | 内容 | 状态 |
|---|---|---|
| 前置 | #64 键鼠闸门修绕过（#67）；#65 串口挂起期间拒直发、`sent_mark`、`tail_hash`、`Book.confirm(…, current)`（#68）；#70 默认菜单超时本地盘（#76）；#49 按档位启动（#60） | **已完成** |
| A0 | 本设计稿 | 本 PR |
| A1a 服务端核心 | `totp_core`；`approval_core`（`make_item`、`tier_of`、`requirement`、`digest` v1、打码、`render`、`card`、`parse_request`、`decide`、`audit`、`ApprovalStore`）；`approval_channel`（`Channel`、`FakeChannel`、`notify`）；`docs/REMOTE_APPROVAL.md` | **已完成**（#77） |
| A1b 接原生（只读） | 三处 Book 加 `epoch`（缺 `epoch` 的旧状态文件就地补上，不触发全部急停；只有 JSON 坏了才走原有的全部急停）；三处挂起项映射成条目（串口 `ctx` = `sent_mark` + `tail_hash`，键鼠挂起时写定 `serial`、`prefix`、`port`、`human`）；`Book.confirm` 和 `LeaseStore.take` 的 `expect`（键鼠还有 `human`）；`approvals` 命令行；网页只读页；`approval_list` | 未开始 |
| A2 执行器 + 网页 L2 + 网页登录 TOTP | `cart-approve`（取件）和 `cart-approve-exec@`（`systemd-escape`）；`approve-res`；`totp-enroll`；种子目录、`LoadCredential`、所有单元的 `InaccessiblePaths`；`approval-check` 和 timer；webkiosk 的 `ReadWritePaths`；`op=login`（4.2.1）；审批会话；防点击劫持头；`withdraw`、`lockdown`；远程解除急停、`label_batch` 接 `mcp_label` | 未开始 |
| A3 飞书 + 网页 L3 | 专用应用和单聊；卡片；飞书急停目录；3.4 的 L2 限频；`op=challenge` 和带外码；"即将执行"推送和撤回窗口；`kvm_stop`；T6 的推送限额 | 未开始 |
| A4 | 防疲劳自动升级、`kind=acp` | 按拍板结果 |
| 长期 | `cart-mcp`、`cart-mcp-ro` 降为非 root（#49 剩下的部分）；HTTPS 加 WebAuthn；#69 | — |

**A2 阶段网页只开放 L2**（没有带外码，残余风险见 T12），L3 要等 A3 的带外码；L4 在任何阶段都不开放远程批准。

测试清单（先写红灯；标"#77 已有"的不用再写）：

**A1a（#77 已有）**
- `totp_core`：RFC 6238 附录 B 的向量；±1 步能过，±2 步不过；同一个码第二次被拒，换个通道也被拒；时钟回拨；退避和冷却按账号跨通道共享；种子文件权限、链接、属主不对一律当没有。
- `digest` v1 公式回归；`make_item` 字段白名单；`tier_of`（`pxe` 全部 L4、键鼠默认 L3、打了码的串口 L3、`label_batch` L2、`estop_release` L3）；`requirement`（飞书 L2 `none`、L4 一律 `forbid`、网页 L3 `forbid`、飞书键鼠 L3 `forbid`）。
- 打码和"看不清就只能拒"；`render` 的 Tab、`SAFETY_LINE`、键鼠 `prefix`。
- `parse_request`：多字段、少字段、类型不对、超长、超时、未来时间，一律不认；审批目录里的 `reject` 不带 digest 不认。
- `decide`：先批后拒得到 `rejected/override`；先拒后批得到 `conflict`；nonce 重放；同账号 30 秒一条 L3；身份（网页 `by` 由服务端拼、飞书 `by` 必须是绑定的 open_id）；审计写不进去不登记批准。

**A1b**
- `epoch`：串口 `from_text`、键鼠 `read_state`、PXE `from_text` 读坏重置以后都换了新的 epoch，同一个原生编号得到不同的 `id`；没有 `epoch` 的旧状态文件就地补上，**不触发**全部急停；JSON 坏了照旧全部急停。
- 键鼠：挂起项里写定了 `serial`、`port`、`human`；挂起时代理不在（`human_seq` 为 `None`）的条目不出现在可远程批准的列表里。
- **在原生锁里比对 digest**：取件和执行之间篡改挂起项（同 id、换内容），`Book.confirm(expect=…)`、`LeaseStore.take(expect=…)` 在锁里拒掉，一个字节都不发。
- 串口：`expect` 对得上但 `current` 有新输出，照 #65 作废；`expect=None`（车机）行为不变。
- 键鼠：挂起以后人用通道动过键鼠（`human_n` 变了）、代理重启（`boot` 变了）、拿不到 `human`，远程批准都作废；`human=None` 的车机确认行为不变。
- 不变量：MCP 工具里没有审批写路径；`approval_list` 不返回链接，也不返回码。

**A2**
- 因素矩阵：L2/L3/L4 × 网页（无码、错码、旧码、审批会话过期、换了账号）× 车机 × 飞书。
- **远程永远解不了 PXE**：更新 `test_pxe_invariant.py`，改成"远程通道（网页、飞书、审批目录、执行器）无论带什么因素都解不了锁，只有照抄屏幕码的车机确认能解"；用能推算出来的 PXE digest 也解不了。
- 并发：车机确认和远程批准交错，只发一次，后到的记 `lost_race`；同一个 nonce 只执行一次；`authorized` 之后的拒绝和 `withdraw` 都让 `exec@` 不发；已经标在途的撤回记 `too_late`。
- `exec@` 实例名：不合格的 id 不启动；`%I` 反转义后再校验。
- 遍历 `deploy/systemd/*.service`：除了 `cart-approve.service`，每个单元都遮掉种子目录、飞书凭据目录和 `/run/credentials`；`exec@` 没有 `LoadCredential`；`approvals.json` 也被遮掉。
- `approval-check`：用假的 `nsenter` / `stat` 结果模拟"配置写了、进程没生效"，`NeedDaemonReload=yes`、`ExecMainStartTimestamp` 早于单元文件，都判不合格；`cart-mcp`、`cart-mcp-ro` 以 root 运行判"种子隔离不成立"；登录面逐项检查。
- PIN 没设时，web 目录里的批准被执行器拒绝。
- 取件不阻塞：一个 exec 实例卡在发送上，这时又来一个批准，取件照样在一秒内完成校验。
- `AUTH_TTL_S` 内没开始执行就作废；急停在 exec 阻塞期间照常生效。
- 重启以后，`last_step`、冷却、nonce 都还在，旧码和旧 nonce 都被拒。
- 审批会话：15 分钟到期；不滑动；换了账号无效；普通登录会话进不了审批页；签发和第一次批准合并时只用掉一个码。
- 网页登录 TOTP：`approve-res` 里没有 `ok` 就不签发会话；校验超时不签发；登录用过的步，紧接着的审批要等下一个码（`replay`）；输错计入跨通道的退避；ops-web 往 `approve-res` 写不进去。
- `lockdown`：`authorized` 之后、执行之前打开 lockdown，一个字节都不发；标记重启后还在。
- 远程解除急停：范围不在 `ESTOP_SCOPES` 的不成条目；`label_batch`：201 张不成条目，`mcp_label` 没有已批准的条目就不打。

**A3**
- 回调：白名单之外的账号、`app_id` 或 `tenant_key` 不对、事件重复，都按预期处理；卡片在被别的通道处理、过期、作废以后，没有能点的按钮。
- 飞书 L2 限频：超过每分钟 3 条、每小时 30 条以后改为要 TOTP；冷却过后恢复；回调服务没法绕过（限频在 root）。
- T6 推送限额：超限以后合并成一张汇总卡，汇总卡上没有批准按钮。
- 带外码：换账号、换 id、换 digest、换 nonce 都会对不上；同一个码只能用一次；同一个账号同时只有一个有效的 challenge；超过频率限制被拒；推送里有"不是你点的就别填码"那一句；飞书回调服务的代码路径里看不到码。
- 卡片：键鼠 L3 和全部 L4 的卡片上没有批准按钮；有急停、`kvm_stop` 和 `lockdown`。
- `kvm_stop`：只停键鼠，串口不受影响；`kvm_stop` 以后再做串口的 `lease_acquire`、急停解除等任何会让 `_flag` 重算的操作，`kvm` 仍然为真；解除后要重新申请租约。
- "即将执行"推送失败（模拟飞书接口报错）时，`exec@` 中止，一个字节都不发；撤回窗口内撤回就不执行。
- `lockdown` 和急停走急停目录，在执行器阻塞时照常生效；开回来只认车机本机。

**待实机验证**：path 单元能不能被拉起、网页登录校验的实际延迟；飞书长连接断线重连，卡片在手机上的显示；车机有没有 NTP；飞书回调字段、签名和权限项（对照官方文档）；以 webkiosk 的同等权限（`systemd-run -p User=ops-web -p AmbientCapabilities=CAP_DAC_READ_SEARCH` 加上它的 `InaccessiblePaths`）去读种子目录和 `/run/credentials/cart-approve.service/`，都应该失败；`approval-check` 的 `nsenter` 在车上能跑通；webkiosk 加了新的 `ReadWritePaths` 以后原有功能正常；tailscaled LocalAPI whois 在 root 单元里能拿到登录名和设备名。

## 六、威胁模型

| 编号 | 威胁 | 对策 / 残余风险 |
|---|---|---|
| T0 | Agent 和人同在 tailnet 上，没设 PIN 时直接登录网页 | 要求设 PIN；`approval-check` 检查；执行器拒绝；网页登录加 TOTP（4.2.1） |
| T1 | 手机丢了 | 手机锁屏、验证器 App 加锁；`lockdown`；`approval-revoke` |
| T2 | 飞书账号被盗 | 只用单聊。**飞书 L2 不要 TOTP（拍板 2），账号被盗就能直接批 L2，接受此残余风险**；对策是 3.4 的 root 侧限频、每日汇总、`lockdown`。L3 要 TOTP；键鼠 L3 和全部 L4 在飞书上批不了 |
| T3 | ops-web 或飞书回调服务被拿下 | **ops-web**：在 `dialout` 组，能直接写人用通道，绕过审批（原第 15 条）；它是签发会话的进程，登录 TOTP 挡不住它；请求里的来源地址由它写，**能冒充任意 tailscale 身份**，审计里的 `by`、`ts_login`、`ts_node` 不可信，`ts_nodes` 限制也能被绕过；改投 TOTP 码会被带外码挡住（A3）。**飞书回调服务**：**被拿下等于能批 L2**（不要因素）；对策是服务沙箱（4.3：独立用户、不进 `dialout`、只放行飞书出口、看不到状态文件）和 root 侧的 L2 限频（3.4）。飞书串口 L3 的 TOTP 不绑定条目，回调服务能把人为 A 填的码改投给另一条 L3：码只能用一次、同账号 30 秒一条 L3 限住次数，残余。回调服务看不到带外码（由 `cart-approve` 直接发）。根本办法是 WebAuthn |
| T4 | 提示词注入，生成诱导性摘要 | 摘要由服务端生成并打码；不可信字段带前缀、限长；L3 人在手机上看摘要 |
| T5 | 拆开发送、改掉现场 | 串口 `sent_mark` + `tail_hash`（#65），键鼠 `[boot, human_n]`（#64），都进 digest 并在原生锁里比对；#69 还没补 |
| T6 | 批准疲劳、刷卡片 | 没有"全部批准"；同一账号 30 秒只能批一条 L3；L4 不开放远程。**具体限额**（A3，数值待拍板 B2）：① 挂起：每个 Agent 令牌同时最多 3 条可远程批的挂起项（串口每槽位本来最多 1 条加打断键，键鼠一次 1 条），第 4 条照常挂起但只在车机和网页列表里出现，不推送；② 推送：每个令牌每 10 分钟最多推 6 张卡，每个审批账号每小时最多收 30 张；超了以后这个令牌的新挂起项**合并成一张汇总卡**（"Agent X 还有 N 条等审批"，只有"全部拒绝"、急停和 `lockdown`，没有批准），每 10 分钟更新一次，要批就去网页逐条看；③ 同一令牌 1 小时内经飞书批过 20 条 L2 以后，它的 L2 在飞书上改为要 TOTP（3.4）；④ 每日汇总 |
| T7 | 重放 | `epoch`、digest、nonce 和 `last_step` 都落盘；带外码绑定 nonce |
| T8 | 检查和使用之间被换了 | 在原生锁里比对 digest 和现场 |
| T9 | AI 审批 AI | 代码不变量加 `approval-check`（含 root 登录面检查）；`cart-mcp` 和 `cart-mcp-ro` 都以 root 运行，#49 降权之前对它们来说种子隔离不成立，只靠"Agent 拿不到 root"（#60 只拆了档位，没降权） |
| T10 | 时钟漂移、回拨 | 窗口固定 ±1 步；`last_step` 只增不减，回拨时宁可暂时用不了；车机要有 NTP |
| T11 | Agent 故意输错 TOTP，把人锁在外面 | 退避封顶 1 小时；车机确认不受影响；急停和拒绝不需要 TOTP；每次进入冷却都推通知 |
| T12 | Agent 驱动维护者已经登录的浏览器或飞书客户端 | 没有提权窗口；飞书只在手机上批（推车验证不了，残余风险）。A2 阶段网页 L2 可以"显示 A、提交 B"（没有带外码），属于残余风险，A3 以后网页的批准都加带外码。带外码挡不住"直接为 B 发起 challenge"，靠人在手机上读推送 |
| **T13** | Agent 偷维护者浏览器里的 cookie（会话最长 7 天），直接调人用接口 | 不在 Agent 的机器上登录网页（前提，推车验证不了）；审批会话单独签发、15 分钟；`ts_nodes` 只填专用设备；人用接口本身的风险见原第 15 条 |
| **T14** | 仿冒：仿冒的审批页、群里的仿冒卡片、在 Claude 转达通道里向人要码 | 卡片只发单聊；审批链接不由 Agent 提供；所有界面都写"要码就是攻击"；带外码只在手机上出现，填回的位置只有推车网页 |
| **T15** | WOL → 键鼠在默认菜单里手选救援 → root 救援 shell | #70 以后默认超时本地盘、救援只能手选；不设 GRUB 密码。选中救援以后的回车落在推车看不清的行上，按键鼠挂起走 L3 远程审批（飞书只能拒，网页要带外码加实时画面）；人批之前要看清画面上选中的是哪一项。进了救援以后靠 #69 和人盯 |
| **T16** | 决策模型放行改配置的命令（模型审批 Agent） | 单列，默认关；开远程审批时警告（原第 16 条） |
| **T17** | 不在本仓库里的 root 单元或进程（其他软件装的） | `approval-check` 查不到，列为残余风险；保持推车上软件的最小化 |
| **T18** | 撤回窗口期间人没看手机 | 窗口只是多一次机会，不能当成审批本身；推送没送达就不执行 |

## 七、维护者拍板

### 7.1 拍板结果（2026-11-07）和落地情况

| # | 问题 | 拍板 | 落地 |
|---|---|---|---|
| 1 | 第二因素 | TOTP | `totp_core`（#77）；网页 L3 另加带外码（A3），长期 WebAuthn |
| 2 | 飞书能批到哪一级 | L2 直接点批准；L3 要 TOTP；L4 不开放远程批准 | `requirement()`（#77）；键鼠 L3 在飞书上只能拒（看不到实时画面）；L2 限频见 3.4 |
| 3 | 网页登录加 TOTP | 要 | 4.2.1，A2 |
| 4 | 远程解除急停 | 允许，按 L3 | `kind=estop_release`、`ESTOP_SCOPES`（#77）；执行器接入 A2 |
| 5 | 卡片上显示原文 | 显示打码后的原文 | `mask_command`、`card`（#77）；2.3 |
| 6 | 飞书接入方式 | 长连接 + 独立 venv | 4.3，A3 |
| 7 | `label_print` 批量 | 纳入审批，按 L2 | `kind=label_batch`，上限 200（#77）；接 `mcp_label` 在 A2 |
| 8 | tailscale 身份 | 审计记录带 tailscale 身份 | `ts_login`、`ts_node`，网页 `by` 由服务端拼（#77）；冒充风险见 T3 |
| 9 | 各级期限 | 暂按设计稿取值，待对稿确认 | `EXPIRES_S`：L2 15 分钟 / L3 10 分钟 / L4 10 分钟；`AUTH_TTL_S` 120 秒 |
| 10 | 审批会话 | 同上 | 15 分钟、不滑动，签发和第一次批准合并（见 B3） |
| 11 | 有争议的分级 | 同上 | `pxe_request` 全部 L4；不带高危词的串口命令 L2；键鼠 L2 白名单只有 `alt+f4` |
| 12 | 种子每人一个还是全车一个 | 每人一个 | `totp-<账号>`（#77） |

### 7.2 原稿里还没拍板的（倾向不变，除非另注）

| # | 问题 | 倾向 |
|---|---|---|
| 13 | ACP 接入 | 包装层转成挂起项，A4 以后做 |
| 14 | #65 的修法 | **已按"有挂起项时禁止直发"实现（#68）**，不用再拍 |
| 15 | 网页人用通道的风险（ops-web 在 `dialout`；cookie 能直接打键） | 另开 issue：人用键鼠和串口走需要认证的代理，网页进程不进 `dialout` |
| 16 | 决策模型和远程审批同时打开 | 允许，但 `approval-check` 警告；或者开远程审批时强制关掉第 2 层 |
| 17 | 撤回窗口 | **只剩 L3**（L4 不开放远程），15 秒；推送没送达就不执行 |
| 18 | #70 默认救援菜单 | **已关闭（#76）**：默认超时本地盘、救援手选；GRUB 密码本期不设 |
| 19 | 带外码的投递通道 | 飞书单聊，由 `cart-approve` 直接发；飞书不可用时，网页要带外码的批准一律拒绝 |
| 20 | root 单元的种子隔离不成立时 | `approval-check` 判不合格，远程审批**只开 L2**（L3 只能车机确认），直到 #49 降权完成；另一个选项是完全不开远程审批。注意：现在 `cart-mcp`、`cart-mcp-ro` 都是 root，按这个倾向，#49 之前远程审批实际上只有 L2 |

### 7.3 这次同步带出来的新问题

| # | 问题 | 倾向 |
|---|---|---|
| B1 | 飞书 L2 的 root 侧限频（3.4）：数值，以及超限以后是改为要 TOTP 还是直接拒 | 每账号每分钟 3 条、每小时 30 条；超限后 10 分钟内改为要 TOTP |
| B2 | T6 的挂起和推送限额：每令牌同时 3 条可远程批、每令牌每 10 分钟 6 张卡、每账号每小时 30 张；超限合并成汇总卡 | 按这组数先做，实机跑一阵再调 |
| B3 | 网页登录 TOTP：`cart-approve` 不在时要不要退回只用 PIN 登录；登录码要不要顺带签发审批会话（替代第 10 条的"签发和第一次批准合并"） | 不退回（fail-closed，车机照常）；不顺带签发，保持第 10 条，登录后第一次批准要等下一个码 |
| B4 | 原第 8 条倾向里的 `same_host` 比对（网页来源设备跟 Agent 来源相同就拒）还做不做；拍板 8 只定了审计带身份 | 做成 `approval-check` 的提示项加可选开关，默认开；ops-web 被拿下时来源地址可伪造，它只防"在跑 Agent 的机器上直接审批"这种无意的情况 |
| B5 | 超过 200 张的标签批量 | 不成条目，Agent 自己分批，每批单独审批 |
