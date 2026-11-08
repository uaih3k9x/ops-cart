# 大肥鱼面板：接口约定（#53）

车机（root）和大肥鱼（普通用户 `bigfish`，rootless Docker）之间只通过两个文件交换信息。车机不进大肥鱼的容器，不读 dsh 的数据卷，也不调用大肥鱼的任何接口。

## 1. 状态文件（大肥鱼写，车机和副屏只读）

- 路径：`/run/bigfish/status.json`。目录由 tmpfiles 建：`d /run/bigfish 0755 bigfish bigfish -`，推车重启后清空。
- 写入方：大肥鱼一侧在宿主上以 bigfish 身份运行的进程（建议由 mcp-wrapper 写，任务表在它手里）。
- 文件属主必须是 bigfish，权限 0644，必须是普通文件。写法：同目录临时文件，写完后 `rename` 覆盖，不要原地改写。
- 车机读取方式：`O_NOFOLLOW | O_NONBLOCK` 打开，`fstat` 检查是普通文件、属主是 bigfish，最多读 64 KiB。不满足任何一条都按"不在线"处理。
- 刷新：至少每 10 秒重写一次 `updated`，空闲时也要写。车机发现 `updated` 落后超过 30 秒，或者比当前时间快 5 秒以上，就显示"状态过期"。

```json
{
  "v": 1,
  "updated": 1762600000.0,
  "state": "running",
  "task": {
    "id": "t-1a2b",
    "state": "running",
    "started": 1762599875.0,
    "title": "看一下 con-1 上是什么设备",
    "step": "cart_serial_tail",
    "focus": "con-1",
    "output": ["最近输出的几行", "最多 5 行"]
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | 整数，固定为 `1` | 不是 1 就按"状态读不懂"处理 |
| `updated` | 数字（epoch 秒） | 写入时刻 |
| `state` | `idle` / `running` / `estopped` | 包装层自己的状态。`estopped` 表示看到了急停（见第 2 节） |
| `task` | 对象或 `null` | 当前或最近一个任务。空闲时可以是 `null` |
| `task.id` | 字符串 | 车机不显示，留给排障 |
| `task.state` | `starting` / `running` / `completed` / `cancelled` / `failed` | 同 `bigfish_get_task` 的 state |
| `task.started` | 数字（epoch 秒） | 用来显示"已经做了多久" |
| `task.title` | 字符串，**不可信** | 派活提示词的第一行 |
| `task.step` | 字符串，**不可信** | 当前工具调用的 title |
| `task.focus` | `con-1`…`con-4` / `kvm`，可省略 | 只认这几个值，其他值车机直接丢弃。由包装层根据工具名和参数推出来，不要直接取模型输出 |
| `task.output` | 字符串数组，**不可信** | 最近输出，车机只取最后 5 行 |

其他字段车机一律忽略，所以不要往里放 key、令牌、prompt 全文或工具参数原文。

车机对不可信字段的处理：删除 ANSI 转义和控制、格式字符（含双向覆盖、零宽字符），所有空白压成一个空格，只显示一行，每行最多 200 字。面板上每一行都加"大肥鱼说："前缀，用单独的颜色和等宽字显示，链接不渲染，内容不执行。锁屏和副屏上只显示由 `state` 和 `focus` 枚举出来的一行，例如"大肥鱼 · 空闲""大肥鱼 · 在看 con-1"，不显示原文。

文件不存在显示"大肥鱼不在线"，内容坏了显示"状态读不懂"，太旧显示"状态过期"。

## 2. 急停信号（推车写，大肥鱼读）

不新增信号文件，直接复用推车现有的公开急停标记：

- 路径：`/run/ops-cart/serial-estop.json`，root 写，0644，所在目录 `/run/ops-cart` 为 root 0755，bigfish 可读。
- 内容示例：`{"slots": [1, 2, 3, 4], "all": true, "kvm": true}`。`mcp_serial.Store` 每次操作后都会原子重写这个文件。
- **`all` 为 `true` 就是急停**。车机执行器条上的"Agent 全部急停"、确认框和大肥鱼面板上的"全部急停"、网页急停、命令行 `cart_mcp.py estop`，以及 Agent 自己调用急停，全部汇到这一个文件。"解除全部急停"只能在车机上做，解除后 `all` 变回 `false`。只停某一路时 `all` 仍为 `false`，大肥鱼不用管。

大肥鱼一侧（wrapper）需要做到：

1. 至少每秒检查一次这个文件（可以先 `stat` 看 mtime，变了再读）。
2. 读到 `all: true` 时：对所有 `starting` / `running` 的任务发 ACP `session/cancel`，状态文件的 `state` 改成 `estopped`，并且立即重写一次状态文件，不等 10 秒的周期。
3. 急停期间 `bigfish_submit_task` 直接返回错误（例如"推车急停中"），不新建会话。
4. 文件不存在时按没有急停处理（推车刚开机，还没人动过串口状态）。文件存在但读不懂（不是 JSON、`all` 不是布尔值）时按急停处理。
5. 读到 `all: false` 后恢复接活。已经取消的任务不会自动重跑。

权限（按仓库核实过）：
- `deploy/tmpfiles.d/ops-cart.conf` 里写的是 `d /run/ops-cart 0755 root root -`。
- 写 `serial-estop.json` 的是 `mcp_serial.Store._flag`，调用 `atomicfile.write_text(path, ..., mode=0o644)`。atomicfile 先用这个 mode 建临时文件，再对临时文件显式 `os.chmod(tmp, 0o644)`，然后改名。所以即使写入进程的 umask 是 0077（cart-mcp、cart-mcp-ro、cart-serial-req 三个单元都设了 `UMask=0077`），最终文件也是 0644。
- 所以 bigfish 在宿主上可以读到这个文件。rootless 容器里看不到宿主的 /run，因此读它的必须是宿主侧以 bigfish 身份运行的 wrapper。

## 3. 硬急停兜底（车机这边做）

大肥鱼没按约定停手时（进程卡死、wrapper 没起来、状态文件没人写），车机直接停掉 bigfish 的整个用户管理器。

- 计时：车机每秒读一次 `serial-estop.json`。`all` 从 false 变成 true 那一刻开始计时，急停期间不会重新计时，也不会反复去停。
- 条件：计时满 10 秒，状态文件的 `state` 还不是 `estopped`，或者状态文件不在线、读不懂、过期。
- 动作：
  - kiosk 以 root 在后台执行 `systemctl stop user@<uid>.service`，超时 30 秒。uid 用 `pwd.getpwnam("bigfish")` 按用户名查，不写死。这样 bigfish 的 rootless dockerd 和所有容器会一起停掉。
  - 记一条 cartlog（`events.log`，kind 为 `bigfish`，带上 rc 和原因）。
  - 面板第一行显示"大肥鱼已硬停，需人工恢复"；systemctl 失败时显示"大肥鱼硬停失败: …"。
- bigfish 用户不存在时：什么都不做，不执行 systemctl，也不记 cartlog，只在面板上提示"找不到 bigfish 用户，没有硬停"。
- 10 秒内大肥鱼报了 `estopped`：面板显示"大肥鱼已急停"，不硬停。
- 10 秒内急停被解除：取消计时。
- 恢复不归车机管。硬停之后，"已硬停"的提示一直留着，直到急停解除、并且大肥鱼的状态文件重新在线（说明有人把它恢复了）。
- kiosk 刚启动时，把上一次看到的 `all` 当作 false。所以如果 kiosk 重启时推车已经在急停中，第一次读到 `all` 为 true 也会开始计时，并且最多只停一次。

## 4. 确认 / 拒绝

大肥鱼面板上的"确认 / 拒绝"按钮会打开现有的确认框：先找有挂起项的第一路串口，没有就找键鼠。面板本身不做审批，也不写任何文件。现阶段大肥鱼只读，不会产生挂起项，所以这个按钮平时是灰的。
