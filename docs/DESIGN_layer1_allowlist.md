# 设计稿：串口写闸门第 1 层改成按厂商的命令前缀白名单

> 状态：**设计稿，等维护者拍板，不含实现**。本文讨论的基线是 **PR #57 合并后**的 `survey_core.is_readonly`。#57 还没合，它加了 H3C / 锐捷、关分页、`?` / Tcl / 写文件参数 / `clear` / 小黑名单等补丁。按键闸门（#73 / PR #74）已经单独改成白名单，不在本文范围。

## 一、为什么要改

现在第 1 层（`serial_write_core.classify` → `survey_core.is_readonly`）的判断方式是：**动词是 display / show，字符满足一组规则，不命中黑名单，就放行**。规则是"允许的动词"，后面的参数基本不限，再一条条补洞。#57 审了四轮，每一轮都挖出一类新的绕过：

| 轮次发现的类别 | 例子 | #57 的补法 |
|---|---|---|
| 缩写 / 唯一前缀解析 | `display diag` 落到 `diagnostic-information` | deny 按前缀拦 |
| 大小写 | `display Diagnostic-information` | 关键词要求全小写 |
| 引号 | `display "diagnostic-information"` | 引号只许出现在过滤词参数里 |
| 在线帮助 `?` | `display current-configuration ?`（残留放回输入行） | `?` 进禁用字符 |
| Tcl 替换 | `show version [exec reload]` | `[ ] { }` 进禁用字符 |
| 写文件参数 | `show logging process smd to-file bootflash:x`、`display x flash:/a` | `to-file` / `file` / 任何带 `:` 的词 |
| 顺手清计数 | `show platform hardware qfp active statistics drop clear` | `clear` / `reset` 任何位置都拒 |
| 主动发包 | `display ntp-service trace`；带主机名的 show 触发 DNS | 小黑名单；DNS 没堵 |
| 巨量输出 | `show memory`、`show running-config all`、`show platform software trace …` | 小黑名单；`show logging` 等没堵 |
| 写文件管道 | `\| save`、`\| redirect`、`\| tee` | 管道只许列出的过滤词 |

问题出在结构上：设备的命令空间是开放的（不同型号、版本、视图下命令都不一样），黑名单只能挡住已经知道的那些。审查员和作者都建议改白名单：**逐条列出确定无副作用的命令，没列出的一律交给第 2 / 3 层**。漏列一条，代价是多问一次人；用黑名单漏掉一条，代价是设备被改。按键闸门在 #74 已经换成这个思路。

## 二、白名单怎么表达

### 2.1 选型：固定词序列 + 有类型的参数槽

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 逐字命令（整句相等） | 最简单、最好审 | `display interface GigabitEthernet0/0/1` 每个口一条，列不完 | 只用于没有参数的命令和会话命令 |
| 每条一个正则 | 灵活 | 正则本身就是攻击面（`.*`、字符类写宽），审查员读不懂；没法自动生成反例 | **不用** |
| **固定词 + 参数槽（选这个）** | 词是字面量，参数从一个封闭的类型集合里选，每种类型的取值都能穷举测试 | 要维护一套类型 | **用** |

### 2.2 条目格式（写在代码里，纯数据）

```python
# serial_allowlist.py (新模块, 纯逻辑, 不 import 设备相关)
ALLOW = {
    "huawei": [
        E("display version",                         src="survey"),
        E("display interface {ifname}",              src="待核"),
        E("display vlan {vlan}",                     src="待核"),
        E("display current-configuration",           src="survey", pipes=PIPES_VRP, big=True),
        ...
    ],
    ...
}
```

- 条目文本按**一个空格**切成词。词有两种：**字面量**（全小写全称，必须逐字相等），以及 `{类型}` **参数槽**（取值必须完全符合该类型）。
- **词数必须正好相等**：不许多一个词，也不许少一个词。条目里没写的词一律不认，"后面还能跟别的参数"这种情况不存在。
- 管道：条目可以带 `pipes`，声明允许接的过滤段，格式是 `| <过滤词> {filter}`，最多一段。没声明就不许带管道。
- 每条带 `src`：`survey` 表示已在巡检默认清单里、现场跑过；`待核` 表示依据是厂商命令手册的显示类命令，还没在本车型号上实机核过。`big=True` 表示输出可能很大，给 conbroker 的超时用，也用于后面的"大输出"拍板。
- 字面量必须是全称。设备按唯一前缀解析，白名单不复刻解析器，所以 `dis` / `sh` / `int` 一律不收，`is_readonly` 的说明里已经论证过。

### 2.3 参数类型（封闭集合，加新类型要改代码、补测试）

| 类型 | 取值（全部 `fullmatch`，大小写敏感） | 备注 |
|---|---|---|
| `{ifname}` | 按厂商列全称前缀 + 编号：华为 / H3C `(GigabitEthernet\|XGigabitEthernet\|25GE\|40GE\|100GE\|Ten-GigabitEthernet\|Eth-Trunk\|Bridge-Aggregation\|Vlanif\|Vlan-interface\|LoopBack\|MEth)\d{1,3}(/\d{1,3}){0,3}(\.\d{1,4})?`；思科 / 锐捷 `(GigabitEthernet\|TenGigabitEthernet\|TwentyFiveGigE\|FortyGigabitEthernet\|HundredGigE\|FastEthernet\|Ethernet\|Port-channel\|AggregatePort\|Vlan\|Loopback)\d{1,3}(/\d{1,3}){0,3}(\.\d{1,4})?` | 不收缩写（`Gi0/1`、`GE0/0/1`），见待拍板 1 |
| `{vlan}` | `[1-9]\d{0,3}`，并且 1–4094 | 不许前导 0 |
| `{ipv4}` | 点分四段，每段 0–255，不许前导 0 | 只收字面量，**不收主机名**（会触发 DNS） |
| `{masklen}` | 0–32 | 配合 `{ipv4}` |
| `{mac}` | 华为 / H3C `[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}`；思科 / 锐捷 `[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}` | 只收小写 |
| `{filter}` | `[A-Za-z0-9._/-]{1,64}`：一个词，不能带引号、空格、正则元字符、`:` | 只用在管道过滤段 |

没有自由文本槽，也没有"其余参数"槽。IPv6 字面量带 `:`，和"带冒号是文件目标"的规则冲突，第一版不收，见待拍板 2。

### 2.4 匹配算法（确定、可穷举）

1. 整条先过现在的通用检查，这部分保留，作为第二道防线：只许可打印 ASCII、词之间恰好一个空格、不许前后空白，也不许 `_FORBIDDEN` 里的字符（`; & < > \` $ # ! ? [ ] { }`）。
2. 按 `" | "` 切成主段和至多一个管道段。出现第二个 `|`，或者 `|` 粘在词上，都拒。
3. 主段逐个和该厂商的条目比：词数相等，字面量逐字相等，参数槽各自 `fullmatch`。必须**恰好命中一条**；命中 0 条，或者因为两条条目重叠命中多条，都不放行。重叠由测试保证不会出现。
4. 有管道段的话，命中的条目得声明过这个过滤词，参数还要符合 `{filter}`。
5. 放行时把命中的条目 id 写进闸门结果（`gate.entry`），日志和确认框都能看到"凭哪一条放行"。

## 三、初始清单（只放巡检和排障最常用、确定没有副作用的）

依据说明：
- **survey**：已在巡检默认清单里（#15，以及 #57 补的 H3C / 锐捷），现场跑过，或者在 #57 里列为待实机验证；
- **待核**：依据是厂商命令手册的显示类命令，还要在本车常见型号上核对三件事：会不会弹确认、输出有多大、会不会主动发包。

所有条目都只在该厂商的 exec 提示符下判断（`slot_vendor` 的 `at_prompt` 不变）。

### 3.1 华为 VRP

| 条目 | 依据 | 备注 |
|---|---|---|
| `screen-length 0 temporary` | survey（#57 关分页） | 会话级 |
| `display version` / `display clock` | survey | |
| `display ip interface brief` / `display interface brief` | survey | |
| `display lldp neighbor brief` | survey | |
| `display mac-address` | survey | 大交换机上输出大，`big` |
| `display arp` | survey | `big` |
| `display current-configuration`（管道：include / exclude / begin / section / count） | survey | `big`，见待拍板 4 |
| `display interface {ifname}` | 待核 | |
| `display vlan` / `display vlan {vlan}` | 待核 | |
| `display stp brief` | 待核 | |
| `display eth-trunk` | 待核 | |
| `display device` / `display elabel` | 待核（#57 测试里当正例用过） | `elabel` 输出长 |
| `display cpu-usage` / `display memory-usage` | 待核 | |
| `display alarm active` | 待核 | |
| `display transceiver interface {ifname} verbose` | 待核 | |
| `display mac-address {mac}` | 待核 | |
| `display ip routing-table {ipv4}` | 待核 | 不收不带参数的全表（核心设备上很大） |

**不进第 1 层**（交给第 2 / 3 层）：`display diagnostic-information`（能写文件）、`display logbuffer`（大）、`display ntp-service trace`（发包）、`ping` / `tracert`（发包，见待拍板 3）。

### 3.2 H3C Comware

| 条目 | 依据 | 备注 |
|---|---|---|
| `screen-length disable` | survey（#57） | 用户视图下只对当前会话生效（#57 待实机验证） |
| `display version` / `display clock` | survey | |
| `display ip interface brief` / `display interface brief` | survey | |
| `display lldp neighbor-information list` | survey | |
| `display mac-address` / `display arp` | survey | `big` |
| `display current-configuration`（管道：include / exclude / begin） | survey | `big` |
| `display interface {ifname}` | 待核 | |
| `display vlan {vlan}` / `display stp brief` / `display link-aggregation summary` | 待核 | |
| `display device` / `display cpu-usage` / `display memory` | 待核 | 核对 Comware 5 / 7 写法差异 |
| `display transceiver interface {ifname}` | 待核 | |

### 3.3 思科 IOS / IOS-XE

| 条目 | 依据 | 备注 |
|---|---|---|
| `terminal length 0` | survey（#57） | 会话级 |
| `show version` / `show clock` | survey | |
| `show ip interface brief` / `show interfaces status` | survey | |
| `show cdp neighbors` / `show lldp neighbors` | survey | |
| `show mac address-table` / `show arp` | survey | `big` |
| `show running-config`（管道：include / exclude / begin / section） | survey | `big`；**不收** `show running-config all`（大） |
| `show interfaces {ifname}` | 待核 | |
| `show vlan brief` / `show spanning-tree summary` / `show etherchannel summary` | 待核 | |
| `show inventory` | 待核 | |
| `show cdp neighbors detail` / `show lldp neighbors detail` | 待核 | |
| `show ip route {ipv4}` | 待核 | 只收字面量，不收主机名（DNS） |
| `show interfaces {ifname} transceiver` | 待核 | 平台相关 |

**不进第 1 层**：`show tech-support`（大，能打包写文件）、`show memory*`（大）、`show logging`（大，#57 设计文档里列为"照样放行"的已知局限，白名单下自然消失）、`show processes cpu*`（大）、`show platform …`（平台相关，有带 `clear` 的变体）、所有 `… to-file …`。NX-OS 的写法有差异，第一版不单列，按思科条目匹配；不认的命令设备会报错（巡检记为 invalid）。

### 3.4 锐捷 RGOS

| 条目 | 依据 | 备注 |
|---|---|---|
| `terminal length 0` | survey（#57） | |
| `show version` / `show clock` | survey | |
| `show ip interface brief` / `show interfaces status` | survey | |
| `show lldp neighbors` | survey | |
| `show mac-address-table` / `show arp` | survey | `big`；注意和思科写法不同 |
| `show running-config`（管道：include / exclude / begin） | survey | `big` |
| `show interfaces {ifname}` / `show vlan` / `show aggregatePort summary` | 待核 | |

#57 列出的"锐捷写法待实机验证"同样适用于上面这些。

### 3.5 Linux 槽位

现状：没有第 1 层（样例 `linux.model=false`），全部要人确认。提议第一版**只收逐字命令，不带参数槽**：

| 条目 | 依据 | 备注 |
|---|---|---|
| `hostname` / `uname -a` / `uptime` / `date` | 待核 | |
| `cat /etc/os-release` | 待核 | 只收这一个路径 |
| `ip -br addr` / `ip -br link` / `ip route` | 待核 | |
| `df -h` / `free -m` / `lsblk` | 待核 | |

Linux 的风险和网络设备不一样：对端 shell 的 alias、函数、`PATH` 都不在推车的控制之内，比如 `uptime` 可能被别名替换掉。shell 元字符已经在第 2 层前就拒了。所以 Linux 第 1 层要不要开，单独拍板，见待拍板 8。

## 四、和其他几层的关系

```
send_command(cmd)
  ├─ 命令合不合格 (一行可打印 ASCII 等, 不变) ── 不合格 → 拒
  ├─ 高危词 (DANGER, 不变)                    ── 命中   → 第 3 层 人确认
  ├─ 认不出厂商 / 不在提示符上 (不变)          ──        → 第 3 层
  ├─ 【新】白名单恰好命中一条                  ──        → 第 1 层 放行 (gate.entry = 条目 id)
  └─ 其余                                       ──        → 第 2 层模型 (开了的话) → 放行 / 第 3 层
```

- **高危词检查保留在白名单之前**，是纵深防御。白名单条目里本来就不会有高危词，测试会断言这一点。
- **巡检**：`survey_core` 改用同一份白名单（只用一份判断）。测试断言每个厂商的默认清单、关分页命令都恰好命中一条。`survey_profiles.json` 仍然只能从白名单里挑命令，挑了不在白名单里的，整次巡检照旧被拒。
- **extra_verbs**：作废。规则文件只能**禁用**内置条目（`allowlist.disable: ["huawei:display elabel", …]`），往更严的方向改；**不能新增**。新增条目要改代码，带测试走 PR。见待拍板 5。
- **第 2 层模型**：不在白名单里的命令都会到它这里，调用量、费用和延迟都会上升。它的 fail-closed 不变。模型没开就直接到人。
- **人确认**：确认框里写明"不在第 1 层白名单"，并附上最接近的条目（按字面量前缀比出来的），方便人判断，也方便以后提 PR 加条目。
- **按键闸门（#74）**：独立，不受本文影响。

## 五、迁移计划

| 阶段 | 内容 | 行为变化 | 退出条件 |
|---|---|---|---|
| 0 | 本设计稿拍板；#57 先合（H3C / 锐捷 / 关分页 / 认厂商有用）；**#57 之后不再往 `is_readonly` 加黑名单补丁**，新发现的绕过类别只进反例回归集（第七节） | 无 | 拍板 |
| 1 | 新模块 `serial_allowlist.py` + 测试；`classify` 在第 1 层**同时**算 `is_readonly` 和白名单，结果写进 `gate.shadow = {"allowlist": 条目 id 或 null, "readonly": bool}`，**判定仍按 `is_readonly`** | 无（只多写日志字段） | 合并 |
| 2 | 影子期：跑满约定的时长或命令数（建议 2 周且不少于 200 条 Agent 命令）。用新脚本 `scripts/gate_shadow_report.py` 在推车上读 `serial-slot*.log` 汇总（日志含设备配置，不出推车，只打印命令和计数） | 无 | 维护者看过报告 |
| 3 | 切换：第 1 层改成 **白名单命中 且 `is_readonly` 也过**（两者同时满足），巡检同样 | 一部分命令从直发变成第 2 / 3 层 | 稳定一个版本 |
| 4 | 去掉 `is_readonly` 里白名单已经覆盖的黑名单补丁，或者长期保留"两者同时满足"（见待拍板 6） | 无 | — |

影子报告要回答的问题：
1. **`is_readonly` 放行、白名单不放行**：这些命令现在会变成要人确认。逐条看：是该补的条目，还是本来就不该直发（大输出、发包）。前者提 PR 加条目，后者确认就行。
2. **白名单放行、`is_readonly` 不放行**：这一类应该是 0 条。出现就是白名单的 bug，切换前必须修掉。
3. 各厂商切换后要人确认的比例估计（按第 1 类占比算）。

回退：阶段 3 带一个代码常量 `LAYER1_MODE = "allowlist+readonly"`，回退时改成 `"readonly"`。只保留一个版本，之后删掉。不放进规则文件：不能让现场配置把闸门放宽。

## 六、对运维可用性的影响和缓解

影响：
- 不在清单里的只读命令（各种 `display xxx` 的长尾），从直发变成走模型或人确认。Agent 排障时可能频繁要人点确认。
- Agent 习惯用的缩写、接口简写（`dis int b`、`Gi0/1`）全部走确认。现在本来也不收缩写，接口简写是新增的限制。
- 清单维护有成本：现场每发现一条常用命令，都要走一次 PR。

缓解：
1. **让 Agent 知道清单**：新增只读 MCP 工具 `gate_allowlist(slot)`，按该槽位登记的厂商返回条目和参数类型说明。`send_command` 的描述里写"先用这个查，照抄写法"。被挂起时的回包也带上"最接近的条目"。
2. **影子报告驱动补条目**：阶段 2 的第 1 类按频次排序，高频的优先补（加条目要走 PR 和测试）。
3. **巡检照旧是批量看的主路**：它的命令全在白名单里，不受影响。
4. **第 2 层模型**：开了以后，长尾命令多数由模型放行，人只看模型拦下的。需要接受费用和延迟，模型配置不变。
5. **确认框**：附上最接近的条目和"凭什么没放行"，人一眼能判断；以后可以考虑"同一条命令本会话内确认过就不再问"（不在本次范围）。

## 七、测试策略

1. **每条条目的正例（自动生成）**：每种参数类型定义一组规范样例（`{vlan}` 取 1、100、4094；`{ipv4}` 取 192.0.2.1、198.51.100.254、203.0.113.0；`{ifname}` 每个前缀各取一两个；`{mac}` 取 `0200-0000-0001` / `0200.0000.0001`）。按条目展开成具体命令，断言：恰好命中这一条；迁移期内 `is_readonly` 也放行。
2. **每条条目的变异反例（自动生成）**，全部要拒：
   - 每个字面量：改大写、首字母大写、所有真前缀（缩写）、加双引号、加单引号；
   - 末尾多一个词，或者少一个词；
   - 插入 `?`，双空格，制表符，前后空白；
   - 拼接 `; reload`，管道 `| save x` / `| redirect flash:x` / `| tee x`；
   - 参数越界：vlan 0 / 4095 / 前导 0，ipv4 256 / 前导 0 / 三段；
   - 参数里带 `:` `[` `{` `"`，参数换成主机名；
   - 接口名用缩写。
3. **已知绕过回归集**：把 #57 四轮测试里的反例汇总成一份数据文件 `tests/data/layer1_bypass.json`，按类别分组：缩写 / 前缀、大小写、引号、`?`、Tcl、写文件参数、`clear` / `reset`、主动发包、巨量输出、写文件管道、关分页变形、跨厂商、写命令 50 条。每个厂商都跑一遍，全部要拒。以后新发现的绕过只往这份文件里加，不再改黑名单。
4. **跨厂商**：每个厂商的条目，在别的厂商下都不命中（除非两边的条目文本本来就一样，比如思科和锐捷的 `show version`）。
5. **结构不变量**：
   - 任意两条条目不重叠：对正例样例做笛卡尔检查，没有一个样例同时命中两条；
   - 条目里没有高危词，没有 `_FORBIDDEN` 字符，没有 `_FILE_WORDS`；
   - 字面量全部是小写全称，没有缩写：维护一张"已知缩写"表，断言不出现；
   - 巡检默认清单和关分页 ⊆ 白名单。
6. **随机组合**：用固定种子，把条目的字面量、参数样例、通用危险词拼成大量组合，断言只有"条目的规范形式"会放行。用标准库 `random` 就行，不新加依赖。
7. **影子期**：`gate.shadow` 字段的写法和报告脚本的汇总口径，用假日志做测试。

## 八、待拍板

1. **接口名缩写**收不收（`Gi0/1`、`GE0/0/1`）？在参数位置上，缩写只会被解释成接口类型，风险低；但每家的缩写表都得维护。建议第一版不收，看影子报告再说。
2. **IPv6 字面量**收不收？它和"带 `:` 就是文件目标"的规则冲突，要单独做一个类型，而且要证明它不会被设备当成文件系统前缀。建议第一版不收。
3. **主动发包的命令**（`ping` / `tracert` / `traceroute`，即使目标只是字面量 IP）进不进第 1 层？建议不进，走第 2 / 3 层。原来经 `extra_verbs` 开 ping 的现场会因此多问人。
4. **大输出命令**（`display current-configuration` / `show running-config` / `mac-address` / `arp`）进不进第 1 层？巡检需要它们，建议进，但标 `big`。`show logging` / `display logbuffer` 建议不进。
5. **规则文件**能不能新增条目？建议只能禁用、不能新增，新增走代码 PR。
6. **切换后**是只用白名单，还是长期保留"白名单命中 且 `is_readonly` 也过"？后者多一道保险，代价是两套逻辑都要维护。建议先保留一个版本，再决定。
7. **影子期**多长，切换门槛是什么（建议 2 周且不少于 200 条 Agent 命令；第 2 类必须为 0）。
8. **Linux 槽位**要不要第 1 层？建议第一版只收逐字命令，或者干脆不开。
9. 要不要加 **`gate_allowlist` 只读工具**给 Agent 查清单？建议加。
10. **#57 之后冻结 `is_readonly` 的黑名单补丁**：新发现的绕过只进回归集，不再补规则。这一条要维护者确认，因为它会改变 #57 后续审查的处理方式。
11. **管道过滤参数**限成一个安全词，带空格或正则的 include 一律走确认，这样可以吗？
12. **"待核"条目**的核对方式：在推车常见型号上逐条实机跑一次，记下输出量、有没有弹确认，结果写回条目的 `src`。谁来做、在哪些型号上做，待定。
