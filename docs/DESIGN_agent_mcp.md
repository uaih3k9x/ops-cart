# 设计：操作头 Agent 接入（issue #3）

把推车的串口操作头开放给现成 Agent 框架（Claude Code / OpenCode），推车只做**提供工具 + 仲裁 + 记录**三件事，不自己写 Agent 循环。人默认只看、随时一键急停；写命令过三层闸门；全程留日志。

本文是实施规划，不改需求。需求以 issue #3 正文和三条评论为准。

## 一、架构：推车当 MCP 服务端

```
   Agent 框架(Claude Code/OpenCode)         人(Kiosk 触摸屏 / 网页)
            │ MCP over stdio/http                 │ 实时看输出、确认、急停
            ▼                                      ▼
   ┌──────────────────────── 推车 MCP 服务 ────────────────────────┐
   │  工具层   列执行绪 / 读输出 / 发命令 / 截屏 / 打标签            │
   │  仲裁层   写租约(单写者,会过期) · 三层闸门 · 急停              │
   │  记录层   每条执行绪完整 transcript(JSONL) + cartlog 摘要      │
   └───────┬───────────────────────┬───────────────────────────────┘
           │ conbroker(单写多读)    │ DeepSeek(决策模型, 复用 cart_dream 调用法)
           ▼                        ▼
     串口操作头 1–4(FTDI 序列号绑槽位)   云端 DeepSeek(超时即 fail-closed)
```

- **监听**：只在 tailnet 和本机；认证复用 `web_core` 的 token/session（`Sessions`、`cookie_token`）。
- **换 Agent 不用改推车**：工具协议是 MCP，思考循环在框架那头。
- **决策模型**：复用 `cart_dream.py` 的模式——推车以 `serial.conf` 里的 user 身份、走它 `~/.claude` 配的 DeepSeek 线路，`subprocess` 调用、设超时和预算上限。超时 / 报错 / 返回格式不对 → 一律按"需要人确认"。

## 二、三层写闸门（核心，fail-closed）

每条**发往设备的命令**都要过闸门：

| 层 | 命令类型 | 处理 |
|---|---|---|
| 1 放行 | 只读：`display` / `show` / `ping` 等（规则文件按厂商列白名单） | 直接发，记日志 |
| 2 模型判 | 其余命令 | 交 DeepSeek：输入=命令 + 设备 + 最近输出上下文 + Agent 声明的意图；输出=放行 / 拦下 + 理由，理由进日志 |
| 3 人确认 | `save` / `reboot` / `reset saved-configuration` / `format` / 删文件等高危词；**以及第 2 层拦下的** | 挂起，执行绪方块变黄闪烁显示"Agent 想执行：…"，人在 Kiosk 或网页点确认才发 |

- **决策模型不可用 → 全部按第 3 层**（人确认），绝不放行。
- 规则写成可配置文件，按厂商分（华为 / 锐捷 / Linux shell …）。
- **给 Agent 的工具只到"行级命令"，不给原始字节**——否则闸门按行解析不了命令，等于形同虚设（10/31 定）。另给固定特殊键工具（Ctrl+C / 回车 / 翻页空格），各自记日志，打断键可要求人确认；需要原始字节的场景（BootROM、按任意键）由人在车机上操作。

## 三、执行绪与操作头

- **操作头**：物理槽位 1–4，按 FTDI 序列号绑定，永不变。`serial_panel` 已有 by-id / 序列号识别。
- **执行绪**：某操作头上的逻辑会话，有名字、占用者、日志；线拔掉即结束、名字清空。
- **单写者**：`conbroker` 本来就是单写多读，直接把"写"这一路做成租约。
- **写租约**：Agent 拿到的写权限有期限，要续期；Agent 卡死 → 租约过期自动释放。同一执行绪同时只有一个写入者。
- **命名**：线插上从设备提示符自动识别（华为 `<名>`、`[名]`）；人和 Agent 都能改、不重名；改名记日志（谁 / 何时 / 从什么到什么）。**日志与调用永远带槽位号，名字只是标签。**

## 四、急停

- 一键让 Agent 立刻停手：**撤销该执行绪写权限 + 丢弃还没发出的命令**。
- **只停发送，不往设备插入任何东西**（不自动发 Ctrl+C）。
- 恢复要人**明确**解除。
- 作用范围：**单条 + 全部都要**（10/31 定）；全部急停 = 对每条执行单条急停。

## 五、日志

- 每条执行绪从开到关完整记：时间戳、谁发的（人 / 哪个 Agent）、发了什么、设备回了什么、模型判断与理由、急停与确认事件。
- 落 `/var/log/ops-cart/`（JSONL，每执行绪一份），交 logrotate；`cartlog.event()` 记摘要 + 日志路径；CART-LOG 只摘要。
- 日志含设备配置 → **不进公开仓库**。
- 用途：留档 + 事后自动摘"命令 / 验证 / 回滚"写变更记录（复用 `cart_dream` 的整理思路）。

## 六、分阶段实施（建议的 build order）

| 阶段 | 内容 | 为什么这个顺序 | 复用 |
|---|---|---|---|
| **P0 标签助手** | MCP 第一个工具：`label_print_text/cable` + `label_printer_status`；预览渲染；缩写对照表（厂商/设备类型）；"模型提议→代码校验(仅 ASCII/宽度/缩写对照)→预览→人确认→写回对照表" | 范围小、风险最低（最坏废一张标签，不碰设备配置），能马上用，且把闸门范式先跑通 | `tsc_label.py`、DeepSeek 调用法 |
| **P1 MCP 骨架 + 只读串口** | 服务端骨架、tailnet/本机监听、认证；只读工具：列执行绪、读输出、截屏 | 全程无写入，先把接入和观察打通，零闸门风险 | `web_core` 认证、`serial_panel`、`cart_snap` |
| **P2 写路径 + 租约 + 急停** | 发命令工具、写租约（续期/过期）、单写者、急停。先只做第 1 层（只读放行），**其余一律人确认**（暂不接模型） | 先把"能写但管得住 + 停得下"做扎实，再谈智能 | `conbroker` 单写多读、`cartlog` |
| **P3 决策模型** | 接 DeepSeek 做第 2 层，把"其余一律人确认"升级成"模型判 + 理由"；规则文件按厂商 | 闸门框架稳了再加判断力，fail-closed 兜底 | `cart_dream` 调用法 |
| **P4 确认 UI + 指示灯** | Kiosk 黄闪确认框 + 网页确认；与 #2 指示灯状态联动 | 可视化和交互收尾 | #2、`kiosk`、`web` |
| **P5 以后** | 键鼠执行绪（#3 已降 P3）、实体急停按钮、推车 2.0 本地决策模型 | 需求还没定清 | — |

## 七、待你拍板

1. ~~急停范围~~：已定，两种都要。
2. **实体急停按钮**（USB 大红蘑菇头）要不要进第一版规划？
3. ~~工具粒度~~：已定，只给行级命令 + 固定特殊键。
4. **P0 的缩写对照表**：要不要我现在就起一个初稿（按厂商/设备类型，中文端口名/长设备名 → 短 ASCII）？

## 八、P1 落地记录（#19）

- 代码：`mcp_core.py`（协议 / 校验 / 命名 / 审计 / HTTP 检查，纯逻辑）+ `cart_mcp.py`（stdio、HTTP、真机读数）。
- 工具：`list_threads`、`read_output`、`screenshot`、`cart_status`、`pxe_status`，全部 `readOnlyHint`。
- **认证没用 `web_core.Sessions`**：那套令牌要先在网页上输 PIN 才签发，Agent 走不了这一步。改成 `/etc/ops-cart/mcp.json` 里的静态 Bearer 令牌（名字进审计的 who，短于 24 字符不认）；来源过滤、Host 白名单、Origin 同源照搬 `web_core`。stdio 不另设认证，靠 ssh。
- **监听**：不绑 0.0.0.0，只绑 `ip -j addr` 里的本机和 tailnet 地址，每 30 秒补绑新出现的（tailscaled 后起）；每个请求再按来源过滤一遍。
- **命名**：只认顶格的华为提示符（`?` 帮助是缩进的），子视图 `[名-GigabitEthernet0/0/1]` 优先对齐窗口里见过的 `<名>`，对不上再按视图关键字切；线拔了或没服务名字为空，不读旧日志。
- **PXE 用途**：`pxe-mode.json` 不记用途，从那台 MAC 的菜单内容反推。
- 写入者字段恒为空，等 #21 的写租约。审计 `/var/log/ops-cart/mcp-audit.log`（JSONL，`*.log` 现成 logrotate 管）。

## 九、P2 落地记录（#21）

- 代码：`serial_write_core.py`（纯逻辑：行校验、高危词、闸门第 1 层、写租约 / 挂起 / 在途 / 急停的 `Book`、日志行）+ `mcp_serial.py`（状态文件 flock、执行绪日志、conbroker send 封装、MCP 工具、车机命令行）。`cart_mcp` 只加注册行和子命令转发。
- 工具：`lease_acquire` / `lease_renew` / `lease_release`、`send_command`（一行可打印 ASCII，不许控制字符 / 换行 / 全角 / 零宽，不许 `-` 开头）、`send_key`（`enter` / `space` 直发，`ctrl_c` 挂起等人）、`pending_list`（只读，不含令牌）、`estop`（单条或全部；Agent 能自停，**没有 resume 工具**）。
- **写租约**：认令牌不认名字（两个 Agent 同一个 ssh 用户也分得开）；默认 60 秒，10–300 可选；`lease_renew` 续，过期不能续要重新申请；过期 / 释放 / 急停撤销时它挂着的命令一起作废；直发命令发完顺手续一个期限（Agent 等慢命令时自己续不了）。每槽位同时只有一条在途，上一条没回来不发下一条；发送进程死了，在途标记过"超时 + 90 秒"自动清掉并记 `status=unknown`。
- **闸门第 1 层**：直发要同时满足——认得出厂商（规则文件登记了槽位，或最后一行是华为 `<名>` / `[名]`；思科 `名#` 和 Linux `bash-4.4#` 分不开，只能登记）、设备停在该厂商的提示符上（跳过 conbroker 自己的 `[时间 …]` 标记行；停在密码 / `[Y/N]` / 翻页 / 半截输出上都不算）、过 `survey_core.is_readonly`（规则文件只能给内置厂商加只读动词，如 `ping`）、不带高危词。高危词（save / reboot / reset / format / delete / write / copy / startup / shutdown …，含 VRP/IOS 式缩写 `sav` `wr` `del`）只增不减，永远要人确认。其余一律挂起。规则文件 `/etc/ops-cart/serial-gate.json`（样例 `examples/serial_gate.example.json`），坏了按内置默认。
- **状态机**（每条命令一个条目，编号在一次开机内递增）：

```
send_command/send_key ─┬─ 闸门放行 ─► sending ─(conbroker send 返回)─► sent / refused / timeout / error
                       │                    └─(发送进程死了, 超时+90s)─► unknown
                       └─ 要人确认 ─► pending ─┬─ 人 confirm (租约仍有效、槽位没急停、没在途) ─► sending ─► …
                                               ├─ 人 reject ─► rejected
                                               └─ 急停 / 租约过期或释放 / 挂起超 15 分钟 ─► dropped
槽位: 正常 ──estop(人或 Agent)──► 急停 (撤租约 + 丢挂起, 不发任何字节) ──resume(只认本机人)──► 正常 (租约不还)
```

- **确认通道**（#23 之前；#23 之后见第十节）：车机上"退出到终端"，`sudo python3 /opt/pxe/cart_mcp.py pending` 看，`confirm <编号>` 显示命令、来源、理由、高危词，敲 `yes` 才发，发的结果直接打印并进日志；`reject <编号>`、`estop [槽位]` 哪都能做；`confirm` 和 `resume <槽位|all>` 只认本机：环境里没有 `SSH_*`、stdin 是终端、祖先进程里没有 sshd / sshd-session / tailscaled / dropbear / mosh-server / cockpit，读不到 `/proc` 也不认。确认后由确认那个进程经 conbroker 发（屏幕上照样标"Claude 发送"）。
- **急停**：状态在 `/run/ops-cart/serial-write.json`（root 0600，路径可注入）；状态文件读不懂 = 全部急停；重启推车 /run 清空 = 急停、租约、挂起都没了（挂起的命令不会在重启后冒出来）。急停事件记下被撤销的租约持有者、丢弃的编号、以及**当时在途的那条**——那条已经交给 conbroker，收不回来。
- **已知缺口：conbroker 里排队的那条收不回**。人 3 秒内敲过键时，Agent 的字节已经进了 conbroker 的连接、在它的服务线程里等人停手；这时急停，等人停手 3 秒后它照样写进串口（我们的进程即使超时退出也一样）。要堵上得改 conbroker（另一个仓库）：服务端写串口前查一下 `/run/ops-cart/serial-write.json` 里该槽位的急停，急停就丢弃并标记。车上的判定只能保证"急停之后不再往 conbroker 交任何东西"。
- **日志**：`/var/log/ops-cart/serial-slot<N>.log`，每槽位一份 JSONL（现成 logrotate `*.log` 规则管），每行 `t / slot / ev / src(agent|human|system) / who`，再加命令、闸门结果（verdict / layer / reason / danger / vendor）、设备回显摘要（超过 1000 字掐中间）、编号、确认人、名字。事件：`lease_acquire` `lease_release` `lease_expired` `pending` `confirm` `reject` `drop` `send` `refused` `estop` `resume`；续期不记。急停 / 解除 / 确认 / 拒绝另在 `events.log` 记 `[serial-write]` 一行摘要 + 日志路径（不写命令）。
- **人优先**：人在车机 / 网页上敲键走 conbroker 的 human 通道，不经租约、不过闸门；人 3 秒内敲过键 Agent 排队（conbroker 原有）；人随时 estop / reject。
- **留给 #22**：`serial_write_core.classify(cmd, vendor, at_prompt, rules)` 返回 `{verdict, layer, reason, danger, vendor}`；第 2 层接在"不在只读白名单里"那一支——模型判放行就 `verdict=pass, layer=2`，判拦或超时 / 报错保持 `confirm`；`danger` 非空的永远不许模型放行。`Book.begin` 只认 `verdict=pass`，调用方不用改。
- **留给 #23**：车机 / 网页确认框直接用 `mcp_serial.Store().view()`（各槽位 `estop / writer / expires_in / inflight / pending`，挂起列表带 `danger` 和 `age_s`）和 `Store.run(lambda b, now: b.confirm(id, by, now))` → `conbroker_send` → `b.finish(...)`（照 `mcp_serial.cli` 的 confirm 分支）；急停 / 解除用 `b.estop(slot, by, "human", now)` / `b.resume(slot, by, now)`。webkiosk 以 `ops-web` 跑，读不了 root 0600 的状态文件，网页确认要经 root 进程转一道或调权限。指示灯黄闪 = 某槽位 `pending > 0`。

## 十、P4 落地记录（#23）

- 代码：`confirm_core.py`（纯逻辑：方块配色 / 文字、确认框内容、动作前置校验、网页请求格式）+ `confirm_panel.py`（车机确认框 Tk）；`mcp_serial` 加 `peek`（只读看状态，不加锁不写盘，读不了 / 读不懂 = 全部急停）、`send_confirmed`（车机和命令行共用的"确认→发→finish"）、`human_action`（车机按钮入口）、`write_request` / `process_requests`（网页请求）。
- **执行绪方块**（车机执行器条、网页、副屏同一套，颜色就是 #2 的 OK 绿 / WARN 黄 / FAIL 红，只多一个 Agent 紫）：急停 = 实心红"急停" > 待确认 = 黄闪"待确认 N" > 响铃红闪 > 在途 = 紫闪"Agent 输入" > 持写租约 = 紫色带"N Agent" > 原样（在动绿闪 = 人或设备自己吐）。分不出人敲键和设备自己输出，只能分"有没有 Agent 在发"。
- **车机**：点黄 / 红 / 紫的操作头弹确认框（命令、来源、几秒前、闸门理由、高危词），确认 / 拒绝 / 急停本路 / 全部急停 / 解除；高危词确认前再问一次；方块条上常驻"Agent 全部急停"，一点就停不问。车机进程是 root、在本机屏幕上，算"本机人"，确认和解除都给。状态由采样线程 peek，动作在后台线程，主线程不碰文件；锁屏时关掉确认框。
- **网页**（"Agent"页 + 顶上"全部急停"）：只给急停（单路 / 全部）和拒绝，**确认发送和解除急停不给**（403，提示去车机）。webkiosk 以 `ops-web` 跑，状态文件靠已有的 `CAP_DAC_READ_SEARCH` 只读；改状态经 `/run/ops-cart/serial-req/`（tmpfiles 建，ops-web 0700）投请求文件，root 的 `cart-serial-req.path` → `cart-serial-req.service`（oneshot，`mcp_serial.py requests`）执行。理由：越权的方向只能是"停"——就算 ops-web 被拿下，最多能急停或拒绝，放行不了任何命令；不加常驻 root 进程，也不给 ops-web 写状态文件。请求只认 estop / reject、一分钟内、无多余字段；链接不跟（O_NOFOLLOW），读不懂删掉；先删后执行，不会反复执行。
- **副屏**：快照多一项 `agent`（每槽位 estop / pending / 有没有写入者 / 在途，不带名字和命令，state.json 是 0644）；操作头方块同车机配色，"操作头"那行右边写"Agent 待确认 N · 急停 1、3"。读不懂 = 全部急停。
- **待定**：网页确认要不要开（要的话得另设认证，比如车机屏幕上显示一次性码照抄）；锁屏上要不要露急停按钮（现在锁屏时只能网页或解锁后急停）。

