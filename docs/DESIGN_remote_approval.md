# 设计：远程审批（issue #50）

推车的定位是"人在办公室，车在机房"。#23、#42、#24 定下的"确认只在车机本机、网页只能拒绝、Agent 没有确认工具"是早期的保守做法：人不在车旁，挂起的操作只能干等。本文把**远程审批定为主路径**，车机确认保留为现场路径，**急停在任何通道都能按、优先级最高**。

本文只是设计，不改代码。引用的函数、文件和单元配置都是写作时 `main` 上的实际实现。

**本期范围**：车机、webkiosk 网页、飞书（与专用应用的单聊）、Claude 会话转达，以及大肥鱼经 ACP 接入时怎么挂进来（4.6）。issue 里提到的"大肥鱼网页"前端**不在本期范围**，以后要做也只能当本设计的又一个前端：同样的请求目录、同样的因素要求。

**相关 issue**：#64（键鼠闸门的拆分绕过）、#65（串口挂起期间能继续直发）、#69（键鼠高危词按模式补全）、#70（PXE 默认菜单 10 秒后自动进带 sshd 的救援）、#49（MCP 拆成只读和动手两个服务）。

## 〇、结论先说

1. **不另起一套挂起状态。** 串口 `serial_write_core.Book`、键鼠 `mcp_kvm.LeaseStore`、PXE `pxe_req_core.Book` 仍然是各自挂起项的唯一事实来源。新增的 `approval_core` 负责三件事：把三处挂起项映射成统一视图；提供一个纯函数 `digest(item)`，原生入口在**自己的 flock 里**用它比对；记录"谁、在哪个通道、用什么因素、做了什么决定"。
2. **人要在手机上看清楚，不能只看电脑。** 维护者和 Agent 往往在同一台电脑上，被 Agent 驱动的浏览器可以让页面显示 A、实际提交 B。所以网页批准都要用**带外码**：推车把服务端生成的摘要和一个跟（账号, id, digest, nonce）绑定的短码，推到维护者手机上，发在与审批专用应用的飞书单聊里。
   带外码**能挡住**的情况：页面为 A 要码，却把码拿去交给 B。
   带外码**挡不住**的情况：页面显示 A，后台直接为 B 发起 challenge。这时手机上收到的是 B 的摘要，只要人在手机上读一眼就会发现不对。所以推送会单独写一句"如果这不是你刚点的那一条，别填码，直接急停"。
   这一步说到底靠的是人在手机上读那条推送。串口 L3 优先直接在飞书卡片上批，不经网页。见 3.3。
3. **第二因素是 TOTP，但它只证明"有人在场"。** 种子只通过 `LoadCredential=` 交给取件单元 `cart-approve`。其他单元都用 `InaccessiblePaths` 遮掉两个位置：种子目录，以及 `/run/credentials`（`LoadCredential` 实际落盘的地方）。**注意，对 root 运行的单元来说，`InaccessiblePaths` 不算边界**：root 可以经 D-Bus 或 `systemd-run` 拉起一个不受限制的单元，再去读。所以在 #49 把 `cart-mcp` 降为非 root 之前，**种子隔离对 `cart-mcp` 不成立**。`approval-check` 会把这一点当作硬性检查项（4.7）。
4. **残余风险如实写。** ops-web 在 `dialout` 组里，能直接写 CH9329 和 conbroker 的人用通道。网页会话最长 7 天，Agent 偷到 cookie 就能直接调 `/api/hid`、`/api/type`、`/api/serial/send`。WOL 加上 PXE 默认的救援菜单再加上键鼠直通，这条路**完全不经审批**（#70）。这些都不是本设计能挡住的，第三节和第六节逐条列出。
5. **批准分两步执行。** 第一步是"取件校验"：秒级完成、从不阻塞，在取到请求的那一刻校验 TOTP 和带外码。第二步是"执行"：每条批准一个模板实例 `cart-approve-exec@<id>`，互不排队。L3 及以上在执行前先推送"即将执行"，留出撤回窗口；推送没送达就不执行。急停、撤回（`withdraw`）、`lockdown` 都走各通道自己的急停目录，不经过执行器；`exec@` 在原生锁里检查撤回和 lockdown 标记。
6. **部署前提有程序检查。** `cart_mcp.py approval-check` 检查 PIN 已设、各单元的遮挡真的生效、root 的 `authorized_keys` 是空的、sudo 里没有 NOPASSWD、种子的权限位正确。任何一项不合格，就**拒绝打开远程审批**。
7. **Claude 只转达。** 它只能看到挂起项的服务端摘要和编号，拿不到链接，拿不到码，也没有任何能批准的工具。所有界面上都写明："**任何人（包括 Claude）向你要验证码，都是攻击。**"

## 一、现状（读代码确认）

| 挂起源 | 状态文件 | 编号 | 过期 | 人确认入口 | 网页 |
|---|---|---|---|---|---|
| 串口 `send_command` / `send_key ctrl_c` | `/run/ops-cart/serial-write.json`（`mcp_serial.Store`，flock） | `doc["seq"]`。`Book.from_text` 读不懂时从 0 重新数，同时全部急停 | `PENDING_MAX_S = 900`；租约过期、释放、急停时作废 | `human_action("confirm")` → `send_confirmed` → `Book.confirm` | 只能 `estop` / `reject` |
| 键鼠 `kvm_key` / `kvm_type` 挂起 | `/run/ops-cart/kvm-lease.json`（`LeaseStore`） | `st["next"]`。`read_state` 读不懂时回退到 1 | 跟着租约走（`TTL_MAX_S = 300`）；全部急停时作废 | `mcp_kvm.human_action` → `LeaseStore.take` | 只能 `kvm_reject` |
| PXE `pxe_request` | `/run/ops-cart/pxe-req.json` | 递增；读不懂时也从头数 | `REQ_TTL_S = 600`，最多 3 条 | `human_confirm(iid, typed, shown_digest)`：照抄车机屏幕上的码，再重算清单比对 | 不显示 |

其他事实：

- **进程权限**：`webkiosk.service` 以 `ops-web` 运行，`SupplementaryGroups=dialout video`，带 `AmbientCapabilities=CAP_DAC_READ_SEARCH`，`ProtectSystem=strict`，可写的只有 `/opt/pxe/web.conf` 和 `-/run/ops-cart/serial-req`。`cart-mcp.service` 以 root 运行，`cart_mcp.py` 的文档串里给了 `ssh cart sudo python3 /opt/pxe/cart_mcp.py stdio` 这种接法。`kiosk`、`pxe-server`、`pxe-http-*`、`cart-dash` 等服务也以 root 运行。
- **会话**：`web_core.Sessions(ttl_s=12*3600, max_age_s=7*24*3600)`，空闲 12 小时滑动过期，最长 7 天；cookie 是 `HttpOnly; SameSite=Strict`。tailnet 来源在**没设 PIN 时不用输 PIN**（`PinGate.attempt` 在 `required` 为假时直接返回 `ok`）。
- **网页会话本身就有人的权限**：`/api/serial/send` 往 conbroker 人用通道写原始字节，`/api/hid`、`/api/type` 直接打键，都不过闸门。
- **键鼠**：`kvm_type` 能带 Tab（`ch9329.KEYS["\t"]`）。`kvm_broker` 只读 `serial-estop.json` 里的 `kvm` 字段，这个字段只有在全部急停时才写，所以**键鼠没有"本路急停"**。`kvm_broker_core.Arbiter.last_human` 记着每块键鼠上人最后一次输入的时刻，但只在代理进程的内存里。
- **PXE**：`pxe_req_core._digest` 就是 `sha256(json)[:16]`，简单清单能从 `[purpose, mac]` 直接推算出来，**不是秘密**。没解锁时，默认菜单 10 秒后自动进 `inst.rescue inst.sshd`（#70）。
- **决策模型**：串口第 2 层（`serial_gate_model`）默认关（`MODEL_DEFAULT["enabled"] = False`）。打开以后，模型能放行改配置的命令，这就是"模型替人审批 Agent"。
- **已知闸门缺口**：#64（键鼠只查最后一行、逐个按键不计入、secret 前半截不计入），#65（串口挂起期间还能直发），#69（`danger_of` 认不出 `rd /s /q`、`> /dev/sda`、`chmod -R 000 /` 这类写法）。

## 二、挂起项的统一模型

### 2.1 字段

`approval_core.collect(serial_view, kvm_view, pxe_view, now) -> [Pending]` 是纯函数。

| 字段 | 说明 |
|---|---|
| `id` | `f"{kind}:{epoch}:{native_id}"`。`epoch` 是**状态文件新建时生成的随机 8 位十六进制**（三处 Book 都加一个 `doc["epoch"]`）。开机、读坏重置（串口 `from_text`、键鼠 `read_state`、PXE `from_text`）都会换一个新的 epoch，所以重号以后的旧 id 也对不上。这比用 boot_id 更好：boot_id 管不了同一次开机里状态文件读坏后重新编号的情况 |
| `kind` / `tool` / `agent` | `serial` / `kvm` / `pxe`；条目自己的 kind 推出工具名；`agent` 是条目的 `who`（MCP 令牌名，只是名字） |
| `target` | 槽位号、键鼠序列号或 MAC。键鼠在**挂起时**就解析出序列号写进条目（`_port` 的结果，不留 `serial=None`），批准时只发给这一块；`target` 为空的挂起项不许远程批准。提示符上认出的设备名放在 `target_label`，标为不可信 |
| `action` | 串口：`cmd`。键鼠：挂起前的行前缀 `prefix`（挂起时 `st["line"]` 的快照，单独保存）加上本次内容，渲染时分开显示"已经敲在屏幕上的"和"批准后才会敲的"。PXE：`purpose` 加清单 |
| `ctx` | 串口：挂起时该槽位的 `send_seq`（2.2）。键鼠：挂起时该键鼠的 `human_seq`（2.2） |
| `evidence` | 串口：挂起时设备最近 20 行输出；键鼠：挂起时的截图。**只给人看，不进 digest**（截图每次都不一样，进了 digest 也没有意义） |
| `danger` / `gate_reason` / `model_reason` / `intent` | `intent` 是 Agent 自述，没有核实过；`model_reason` 是模型意见 |
| `tier`、`created`、`expires`、`digest`、`status` | |

### 2.2 digest 和现场绑定（A1 一次定好，后面不改公式）

```
digest = sha256(canonical_json({"v": 1, id, kind, target, action, danger, created, ctx}))[:16]
```

- **A1 就把 `send_seq` 和 `human_seq` 做出来**（只是加计数，不改变行为），这样 A1 和 A2 的 digest 是同一个公式，测试不会出现"A1 测了 A2 才有的字段"。
- **串口 `send_seq` 无条件计数**：这个槽位上的任何发送都加 1，包括闸门放行的直发、同槽位其他挂起项被批准后发出（`PENDING_MAX = 5`，兄弟挂起项发出也要算）、人用通道的写入。人用通道的写入走 conbroker，Book 看不到，分两路处理：
  - 网页的 `/api/serial/send`：该槽位有挂起项时，终端上方显示"有 N 条等审批，你现在键入会让它们作废"；每条挂起项**挂起之后**，人第一次写入时往 `serial-req` 投一个 `human_touch` 请求（网页按"槽位 + 当前最大挂起编号"记住已经投过，同一批挂起项不重复投），root 收到后给 `send_seq` 加 1。
  - 车机本机终端和其他 conbroker 客户端：conbroker 在另一个仓库，要请它在人用写入时更新一个公开的计数文件（**待核**）。在那之前，审批摘要里写明"人用通道的写入可能没有计入"。
  - #65 如果采用"有挂起项时禁止直发"，`send_seq` 仍然保留，用来覆盖兄弟挂起项和人用通道的情况。
- **键鼠 `human_seq`**：`kvm_broker` 每次接受一个人用通道的有效输入（也就是更新 `Arbiter.last_human` 的地方），就把 `{序列号: {seq, last, ts}}` 写进 `/run/ops-kvm/kvm-human.json`（0644，不含内容）。`kvm-broker.service` 是 `ProtectSystem=strict`，现在只能写 `/var/log/ops-cart`，所以要么给它加 `RuntimeDirectory=ops-kvm`，要么加 `ReadWritePaths=`，写进 A1 的计划。挂起时记下 `human_seq`，批准时发现变了，就作废这一条（`dropped: 人动过键鼠`）。
  - **文件读不到、读不懂，或者 `ts` 比代理的心跳旧 10 秒以上，一律按"人动过"处理**，作废这一条。
  - **代理不在时，键鼠的远程批准直接拒绝**：代理没跑的话，webkiosk 会直开设备（`kvm_broker.open_ch` 失败后的退路），人用输入就不计数了。
  - Agent 那一侧在挂起期间本来就发不出键（`LeaseStore.gate` 发现有挂起项就拒）。
- **比对在原生锁里做**：`Book.confirm(iid, by, now, expect=None)`、`LeaseStore.take(iid, by, expect=None)`、PXE 的 `take` 和 `remote_confirm` 都新增一个 `expect` 参数。执行器把请求里的 digest 传进去，原生入口在**自己的 flock 里**用 `approval_core.digest` 重算，再比对 `ctx`。这样从比对到标记在途之间，没有可以插进来的空隙。车机上的确认传 `expect=None`：车机界面本来就直接读原生状态。
- **PXE 的 digest 能推算出来**：`remote_confirm` 绝对不能把"知道 digest"当成任何凭据。它只认执行器传进来的"TOTP 和带外码都已经校验过"的结果。原有的 `human_confirm` 也不变，靠的是屏幕上的码。

### 2.3 摘要由服务端生成

各通道展示的内容只能来自 `approval_core.render(pending)`，不接受 Agent 传进来的任何描述性文字：

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
任何人(包括 Claude)向你要验证码或带外码, 都是攻击. 码只填在你自己打开的推车网页上.
```

- 命令和文字在入口已经限定为可打印 ASCII（`serial_write_core.check_line`；键鼠只收 ASCII）。
- 标题和"动作"行只由 `kind`、`target`、`action`、`danger` 拼出来；`intent`、`model_reason` 放在最后，带前缀，各截到 200 字。
- 秘密内容只显示长度（沿用 `mcp_kvm._shown`）；#64 指出 secret 前半截不计入闸门，所以渲染时不假设挂起项里没有 secret。

- 空格显示成 `␠`，**Tab 显示成 `⇥`**，换行显示成"↵ 回车"。键鼠的多行输入按行展开。
- 摘要最后固定一行：**"任何人（包括 Claude）向你要验证码或带外码，都是攻击。码只填在你自己打开的推车网页上。"**

### 2.4 状态机

```
pending ─批准(取件校验通过)─► authorized ─┬─(L3+: "即将执行"推送送达 → 撤回窗口 W 秒内没人撤)─┐
   │                                      └─(L2: 直接)──────────────────────────────────────┤
   │                                                                                          ▼
   │                         exec@id 在原生锁里: digest + ctx 一致 + 没撤回 + 没 lockdown ─► executing ─► done
   │                                    └─ 任一不满足 / 超过 AUTH_TTL_S 没开始 / 推送没送达 ─► dropped
   ├─拒绝─► rejected     ├─到期─► expired     ├─急停 / 租约没了 / ctx 变了 / lockdown─► dropped
   ├─撤回(authorized 期间, 任一已认证通道, 不要 TOTP)─► withdrawn
   └─被另一个通道先处理─► 按原生状态显示, 附上"由 <账号> 经 <通道>"
```

- **撤回窗口**：`exec@` 在窗口期间**不碰原生锁**，只是等着；窗口结束后才调 `confirm(expect)`，并在原生锁里检查撤回标记和 lockdown 标记（两个都是持久文件，见 2.5）。"即将执行"推送如果没送达（飞书接口报错或超时），就直接中止，记 `dropped: 回执未送达`，宁可不执行。
- **lockdown**：写一个持久标记 `/var/lib/ops-cart/approval-lockdown`，同时把所有 `authorized` 状态的条目作废。`exec@` 在原生锁里也查这个标记，所以 lockdown 之前已经批准、还没开始发的，一个字节都不会发。已经交给 conbroker 的那一条收不回来，这跟急停一样，靠急停去拦。

决定记录放在 **`/var/lib/ops-cart/approvals.json`**（root 0600，flock，原子写）。里面有每个账号的 TOTP `last_step`、输错计数和冷却时间，已经用过的 nonce（保留 24 小时），以及每条挂起项的决定。放在 `/var/lib` 而不是 `/run`，是为了重启以后防重放和冷却记录还在。挂起项本身在 `/run`，重启就没了，跟现在一样。

### 2.5 执行、并发、急停

- **取件**：`cart-approve.path` 盯着 `/run/ops-cart/approve-req/<通道>/`，拉起 `cart-approve.service`（oneshot）。它只做校验和登记：解析请求，校验 TOTP 和带外码（**校验时刻就是取件时刻**，取件从不阻塞），通过就记 `authorized`，然后 `systemctl start --no-block cart-approve-exec@<id>`。请求文件照旧只认 60 秒以内的。TOTP 按取件时刻容许 ±1 步。**只有取件单元有 `LoadCredential`**。
- **执行**：`cart-approve-exec@.service` 每条批准一个实例，**不带 `LoadCredential`**（因素在取件时已经校验完）。它调原生入口，并传入 `expect`。
- **同一个槽位一次只能批一条**：`Book.confirm` 遇到在途会抛 `Refused("inflight")`；第一条发出去以后 `send_seq` 加 1，同槽位的第二条就必然作废。界面上写明："同一路一次批一条；这条发出后，同一路其余挂起项会作废，要 Agent 重新提交。"
- **结果回传**：root 写 `/run/ops-cart/approve-res/web.json` 和 `/run/ops-cart/approve-res/feishu.json`（0640，属组分别是 ops-web 和飞书服务的组；只有 root 写，两个服务都只读）。内容是按 nonce 索引的结果，以及每个账号当前的 challenge 状态（不含码）。网页和飞书服务据此显示"已提交 / 已批准 / 被拒（原因）"。
- **幂等**：nonce 只能用一次（重启后也是）。同一条挂起项上重复同一个决定，返回 `already`；先后两个相反的决定，后到的返回 `conflict`。**撤回豁免 `conflict`**：撤回本来就是"先批准、后反悔"。车机确认和远程批准撞上时，先拿到原生锁的那个生效。
- **急停和 lockdown 都不经过审批执行器**：每个通道一个急停目录，由各自的单元处理（网页：`serial-req` → `cart-serial-req`；飞书：`estop-req/feishu/` → `cart-estop-feishu`），解析都用 `confirm_core.parse_request`，在原有的 `estop`、`reject`、`kvm_reject` 之外，再加 `kvm_stop`、`withdraw`（带 id，停的方向）和 `lockdown`。
- **键鼠的"本路急停"要新做，而且要有自己的持久状态**：`mcp_serial.Store._flag` 每次存状态都会**重新算**一遍 `serial-estop.json` 的 `kvm` 字段（`everything or (bool(stopped) and serial_all_stopped(path))`）。没有串口槽位急停时，这个字段会被算成 False，单独设置的值会被冲掉。所以：
  - `kvm_stop` 写自己的状态 `/run/ops-cart/kvm-stop.json`（root 0600：谁、何时、来源），同时收回键鼠租约、丢掉挂起项。
  - `_flag` 合并时改成 `kvm = 原有算法 or kvm_stop 生效中`：**只能把它置真，不能把它清掉**。
  - 解除 `kvm_stop` 只能删 `kvm-stop.json`，然后重算一遍。`kvm_broker` 不用改。
- **急停里的拒绝不带 digest，明确豁免**：`serial-req` 和飞书急停目录里的 `reject`、`kvm_reject` 照旧只带 id。拒绝是"停"的方向，id 投错了，最多是多拒掉一条。审批目录里的 `reject` 要带 digest，因为它和批准共用同一套记录。

## 三、风险分级

### 3.1 级别和各通道要求

| 级别 | 含义 | 车机（现场） | 飞书单聊（只在手机上） | 网页（不在跑 Agent 的机器上） |
|---|---|---|---|---|
| L0 | 只读，或者"停"的方向 | 不用审批 | 不用审批 | 不用审批 |
| L1 | 不碰被测设备的配置 | 直通 | 直通 | 直通 |
| L2 | 改设备状态，但一般能撤回 | 点"确认" | 点卡片并填 **TOTP** | 审批会话（4.2）加 TOTP；A3 以后再加**带外码**（A2 阶段网页 L2 没有带外码，列为残余风险 T12） |
| L3 | 可能中断业务、丢配置 | 点"确认"，再问一次 | 串口：点卡片并填 TOTP。**键鼠：只能拒绝**，批准要去网页看实时画面 | TOTP 加**带外码**；键鼠还要看实时画面 |
| L4 | 不可逆，或者进入一个能任意写盘的环境 | 照抄屏幕确认码 | 只能拒绝 | TOTP 加带外码，再**复述目标**；30 秒撤回窗口 |

- 同一个账号 30 秒内只能批一条 L3 或 L4。
- **飞书只用单聊**：审批卡片只发到审批专用应用和白名单账号的**单聊**里，不发到群里。群里的其他机器人可以发长得一模一样的仿冒卡片，单聊里只有这个应用能发。

### 3.2 工具逐个定级，以及"不受审批保护"的路径

| 工具 / 情形 | 建议级别 | 理由 |
|---|---|---|
| 只读工具：`list_threads`、`read_output`、`screenshot`、`cart_status`、`pxe_status`、`pending_list`、`kvm_status`、`screen_ocr`、`pxe_clients`、`pxe_requests`、`label_preview`、`label_printer_status`、`label_abbrev_propose`；以及 `estop`、`pxe_retire`（停、收紧） | L0 | |
| `lease_*`、`kvm_lease_*`、`label_print`、`send_key enter/space`、`kvm_key` 普通键 | L1 | 不变 |
| `wol_send` | L1，**加注** | 本身无害，但它是下面那条"不受保护的路径"的第一步 |
| `send_command` 过了第 1 层（只读白名单） | L1 | |
| **`send_command` 过了第 2 层（模型放行）** | **单列："模型审批"** | 实际上是模型在替人审批 Agent，能放行改配置的命令。默认关；`approval-check` 发现远程审批和模型同时打开时给出警告（待拍板第 16 条） |
| `send_command` 挂起、不带高危词 / `send_key ctrl_c` | L2 | 配置命令多数能 `undo`；靠 `send_seq` 保证批的就是挂起时那个现场 |
| `send_command` 挂起、带高危词 | L3 | |
| 键鼠挂起（默认） | L3 | 闸门是减速带（#64、#69）。只有白名单（`alt+f4`）降到 L2 |
| `kvm_key ctrl+alt+delete`、`alt+sysrq` | L3 | |
| **`pxe_request` 的全部用途（winpe、clone-save、rocky、clone-restore）** | **L4** | 统一按"进入一个能任意写盘的环境，或者直接写盘"定级。WinPE 和 Clonezilla live 里都有 shell，加上键鼠就能清盘，跟 rocky 的后果是同一级。clone-save 也按这条定 L4，理由跟 WinPE 一致 |
| 解除急停 | 远程 L3 | 待拍板第 4 条 |

**不受审批保护的路径（如实列出）**：

| 路径 | 为什么没有审批 | 跟踪 |
|---|---|---|
| `wol_send`（L1）→ 没解锁的默认 GRUB 菜单 10 秒后自动进 `inst.rescue inst.sshd` → `kvm_type` 在救援 shell 里敲清盘命令 | 全程只有 L1 操作，外加键鼠直通。键鼠闸门的词表认不出 `rd /s /q`、`> /dev/sda`、`chmod -R 000 /` 这类写法 | #70（默认菜单要不要改成不自动进救援、不带 sshd）、#69（按模式补全词表）、#64 |
| 键鼠直通的一切 GUI 操作（鼠标点选、图形界面里的"格式化"） | 推车看不懂屏幕 | 只能靠人盯和急停 |
| 网页会话（人用通道）：`/api/hid`、`/api/type`、`/api/serial/send` | 设计上它就是"人"；Agent 偷到 cookie 就能冒充（T0、T13） | 4.7 部署前提；待拍板第 15 条 |
| ops-web 被拿下后直接写 CH9329 和 conbroker | `dialout` 组 | 待拍板第 15 条 |

所以，**PXE 定 L4 只有在 #70 解决以后才名副其实**。在那之前，审批页和 `approval-check` 都要提示："默认救援菜单仍会自动进 shell，PXE 的分级不构成保护。"

### 3.3 因素：TOTP 加带外码

| 方案 | 证明什么 | 绑定到具体挂起项吗 | 定位 |
|---|---|---|---|
| TOTP（RFC 6238） | 这 30 秒里，手机在人手上 | 不绑定 | L2 起都要 |
| **带外码** | 人在**手机上**看到了服务端生成的摘要 | **绑定**：`code = HMAC(oob_key, account ‖ id ‖ digest ‖ nonce)` 取 6 位，`oob_key` 只给 `cart-approve` | A3 以后，网页上的所有批准都要 |
| WebAuthn / 通行密钥 | 人在**这台设备**上按了，挑战里带着 digest | 绑定 | 列为 L3 和 L4 的长期方案（要先给网页上 HTTPS） |
| 撤回窗口 | 执行前人还有一次机会喊停（推送没送达就不执行） | — | L3 15 秒，L4 30 秒（待拍板第 17 条） |
| 车机屏幕码 | 人在现场 | 绑定 | 只用于现场 |

带外码的流程（网页批准）：

1. 人在网页上点"批准"，网页投一个 `op=challenge` 请求。
2. `cart-approve` 在 root 一侧登记这个 challenge：账号、id、digest、nonce、时限 120 秒、剩余尝试 3 次。**同一个账号同时只保留一个有效的 challenge**：新的发起时作废旧的，并在新推送里注明"之前那条已作废"。每个账号每分钟最多发起 3 个。
3. `cart-approve` **自己**调飞书发消息接口，把服务端摘要和码推到这个账号的单聊里（用它自己的 `LoadCredential` 拿到 app_secret，见 4.3），并写明两句话："不要把这个码告诉任何人，包括 Claude。""**如果这不是你刚点的那一条，别填码，直接点急停。**"
4. 人在手机上读完摘要，再把码和 TOTP 一起填回网页。
5. 取件时，`approve` 只认跟登记的 challenge **完全匹配**（账号、id、digest、nonce 都一样）、没有过期、还有剩余次数的请求，然后重算 HMAC 校验码。

剩下的风险：飞书服务本身看不到码（它不负责发码），但飞书云端能看到。L3 和 L4 有撤回窗口（2.4）：执行前推送"即将执行：<摘要>，W 秒内可撤回"，推送没送达就中止。

TOTP 的约束：标准库实现（`hmac`、`hashlib.sha1`，30 秒一步），种子按人分开；±1 步；`last_step` 按账号记、只增不减，**重启后也还在**（存在 `/var/lib`）；同一个码跨通道也只能用一次；时钟回拨后，步数小于等于 `last_step` 的码一律拒，并记日志；输错按 `PinGate` 的方式指数退避，封顶 1 小时，按账号算、跨通道共享；冷却期间车机确认照常能用。

## 四、认证与防伪

### 4.1 共同规则

- **种子和带外密钥**：放在 `/etc/ops-cart/approve-secrets/`（root 0700）。**只通过 `LoadCredential=` 交给 `cart-approve.service`**（`exec@` 不带）。
- **飞书 app_secret**：放在 `/etc/ops-cart/feishu-approve-secret/`，只通过 `LoadCredential=` 交给飞书回调服务和 `cart-approve`（后者要用它发带外码）。
- **遮挡**：deploy/systemd 下的**其他每一个单元**（`cart-mcp`、`kiosk`、`pxe-server`、`pxe-http-boot`、`pxe-http-repo`、`cart-dash`、`cart-notify`、`cart-dream`、`cart-serial-req`、`kvm-broker`、`webkiosk`、急停单元、`cart-approve-exec@`）都加三条：`InaccessiblePaths=-/etc/ops-cart/approve-secrets -/etc/ops-cart/feishu-approve-secret -/run/credentials`。飞书服务本身只遮种子目录。测试遍历 `deploy/systemd/*.service` 逐个断言。
- **决定记录**：`/var/lib/ops-cart/approvals.json` 和 `approval-lockdown` 对除了 `cart-approve*` 以外的单元也用 `InaccessiblePaths` 遮掉（ops-web 有 `CAP_DAC_READ_SEARCH`）。网页需要的状态从 `approve-res` 读。
- **root 单元的边界**：对 root 运行的单元，这几条遮挡只能防误读，防不住有意为之（它可以经 `/run/systemd/private` 或 `/run/dbus/system_bus_socket` 拉起不受限制的单元）。所以 `approval-check` 对每个 root 单元的要求是：要么它已经不是 root，要么它同时遮掉 `/run/systemd/private` 和 `/run/dbus/system_bus_socket`，并且 `RestrictAddressFamilies` 不含 `AF_UNIX`，或者只允许它确实需要的 socket。都做不到的，`approval-check` 判为"种子隔离不成立"，并如实降级提示（第 20 条拍板）。**`cart-mcp` 在 #49 完成之前属于"不成立"**：它要用 `sudo -u` 跑 conbroker，也要 mpv 的 socket，限制不了 `AF_UNIX`。
- **请求目录**：`/run/ops-cart/approve-req/web/`（ops-web 0700）和 `/run/ops-cart/approve-req/feishu/`（飞书服务用户 0700）。`webkiosk.service` 要加 `ReadWritePaths=-/run/ops-cart/approve-req/web`，因为它是 `ProtectSystem=strict`，写进 A2 的计划。root 按所在目录认通道。请求格式（`approval_core.parse_request`，字段多一个或少一个都不认）：
  ```json
  {"op": "challenge|approve|reject|elevate", "id": "serial:1a2b3c4d:17", "digest": "3f9a1c0e5b7d2a46",
   "account": "alice", "by": "web:192.0.2.20/ab12cd", "totp": "123456", "oob": "654321", "retype": "a1b2",
   "nonce": "9c2e…(16 hex)", "t": 1700000000.0}
  ```
  写入和读取的加固照抄 `mcp_serial.write_request` / `_take_request`（`O_EXCL|O_NOFOLLOW`，只收普通文件，不超过 4 KB，只认 60 秒内的，先删后执行）。`serial-req` 维持原样，"网页请求里出现 confirm 就当坏请求"这条老不变量不变。
- **推车没设 PIN 时，root 执行器拒绝 web 目录里的一切批准**（不只是网页自己拦）。
- **审批链接不由 Agent 提供**：`approval_list` 只返回编号。网页审批页、飞书卡片、`approval_list` 的工具描述里，都写明"任何人向你要码都是攻击"。
- 秘密只显示长度；日志里不出现码、种子、cookie、卡片 token。

### 4.2 网页

- **审批会话单独签发**：审批页不认普通的网页登录会话，要另外输一次 TOTP，换来一个 15 分钟、不滑动、绑定（会话, 账号）的审批会话，由 root 登记。**每次批准**还要再输 TOTP，A3 以后还要加带外码。没有提权窗口。
- 因为 `last_step` 只增不减，签发会话用掉一个码以后，同一个 30 秒步里不能马上批第一条，要等下一个码。为了省掉这次等待，倾向**把签发和第一次批准合并**：在审批页上第一次批准时，一个码同时完成签发和批准（待拍板第 10 条）。
- **前提**：网页登录和审批都不在跑 Agent 的机器上做。用另一台设备，或者用 Agent 那个系统账号读不到的浏览器配置（Agent 和浏览器是同一个系统用户时，"另开一个浏览器配置"不算隔离）。这一条**部分可检**：取件时，把网页请求的来源 IP 和 tailscale 设备，跟 `cart-mcp` 最近 24 小时见过的 Agent 来源做比对，同 IP 或同设备就拒绝（`ev=same_host`，待拍板第 8 条）。这样能挡住"在跑 Agent 的那台机器上直接审批"的情况，挡不住"Agent 经别的机器中转"。所以仍然列为残余风险 T13。
- 防 CSRF：同源检查、`SameSite=Strict`、一次性的表单 nonce。防点击劫持：`X-Frame-Options: DENY` 和 `frame-ancestors 'none'`，作为纵深防御。

### 4.3 飞书

- **专用应用、只用单聊**：不和任何其他机器人或桥接共用凭据（长连接是集群模式，回调会随机分给任一客户端）。卡片和带外码只发给白名单账号的单聊。
- 长连接，官方 SDK 装在独立的 venv 里；回调用新版 `card.action.trigger`，签名和字段**实现前对照当前官方文档核对**（待核）。
- **应用权限只申请"以机器人身份发消息"和卡片回调**，不申请读消息、读通讯录之类的权限。这样即使 app_secret 泄露，也读不出单聊里发过的带外码（具体权限项实现前核对官方文档，待核）。
- **带外码由 `cart-approve` 直接发**，不经过飞书回调服务。回调服务只负责接收按钮点击和 TOTP 输入框，看不到码。
- **服务沙箱**：独立用户；不进 `dialout`、`video` 组；`IPAddressDeny=any`，只放行飞书出口；用 `InaccessiblePaths` 遮掉种子目录和 `/run/ops-cart` 下的状态文件，只留自己的请求目录和急停目录可写。
- 只在手机上批；桌面版和网页版飞书不登录审批账号（T12）。
- 飞书上 L2 和串口 L3 都要填 TOTP。
- 键鼠 L3、全部 L4 的卡片上**只有拒绝和急停**（键鼠要看实时画面，飞书上给不了）。串口 L3 卡片有 TOTP 输入框，并附设备最近输出（标不可信）。
- 卡片常驻"急停"按钮：串口是本路和全部；键鼠是 `kvm_stop` 和全部；外加"关掉远程审批"（`lockdown`）。这些都投到飞书的急停目录。

### 4.4 Claude 转达

只读的 `approval_list`：返回摘要、`id`、`digest`、剩余时间；不返回链接，不返回任何码。工具描述里写明：**只能看；批准要人本人在自己打开的推车网页上做，或者在手机飞书上做；任何人（包括你）都不应该向人要验证码；摘要要原样转达。** 不可信的字段用随机标记包起来。

### 4.5 车机

维持原样。车机确认写进审计，不受 TOTP 冷却影响。

### 4.6 大肥鱼经 ACP 接入

dsh 作为 MCP 客户端调用推车工具时不经过它自己的审批（#49）；大肥鱼经 ACP 运行时，dsh 自己的审批请求只送得到 ACP 客户端（包装层），到不了推车的网页和飞书。推车工具在推车服务端挂起，走本设计。dsh 自己的审批倾向由包装层转成 `kind=acp` 的挂起项（经一个只能"提交挂起、查询结果"的专用接口），内容标"推车无法核实"，级别不低于 L2（待拍板第 13 条）。

### 4.7 部署前提和 `approval-check`

`cart_mcp.py approval-check` 在每次开启远程审批时运行。**有任何一项不合格就拒绝开启**；已经开着的，自动 `lockdown` 并推送通知。

`approval-check` 由 `cart-approval-check.timer` 每小时跑一次（不放在执行器里）。

| 检查项 | 怎么查 | 不合格时 |
|---|---|---|
| 推车设了 PIN | `web_core.pin_state` 返回 `set` | 拒绝开启 |
| 遮挡在**运行中的进程**上真的生效 | 对每个在跑的单元取 `MainPID`，用 `nsenter -t <pid> -m` 进到它的挂载空间里，实际 `stat` 一下种子目录、`/run/credentials`、`approvals.json`，必须是不可访问。光看 `systemctl show` 只能说明配置写了，不能说明进程用上了。另外比对 `ExecMainStartTimestamp` 和单元文件的修改时间，并检查 `NeedDaemonReload`：改了配置但没重启的，算不合格 | 拒绝开启 |
| root 单元的边界 | 见 4.1：不是 root，或者遮掉了 systemd 和 D-Bus 的 socket 并限制了 `AF_UNIX` | 判为"种子隔离不成立"，按第 20 条拍板结果处理（拒绝开启，或者降级：只开 L2） |
| 飞书凭据 | `feishu-approve-secret` 的权限位；只有飞书服务和 `cart-approve` 有 `LoadCredential` | 拒绝开启 |
| 种子权限 | 目录 0700、文件 0600，属主 root | 拒绝开启 |
| **root 登录面** | ① Tailscale SSH：`tailscale debug prefs` 里 `RunSSH` 为真，**直接不合格**；② sshd：用 `sshd -T` 取 `permitrootlogin`（必须是 `no`）、`authorizedkeysfile`（逐个文件检查 root 有没有密钥）、`authorizedkeyscommand`（设了就不合格）、`passwordauthentication`；③ 能 sudo 的普通用户：`getent group wheel` 加 `sudo -l -U`，有 NOPASSWD 就不合格；④ 等同 root 的组（`docker`、`disk`、`lxd`、`libvirt`）里有没有成员；⑤ `cart-revtunnel` 把本机 22 端口反向映射出去（`-R localhost:<端口>:localhost:22`），跳板机那头的人就能到推车的 sshd，所以要求 ②③ 也成立 | ① 到 ④ 拒绝开启；⑤ 提示 |
| 提示项（不拒绝） | 决策模型是否打开（第 16 条）；#70 的默认救援是否还在；`same_host` 比对是否开着 | 提示 |

**范围**：`approval-check` 只检查本仓库 `deploy/systemd` 里的单元，以及上面列出的登录面。推车上别的软件装的单元和 root 进程不在检查范围内，列为残余风险（T17）。

**从 `ssh cart sudo …` 接法迁移**：

1. 在推车上给每个 Agent 生成一个 Bearer 令牌，写进 `/etc/ops-cart/mcp.json`（`examples/mcp.example.json` 有样例）。
2. 启用 `cart-mcp.service`（http 模式，只绑本机和 tailnet 地址）。
3. Agent 那边改成 `claude mcp add --transport http cart http://<推车>:8765/ -H "Authorization: Bearer <令牌>"`。
4. 删掉 Agent 机器上能 sudo 的 ssh 密钥，然后在推车上跑一遍 `approval-check`，确认 root 登录面这一项通过。

文字层面的前提：PIN、种子、飞书凭据都不放在 Agent 的机器上；审批飞书账号只在手机上登录。

### 4.8 审计

- `/var/log/ops-cart/approval.log`（JSONL；`*.log` 已有的 logrotate 规则管）。每行包括：`t`、`ev`、`id`、`digest`、`kind`、`tier`、`channel`（kiosk / web / feishu / cli）、`account`、`by`（web 是 `ip/会话标签`，feishu 是 open_id，kiosk 是 `human:kiosk@local`）、`factor`（`totp:ok`、`oob:ok`、`none`）、`summary`（就是 `render` 的标题和动作行；secret 只显示长度）、`result`。
- `ev` 有这些：`notify`、`challenge`、`approve`、`authorized`、`executed`、`reject`、`expire`、`drop`、`lost_race`、`conflict`、`replay`、`forbidden`、`totp_bad`、`totp_lock`、`oob_bad`、`clock_back`、`lockdown`、`check_fail`、`withdraw`、`receipt_fail`、`same_host`、`challenge_superseded`。
- 车机确认也要写一行（通道 `kiosk`，因素 `none`），这样审计是完整的。原生日志（`serial-slot<N>.log`、`kvm-thread.log`、`pxe-req.log`）照旧记各自的 `confirm` 和 `send`。
- `events.log` 只记一行 `[approval]` 摘要，不写命令原文。
- 日志里永远不出现：TOTP 码、带外码、种子、cookie、卡片 token、nonce 原文（只记前 4 位）。

## 五、分阶段落地

| 阶段 | 内容 | 前置 |
|---|---|---|
| A0 | 本设计稿 | — |
| A1 统一视图（只读） | `approval_core`（`collect`、`TIER`、`render`、`digest` v1）；三处 Book 加 `epoch`（**升级步骤**：旧版状态文件里没有 `epoch`，按"读不懂"处理会全部急停。所以缺 `epoch` 时就地补一个新的，不算读坏；只在 JSON 坏了的时候才走原有的全部急停）；串口 `send_seq`；键鼠挂起时写定 `serial` 和 `prefix`；`kvm_broker` 写 `/run/ops-kvm/kvm-human.json`（加 `RuntimeDirectory`）；`approvals` 命令行；网页只读页；`approval_list` | #64、#65 |
| A2 执行器 + TOTP + 网页 | `totp_core`；`totp-enroll`；种子目录、`LoadCredential`、所有单元的 `InaccessiblePaths`；`approval-check`；`cart-approve` 和 `cart-approve-exec@`；原生入口的 `expect` 参数；`remote_confirm`；`/var/lib/ops-cart/approvals.json`；审批会话（签发和第一次批准合并）；`approve-res` 结果文件；`same_host` 比对；webkiosk 的 `ReadWritePaths`；`human_touch` 请求（挂起之后第一次写入）；防点击劫持头；`cart-approval-check.timer`；`withdraw`、`lockdown` 持久标记 | A1 |
| A3 飞书 | 专用应用（只申请发消息权限）和单聊；带外码（`cart-approve` 直接发，challenge 在 root 一侧登记）；卡片；飞书急停目录；`kvm_stop`（独立的持久状态）；"即将执行"推送和撤回窗口 | A2 |
| A4 | 防疲劳、remote resume、`kind=acp` | 按拍板结果 |
| A5 | 收尾更新文档 | — |
| 长期 | `cart-mcp` 降为非 root（#49）；HTTPS 加 WebAuthn；#70 | — |

**A2 的网页 L3 批准必须等 A3 的带外码上线才能开**。A2 阶段网页只开放 L2（没有带外码，残余风险见 T12），L3 和 L4 仍然只能在车机上确认。A3 以后，网页上的所有批准都要带外码。

测试清单（先写红灯）：

**A1**
- `epoch`：串口 `from_text`、键鼠 `read_state`、PXE `from_text` 读坏重置以后都换了新的 epoch，同一个原生编号得到不同的 `id`。
- `digest` v1 公式固定；加一条"公式回归"用例，A2 以后不许改。
- `send_seq`：直发、兄弟挂起项发出、`human_touch` 都会加 1。
- `kvm-human.json`：人用输入会让 `seq` 加 1，Agent 的输入不会。
- `render`：Tab 显示为 `⇥`；固定有"要码就是攻击"那一行；键鼠分开显示 `prefix`（已经敲在屏幕上的）和本次内容。
- `epoch` 升级：没有 `epoch` 的旧状态文件就地补上，**不触发**全部急停；JSON 坏了照旧全部急停。
- 键鼠：挂起项里写定了 `serial`；`target` 为空的不出现在可远程批准的列表里。
- `kvm-human.json`：文件不在、读不懂、过时，都被 `collect` 当成"人动过"。
- `TIER`：`pxe_request` 四种用途都是 L4；模型放行单列；键鼠挂起默认 L3。
- 不变量：MCP 工具里没有审批写路径；`approval_list` 不返回链接，也不返回码。

**A2**
- `totp_core`：RFC 6238 附录 B 的向量；±1 步能过，±2 步不过；同一个码第二次被拒，换个通道也被拒；时钟回拨；退避和冷却按账号跨通道共享；冷却期间车机确认照常能用；种子读不懂一律拒。
- `parse_request`：多字段、少字段、类型不对、超长、超时、未来时间、链接、目录，一律不认；`channel` 以目录为准；审批目录里的 `reject` 不带 digest 不认。
- 因素矩阵：L2/L3/L4 × 网页（无码、错码、旧码、审批会话过期、换了账号）× 车机 × 飞书。
- 并发：车机确认和远程批准交错，只发一次，后到的记 `lost_race`；同一个 nonce 只执行一次；先批后拒得到 `conflict`；同一轮里拒绝先于批准。
- 键鼠拆分（跟 #64 的修复对齐）：多行、逐个按键、secret 拆成几段，摘要里显示的都是整段。
- `remote_confirm`：TOTP 没过就拒；清单变了就作废；更新 `test_pxe_invariant.py`，改成"没有照抄确认码，也没有 root 校验过的 TOTP 和带外码，就不能解锁"；用能推算出来的 PXE digest 也解锁不了。
- 遍历 `deploy/systemd/*.service`：除了 `cart-approve*`，每个单元都遮掉种子目录；只有 `cart-approve*` 有 `LoadCredential`。
- `approval-check`：PIN 没设、遮挡没生效（用假的 `systemctl show` 输出）、`authorized_keys` 不为空、有 NOPASSWD、权限位不对，逐项都拒绝开启。
- PIN 没设时，web 目录里的批准被执行器拒绝。
- **在原生锁里比对 digest**：在执行器取件和执行之间篡改挂起项（同 id、换内容），`Book.confirm(expect=…)` 在锁里拒掉，一个字节都不发；`ctx` 变了同样拒绝。
- 键鼠：挂起以后人用通道动过键鼠（`human_seq` 变了），批准被作废。
- 取件不阻塞：一个 exec 实例卡在发送上，这时又来一个批准，取件照样在一秒内完成校验。
- `AUTH_TTL_S` 内没开始执行就作废。
- 急停在 exec 阻塞期间照常生效。
- 重启以后（重新加载 `/var/lib` 的文件），`last_step`、冷却、nonce 都还在，旧码和旧 nonce 都被拒。
- 审批会话：15 分钟到期；不滑动；换了账号无效；普通登录会话进不了审批页。
- 网页 L3、L4 在 A2 阶段被拒（带外码还没上线）。
- 遍历单元文件：除了 `cart-approve.service`，每个单元都遮掉种子目录、飞书凭据目录和 `/run/credentials`；`exec@` 没有 `LoadCredential`；`approvals.json` 也被遮掉。
- `approval-check`：用假的 `nsenter` / `stat` 结果模拟"配置写了、进程没生效"，以及 `NeedDaemonReload=yes`、`ExecMainStartTimestamp` 早于单元文件，都判不合格；root 单元没遮 D-Bus 和 systemd 的 socket，判"种子隔离不成立"；登录面逐项检查（`RunSSH`、`permitrootlogin`、`authorizedkeyscommand`、NOPASSWD、docker/disk/lxd 组成员）。
- `same_host`：网页请求的来源跟 `cart-mcp` 见过的 Agent 来源相同就拒。
- `lockdown`：`authorized` 之后、执行之前打开 lockdown，一个字节都不发；lockdown 标记重启后还在。
- 撤回：`authorized` 期间撤回，`exec@` 在原生锁里看到撤回标记，不执行；撤回不算 `conflict`。
- `approve-res`：只有 root 写；ops-web 能读到结果，读不到别的账号的 challenge 细节以外的东西（里面本来就不含码）。
- 同一个槽位两条都批了：第二条要么收到 `Refused("inflight")`，要么因为 `send_seq` 变了被作废，只发一条。
- 签发审批会话和第一次批准合并时，只用掉一个码，紧接着的下一条要等下一个码。

**A3**
- 回调：白名单之外的账号、`app_id` 或 `tenant_key` 不对、事件重复，都按预期处理；卡片在被别的通道处理、过期、作废以后，没有能点的按钮。
- 带外码：换账号、换 id、换 digest、换 nonce 都会对不上；同一个码只能用一次；码只发到单聊；同一个账号同时只有一个有效的 challenge，新的作废旧的；超过频率限制被拒；推送里有"不是你点的就别填码"那一句；推送由 `cart-approve` 发，飞书回调服务的代码路径里看不到码。
- 卡片：键鼠 L3 和全部 L4 的卡片上没有批准按钮；有急停、`kvm_stop` 和 `lockdown`。
- `kvm_stop`：只停键鼠，串口不受影响；`kvm_broker` 读到 `kvm` 为真时不发键；**`kvm_stop` 以后再做串口的 `lease_acquire`、急停解除等任何会让 `_flag` 重算的操作，`kvm` 仍然为真**；解除后要重新申请租约。
- "即将执行"推送失败（模拟飞书接口报错）时，`exec@` 中止，一个字节都不发。
- `lockdown` 和急停走急停目录，在执行器阻塞时照常生效；开回来只认 `local_human`。
- 撤回窗口内撤回，就不执行。

**待实机验证**：path 单元能不能被拉起；飞书长连接断线重连，卡片在手机上的显示；车机有没有 NTP；飞书回调字段和签名（对照官方文档）；以 webkiosk 的同等权限（`systemd-run -p User=ops-web -p AmbientCapabilities=CAP_DAC_READ_SEARCH` 加上它的 `InaccessiblePaths`）去读种子目录和 `/run/credentials/cart-approve.service/`，都应该失败；`approval-check` 的 `nsenter` 实测在车上能跑通；`kvm-broker` 加了 `RuntimeDirectory` 以后能写 `/run/ops-kvm/`；webkiosk 加了新的 `ReadWritePaths` 以后原有功能正常；`kvm-human.json` 在车机键鼠和网页键鼠两条路上都会计数；conbroker 人用写入的计数（另一个仓库，待核）。

## 六、威胁模型

| 编号 | 威胁 | 对策 / 残余风险 |
|---|---|---|
| T0 | Agent 和人同在 tailnet 上，没设 PIN 时直接登录网页 | 要求设 PIN；`approval-check` 检查；执行器拒绝 |
| T1 | 手机丢了 | 手机锁屏、验证器 App 加锁；`lockdown`；`approval-revoke` |
| T2 | 飞书账号被盗 | 只用单聊；L2 起就要 TOTP；键鼠 L3 和 L4 在飞书上批不了 |
| T3 | ops-web 或飞书服务被拿下 | ops-web 在 `dialout` 组，能绕过审批（残余风险，待拍板第 15 条）；改投 TOTP 码会被带外码挡住；飞书回调服务看不到带外码（码由 `cart-approve` 直接发）；它被拿下以后能伪造点击，但每一级都要 TOTP；L4 的根本办法是 WebAuthn |
| T4 | 提示词注入，生成诱导性摘要 | 摘要由服务端生成；L3 及以上人在手机上看摘要 |
| T5 | 拆开发送、改掉现场 | `send_seq`、`human_seq`，在原生锁里比对；#64、#65、#69 |
| T6 | 批准疲劳 | 没有"全部批准"；L2 也要 TOTP；同一账号 30 秒只能批一条 L3 或 L4；L4 复述目标、有撤回窗口；同类请求计数，超了自动升级；每日汇总 |
| T7 | 重放 | `epoch`、digest、nonce 和 `last_step` 都落盘；带外码绑定 nonce |
| T8 | 检查和使用之间被换了 | 在原生锁里比对 digest 和 ctx |
| T9 | AI 审批 AI | 代码不变量加 `approval-check`（含 root 登录面检查）；`cart-mcp` 是 root，在 #49 之前，对它来说种子隔离不成立，只靠"Agent 拿不到 root" |
| T10 | 时钟漂移、回拨 | 窗口固定 ±1 步；`last_step` 只增不减，回拨时宁可暂时用不了；车机要有 NTP |
| T11 | Agent 故意输错 TOTP，把人锁在外面 | 退避封顶 1 小时；车机确认不受影响；急停和拒绝不需要 TOTP；每次进入冷却都推通知 |
| T12 | Agent 驱动维护者已经登录的浏览器或飞书客户端 | 没有提权窗口；飞书只在手机上批（推车验证不了，残余风险）。A2 阶段网页 L2 可以"显示 A、提交 B"（没有带外码），属于残余风险，A3 以后网页的所有批准都加带外码。带外码挡不住"直接为 B 发起 challenge"，靠人在手机上读推送 |
| **T13** | Agent 偷维护者浏览器里的 cookie（会话最长 7 天），直接调人用接口 | 不在 Agent 的机器上登录网页（前提，推车验证不了）；审批会话单独签发、15 分钟；人用接口本身的风险见待拍板第 15 条 |
| **T14** | 仿冒：仿冒的审批页、群里的仿冒卡片、在 Claude 转达通道里向人要码 | 卡片只发单聊；审批链接不由 Agent 提供；所有界面都写"要码就是攻击"；带外码只在手机上出现，填回的位置只有推车网页 |
| **T15** | WOL、默认救援、键鼠直通这条不经审批的路 | 3.2 如实列出；#70、#69；`approval-check` 提示 |
| **T16** | 决策模型放行改配置的命令（模型审批 Agent） | 单列，默认关；开远程审批时警告（第 16 条） |
| **T17** | 不在本仓库里的 root 单元或进程（其他软件装的） | `approval-check` 查不到，列为残余风险；保持推车上软件的最小化 |
| **T18** | 撤回窗口期间人没看手机 | 窗口只是多一次机会，不能当成审批本身；推送没送达就不执行 |

## 七、需要维护者拍板

| # | 问题 | 我的倾向 |
|---|---|---|
| 1 | 第二因素 | TOTP；L3 和 L4 加带外码；长期上 WebAuthn |
| 2 | 飞书能批到哪一级 | 串口 L2、L3（都要 TOTP）；键鼠 L3 和全部 L4 只能拒绝 |
| 3 | 网页登录加 TOTP | 要（挡得住 curl 自己登录，挡不住 T13） |
| 4 | 远程解除急停 | 远程按 L3 |
| 5 | 卡片上显示原文 | 显示（只在单聊里） |
| 6 | 飞书接入方式 | 长连接，专用应用 |
| 7 | `label_print` 批量的 `confirmed` | 维持现状 |
| 8 | tailscale 身份 | 审计里不带；但**取件时做 `same_host` 比对**：网页请求的来源 IP 或 tailscale 设备，跟 `cart-mcp` 见过的 Agent 来源相同就拒。这能部分检出"在跑 Agent 的机器上审批"（要读 tailscaled 的 LocalAPI，由 root 的取件单元做，不给 ops-web） |
| 9 | 各级期限 | L2 15 分钟 / L3 10 分钟 / L4 10 分钟；`AUTH_TTL_S` 120 秒 |
| 10 | 审批会话 | 15 分钟，不滑动；`last_step` 只增不减，签发会话会用掉一个码，倾向**把签发和第一次批准合并**，免得签发以后还要等下一个 30 秒 |
| 11 | 有争议的分级 | `pxe_request` 全部 L4；不带高危词的串口配置命令 L2；键鼠 L2 白名单只放 `alt+f4` |
| 12 | 种子每人一个还是全车一个 | 每人一个 |
| 13 | ACP 接入 | 包装层转成挂起项，A4 以后做 |
| 14 | #65 的修法 | 有挂起项时禁止直发，`send_seq` 照做 |
| 15 | 网页人用通道的风险（ops-web 在 `dialout`；cookie 能直接打键） | 另开 issue：人用键鼠和串口走需要认证的代理，网页进程不进 `dialout` |
| 16 | 决策模型和远程审批同时打开 | 允许，但 `approval-check` 警告；或者开远程审批时强制关掉第 2 层 |
| 17 | 撤回窗口 | L3 15 秒，L4 30 秒；推送没送达就不执行 |
| 18 | #70 默认救援菜单 | 不自动进救援、不带 sshd（在 #70 里定） |
| 19 | 带外码的投递通道 | 飞书单聊，由 `cart-approve` 直接发；飞书不可用时，网页的批准一律拒绝（fail-closed），回到车机确认 |
| 20 | root 单元的种子隔离不成立时 | 倾向：`approval-check` 判不合格，远程审批**只开 L2**（L3、L4 只能车机确认），直到 #49 完成；另一个选项是完全不开远程审批 |
