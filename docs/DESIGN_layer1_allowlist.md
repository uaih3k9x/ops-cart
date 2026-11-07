# 设计稿：串口写闸门第 1 层改成按厂商的命令前缀白名单

> 状态：**已拍板（2026-11-07，见第八节），阶段 1（影子期）已实现**，实现和设计稿的出入见第九节。切换条件：**影子期覆盖纳管一轮后再切换**。
>
> 本文讨论的基线是 **PR #57 合并后**的代码，#57 还没合：
> - `survey_core.is_readonly` 加了 H3C / 锐捷、关分页（`session`）、`?` / Tcl / 写文件参数 / `clear` / 小黑名单（`_DENY_PHRASES`）；
> - `serial_write_core.slot_vendor` 排除确认 / 补齐行和 tclsh。
>
> 按键闸门（#73 / PR #74）已经单独改成白名单，不在本文范围。修订记录见文末。

## 一、为什么要改

现在第 1 层（`serial_write_core.classify` → `survey_core.is_readonly`）的判断方式是：**动词是 display / show，字符满足一组规则，不命中黑名单，就放行**。规则是"允许的动词"，后面的参数基本不限，再一条条补洞。#57 审了四轮，每一轮都挖出一类新的绕过：

| 轮次发现的类别 | 例子 | #57 的补法 |
|---|---|---|
| 缩写 / 唯一前缀解析 | `display diag` 落到 `diagnostic-information` | deny 按前缀拦 |
| 大小写 | `display Diagnostic-information` | 关键词要求全小写 |
| 引号 | `display "diagnostic-information"` | 引号只许出现在过滤词参数里 |
| 在线帮助 `?` | `display current-configuration ?`（残留放回输入行） | `?` 进禁用字符 |
| Tcl 替换 | `show version [exec reload]` | `[ ] { }` 进禁用字符 |
| 写文件参数 | `show logging process smd to-file bootflash:x`、`display x flash:/a` | `_FILE_WORDS` / 任何带 `:` 的词 |
| 顺手清计数 | `show platform hardware qfp active statistics drop clear` | `clear` / `reset` 任何位置都拒 |
| 主动发包 | `display ntp-service trace`；带主机名的 show 触发 DNS | 小黑名单；DNS 没堵 |
| 巨量输出 | `show memory`、`show running-config all`、`show platform software trace …` | 小黑名单；`show logging` 等没堵 |
| 写文件管道 | `\| save`、`\| redirect`、`\| tee` | 管道只许列出的过滤词 |

问题出在结构上：设备的命令空间是开放的（不同型号、版本、视图下命令都不一样），黑名单只能挡住已经知道的那些。审查员和作者都建议改白名单：**逐条列出确定无副作用的命令，没列出的一律交给第 2 / 3 层**。漏列一条，代价是多问一次人；用黑名单漏掉一条，代价是设备被改。

## 二、白名单怎么表达

### 2.1 选型：固定词序列 + 有类型的参数槽

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 逐字命令（整句相等） | 最简单、最好审 | `display interface GigabitEthernet0/0/1` 每个口一条，列不完 | 只用于没有参数的命令和会话命令 |
| 每条一个正则 | 灵活 | 正则本身就是攻击面，审查读不懂，没法自动生成反例 | **不用** |
| **固定词 + 参数槽（选这个）** | 词是字面量，参数从一个封闭的类型集合里选，每种类型的取值都能穷举测试 | 要维护一套类型 | **用** |

### 2.2 条目格式（写在代码里，纯数据）

```python
# serial_allowlist.py (新模块, 纯逻辑, 不 import 设备相关)
PIPES_HUAWEI = {"include": True, "exclude": True, "begin": True, "section": True,
                "count": False, "no-more": False}            # True = 要带一个 {filter} 参数, False = 不带参数
PIPES_SHOW = {"include": True, "exclude": True, "begin": True, "section": True, "count": False}   # 思科
PIPES_MIN = {"include": True, "exclude": True, "begin": True}                                    # H3C / 锐捷 (同 #57)

ALLOW = {
    "huawei": [
        E("screen-length 0 temporary",      src="survey", session=True),
        E("display version",                src="survey"),
        E("display interface {ifname}",     src="待核"),
        E("display current-configuration",  src="survey", pipes=PIPES_HUAWEI, big=True),
        ...
    ],
    ...
}
```

- 条目文本按**一个空格**切成词。词有两种：**字面量**（全小写全称，必须逐字相等），以及 `{类型}` **参数槽**（取值必须完全符合该类型）。
- **词数必须正好相等**：不许多一个词，也不许少一个词。条目里没写的词一律不认。
- **管道**：条目可以带 `pipes`，形式是 `{过滤词: 带不带参数}`，和 #57 `_RULES[...]["pipes"]` 的语义一样。比如华为的 `count` / `no-more` **不带**参数，`include` 等**必须带**一个 `{filter}`。管道最多一段。没声明 `pipes` 的条目不许带管道。
- **`session=True`**：会话级命令（关分页），只在 exec 提示符下放行，见 2.5。
- **`src`**：`survey` 表示已在巡检默认清单里、现场跑过，或者是 #57 列为待实机验证的；`待核` 表示依据是厂商命令手册的显示类命令，还没在本车型号上核过（核法见 3.6）。
- **`big=True`**：输出可能很大。给 conbroker 的超时用，也用于"大输出"的拍板。
- **条目 id**：`<厂商>:<条目文本原样>`，参数槽也写进去，比如 `huawei:display interface {ifname}`。禁用（见第四节）按 id 进行；禁用一个带参数槽的条目，就是禁用它的全部取值。
- 字面量必须是全称：设备按唯一前缀解析，白名单不复刻解析器，所以 `dis` / `sh` / `int` 一律不收。

### 2.3 参数类型（封闭集合，加新类型要改代码、补测试）

| 类型 | 取值（全部 `fullmatch`，大小写敏感） | 备注 |
|---|---|---|
| `{ifname}` | 一个词，按厂商列全称前缀 + 编号 `\d{1,3}(/\d{1,3}){0,3}(\.\d{1,4})?`。<br>华为：`GigabitEthernet` `XGigabitEthernet` `25GE` `40GE` `100GE` `Eth-Trunk` `Vlanif` `LoopBack` `MEth`<br>H3C（走华为清单时一起收，见 3.2）：`GigabitEthernet` `Ten-GigabitEthernet` `Twenty-FiveGigE` `FortyGigE` `HundredGigE` `Bridge-Aggregation` `Vlan-interface` `LoopBack` `M-GigabitEthernet`<br>思科：`GigabitEthernet` `TenGigabitEthernet` `TwentyFiveGigE` `FortyGigabitEthernet` `HundredGigE` `FastEthernet` `Ethernet` `Port-channel` `Vlan` `Loopback`<br>锐捷：`GigabitEthernet` `TenGigabitEthernet` `AggregatePort` `VLAN` `Loopback`（只收不带空格的写法） | 不收缩写（`Gi0/1`、`GE0/0/1`），见待拍板 1。**锐捷常见的 `GigabitEthernet 0/1`（带空格）第一版不收**：词数对不上就不命中，影子报告里会出现，见待拍板 1 |
| `{vlan}` | `[1-9]\d{0,3}`，并且 1–4094 | 不许前导 0 |
| `{ipv4}` | 点分四段，每段 0–255，不许前导 0 | 只收字面量，**不收主机名**（会触发 DNS） |
| `{mac}` | 华为 / H3C `[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}`；思科 / 锐捷 `[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}` | 只收小写 |
| `{filter}` | `[A-Za-z0-9._/-]{1,64}`，一个词。**不含空格、引号、冒号，也不含量词和分组**（`* + ? { } ( ) [ ]` 都不在字符集里，`.` 在设备正则里只匹配一个字符，接受）。**另外不许等于 `_FILE_WORDS` 里的任何一个词**（`to-file` `file` `filename` `tftp` `ftp` `sftp` `scp` `http` `https` `clear` `reset`），按小写比 | 只用在管道过滤段 |

没有自由文本槽，也没有"其余参数"槽。IPv6 字面量带 `:`，第一版不收，见待拍板 2。（初稿里的 `{masklen}` 没有条目用到，已删掉。）

### 2.4 匹配算法（确定、可穷举）

1. 整条先过现在的通用检查，这部分保留，作为第二道防线：只许可打印 ASCII、词之间恰好一个空格、不许前后空白，也不许 `_FORBIDDEN` 里的字符（`; & < > \` $ # ! ? [ ] { }`）。
2. 按 `" | "` 切成主段和至多一个管道段。出现第二个 `|`，或者 `|` 粘在词上，都拒。
3. 主段逐个和该厂商**没有被禁用的**条目比：词数相等，字面量逐字相等，参数槽各自 `fullmatch`。必须**恰好命中一条**；命中 0 条，或者因为两条条目重叠命中多条，都不放行。重叠由测试保证不会出现。
4. 有管道段：命中的条目得声明过这个过滤词；`True` 的必须正好带一个 `{filter}`，`False` 的不许带参数。
5. 命中的是 `session` 条目时，还要满足 2.5 的提示符约束。
6. 放行时把命中的条目 id 写进闸门结果（`gate.entry`），日志和确认框都能看到"凭哪一条放行"。

### 2.5 提示符上下文（更正初稿）

**初稿写的"所有条目都只在 exec 提示符下判断"和代码不符，更正如下。** 写闸门的 `slot_vendor` 认这些提示符：
- 华为：`<名>`、`[名]`、`[~名]`、`[*名]`、`[名-子视图]`；
- 思科：`名#`、`名>`、`名(config)#`、`名(config-if)#`；
- 锐捷同思科，子视图可以带空格。

（#57 起，确认 / 补齐行、tclsh、shell 都不算提示符。）所以第 1 层判断时，设备可能停在系统视图或者 config 模式里：

- **普通 display / show 条目**：可以在上面任何一种提示符下放行。华为 `display` 在系统视图和子视图里照样是显示命令；思科在 `(config)#` 下直接发 `show` 会报 invalid，只是没效果，不会改配置。（`do show …` 不在白名单里，要确认。）
- **`session` 条目**（`screen-length 0 temporary`、`screen-length disable`、`terminal length 0`）**只在用户视图 / exec 提示符下放行**：华为 / H3C 只认 `<名>`，思科 / 锐捷只认 `名#` 或 `名>`，并且不带括号。理由：同样的词在系统视图或 `user-interface` 视图里可能变成配置命令，比如不带 `temporary` 的 `screen-length 0` 就是 user-interface 配置。
- 实现上，`slot_vendor` 返回的"在提示符上"细化成 `mode ∈ {"exec", "config"}`，第 1 层按 `(cmd, vendor, mode)` 匹配。`mode` 由最后一行判断，判断不了就按 `config` 处理（更严）。

## 三、初始清单（只放巡检和排障最常用、确定没有副作用的）

依据：`survey`、`待核` 的含义见 2.2。本节**已经去掉和 #57 `is_readonly` 矛盾的条目**，矛盾的条目和处理办法见 3.7。

### 3.1 华为 VRP（写闸门里登记成 `huawei` 的槽位用这张，H3C 也暂时用它，见 3.2）

| 条目 | 依据 | 备注 |
|---|---|---|
| `screen-length 0 temporary` | survey（#57 关分页） | `session`，只认 `<名>` |
| `display version` / `display clock` | survey | |
| `display ip interface brief` / `display interface brief` | survey | |
| `display lldp neighbor brief` | survey | |
| `display mac-address` / `display arp` | survey | `big` |
| `display current-configuration`（管道：include / exclude / begin / section 带参数；count / no-more 不带参数） | survey | `big`，见待拍板 4 |
| `display interface {ifname}` | 待核 | |
| `display vlan` / `display vlan {vlan}` | 待核 | |
| `display stp brief` / `display eth-trunk` | 待核 | |
| `display device` / `display elabel` | 待核（#57 测试里当正例用过） | `elabel` 输出长 |
| `display cpu-usage` / `display memory-usage` | 待核 | `memory-usage` 是一个词，不命中 #57 的 `("memory",)` 短语 |
| `display alarm active` | 待核 | |
| `display transceiver interface {ifname} verbose` | 待核 | |
| `display mac-address {mac}` | 待核 | |
| `display ip routing-table {ipv4}` | 待核 | 不收不带参数的全表（核心设备上很大） |

**不进第 1 层**（交给第 2 / 3 层）：`display diagnostic-information`（能写文件）、`display logbuffer`（大）、`display ntp-service trace`（发包）、`ping` / `tracert`（发包，见待拍板 3）。

### 3.2 H3C Comware

**更正初稿**：写闸门（`serial_write_core`）**不能登记 `h3c`**。H3C 的 `<名>` / `[名]` 和华为同形，#57 明确把 H3C 按华为处理。所以初稿里单列的 H3C 清单**只对巡检生效**（巡检的 `--vendor h3c` 或 `auto` 认出来的），在写闸门里不生效；初稿里"H3C 和华为互不命中"的跨厂商测试，前提在写闸门里不成立。两种走法写进待拍板 13：

- **A. 写闸门新增 `h3c` 厂商**：只能靠登记槽位，因为提示符分不开；`auto_huawei` 会把 H3C 认成华为，需要另想办法，比如发前看 `display version` 的缓存结果。工作量大。
- **B. 写闸门里 H3C 继续走华为清单（建议第一版这么做）**：3.1 的华为条目同时用于 H3C 设备，"华为条目在 Comware 5 / 7 上的行为"列入待核（3.6），重点核：
  - `screen-length 0 temporary` 在 Comware 上会不会被当成别的命令；
  - `display lldp neighbor brief` / `display eth-trunk` 在 Comware 上不存在时，设备是报错，还是按前缀落到别的命令上。

巡检用的 H3C 清单（和 #57 一致，`session` 为 `screen-length disable`，只认 `<名>`）：

| 条目 | 依据 | 备注 |
|---|---|---|
| `screen-length disable` | survey（#57） | `session`；用户视图下只对当前会话生效（#57 待实机验证） |
| `display version` / `display clock` | survey | |
| `display ip interface brief` / `display interface brief` | survey | |
| `display lldp neighbor-information list` | survey | |
| `display mac-address` / `display arp` | survey | `big` |
| `display current-configuration`（管道：include / exclude / begin） | survey | `big` |
| `display interface {ifname}` / `display vlan {vlan}` / `display stp brief` / `display link-aggregation summary` | 待核 | |
| `display device` / `display cpu-usage` | 待核 | |
| `display transceiver interface {ifname}` | 待核 | |

初稿里的 `display memory` 已删掉：它命中 #57 的小黑名单 `("memory",)`，见 3.7。

### 3.3 思科 IOS / IOS-XE

| 条目 | 依据 | 备注 |
|---|---|---|
| `terminal length 0` | survey（#57） | `session`，只认 `名#` / `名>` |
| `show version` / `show clock` | survey | |
| `show ip interface brief` / `show interfaces status` | survey | |
| `show cdp neighbors` / `show lldp neighbors` | survey | |
| `show mac address-table` / `show arp` | survey | `big` |
| `show running-config`（管道：include / exclude / begin / section 带参数；count 不带参数） | survey | `big`；**不收** `show running-config all` |
| `show interfaces {ifname}` | 待核 | |
| `show vlan brief` / `show spanning-tree summary` / `show etherchannel summary` | 待核 | |
| `show inventory` | 待核 | |
| `show cdp neighbors detail` / `show lldp neighbors detail` | 待核 | |
| `show ip route {ipv4}` | 待核 | 只收字面量 |
| `show interfaces {ifname} transceiver` | 待核 | 平台相关 |

**不进第 1 层**：`show tech-support`、`show memory*`、`show logging`（#57 设计文档里列为"照样放行"的已知局限，白名单下自然消失）、`show processes cpu*`、`show platform …`、所有 `… to-file …`。NX-OS 第一版不单列，按思科条目匹配，3.6 的回显核对同样适用。

### 3.4 锐捷 RGOS

| 条目 | 依据 | 备注 |
|---|---|---|
| `terminal length 0` | survey（#57） | `session`，只认 `名#` / `名>` |
| `show version` / `show clock` | survey | |
| `show ip interface brief` / `show interfaces status` | survey | |
| `show lldp neighbors` | survey | |
| `show mac-address-table` / `show arp` | survey | `big`；和思科写法不同 |
| `show running-config`（管道：include / exclude / begin） | survey | `big` |
| `show interfaces {ifname}` / `show vlan` / `show aggregatePort summary` | 待核 | 接口名带空格的写法第一版不收（2.3） |

### 3.5 Linux 槽位：第一版不开

**更正初稿**：#57 的 `_RULES` 里没有 `linux`，`is_readonly(…, "linux")` 恒为 False。迁移期第 1 层用的是"白名单命中 **且** `is_readonly` 也过"，初稿的 Linux 条目在这个组合下永远不会放行。所以第一版**不给 Linux 列白名单**，维持现状：全部走第 2 层（样例里 `linux.model=false`）或人确认。以后要开，需要单独的设计（`is_readonly` 不适用，对端 alias / 函数 / `PATH` 也不可控），见待拍板 8。

### 3.6 "待核"怎么核（每条都要做，结果写回 `src`）

在推车常见型号上实机跑一次，记下这几件事：
1. **回显和输出确实是这条命令本身**。设备上没有这个关键词时，可能报错，也可能被当成别的命令的前缀或参数解析。看回显的命令行和输出标题是否对得上；对不上的条目不收。（这一条也覆盖 3.2 B 方案里"华为条目在 Comware 上"的情况。）
2. 会不会弹确认、要不要分页。
3. 输出有多大、要跑多久（决定是否标 `big`）。
4. 会不会主动发包（DNS、NTP、LLDP 主动探测等）。

### 3.7 和 #57 `is_readonly` 的已知差异（初稿矛盾的处理）

| 条目 / 规则 | 矛盾在哪 | 处理 |
|---|---|---|
| H3C `display memory` | #57 `_DENY_PHRASES` 有 `("memory",)`，`is_readonly` 拒 | **移出第一版** |
| Linux 全部条目 | #57 `_RULES` 没有 `linux` | **移出第一版**（3.5） |
| `{filter}` 参数 | #57 在任何位置都拒 `_FILE_WORDS` 和带 `:` 的词；初稿的 `{filter}` 没排除 | **`{filter}` 排除 `_FILE_WORDS` 和冒号**（2.3） |

处理完之后，"白名单命中但 `is_readonly` 不过"的条目应该是**零条**。测试会断言这一点（7.3）；影子报告里一旦出现，就是 bug，不能当成已知差异扣掉。以后要是**有意**加一条 `is_readonly` 不收的条目（比如长期去掉"且 `is_readonly` 也过"之后），先进这张表，测试和影子报告都按这张表扣减。

## 四、和其他几层、其他调用方的关系

```
send_command(cmd)
  ├─ 命令合不合格 (一行可打印 ASCII 等, 不变)  ── 不合格 → 拒
  ├─ 高危词 (DANGER, 不变)                      ── 命中   → 第 3 层 人确认
  ├─ 认不出厂商 / 不在提示符上 (不变)            ──        → 第 3 层
  ├─ 【新】白名单恰好命中一条 (按 vendor + mode) ──        → 第 1 层 放行 (gate.entry = 条目 id)
  └─ 其余                                         ──        → 第 2 层模型 (开了的话) → 放行 / 第 3 层
```

- **高危词检查保留在白名单之前**，是纵深防御。测试会断言条目里不含高危词。
- **巡检**：`survey_core` 改用同一份白名单。默认清单、关分页命令都要恰好命中一条；`survey_profiles.json` 仍然只能从白名单里挑命令，挑了不在白名单里的，整次巡检被拒。**迁移期和切换的影响**见 5.2。
- **`survey_core.conbroker_send`**（巡检真正发出去前的最后一道检查）：现在是"任一厂商的 `is_readonly` 放行"（各厂商并集）。改成"任一厂商白名单里**没被禁用的**条目命中，且（迁移期内）该厂商的 `is_readonly` 也过"。它拿不到提示符，所以不判 `session` 的 mode。这一点由 `run_survey` 发第一条之前的 `at_prompt` 保证：#57 起巡检只认 exec 提示符。
- **`probe_vendor` 的探测命令**（`show version`、`display version`）：发的时候还不知道厂商，按上面的并集判断。两条分别是思科 / 锐捷和华为 / H3C 的 survey 条目，正常都命中。要是被禁用（见下面），`auto` 认不了厂商，巡检会拒绝并提示用 `--vendor` 指定。测试断言：默认配置下 `_PROBES` ⊆ 并集。
- **规则文件 `allowlist.disable`**（取代 `extra_verbs`，只能收紧）：
  - 写法是条目 id 的列表（2.2）。不认识的 id 忽略，并在 `load_rules` 里记一条 cartlog 警告（和现在规则文件的容错一致）。
  - 生效范围：写闸门第 1 层、巡检预检、`conbroker_send`、`probe_vendor` 全部生效，一处禁用处处禁用。
  - 禁用了**默认巡检清单里的条目**：巡检预检时该命令不合格，按现在的规则整次巡检被拒，并提示哪一条被禁用了。禁用了关分页，巡检同样被拒（#57 规定关分页失败就中止）。禁用了探测命令，`auto` 不可用。要不要保护这几类条目不让禁用，见待拍板 14。
  - **回退到 `LAYER1_MODE = "readonly"` 时，`disable` 照样生效**：命中被禁用条目的命令，即使 `is_readonly` 放行也不放。`disable` 只往更严的方向改，回退不能让它失效。命中不了任何条目的命令，在 readonly 模式下按 `is_readonly` 判，和现在一样。
  - 不能新增条目：新增要改代码，带测试走 PR。见待拍板 5。
- **第 2 层模型**：不在白名单里的命令都会到它这里，调用量、费用和延迟都会上升。它的 fail-closed 不变；模型没开就直接到人。
- **人确认**：确认框里写明"不在第 1 层白名单"，并附上最接近的条目（按字面量前缀比出来的）。
- **按键闸门（#74）**：独立，不受影响。

## 五、迁移计划

### 5.1 阶段

| 阶段 | 内容 | 行为变化 | 退出条件 |
|---|---|---|---|
| 0 | 本设计稿拍板；#57 先合；**#57 之后不再往 `is_readonly` 加黑名单补丁**，新发现的绕过类别只进反例回归集（7.4） | 无 | 拍板 |
| 1 | 新模块 `serial_allowlist.py` + 测试。写闸门第 1 层**同时**算两种判断，写进 `gate.shadow = {"allowlist": 条目 id 或 null, "readonly": bool}`，**判定仍按 `is_readonly`**。**巡检预检（`run_survey` 发第一条前对整份清单的检查）也同时算白名单**：不在白名单的命令记一条 cartlog `[survey] allowlist-miss 厂商 命令`，巡检照常跑 | 无（只多写日志） | 合并 |
| 2 | 影子期：**覆盖纳管一轮**（拍板 7：纳管现场每类设备、每种自定义巡检清单都跑过；"待核"条目随巡检核完）。用新脚本 `scripts/gate_shadow_report.py` 在推车上读 `serial-slot*.log` 和 `events.log` 汇总（日志含设备配置，不出推车，只打印命令和计数） | 无 | 覆盖纳管一轮，且 5.2 第 2 类为 0、第 3 类清零或每条有结论，维护者看过报告 |
| 3 | 切换：第 1 层、巡检预检、`conbroker_send` 都改成"白名单命中 **且** `is_readonly` 也过" | 一部分命令从直发变成第 2 / 3 层；不在白名单的自定义巡检命令会让巡检被拒 | 稳定一个版本 |
| 4 | 去掉 `is_readonly` 里白名单已覆盖的补丁，或者长期保留"且 `is_readonly` 也过"（待拍板 6） | 无 | — |

### 5.2 影子报告的分类

1. **写闸门：`is_readonly` 放行、白名单不放行**。这些命令切换后会变成要人确认。逐条看：是该补的条目，还是本来就不该直发（大输出、发包、接口名带空格之类的写法）。按频次排序。
2. **写闸门：白名单放行、`is_readonly` 不放行**。这一类应该是 0 条（3.7），出现就是白名单的 bug，切换前必须修掉。
3. **现场巡检清单里不在白名单的命令**（来自 `[survey] allowlist-miss`），单独列出：这些清单在阶段 3 会**直接失效**，整次巡检被拒。切换前要么补条目，要么让现场改清单。阶段 3 的门槛里加一条：这一类要么清零，要么每条都有处理结论。
4. 各厂商切换后要人确认的比例估计（按第 1 类占比算）。

回退：阶段 3 带一个代码常量 `LAYER1_MODE = "allowlist+readonly"`，回退时改成 `"readonly"`，`disable` 仍然生效（第四节）。只保留一个版本，之后删掉。不放进规则文件，不能让现场配置把闸门放宽。

## 六、对运维可用性的影响和缓解

影响：
- 不在清单里的只读命令（各种 display / show 的长尾），从直发变成走模型或人确认。
- 缩写和接口简写（`dis int b`、`Gi0/1`）、锐捷带空格的接口名（`GigabitEthernet 0/1`）全部走确认。
- **管道**：`no-more` 只有华为条目收，H3C / 锐捷 / 思科条目都不收（和 #57 一致）；`include` 等只收一个安全词，带空格或正则的过滤走确认。#57 下 `show running-config | include "a b"` 能直发，切换后要人确认。
- H3C 在写闸门里走华为清单（3.2 B）。Comware 特有的命令，比如 `display lldp neighbor-information list`，在写闸门里会走确认，巡检不受影响。
- 自定义巡检清单里不在白名单的命令，在阶段 3 会让巡检被拒（5.2 第 3 类）。
- 清单维护有成本：现场每发现一条常用命令，都要走一次 PR。

缓解：
1. **让 Agent 知道清单**：新增只读 MCP 工具 `gate_allowlist(slot)`，按该槽位登记的厂商和当前 mode 返回可用条目和参数类型说明。`send_command` 的描述里写"先查、照抄写法"。被挂起时的回包也带上"最接近的条目"。
2. **影子报告驱动补条目**：第 1 类、第 3 类按频次排序，高频的优先补。
3. **巡检照旧是批量看的主路**：默认清单全在白名单里。
4. **第 2 层模型**：开了以后，长尾命令多数由模型放行，代价是费用和延迟。
5. **确认框**：附上最接近的条目和"凭什么没放行"。以后可以考虑"同一条命令本会话内确认过就不再问"（不在本次范围）。

## 七、测试策略（按上面各节逐条对应）

1. **正例（自动生成，对应 2.2 / 2.3）**：每种参数类型定义一组规范样例。`{vlan}` 取 1、100、4094；`{ipv4}` 取 192.0.2.1、198.51.100.254、203.0.113.0；`{ifname}` 每个厂商的每个前缀各取一两个，H3C 的 `FortyGigE` / `HundredGigE` / `Twenty-FiveGigE` 也包括在内；`{mac}` 取 `0200-0000-0001` / `0200.0000.0001`；`{filter}` 取 `sysname`、`GigabitEthernet0/0/1`。按条目展开成具体命令，断言恰好命中这一条。
2. **变异反例（自动生成，对应 2.2 / 2.3 / 2.4）**，全部要拒：
   - 每个字面量：改大写、首字母大写、所有真前缀（缩写）、加双引号、加单引号；
   - 末尾多一个词，或者少一个词；
   - 插入 `?`，双空格，制表符，前后空白；
   - 拼接 `; reload`，管道 `| save x` / `| redirect flash:x` / `| tee x`；
   - 参数越界：vlan 0 / 4095 / 前导 0，ipv4 256 / 前导 0 / 三段；
   - 参数里带 `:` `[` `{` `"`，参数换成主机名；接口名用缩写；锐捷 `GigabitEthernet 0/1`（带空格）。
3. **和 #57 `is_readonly` 一致（对应 3.7）**：每条条目的每个正例样例，`is_readonly(样例, 同厂商)` 都要放行，除非条目在 3.7 的差异表里登记过（现在表里没有"白名单比它宽"的条目）。另外断言：没有 Linux 条目；没有 `display memory`；`{filter}` 取 `_FILE_WORDS` 里的每个词、取带冒号的词，都拒。
4. **已知绕过回归集**：把 #57 四轮测试里的反例汇总成数据文件 `tests/data/layer1_bypass.json`，按类别分组：缩写 / 前缀、大小写、引号、`?`、Tcl、写文件参数、`clear` / `reset`、主动发包、巨量输出、写文件管道、关分页变形、写命令 50 条。每个厂商、每种 mode 都跑一遍，全部要拒。以后新发现的绕过只往这份文件里加。
5. **管道（对应 2.2 第 4 点）**：
   - 华为 `count` / `no-more` 不带参数放行，带参数拒；
   - `include` / `exclude` / `begin` / `section` 不带参数拒；
   - H3C / 锐捷 / 思科的条目带 `no-more` 拒；
   - 两段管道拒；`|` 粘在词上拒；
   - `{filter}` 含 `*` `+` `(` `)`、空格、引号都拒。
6. **提示符上下文（对应 2.5）**：
   - `session` 条目只在 `<名>` / `名#` / `名>` 下放行，在 `[名]`、`[~名]`、`[名-子视图]`、`名(config)#`、`名(config-if)#` 下都拒；
   - 普通 display / show 条目在这些提示符下都放行；
   - mode 判断不了时按 config 处理（`session` 被拒）。
7. **H3C（对应 3.2）**：
   - 巡检：`vendor="h3c"` 用 H3C 清单，H3C 条目在华为下不命中，反之亦然（这是巡检层面的跨厂商测试）；
   - 写闸门：H3C 没有单独的厂商，登记不进 `h3c`，`auto_huawei` 认成华为，用华为清单。测试把这一点固定下来，跨厂商测试只在思科 / 锐捷 / 华为之间做，思科和锐捷文本相同的条目除外。
8. **结构不变量**：
   - 任意两条条目不重叠：对正例样例做笛卡尔检查，没有一个样例同时命中两条；
   - 条目里没有高危词，没有 `_FORBIDDEN` 字符，没有 `_FILE_WORDS`；
   - 字面量全部是小写全称：维护一张"已知缩写"表，断言不出现；
   - 巡检默认清单和关分页 ⊆ 白名单；`_PROBES` ⊆ 各厂商并集；
   - 参数类型表里每个类型至少被一条条目用到（防止再出现 `{masklen}` 这种死类型）。
9. **`conbroker_send` / `probe_vendor`（对应第四节）**：并集判断；被禁用的条目在 `conbroker_send` 里也拒；禁用 `cisco:show version` 和 `ruijie:show version` 之后，`probe_vendor` 的 `show version` 不发，`auto` 报错提示用 `--vendor`。
10. **`allowlist.disable`（对应第四节）**：
    - 不认识的 id 被忽略并记警告；
    - 禁用带参数槽的条目，它的所有取值都拒；
    - 禁用默认巡检条目，整次巡检被拒，提示里写明是哪一条；禁用关分页同样被拒；
    - `LAYER1_MODE="readonly"` 时，命中被禁用条目的命令即使 `is_readonly` 放行也拒；
    - 规则文件里写新增条目无效。
11. **巡检预检影子（对应 5.1 阶段 1）**：自定义清单里不在白名单的命令，会记一条 `[survey] allowlist-miss`，巡检照常跑；默认清单不产生这种记录。
12. **随机组合**：用固定种子，把字面量、参数样例、通用危险词拼成大量组合，断言只有条目的规范形式会放行。只用标准库 `random`。
13. **影子报告（对应 5.2）**：`gate.shadow` 的写法，以及报告脚本四类汇总的口径，用假日志测；第 2 类非零时报告显式标红；3.7 差异表里登记的条目从第 2 类里扣掉（现在表里没有）。

## 八、待拍板

1. **接口名缩写**收不收（`Gi0/1`、`GE0/0/1`）？**锐捷带空格的写法**（`GigabitEthernet 0/1`）要不要做成两个词的槽位？建议第一版都不收，看影子报告再说。
2. **IPv6 字面量**收不收？建议第一版不收。
3. **主动发包的命令**（`ping` / `tracert` / `traceroute`）进不进第 1 层？建议不进。原来经 `extra_verbs` 开 ping 的现场会因此多问人。
4. **大输出命令**进不进？建议 `display current-configuration` / `show running-config` / `mac-address` / `arp` 进（巡检需要），标 `big`；`show logging` / `display logbuffer` 不进。
5. **规则文件**能不能新增条目？建议只能禁用。
6. **切换后**是只用白名单，还是长期保留"且 `is_readonly` 也过"？建议先保留一个版本。
7. **影子期**多长、切换门槛是什么？建议 2 周且不少于 200 条 Agent 命令；5.2 第 2 类为 0；第 3 类清零或者每条都有结论。
8. **Linux 槽位**以后要不要单独设计第 1 层？第一版不开（3.5）。
9. 要不要加 **`gate_allowlist` 只读工具**？建议加。
10. **#57 之后冻结 `is_readonly` 的黑名单补丁**：新发现的绕过只进回归集。
11. **管道过滤参数**限成一个安全词，可以吗？
12. **"待核"条目**由谁、在哪些型号上核（按 3.6 的四项）？
13. **H3C 在写闸门里怎么办**：A 新增 `h3c` 厂商（要解决提示符同形），还是 B 第一版走华为清单，把"华为条目在 Comware 5 / 7 上的行为"列入待核？建议 B。
14. **能不能禁用默认巡检条目、关分页和探测命令**？允许的话巡检会被拒或者 `auto` 不可用（第四节）。建议允许但记警告，或者规则文件校验时直接忽略这几类条目的禁用。

### 拍板结果（2026-11-07）

1. 接口名缩写不收（锐捷带空格写法也不收）。
2. IPv6 先不收。
3. ping / traceroute 不进第 1 层。
4. 大输出命令不进（写闸门第 1 层）。实现上见第九节第 2 条。
5. 规则文件只能禁用、不能新增。
6. 长期保留"白名单命中 **且** `is_readonly` 也通过"。影子期只记录这一组合结论（`gate.shadow.pass`）。
7. 影子期覆盖纳管一轮后再切换。
8. Linux 槽位暂不开第 1 层。
9. 加 `gate_allowlist`（只读 MCP 工具）。
10. 冻结黑名单补丁：`is_readonly` 不再加新规则，新发现的绕过只进反例集。
11. 管道参数限一个安全词（`{filter}`）。
12. 待核条目在纳管现场随巡检核实（条目里 `src="待核"`，不猜）。

13、14 未单独拍板：13 按建议 B（写闸门里 H3C 走华为清单）；14 影子期不涉及（禁用只影响影子结论），切换前再定。

## 九、阶段 1（影子期）实现记录

代码：`serial_allowlist.py`（纯数据 + 纯逻辑）；接线在 `serial_write_core.classify` / `load_rules`、`mcp_serial`（`send_command` 传 mode，新工具 `gate_allowlist`）、`survey_core.run_survey`（预检影子）。测试：`tests/test_serial_allowlist.py`、`tests/test_layer1_shadow.py`。

1. **放行结果不变**：`classify` 轮到第 1 层判（不带高危词、认得出厂商、停在提示符上）的命令，闸门结果多一个 `shadow = {allowlist: 条目 id 或 null, src, readonly, pass[, why]}`，随闸门结果进 `serial-slot<N>.log`。`pass` = 白名单命中且 `is_readonly` 也过。高危词 / 认不出厂商 / 不在提示符上的命令轮不到第 1 层，没有 `shadow`。测试断言：任意 mode、禁用全部条目，闸门结果（除 `shadow`）都和不算影子时一样；影子计算抛异常时写 `shadow.error`，结论照旧。`is_readonly` 的规则有快照测试（冻结黑名单补丁）。
2. **大输出（拍板 4）**：条目上的 `big=True` 改成"只给巡检"：写闸门范围（`scope="gate"`）不命中，`why` 写明大输出；巡检预检（`scope="survey"`）命中。这样巡检默认清单仍 ⊆ 白名单（第四节），Agent 直发的 `display current-configuration` / `show running-config` / `mac-address` / `arp` 将来要模型或人确认。**这是对拍板 4 的解读，待维护者确认。**
3. **mode**：`serial_allowlist.prompt_mode` 由最后一行判（华为 / H3C `<名>`，思科 / 锐捷不带括号的 `名#` / `名>` 为 exec，其余按 config）。main 上的 `slot_vendor` 还没有 mode（#57 才加），所以 mode 在 `mcp_serial` 里另算，`classify(..., mode=None)` 不给按 config。
4. **规则文件**：`allowlist.disable` 只认已有条目 id；不认识的进 `allowlist_unknown`（每条截断到 80 字符、最多 20 条，超出给计数；`gate_allowlist` 返回，巡检命令行和面板巡检都经 `survey_core.gate_disabled` 读、记 `[survey] allowlist-disable-unknown`）；`allowlist` 里别的键、`vendors.<厂商>.allowlist` 一律无效。影子期禁用只影响影子结论。
5. **巡检预检影子**：整份清单在 `is_readonly` 预检通过后再按白名单（`mode="exec"`, `scope="survey"`）算一遍，不命中的每条记 `[survey] allowlist-miss vendor=… cmd=… why=…`，结果里 `allowlist_miss` 列出，巡检照常跑。
6. **`conbroker_send` / `probe_vendor` 不动**：影子期它们仍按 `is_readonly`；main 上还没有 `probe_vendor`（#57）。阶段 3 再改。
7. **和 `is_readonly` 的差异（3.7）**：main 上 `is_readonly` 只有 huawei / cisco，关分页还不放，所以 `KNOWN_DIFF` 暂时登记 `huawei:screen-length 0 temporary`、`cisco:terminal length 0`，#57 合并后删掉；H3C / 锐捷的一致性测试等 #57。
8. **字面量大小写例外**：`ruijie:show aggregatePort summary` 照手册原样（`CASE_EXCEPTIONS`），待核。
9. **没做的**：影子报告脚本 `scripts/gate_shadow_report.py`（阶段 2 用，另起 PR）；确认框里的"最接近的条目"；反例数据文件 `tests/data/layer1_bypass.json`（反例现在写在测试里，等 #57 合并后把四轮反例汇总进去）。

## 修订记录

- **初稿**（PR #75 首个提交）。
- **按审查修订**：
  - 更正提示符上下文，`session` 条目只认 exec（2.5）；
  - 写清写闸门不能登记 H3C，新增待拍板 13（3.2）；
  - 去掉和 #57 `is_readonly` 矛盾的 `display memory`、Linux 条目，`{filter}` 排除 `_FILE_WORDS` 和冒号，新增差异表（3.7）；
  - 巡检预检纳入影子期，报告单列自定义巡检命令（5.1 / 5.2）；
  - 管道写明是否带参数，补 `no-more`（2.2 / 3.1 / 第六节）；
  - 写明 `conbroker_send`、`probe_vendor` 的判法，`allowlist.disable` 的 id 规则、对巡检和回退的影响（第四节）；
  - `{filter}` 措辞改成"不含量词和分组"；待核加回显核对（3.6）；
  - 删掉 `{masklen}`，补 H3C 接口名，写明锐捷带空格写法第一版不收（2.3）；
  - 第七节测试和各节逐条对应。
