# cart-dash 接 Monita-CS80 副屏：设计稿

状态：设计稿，未实现。分支 `feat/monita-dash`。

目标：cart-dash 除了现有的 xfz1986 ESP32-P4 USB 副屏（下称 udisp），也能把状态板推到 Monita-CS80（收款机改造的 800×1280 屏 + 段码屏 + RGB 灯 + 4 个按键，下称 CS80）。只读展示，不加任何会改推车状态的操作。

协议依据：Monita 仓库 `docs/protocol.md`、`docs/segmap.md`（固件 v10，分支 `fw-v10-hostlost`）。下文说的“协议文档”都指它们。

## 1. 先说结论：版面不用重排

任务最初的设想是“CS80 分辨率和 udisp 不同，版面要重排”。查下来不成立：

| | udisp | CS80 |
|---|---|---|
| 设备报的尺寸 | 配置串 `R1280x800`（横屏帧头坐标） | HELLO_ACK `width=800 height=1280` |
| 面板物理朝向 | 竖屏，`dash_render` 按 800×1280 画完再 `rot90` | 竖屏原生，行跨度 1600 B，一帧 2,048,000 B |
| 像素格式 | JPEG（≤100 KB） | RGB565 小端（R 在高 5 位，`0xF800` 是红） |
| 局部刷新 | 不显示（2026-11-02 实测） | RECT 任意矩形，无对齐要求 |

`dash_render.render(view)` 出的竖屏 800×1280 图本来就是 CS80 要的坐标系，所以：

- **版面、字号、分隔线、桌宠坐标（`pet_core` 里写死的 W/CEILING/DIVIDER_Y/FLOOR_Y）一律不改**；
- CS80 出口**不转向、不编 JPEG**，直接把 RGB 图转成 RGB565，按脏区发 RECT；
- 深浅主题照旧（`th.use()` 在排版之前切配色，CS80 只是另一个出口，不感知主题）。

保险：后端在 HELLO_ACK 里校验 `proto_ver == 0`、`width == 800`、`height == 1280`、`pixfmt == 1`，不符就当“打不开”（打日志，3 s 后重试），不去硬缩放。

屏在车上怎么装（竖装还是横装）还没定，见第 11 节。若横装，再在出口加一次转向即可，版面仍不动。

## 2. 后端抽象：并存的最小改法

现状（`dash_service._loop`）：`factory().open_display()` 返回带 `send(bytes)`、`close()`、可选 `cfg_text` 的对象；`paint(view)` 返回**已转向的 JPEG**（`make_frame` 和 `pet_render.Stage.frame` 都把转向和编码焊在里面）。

改法：显示对象**自愿声明**要图而不是要字节，旧路径一个字节都不变。

```text
udisp_usb.Display          send(jpeg_bytes) / close() / cfg_text          # 不动
monita_cs80.Display        wants_image = True
                           send(img)        # img: 竖屏 800×1280 PIL RGB 图
                           tick(now, snap, view) -> [按键事件]   # 每轮采集都调
                           close()
                           cfg_text
```

`dash_service` 的改动：

1. `paint(v)` 拆成两步：先出图，再按后端要什么交出去。
   - 有 `wants_image`：没猫用 `dash_render.render(v)`；有猫用 `Stage.image(v, pet)`（新方法，返回贴好猫的 RGB 图）。
   - 没有 `wants_image`：照旧 `make_frame(v)` / `Stage.frame(v, pet)`。`Stage.frame` 改成 `encode_within_budget(rot90(self.image(...)))`，输出逐字节不变。
   - 注入了 `render=`（测试）时照旧直接用它。`test_make_frame_is_landscape_for_the_panel` 和降质那条测试保持原样通过。
2. **每轮采集**（0.2 s）只要 `disp` 开着且有 `tick` 就调一次 `disp.tick(now, snap, view)`，**不受 `want` / `max_gap` 控制**。保活、段码屏、灯、读按键都在 `tick` 里做。`tick` 抛异常和 `send` 抛异常走同一条“关掉、3 s 后重连”的路。
3. 重连要按时间驱动（见第 8 节的问题 1）。

不起线程：CS80 后端所有动作都在采集线程里、带超时（写 ≤ 1 s，等 ACK ≤ 0.5 s），保活靠 `tick` 每 0.2 s 一次的机会。后端自愈：距上次发送 ≥ 3.5 s 就先 HELLO、重发灯、推整帧。

## 3. 协议编码器：ops-cart 内独立实现

新模块（MIT，按协议文档从零写，**不复制 Monita 仓库任何代码**，包括 `tools/host-ref` 和 `tools/fwsim`）：

- `cs80_proto.py`：纯函数，无 I/O。帧编码、流式解析、HELLO_ACK / ACK / KEY 解码、段码屏编码、RGB565 转换、脏区计算。
- `cs80_link.py`：字节流传输 + 会话（HELLO、按 type+seq 等应答、KEY 分流、超时）。
- `monita_cs80.py`：`open_display()` 和 `Display`，接到 dash_service。

帧格式（全小端）：`A5 5A | u8 type | u8 seq | u16 len | payload | u32 crc`，CRC 用 `zlib.crc32` 算 `type..payload`（不含 `A5 5A`）。坏帧设备静默丢弃、不应答。

只实现用到的消息：

| type | 名字 | 方向 | 用途 | 应答 |
|---|---|---|---|---|
| 0x01 | HELLO | → | 握手；带 `u32 unix_time + i16 tz_min` | HELLO_ACK 0x81（16 B） |
| 0x10 | SEG | → | 72 字节段码屏显存 | ACK |
| 0x20 | RECT | → | `u16 x,y,w,h` + RGB565 | **无** |
| 0x21 | PRESENT | → | `u16 frame_id`，一批 RECT 的同步点 | ACK（40 B） |
| 0x30 | BACKLIGHT | → | 背光开 / 关 | ACK |
| 0x31 | LED | → | 灯三态 | ACK |
| 0x32 | SLEEP | → | v1 不发（第 7 节） | ACK |
| 0x7F | PING | → | 保活 | ACK |
| 0x90 | KEY | ← | 设备主动上报，`seq=0` | 主机不应答 |
| 0xFE | ACK | ← | `for_type, status`，再 9 个 u32 计数 | — |

不实现：COPY（只对滚动有用）、FBCRC、DIAG、沉浸模式 IMM_*。

要点：

- 等应答时按 type 分流：KEY 进按键队列，其余按 type+seq 匹配；对不上的丢掉。
- RECT 载荷必须正好 `8 + w*h*2` 且 ≤ HELLO_ACK 的 `max_payload`（不写死 32768；测试里用 16384 验证）。
- `caps` 按位开功能：bit4 KEYS、bit5 LED、bit7 SLEEP / 主机走了。没有的位就不发对应消息（fw v8 之前的固件只当屏用）。
- PRESENT 的 ACK 里 `crc_err + len_err + bad_param` 比上一次大，说明之前的 RECT 有被拒的，下一帧推整帧。

## 4. 推送策略：脏区 + PRESENT

后端缓存上一次**已提交**的 RGB 图（800×1280）。每次 `send(img)`：

1. 和上一帧做差（`ImageChops.difference`），按 16 行一条带取 `getbbox()`；有变化的条带，把横向范围对齐到 32 像素，成为一个 RECT（等价于 32×16 tile 差分后同一行合并，只是不拆开中间的干净 tile，简单）。
2. 单个 RECT 超过 `max_payload` 就横向切开（每块宽 ≤ `(max_payload - 8) // (16*2)`，向下对齐到 32）。800 宽、16 行是 25,608 B，在 32768 下一条带一个 RECT；整帧 80 个 RECT。
3. 转 RGB565 用 Pillow 的通道查表：`split()` → `point()` 做高 / 低字节 → `ImageChops.add` → `Image.merge("LA", (lo, hi)).tobytes()`，不用逐像素 Python 循环，也不依赖各版本不一致的 16 位 raw 模式。
4. 发完这批 RECT，发 PRESENT，等 ACK 才返回（这是唯一的等待，也是流控）。
5. 下列情况推整帧：刚 HELLO、上次 ACK 报错数增加、上次 `send` 失败。

Mac 上实测（Pillow 11.1，800×1280）：整帧转 RGB565 约 8 ms，差分 + 找条带约 5 ms。推车 CPU 更慢，按数倍估算仍在几十毫秒内；实现时在推车上量一次再定猫的帧率。

线上开销（协议文档实测）：整帧约 2 MB，320–390 ms；小块差分 5–52 ms。所以：

- 状态板：仍按现有 `want` 逻辑（变了才推，上限 `--fps` 默认 2）。
- 桌宠：猫只占一小块，差分后每帧几 KB 到几十 KB，8 fps 可行。若实测 PRESENT 等待加起来超过采集间隙，CS80 下把猫的动帧率降到 4 fps（后端暴露 `max_pet_fps`，YAGNI 前不做）。
- 推送路径里出现整帧（主题切换、锁屏 / 解锁）时会占用一轮采集约 0.4 s，可以接受（采集写 state.json 晚一轮）。

## 5. 保活：满足 5 s 主机走了看门狗

固件 v10：HELLO 后 5 s 没有 CRC 正确的帧就回开机版权页、灯回蓝灯心跳、停发 KEY；另外 SEG 也有一个独立的 5 s：超时后段码屏交回固件。

`tick()` 的节奏：

- **SEG 每 1 s 一次**（内容就是时钟，见第 6 节），同时喂两个 5 s 计时器。
- 兜底：距上次发送 ≥ 2 s 还没发过任何帧（比如 SEG 那一轮被跳过）就发 PING。
- 距上次发送 ≥ 3.5 s（采集线程被整帧或别的事拖住、刚重连）：先 HELLO（带当前时间和时区）、重发灯、下一次 `send` 推整帧。若这时 dash_service 不打算推帧（版面没变），后端用缓存的上一帧自己推整帧，保证不停在开机页。
- fw v8/v9（`caps` 没有 bit7）：主机走了的判定是 15 s 没字节，上面的节奏照样满足。

## 6. 段码屏、灯、按键

段码屏和灯的输入是纯函数 `seg_state(snap, view, now)`、`led_state(snap, view)`，放在 `cs80_proto.py` 旁边单测。**不往 `layout_view` 加字段**（加了会改 `_view_key`，可能多推帧）。

### 段码屏（主机接管时整块屏都由主机画，包括时钟）

协议文档写明：主机发 SEG 后 5 s 内段码屏完全由主机控制，固件的小时钟只在固件接回来时才画。所以时钟要主机自己画。

| 区域 | 显示 | 没数据时 |
|---|---|---|
| 主区 6 位 + COLON2 / COLON4 | 本机时钟 `HH:MM:SS`，冒号常亮 | — |
| 小时钟 88:88:88（第 8–13 列） | UPS 电量百分比（右对齐，最多 3 位），不点冒号；走电池时改显示剩余分钟 | 全灭 |
| `元88`（第 14–15 列） | 自检 FAIL + WARN 数（1–99，超过显示 99），0 时灭 | 灭 |
| 电池外框 + 4 格 + 闪电 | 外框：有 UPS；格数：`ceil(percent / 25)`；闪电：不在电池上（`ups_core.on_battery` 为假） | 全灭 |
| WiFi 4 级 + × | `net`：on 4 级；warn 2 级 + ×；off / down 0 级 + × | — |
| 信号 4 格、8G、喇叭、小数点 | 不用 | 灭 |

开机自检页（`view.boot`）：只显示时钟，其他全灭。`view.stale`：主区照常走时钟（它由本机时间算），小时钟区显示 `------`。

7 段编码按协议文档的段位表：第 k 列用 `fb[k]`（bit7 f、bit3 g）、`fb[18+k]`（bit7 a、bit3 b）、`fb[36+k]`（bit7 e、bit3 c）、`fb[54+k]`（bit7 d）；数字掩码 `3f 06 5b 4f 66 6d 7d 07 7f 6f`，`-` 为 `40`。图标位按 `segmap.md`。只用数字、`-`、空白和两个冒号；小数点 DP4 / DP5 文档里位置描述有出入，v1 不用。

### 灯（`caps` bit5）

| 状态 | 条件（按优先级） | 灯 |
|---|---|---|
| 自检中 | `view.boot` 或 `view.stale` | 蓝闪（1 s 周期） |
| 告警 | 自检有 FAIL；`ups_alert.level == "bad"`；`serve.level == "bad"`；急停 | 红闪（0.5 s 周期） |
| 注意 | 自检有 WARN；`ups_alert.level == "warn"`；`serve.level == "warn"`；有待确认 | 黄常亮（红 + 绿） |
| 正常 | 其他 | 绿常亮 |

只在状态变化时和每次 HELLO 之后发 LED（HELLO 会把灯灭掉）。

### 按键（`caps` bit4）

全部只读，只改后端自己的显示状态，**不碰推车任何状态，不做任何确认类操作**。

| 键 | 事件 | 动作 |
|---|---|---|
| ≡（键 2） | UP | 翻页：主页 ↔ 自检明细页（第二阶段才做，见第 10 节） |
| +（键 3） | UP | 背光开 |
| −（键 4） | UP | 背光关 |
| 查询（键 1，PWRKEY） | — | 不用：真机长按 4 s 固件会自己补 UP，容易误触 |

- 背光只有开 / 关（BACKLIGHT 0x30 是 GPIO），没有调光；这里叫“背光开关”，不叫亮度。深浅仍由主题设置决定。背光关着时保活照常。
- **锁屏时不翻页**（明细页信息比锁屏页多）；明细页只从已经脱敏的 `view` 取数据（`checks.problems`、`temps`、`fans`），60 s 无按键自动回主页。
- KEY 的 `n` 断号只打日志，不补。

## 7. 热插拔、SLEEP

**发现设备。** Linux 上 CS80 是 Quectel 模组口 `2c7c:0901`，协议走接口 7（厂商类 0xFF，bulk IN 0x8A / OUT 0x07）。v1 选**串口节点**这一条路：扫 `/sys/class/tty/ttyUSB*` 和 `ttyACM*`，按 `device/../idVendor`、`idProduct` 和接口号 `bInterfaceNumber == 07` 找节点，用标准库 `os.open` + `termios` 设 raw（不设波特率、不要 DTR）。也接受 `--cs80-port PATH` 直接指定（fwsim 开的是 PTY，联调必须走这条）。不加 pyserial。

推车上没验证的：Rocky 9 是 `option` 驱动绑成 `ttyUSB*` 还是别的；ModemManager 会不会去探这个口（会发 AT 命令，打乱协议帧）。上车时要补一条 udev 规则（`ENV{ID_MM_DEVICE_IGNORE}="1"`，可选 `SYMLINK+="monita-cs80"`），仓库里目前没有 udev 目录，放哪里见第 11 节。备选是 pyusb 直接 claim 接口 7（venv 里已有 pyusb），v1 不做。

**拔插。** 打开失败、写失败、等 ACK 超时都当“没插”：关掉，3 s 后按 VID/PID 重新找节点（拔插后编号会变）。重连后 HELLO → 灯 → 整帧。

**SLEEP。** `dash_service` 没有信号处理，SIGTERM 直接结束进程；SLEEP 也不能放进 `close()`（出错重连时也会走 `close()`，下一条帧又会把设备叫醒并闪一下 `888888`）。所以 v1 **不发 SLEEP**：cart-dash 停了，设备 5 s 后自己回开机页，10 min 后自己睡。空闲时要不要主动关屏，快照里目前没有可靠的“该关屏”信号，列为待定。

## 8. 现有代码里要先修的一处

**问题 1：没猫时拔屏，重连要等到下一分钟。** `_connect` 放在 `if want and ...` 里面；打开失败和非猫路径的 `send` 失败都不置 `retry`。版面不变时 `want` 只在跳分钟时为真，所以实际重连间隔是“下一次版面变化或跳分钟”，不是 3 s。udisp 上影响小（最多晚 1 分钟），CS80 上会让开机页多停一会。

实现时先写一条假时钟红测试复现，再改成：`disp is None and now >= next_try` 时就尝试连，连上后把 `want` 置真推一帧。udisp 的推帧内容和日志措辞不变，只是重连更及时。

## 9. 配置

- `dash_service.py --display udisp|cs80|auto`，**默认 `udisp`**，推车现有行为不变，`cart-dash.service` 不用改。
  - `cs80`：只用 CS80。
  - `auto`：先找 CS80，找不到再找 udisp；每次重连重新选。
- `--cs80-port PATH`：指定串口节点（fwsim 的 PTY、或 udev 符号链接）。
- 新模块进下一版的 install / pack 清单（`scripts/install-*.sh`、`pack-*.sh`）。
- 不做两块屏同时出。

## 10. 实现顺序和测试计划

TDD，假设备，不打真机。每一步 3.13 全绿，`tests/test_py39_compat.py` 扫过（新文件先 `git add`；不用 `match`、`X | Y` 注解、`int.bit_count`、`zip(strict=)`）。

1. `cs80_proto.py` + `tests/test_cs80_proto.py`：帧编码 / CRC / 解析（拆包、粘包、坏 CRC 丢弃）、HELLO_ACK / ACK / KEY 解码、RGB565（`(255,0,0)` → `00 f8`，`(0,255,0)` → `e0 07`）、脏区（无变化 0 个、单点 1 个、`max_payload=16384` 切块、载荷长度恰好 `8+w*h*2`）、段码屏编码（数字、`-`、冒号、电池格、WiFi）、`seg_state` / `led_state` 各条件。
2. `cs80_link.py` + 假串口（内存字节流、能注入 KEY 和应答、能模拟超时）：HELLO 握手、尺寸 / 格式不符拒绝、等 ACK 时 KEY 分流、超时抛错。
3. `monita_cs80.Display` + 假时钟：`tick` 节奏（SEG 1 s、≥3.5 s 重新 HELLO + 灯 + 整帧）、LED 只在变化和 HELLO 后发、ACK 错数增加后整帧、`caps` 缺位时不发 KEY / LED。
4. `dash_service`：先修第 8 节（红测试）；`wants_image` 分支、`tick` 每轮调用且异常走重连、`--display`；`Stage.image` 加测试，`Stage.frame` 原测试不动。
5. 按键：背光开关；第二阶段做明细页（`dash_render` 加一个只吃 `view` 的页面函数 + 预览测试）。
6. 预览：`--preview out.png --display cs80` 把要推的竖屏图存成 PNG；再加一个小脚本把 72 字节段码屏状态画成文字（`88:88:88` 样式）方便看。
7. fwsim 联调（**可选、不进仓库**）：环境变量 `CS80_FWSIM` 指向本机自行编译的 fwsim 时才跑，否则 skip。用例：HELLO caps、整帧 80 RECT + PRESENT 后 `bad_param == 0`、差分帧、SEG / LED 内容和 `stat` 对上、`tap 3` 收到 KEY、停发 5.6 s 后 `boot_screen == 1` 而后端能自愈、关掉再开 PTY 后重连、`--max-payload 16384`。fwsim 是 PolyForm Noncommercial，不 vendor、不在 CI 跑。

## 11. 明确不做

- 版面重排、缩放、为 CS80 另画一套版面。
- COPY、FBCRC、DIAG、沉浸模式。
- 任何确认类按键操作（解除急停、批准、发地址、关机……），包括长按。
- 查询键（PWRKEY）。
- 两块屏同时出。
- SLEEP（第 7 节）。
- 调光（硬件没有）。
- 引入 pyserial 或其他新依赖。
- 复制 Monita 仓库代码；把 fwsim 放进仓库或 CI。
- 部署到推车、改 `cart-dash.service`、加 udev 规则文件（等上车时一起定）。

## 12. 待维护者决定

1. **许可证。** ops-cart 是 MIT，Monita 是 PolyForm Noncommercial。本稿选择在 ops-cart 里按协议文档独立实现最小编码器（第 3 节），不依赖 Monita 代码。是否把两边的协议参考实现统一到同一许可证（比如 Monita 的协议文档和 host-ref 改成 MIT，或单独抽一个 MIT 的协议包做可选依赖），待定。
2. **默认用哪块屏。** 本稿默认 `udisp`，CS80 要显式 `--display cs80` 或 `auto`。上车后是否改默认值。
3. **屏怎么装。** 竖装则版面直接用；横装要在 CS80 出口加转向，并重新确认桌宠坐标。
4. **上车时机。** 固件 v8–v10 的 KEY / LED / SLEEP / 主机走了在协议文档里标着“未上真机测”。建议先在 Mac 上过 fwsim，再在真机过一遍，再上车。
5. **udev 规则放哪。** 仓库里还没有 `deploy/udev/`；CS80 需要屏蔽 ModemManager 的规则，要不要连同 udisp、UPS、串口头的规则一起收进仓库。
6. **空闲关屏 / SLEEP。** 要不要在某种条件下（夜间、长时间锁屏）主动关背光或 SLEEP；需要先有一个可靠的信号。
7. **正常时灯亮不亮。** 本稿正常时绿常亮；若嫌晃眼，可改成正常时灭灯、只在注意 / 告警时亮。
