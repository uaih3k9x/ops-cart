# 蓝牙隔空打字（BLE 输入，#52）

车机屏幕是 Tk + X，没有中文输入。手机经蓝牙 BLE 把**文字**发给推车，推车放进本地队列，由车机或大肥鱼去取。
不依赖现场网络。

蓝牙通道**只是打字**：推车只重组、校验、排队，不执行任何东西。危险操作的确认规则不变。

**取件方契约**（写死，接取件方时照这个做）：文本只进输入框，\n 不得触发提交；送往 HID 或串口必须再走现有危险操作确认。
投递 JSON 里的 `multiline` 就是提醒这件事的：为 `true` 时取件方要把换行当普通字符插进输入框，不能当回车键。

协议版本 v2（见文末"v1 → v2"）。

## 组成

| 文件 | 作用 |
|---|---|
| `ble_input_core.py` | 纯逻辑：分包编解码、重组、校验、限长、去重、受信设备判断、审计行 |
| `ble_input.py` | 薄外壳：BlueZ D-Bus 上的 GATT 服务和 LE 广播；投递目标；回复取件 |
| `deploy/systemd/cart-ble-input.service` | 单元草稿，默认不开 |
| `docs/ble_input_vectors.json` | Python 和安卓两边共用的测试向量 |
| `android/` | 手机 App（另一个 PR） |

依赖：`bluez`、`python3-dbus`、`python3-gobject`（缺了 `ble_input.py` 退出码 2，说明要装什么）。

## GATT 服务

| 项 | UUID | 属性 |
|---|---|---|
| 服务 | `c7fa50cf-d4f6-4e23-a71d-7b725c250660` | primary，同时放进 LE 广播，本地名 `ops-cart` |
| RX（手机写） | `d0b9f7e0-8914-4d78-9501-69062f225d38` | `secure-write`（带回应的写） |
| TX（推车通知） | `a1e1ae6b-abf5-4e3d-9e69-19bafa051e86` | `notify`、`secure-notify` |

`secure-*` 要求 **LE Secure Connections** 加密（BlueZ 在 ATT 层就拒掉老式配对和没配对的连接）。另外建议在
`/etc/bluetooth/main.conf` 的 `[General]` 里写 `SecureConnections = only`，适配器层面就不接受老式配对。

外壳另外查 `org.bluez.Device1` 属性（`device_trusted`）：要 `Paired` 和 `Trusted`，不能 `Blocked`；BlueZ 有
`Bonded` 属性时还要 `Bonded`。不受信的设备写进来回 `unpaired`，不留半包。

D-Bus 方法（`WriteValue` / `StartNotify` / `StopNotify`）只认 `org.bluez` 当前属主调用，别的调用者抛
`org.bluez.Error.NotPermitted`。启动时读适配器 `Pairable`，开着就关掉并记事件（关不掉记 WARN）。
`org.bluez` 属主变了（bluetoothd 重启）进程退出，交给 systemd 重拉；单元 `BindsTo=bluetooth.service`。

## 分包

单帧最多 `min(MTU - 3, 512)` 字节（ATT 属性值上限 512，MTU 517 时 `MTU - 3` 是 514，超了）。手机连上后请求大 MTU
（App 请求 517）按协商结果分包写。推车的通知是广播、分不了设备，**一律按 MTU 23 切**（每帧 20 字节），哪台手机都收得下，
也不受哪台设备报的 MTU 影响。

所有整数大端。

首帧（START）：

| 字节 | 含义 |
|---|---|
| 0 | `0x01` |
| 1–2 | 消息序号（0–65535，发送方自己递增，回绕；手机序号跨重启持久化，首次随机起点） |
| 3–4 | 消息总长（字节，1–4096） |
| 5–8 | 整条消息的 CRC32（zlib / IEEE 802.3） |
| 9… | 正文开头 |

后续帧（CONT）：

| 字节 | 含义 |
|---|---|
| 0 | `0x02` |
| 1–2 | 消息序号 |
| 3–4 | 这一片在消息里的偏移 |
| 5… | 正文片 |

接收规则：

- 总长 0 或超过 4096：`len`；分片越过总长：`len`
- 先到 CONT、没有 START：`nostart`
- CONT 之间顺序随意，按偏移拼；完全一样的重复分片忽略；和已有分片交叠且内容不同：`overlap`，整条丢掉
- 收齐后算 CRC32，不对：`crc`
- 正文必须是 UTF-8（`utf8`）；检查在**重组之后**做（看不见的字符被切到两帧也拦得住），以下一律 `ctrl`：
  控制字符 Cc（换行、制表除外，含 ESC 序列）、格式字符 Cf（只放行 U+200D ZWJ，含双向覆盖符、零宽空格、BOM、软连字符）、
  标签字符 U+E0000–E007F 整段、行 / 段分隔符 Zl / Zp（含 U+2028 / U+2029）、未分配 Cn、私用区 Co、代理 Cs、
  看不见的韩文填充字（U+115F、U+1160、U+3164、U+FFA0）
- 半包 10 秒没新分片：`timeout`；每台设备同时最多 2 个半包，再来新的就丢最老的那个：`busy`
- 同一序号重新发 START：按新的来（手机重发）
- 已投递的（来源设备，序号，总长，CRC）10 分钟内再来：回 `ack` 加 `dup`，不重复投递；同序号不同内容当新消息；
  去重表按来源设备分开，每台最多记 64 条
- 队列满：`full`，手机可以稍后用同一序号重发

## 通知（TX）

推车回手机的也用同一种分包（推车自己的序号，启动时随机起点），内容是紧凑 JSON：

```
{"t":"ack","id":5,"crc":3735928559}
{"t":"ack","id":5,"crc":3735928559,"dup":true}
{"t":"err","id":5,"crc":3735928559,"code":"crc"}   id / crc 读不出来时是 null
{"t":"reply","text":"大肥鱼回复的摘要"}              正文最多 512 字节，超了在字符边界截断加 …
```

**回执归属**：`crc` 是被确认消息 START 里声明的 CRC32。通知是广播，同时连着两台手机时都会收到；手机只认
`(id, crc)` 和自己发件箱对得上的回执。`crc` 为 null 的错误（例如 `nostart`、`frame`）手机只能按序号猜：最多安排一次重发
（推车会去重），不从发件箱删。

BlueZ 的通知发给所有订阅者，分不了设备：**只有连着的设备全是受信设备时**才发 `reply`，否则丢弃并写审计。
回执和错误只有序号、CRC 和错误码，照常发。

**推车重启**：推车的通知序号从随机起点重来；手机每次订阅 TX 成功（CCCD 写成功）都新建回执重组和去重状态，
不会把新推车的回执当成旧的重复丢掉。

**手机发件箱**：没收到回执的留着，重连后同序号重发；超过 10 分钟（推车去重时效）还没送达的丢掉并提示用户
（再发可能重复投递，让用户自己决定）；`unpaired` 留在发件箱，等配对和 trust 后重发；同一条的定时重发同时只安排一次。

## 投递和回复

默认投递目标是目录 `/run/ops-cart/ble-input/in/`（root，目录 0700，文件 0600），一条一个 JSON：

```json
{"v": 1, "t": "2026-01-01T12:00:00", "src": "02:00:00:00:00:01", "id": 5, "text": "你好", "multiline": false}
```

字段就这 6 个（`tests/test_ble_input.py` 锁着）；`multiline` 为 `true` 表示正文里有换行，见上面的取件方契约。

文件名 `<毫秒时间戳>-<设备>-<序号>.json`，按名字排序就是到达顺序。取件方读完自己删。压了 64 条没人取就回 `full`。
投递目标可插拔：任何有 `put(text, src, msg_id) -> bool` 的对象都行。

回复：往 `/run/ops-cart/ble-input/reply/` 放 `*.txt`（UTF-8，先写临时名再改名），推车每秒取最老的一条通知手机，取完删。

谁来取队列（车机当前输入框 / 大肥鱼）还没接，见 PR 的待拍板。

## 审计

每条消息一行进 `/var/log/ops-cart/events.log`，类别 `ble-input`，只有来源设备、序号、长度、结果：

```
2026-01-01 12:00:00 [ble-input] dev=02:00:00:00:00:01 id=5 len=6 kind=text result=queued
```

不写正文。

## 配对（车机上确认）

推车平时**不可配对**。配一台新手机：

```
sudo bluetoothctl
[bluetooth]# agent DisplayYesNo
[bluetooth]# default-agent
[bluetooth]# pairable on
[bluetooth]# discoverable on          # 广播本身由 ble_input 发，这条只为首次配对
```

手机 App 连上推车后第一次写入会触发配对；手机和车机终端上显示同一个六位数，两边都确认（数字比较，防中间人）。
然后在车机上：

```
[bluetooth]# trust <手机地址>
[bluetooth]# pairable off
[bluetooth]# discoverable off
```

没 `trust` 的设备即使配了对也不收。撤销：`remove <手机地址>`。

配完对**核对一遍**：

```
[bluetooth]# devices Paired
```

只应有自己认识的手机；不认识的立刻 `remove <地址>`。`pairable off` 忘了关也没事，`ble_input` 下次启动会关掉并记事件。

手机用随机私有地址时，配对后 BlueZ 按身份地址（IRK 解析后）记设备，审计里写的是这个地址。

## 单元

`cart-ble-input.service`：root（BlueZ 的 D-Bus 策略只让 root 注册 GATT 服务），没有能力、没有网络（只用 D-Bus 的
Unix 套接字）、文件系统只读，只能写自己的运行目录和 `/var/log/ops-cart`。重启单元不清队列。默认不开：

```
sudo systemctl enable --now cart-ble-input.service
```

## 测试向量

`docs/ble_input_vectors.json`，`tests/test_ble_input_core.py` 和安卓单元测试读的是同一个文件。

`vectors`：消息序号、MTU、正文 → 帧（十六进制）；接收方喂这些帧应该还原出正文。

| 名字 | 序号 | MTU | 正文 | 帧数 |
|---|---|---|---|---|
| ascii-one-frame | 1 | 23 | `hi` | 1 |
| chinese-multi-frame | 0x0102 | 23 | `你好，推车` | 2 |
| emoji-zwj | 0xBEEF | 23 | `👨‍👩‍👧✅` | 2 |
| newline-tab | 7 | 23 | `第一行\n\t第二行` | 2 |
| long-mtu185 | 0xFFFF | 185 | `装机完成。` × 30 | 3 |

例：`hi`，序号 1，MTU 23 → 一帧 `01 0001 0002 d8932aac 6869`。

`rejects`：喂这些帧，最后一个事件应该是对应的错误码：`crc`、`len`、`ctrl`、`overlap`、`nostart`、`frame`。

`capped-mtu517`：MTU 517 时首帧正好 512 字节。

`text_rejects` / `text_accepts`：正文（UTF-8 十六进制）应该被拒（`ctrl`）/ 放行。

`status`：事件 → 回执 JSON 的确切字节（手机解析用）。

## v1 → v2

| 项 | v1 | v2 |
|---|---|---|
| 单帧上限 | `MTU - 3` | `min(MTU - 3, 512)` |
| 去重键 | （来源，序号） | （来源，序号，总长，CRC），每台设备单独 64 条 |
| 回执 | `id` | `id` + `crc`（错误也带，读不出是 null） |
| 通知分包 | 跟最后一次写的 MTU | 固定 23 |
| 正文过滤 | Cc、双向覆盖符 | Cc、Cf（放行 ZWJ）、标签字符、Zl/Zp、Cn、Co、Cs、填充字 |
| 投递 JSON | 5 个字段 | 加 `multiline` |
| GATT 标志 | `encrypt-authenticated-*` | `secure-*`（LE Secure Connections） |

帧格式本身没变，v1 手机解析回执时忽略多出来的 `crc` 照样能用，只是没有回执归属保护；两边一起升级。
