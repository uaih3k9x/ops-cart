# 大肥鱼面板：接口约定 v1（#53）

车机（root）和大肥鱼（普通用户 `bigfish`，rootless Docker）之间只通过两个文件交换信息。车机不进大肥鱼的容器，不读 dsh 的数据卷，也不调用大肥鱼的任何接口。

下面第一部分是两边定稿的约定原文（v1 最终版），第二部分说明推车这边具体怎么实现，方便两边对照。

## 一、约定原文（v1 最终版）

【状态文件】`/home/bigfish/status/bigfish.json`。目录 0711，文件 0644，属主 bigfish（按用户名查 uid）。大小不超过 64 KB，空闲时每 5 秒心跳一次。读取时：
- 用 O_NOFOLLOW 打开，fstat 核对三项：是普通文件、属主 uid 正确、大小不超过上限。
- JSON 坏了就丢弃。
- `written_at` 超过 20 秒没更新，显示为"离线"。

可信的顶层字段（类型和取值仍然要校验）：
- `schema`: 1
- `writer`
- `seq`
- `written_at`
- `estop`: {`seen`, `since`}
- `current`: null，或者 {`task_id`, `state`（starting|running|done|failed|cancelled|estopped）, `started_at`, `elapsed_s`, `steps_total`, `step_current`, `stop_reason`, `source`}
- `running_count`

不可信字段 `untrusted`：
- `title`：最长 200 字
- `output_tail`：最后 2 KB
- `steps`：最多 10 项，每项 {`title` 最长 120 字, `status`}

这几项都要清洗后只当纯文本显示，并标明"大肥鱼说："。

【急停】
- 目录 `/run/bigfish-ctl`，root:bigfish 0750，由大肥鱼那边的 setup-root.sh 用 tmpfiles 建，推车仓库不加 tmpfiles。目录不存在时，面板显示"急停通道未就绪"，急停直接走下面的硬急停。
- kiosk 急停时原子写 `/run/bigfish-ctl/estop.json`，权限 root:bigfish 0640，内容 `{"seq": n, "at": unix, "by": "kiosk"}`。seq 每次加 1，可以存在同一个文件里，先读旧值再加。解除急停时删除这个文件。
- 车机面板上的急停按钮，和现有的"Agent 全部急停"，都要挂到这个动作上。全部急停解除时，同步删除这个文件。
- 硬急停兜底：写完 estop.json 后 10 秒内，如果状态文件里 `estop.seen` 没变成 true（或者状态文件离线），kiosk 就执行 `systemctl stop user@<bigfish 的 uid>.service`，按用户名查 uid，不要写死，并记一条 cartlog。面板显示"已硬停，需人工恢复"。恢复的事 kiosk 不管。

## 一点五、v1.1 新增字段（向后兼容）

- 状态文件 `estop` 新增 `seq`：wrapper 回显它最近处理过的那个 estop.json 的 `seq`。
- 判定规则：kiosk 判"大肥鱼已急停"时，状态里有 `estop.seq`，就要求 `estop.seen == true` 并且 `estop.seq` 等于这次写入的 seq；字段给了但不是非负整数时，按对不上处理。字段是 `null` 时（wrapper 还没处理过任何 estop.json，或者文件不存在、读不懂），也算对不上。没有这个字段时，退回 v1 的判断，只看 `estop.seen == true`。
- 兼容性：v1 的 wrapper 不写这个字段，判断照旧，`schema` 仍然是 1。

## 二、推车这边的实现（`bigfish_core.py`、`bigfish_panel.py`、`kiosk.py`）

### 读状态文件
- 打开方式是 `os.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)`：加 O_NONBLOCK 是为了防止对方放一个 FIFO 把车机卡住。打开后用 fstat 核对三项（普通文件、`st_uid` 等于 `pwd.getpwnam("bigfish")`、`st_size` ≤ 64 KiB），任何一条不满足就当作没有数据。这样 bigfish 就没法用软链接让 root 替它读出 /etc/shadow 之类的文件。
- 只认 `schema == 1`，`written_at` 必须是数字。超过 20 秒没更新，或者比当前时间快 5 秒以上，都显示"离线"。文件不存在显示"不在线"，内容坏了显示"状态读不懂"。未知字段一律忽略。
- 可信字段也做校验，不合格的单独丢掉，不影响其他字段：
  - `current.state` 不在白名单里，整个 `current` 丢弃。
  - `elapsed_s` 必须是 ≥ 0 的数字。
  - `steps_total` 必须是 0 到 10000 的整数，0 表示总步数未知。`step_current` 是不超过 `steps_total` 的非负整数；`steps_total` 为 0 时上限是 10000；`steps_total` 不合格时，`step_current` 一起丢弃。显示时，总步数已知写"第 2/5 步"，为 0 时只写"第 2 步"。
  - `estop.seen` 只认布尔值 `true`。
  - `running_count` 必须是 ≥ 0 的整数。
  - `stop_reason` 只按枚举显示，不显示原文：`estop` 显示"急停"，`cancelled` 显示"已取消"，`end_turn` 显示"正常结束"，`max_tokens` 显示"超出长度"，`max_turn_requests` 显示"轮数到上限"，`refusal` 显示"模型拒绝"，其他值一律显示"其他"。`source` 清洗后最多 40 字，不显示。
  - `writer` 和 `seq` 不显示。
- 不可信字段先清洗再显示，清洗规则是 `confirm_core.one_line`：删 ANSI 和 OSC 转义，控制、格式字符（含双向覆盖、零宽字符、U+2028）换成 `?`，空白压成一个空格，超长截断加"…"。
  - `title` 最多 200 字。
  - `output_tail` 只取最后 2048 个字符，按行切开，每行单独清洗，只显示最后 5 行。
  - `steps` 只取前 10 项，`title` 最多 120 字，`status` 最多 20 字。
- 面板上，每一行大肥鱼的话都加"大肥鱼说："前缀，用单独的颜色和等宽字，并且缩进。Text 里只插入纯文字，不认链接，不执行任何内容。
- 锁屏（车机右上角）和副屏锁屏页只显示一行，例如"大肥鱼 · 空闲""大肥鱼 · 在做事 · 第 2/5 步""大肥鱼 · 急停中"。这一行只用可信字段拼出来，不包含原文。

### 急停
- 入口都在 `Kiosk._serial_action("estop", None)`：执行器条上的"Agent 全部急停"、串口和键鼠确认框里的"全部急停"、大肥鱼面板上的"全部急停"，走的都是它。它在串口急停之前先写 estop.json，所以写 estop.json 不会因为串口那边正忙而被耽误。只急停某一路时不写。
- 写入流程：
  1. 读出旧 seq，没有或者读不懂就当 0，新 seq 等于旧值加 1。
  2. 用 `O_EXCL | O_NOFOLLOW` 建临时文件，先 `fchown` 成 bigfish 组、`fchmod 0640`，写入后 fsync。
  3. `rename` 覆盖正式文件。这样大肥鱼不会读到半个文件，也不会读到一个它没有权限读的文件。
- 解除全部急停：串口那边解除成功后，删除 estop.json，同时清掉看门器。解除失败时保留文件。如果串口那边已经没有急停、只剩 estop.json，"解除全部急停"的检查也照样放行，只删 estop.json。
- **estop.json 残留后的恢复**：有三种情况会让 estop.json 留下来，而串口那边已经没有急停：在终端里 resume 解除；按路逐个解除；或者写完 estop.json 后串口急停本身失败了。这时大肥鱼会一直拒任务。恢复办法：
  1. 在车机上：大肥鱼面板只要 estop.json 还在，就显示「解除大肥鱼急停」按钮。按钮放在按钮条的另一头，和"全部急停"隔开。点它要先过一道门：设了 PIN 就验 PIN（和锁屏、电源用的是同一个闸门）；没设 PIN 就弹一个"是 / 否"二次确认。通过后删掉 estop.json，不动串口。只剩 estop.json 时点"解除全部急停"，过的也是这同一道门。
  2. 在终端里：手动删除文件 `sudo rm -f /run/bigfish-ctl/estop.json`。大肥鱼读不到这个文件后，就会恢复接活。
- 只有车机上的入口会写 estop.json。在终端里执行 `mcp_serial` / `cart_mcp.py estop`（全部急停）只停串口，不写 estop.json。所以"全部急停也停大肥鱼"只对车机入口成立。
- 看门器：写完 estop.json 后每秒读一次状态文件。看到确认（v1 只看 `estop.seen == true`，v1.1 还要 `estop.seq` 等于这次写的 seq）就显示"大肥鱼已急停"。满 10 秒还没看到确认，或者状态文件离线，就硬停：
  1. 先执行 `loginctl terminate-user bigfish`，结束 bigfish 的会话。
  2. 再执行 `systemctl stop user@<uid>.service`，停掉整个用户管理器，rootless dockerd 和所有容器会一起停。
  - 两条命令都用列表参数调用，用户名和 uid 按 `pwd` 查，超时各 30 秒。成败只看 systemctl：bigfish 没有登录会话时，terminate-user 会失败，这不算硬停失败。
  - 记一条 cartlog（kind 为 `bigfish`，带两条命令的 rc 和原因）。成功后面板显示"已硬停，需人工恢复"，失败则显示"硬停失败: …"。
- 硬停之后，bigfish 下一次 ssh 登录会把 `user@` 拉起来。但只要 estop.json 还在，大肥鱼就会继续拒任务。
- `/run/bigfish-ctl` 不存在，或者 estop.json 写不进去时，直接硬停。没有在急停的时候，如果目录不存在，面板最上面一行常驻"急停通道未就绪"。
- 注意：解除急停时文件被删掉，所以下一次急停的 seq 会从 1 重新开始（见下面"权限核实"一节的最后一条）。

### 权限核实（按代码核实，还没上车实测）
- **大肥鱼能读到 estop.json**：
  - 目录 `/run/bigfish-ctl` 是 root:bigfish 0750，bigfish 通过属组拿到 r-x，可以进入目录、列目录。
  - `estop_write` 先用 0600 建临时文件，再显式 `fchown(fd, -1, <bigfish 组>)`、`fchmod(fd, 0o640)`，写入、fsync 之后才改名覆盖。因为权限是用 `fchmod` 显式设置的，kiosk 进程的 umask 不影响结果；改名之后，正式文件一出现就是 root:bigfish 0640，bigfish 组可读。
  - bigfish 组由大肥鱼的 `setup-root.sh` 用 `useradd -U` 建，是 bigfish 的主组，gid 按组名 `grp.getgrnam("bigfish")` 查。如果这个组不存在，文件会留成 root:root 0640，bigfish 读不到。按约定，大肥鱼读不到就按急停处理，所以结果是偏向安全的。
  - kiosk 写入和改名需要对目录有写权限：kiosk 以 root 运行（`kiosk.service` 没有设 `User=`），root 是目录属主，有写权限。
- **kiosk 能读到 bigfish.json**：
  - kiosk（`kiosk.service`）和 cart-dash（`cart-dash.service`）都以 root 运行，单元里没有 `ProtectHome`、`ProtectSystem`、`InaccessiblePaths`。root 有 CAP_DAC_READ_SEARCH，不受 `/home/bigfish`（通常 0700）、`/home/bigfish/status`（0711）和文件 0644 的限制，都能读。
  - 0711 和 0644 这两个权限是给其他非 root 读者留的，车机本身不依赖它们。
- **软链接**：`O_NOFOLLOW` 只管最后一级路径。如果 bigfish 把 `/home/bigfish/status` 换成指向别处的链接，还有 fstat 的属主核对把关：目标文件属主必须是 bigfish 才会读，所以拿 root 才能读的文件冒充是行不通的。
- 本仓库不建这两个目录（`/home/bigfish/status` 和 `/run/bigfish-ctl`），由大肥鱼那边的 `setup-root.sh` 负责。
- **seq 会重新从 1 开始**：解除急停时 estop.json 会被删掉，下一次急停的 seq 从 1 重新开始。大肥鱼那边不要假定 seq 在多次急停之间一直递增，判断"有没有急停"要看文件在不在。

### 在线 / 离线
wrapper 是常驻的 bigfish 用户服务（bigfish-supervisor，用户单元），正常情况下一直在线，状态文件每 5 秒心跳一次。所以面板上出现"离线"或"不在线"，说明大肥鱼服务挂了，不是空闲。

### 确认 / 拒绝
面板上的"确认 / 拒绝"按钮打开现有的确认框：先找有挂起项的第一路串口，没有就找键鼠。面板本身不做审批。现阶段大肥鱼只读，不会产生挂起项，所以这个按钮平时是灰的。

### 部署
推车仓库不用改 tmpfiles。`/home/bigfish/status` 和 `/run/bigfish-ctl` 两个目录都由大肥鱼那边的 `setup-root.sh` 建。在那边部署之前，车机面板会显示"大肥鱼不在线"和"急停通道未就绪"。这时按下全部急停会直接硬停 bigfish 的用户管理器；如果推车上还没有 bigfish 用户，只会记一条"找不到 bigfish 用户"的 cartlog。
