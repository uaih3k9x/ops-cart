# ops-cart

ops-cart 是一台机房运维推车的整套软件：PXE 装机、KVM、串口 console、线缆标签，以及让 Agent 在人工闸门下操作这些设备的 MCP 服务。

当前版本见 [`VERSION`](VERSION)。功能的完成状态按本文“功能总览”的状态列为准。

## 解决什么现场问题

被处理的机器常常没有系统、没有网络，或者卡在 BIOS，SSH 和带外管理都用不上。交换机也可能只有 console 口可用。现场通常要同时带显示器、键盘、串口线、U 盘和标签机，维护者还不一定在机房。

推车把这些工具集中到一台车上，推到机器旁边插线即可使用：

- 接 HDMI 和 USB，直接看画面、敲键盘，包括 BIOS 阶段。
- 接网线，由 PXE 装系统或进救援。
- 接 console 线，操作交换机并做只读巡检。
- 维护者不在车旁时，经 tailnet 用网页或 MCP 操作。
- Agent 可以读屏幕和串口；写操作受租约、闸门和急停约束。

## 架构

### 硬件

```
被测机 ◄── HDMI ───── 采集卡 (SC0710 / MS2130) ──┐
被测机 ◄── USB ────── CH9329 键鼠注入 ───────────┤
被测机 ◄── 网线 ───── LAN1 装机口 ───────────────┤
交换机 ◄── console ── FT232R 串口头 (槽位 1-4) ──┤ 推车主机 (Rocky 9) ── Wi-Fi / LAN2 ── tailnet ──► 维护者 / Agent
TSC 标签机 ◄── USB ──────────────────────────────┤
APC UPS ◄── USB HID ─────────────────────────────┤
USB 副屏 (800×1280) ◄── USB bulk ────────────────┘
```

推车主机由一台二手 X 光安检识别盒改装。串口头按 FTDI 序列号绑定到固定槽位。可选硬件包括 SATA 数据盘（`/srv/data`，放装机镜像）和 BLE 适配器。

网口名写在代码和单元文件里：LAN1 `eno1`，LAN2 `enp3s0`，无线 `wlp4s0`。换机器要改这些位置。

### 软件组件

```
      现场                       远程 (本机 / tailnet)
   主屏 + 键鼠                浏览器              Agent 框架
       │                        │                     │ MCP (stdio / HTTP)
       ▼                        ▼                     ▼
 ┌──────────┐             ┌──────────┐         ┌────────────────────┐
 │  kiosk   │             │ webkiosk │         │ cart_mcp           │
 │ (Tk,root)│             │ (ops-web)│         │ 动手档 / 只读档    │
 └────┬─────┘             └────┬─────┘         └─────────┬──────────┘
      │  /run/ops-cart/ 状态文件 (租约、挂起、急停)      │
      ├──────────────────────────────────────────────────┤
      ▼                        ▼                         ▼
 kvm-broker              conbroker (仓库外)       pxe-server / pxe-http-*
 CH9329 仲裁             串口单写多读             DHCP / TFTP / HTTP

 cart-dash: 采集 state.json, 驱动副屏          cart-ocr: 常驻 OCR, 无网络, 动态用户
 大肥鱼: bigfish 用户下 rootless Docker, 与 kiosk 只经状态文件、急停文件和任务 socket 交互
```

| 组件 | 运行方式 | 职责 |
|---|---|---|
| 车机 `kiosk` | root，Tk，无窗口管理器 | 自检、状态板、KVM、串口面板、装机解锁、写操作确认、PIN 锁屏 |
| 副屏 `cart-dash` | systemd，独立 venv | 采集状态写 `/run/ops-cart/state.json`，画只读仪表板和桌宠 |
| 网页口 `webkiosk` | 专用用户 `ops-web`，默认不自启 | 经浏览器看状态、画面，使用键鼠和串口；对 Agent 只能急停和拒绝，请求经 root 的 `cart-serial-req` 落地 |
| 键鼠代理 `kvm-broker` | systemd，`/run/ops-kvm/kvm.sock` | 所有键鼠操作的统一入口；人优先，按对端 uid 判断来源 |
| 串口 `conbroker` | 仓库外部署，见 #89 | 串口单写多读、日志、Agent 急停丢弃 |
| PXE 三件套 | `pxe-server`、`pxe-http-boot`、`pxe-http-repo` | DHCP / proxyDHCP / TFTP / HTTP，装机解锁按 MAC 限时 |
| MCP `cart_mcp` | stdio 或 HTTP | 向 Agent 暴露工具，分只读档和动手档 |
| OCR `cart-ocr` | 可选，动态用户，断网 | RapidOCR + OpenVINO，没开时退回 Tesseract |
| 大肥鱼 | 运行环境不在本仓库 | 推车上的运维 Agent（基于 DeepSeek Harness）。本仓库只含车机面板和接口约定 |

车上程序只用 Python 3.9 标准库和 Tkinter。例外放在独立 venv：`cart-dash` 用 Pillow、pyusb（`/opt/pxe/venv`），`cart-ocr` 用 RapidOCR（`/opt/pxe/ocr-venv`）。

## 功能总览

状态列含义：

- **已实现**：代码和离线测试都在 main。
- **待真机验收**：代码已合并，真屏或真硬件的验收还没有记录。
- **部分实现**：说明列写明已有和缺失的部分。
- **默认关闭**：代码已在，需要手动启用。

| 功能 | 状态 | 说明 |
|---|---|---|
| PXE 装机 | 已实现 | 只支持 UEFI，网线直插自动装 Rocky 9。车机解锁后，倒计时内只有选定的 MAC 能装。其他机器的菜单超时后从本地盘启动，救援只能手选。链路上有别的 DHCP 时转 proxyDHCP；自己发地址默认限时 30 分钟 |
| LAN1 防火墙区 | 待真机验收 | 网口模式建连接时写 `pxe` 区；断线重连的实机确认未记录（#38） |
| 停发横条收起 | 待真机验收 | 到时停发的提示 10 分钟后收起，之后点 PXE 灯续期（#33） |
| Windows PE | 已实现 | GRUB → iPXE → wimboot，被测机要关 Secure Boot，见 [WINPE](docs/WINPE.md) |
| 整盘克隆 | 部分实现 | Clonezilla 单播抓取和恢复，目标盘显式匹配。组播未实现，实机结果未记录，见 [CLONE](docs/CLONE.md) |
| 数据盘和 Samba | 已实现 | 见 [DATA_DISK](docs/DATA_DISK.md)。LAN1 无链路时的 smbd 处理见 [MIGRATE_95](docs/MIGRATE_95.md) |
| KVM | 已实现 | HDMI 采集预览，CH9329 透传键鼠，开机选有信号的画面源 |
| 串口 console | 已实现 | 车机面板经 conbroker 操作四个槽位 |
| 串口只读巡检 | 已实现 | 华为、H3C、思科、锐捷，自动认厂商，比对配置漂移，可 `--export-topo` 导出 |
| 串口观察模式 | 待真机验收 | 人登录并确认设备名后，只发 `display` 只读命令画前面板。堆叠成员分层显示待真屏和真机验证（#108） |
| 车机 Kiosk | 已实现 | 开机自检、状态板、PIN 锁屏、底部虚拟指示灯 |
| CDE 深浅配色 | 待真机验收 | 代码已合并，按日出日落自动切换；各界面的截图验收未记录（#28、#30、#32） |
| 开关按钮状态、副屏红条让位 | 待真机验收 | #83 |
| 中文输入 | 待真机验收 | librime + Tk 候选栏，接在网络唤醒名字框、大肥鱼任务框、标签文字框（#54） |
| 输入法状态灯 | 待真机验收 | 底部指示灯排显示 ENG / 简（#100） |
| 大写锁定提示 | 待真机验收 | 打码输入框和串口面板显示 ⇪（#88） |
| 网页口 | 已实现 | tailnet 放行；局域网要开关和 PIN；LAN1 来源一律拒绝 |
| 副屏仪表板 | 部分实现 | `cart-dash` 已上线。车机仍有自己的采样和自检，尚未改成只读快照（#6） |
| 副屏桌宠 | 部分实现 | 像素猫，锁屏且无事时出现。空闲判断暂用副屏已有信号，未接统一的后台活动上报（#35） |
| UPS | 部分实现 | 读数和 15% 倒计时关机已实测，低电量预告的提前量待下次放电演练。断电省电模式和电池健康未实现（#4） |
| 联网 | 已实现 | Wi-Fi 是唯一主出口，LAN2 最多当备用出口，LAN1 不当出口。LAN1 / LAN2 按口选模式（关闭、DHCP、静态），每次切换可撤回。PXE、共享上网、透明桥模式未实现（#12）。校园网 ePortal 自动认证 |
| 网络唤醒 | 已实现 | 只从 LAN1 发送 |
| MCP 标签工具 | 已实现 | TSC 线缆和资产标签 |
| 车机打标签 | 待真机验收 | 输入、预览、份数、走纸、校准；真实出纸、连打、中文待验收（#104） |
| MCP 服务 | 已实现 | 串口、截屏、OCR、键鼠、WOL、PXE 申请、标签，见“安全模型” |
| OCR 常驻服务 | 待真机验收 | 实现后的内存和耗时实测未记录（#47） |
| 大肥鱼车机面板 | 待真机验收 | 看进度、派任务、取消、急停（#53、#99）。确认和拒绝走现有确认框，输入框没有批准动作 |
| 远程审批 | 部分实现 | 只有服务端纯逻辑（分级、digest、TOTP）。未接入真实闸门和飞书通道（#50） |
| 蓝牙打字 | 部分实现，默认关闭 | 手机经 BLE 把文字排进队列，不执行命令，App 在 `android/`。队列取件方未接（#52） |
| 其他 | 已实现 | LLDP 被动看对端、事件推送（ntfy / 飞书）、屏幕配方（`boot-pxe`）、夜间日志小结 |

## 安全模型

设计前提是 Agent 不可信，设备回显和 OCR 结果也不可信。限制都在推车服务端实现，不依赖 Agent 客户端自身的审批。详细设计见 [DESIGN_agent_mcp](docs/DESIGN_agent_mcp.md)。

### 只读与动手分离

- `cart_mcp.py --profile readonly` 只注册只看的工具，动手工具根本不存在。默认档 `act` 注册全部工具。
- 两档是不同的进程、端口和令牌（`cart-mcp-ro.service`、`cart-mcp.service`）。
- HTTP 依次检查来源（本机或 tailnet）、Host 白名单、Origin、Bearer 令牌。
- 每次调用写审计 `/var/log/ops-cart/mcp-audit.log`，`secret` 内容不进日志。

### 写租约

串口和键鼠的写操作都要先拿租约。租约有期限、要续期，同一目标同时只有一个写入者。租约过期、释放或被急停撤销时，它挂着的操作一起作废。

### 串口闸门

Agent 只能发单行命令和固定的特殊键，拿不到原始字节。

| 层 | 条件 | 结果 |
|---|---|---|
| 1 放行 | 认得出厂商，停在提示符上，命令只读且不带高危词 | 直接发送 |
| 2 决策模型 | 其余不带高危词的命令。规则文件默认关闭；超时、报错、格式不对都不放行 | 模型放行或转第 3 层 |
| 3 人确认 | 高危词、模型拦下的命令、其他无法放行的命令 | 挂起，等人在车机确认 |

- 回车和空格只在设备最后一行是命令提示符或翻页提示时直发。确认类提示要人确认，看不到最后一行时拒绝。
- 槽位有挂起项时拒绝直发，批准绑定发送序号，防止批准落到被改变的设备上下文上。
- 第 1 层的按厂商前缀白名单还在影子期，只记录结论，放行仍按只读规则，见 [DESIGN_layer1_allowlist](docs/DESIGN_layer1_allowlist.md)。

### 键鼠闸门

逐行、逐键检查高危词和危险组合键，命中后挂起等人确认。`secret` 内容不进日志，命中高危词直接拒绝。

这是减速带，不是安全边界。推车看不懂屏幕上下文，鼠标点选、图形界面、脚本和编码后的命令都拦不住。兜底靠人盯和急停。

### 审批通道

- 串口、键鼠的确认和解除急停只能在车机本机进行。
- 网页只能急停和拒绝。
- Agent 没有确认工具。
- PXE 解锁由 Agent 提申请，人在车机上照抄确认码才生效，10 分钟内没确认就作废。
- 远程审批目前没有接入，见功能总览。

### 急停

- 单路急停只管一个串口槽位；全部急停覆盖四路串口和键鼠，并作废等待中的 PXE 申请，同时向大肥鱼写急停信号，10 秒内没有确认就停掉它的用户级服务。
- 急停只停止发送，不向设备插入任何内容。已经交给设备的命令收不回来。
- 人和 Agent 都能触发急停，解除只能由本机的人进行。
- 串口状态文件读不懂时按全部急停处理。

### PXE 与网络暴露

- 默认菜单超时后从本地盘启动。救援环境带 sshd，只能手动选择，风险说明见 [PUBLIC_SAFETY](docs/PUBLIC_SAFETY.md)。
- 装机和克隆菜单只给解锁的 MAC，到时自动上锁。
- MAC 由客户端自报，真正的边界是 LAN1 物理上接了谁。

### 大肥鱼

- 大肥鱼的审批模式管不到 MCP 工具调用，所以限制必须在 MCP 服务端。
- 第一阶段只接只读档，动手阶段要等远程审批接入闸门。部署不在本仓库，这里无法校验。
- 车机只读取白名单状态文件，它的输出作为不可信文本显示。

### 已知的边界

- `wol_send` 不需要租约，调用后直接发送，不经人确认（#49）。
- 键鼠高危规则未覆盖部分写法，例如重定向写块设备、`rd /s /q`（#69）。
- 回车和空格的判断在推车侧已生效，conbroker 的发前把关还没换成同一套（#73）。

## 部署和回滚

### 升级

1.16.00 和 1.16.01 有完整的打包和安装脚本，参考 [DEPLOY_1.16.01](docs/DEPLOY_1.16.01.md)。流程是：

```bash
scripts/pack-1.16.01.sh <输出目录>                                  # 开发机打包, 工作区必须干净
sudo systemd-run --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.01/install.sh   # 推车上, 不依赖 ssh 会话
journalctl -fu ops-cart-deploy
```

安装脚本按 manifest 核对包内容，备份到 `/opt/pxe/.bak-<时间戳>/`，生成 `rollback.sh`，只重启部署前开着的服务，最后跑验收。

1.16.02 之后的版本仓库里没有对应的清单和脚本。升级前用 `git diff --name-status <推车上的提交>..HEAD` 算变更面，以上一版脚本为模板。

### 回滚

```bash
sudo systemd-run --unit=ops-cart-rollback --collect bash /opt/pxe/.bak-<时间戳>/rollback.sh
journalctl -fu ops-cart-rollback
```

安装出错时，脚本会打印带具体时间戳的回滚命令。验收 FAIL 不会自动回滚。

### 验收

```bash
sudo python3 cart_accept.py --expect-version <版本>    # 只读: PASS / FAIL / SKIP / 人工
```

`scripts/cart_accept.py` 不启停服务，也不发键。人工部分见 [ACCEPT_1.11](docs/ACCEPT_1.11.md) 和各版部署清单的车边检查表。

### 从零安装

系统包：python3-tkinter、xorg-x11-xinit、xorg-x11-server-Xorg、v4l-utils、libusbx、poppler-utils、tailscale；EPEL 的 tesseract、ImageMagick；RPM Fusion 的 mpv、ffmpeg。conbroker 目前需要单独部署（#89）。

```bash
scripts/stamp-build.sh                                   # 生成 BUILD (提交数 + 短哈希)
ssh cart mkdir -p ops-cart
scp -r *.py VERSION BUILD web icons fonts deploy examples scripts cart:ops-cart/
# 以下在推车上 cd ~/ops-cart 后执行
sudo install -d -m 755 /opt/pxe && sudo cp -r *.py VERSION BUILD web icons fonts /opt/pxe/
sudo install -D -m 644 -t /usr/local/share/fonts/ops-cart/ /opt/pxe/fonts/unifont-18.0.01.otf && sudo fc-cache -f
sudo install -D -m 644 deploy/pxe.env.example /etc/ops-cart/pxe.env      # 改成现场装机网段
sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo cp deploy/sysctl.d/* /etc/sysctl.d/ && sudo sysctl --system
sudo cp deploy/tmpfiles.d/ops-cart.conf /etc/tmpfiles.d/ && sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf   # 没建 ops-web 用户时 serial-req 那行报错属预期, 其余照建
sudo cp deploy/logrotate.d/ops-cart /etc/logrotate.d/
sudo install -D -m 644 -t /etc/systemd/journald.conf.d/ deploy/journald.conf.d/ops-cart.conf && sudo systemctl restart systemd-journald
sudo install -m 755 deploy/bin/kiosk-launcher /usr/local/bin/kiosk
sudo install -m 755 -t /usr/local/bin/ deploy/bin/kiosk-setpin deploy/bin/cart-fan-curve
sudo kiosk-setpin
sudo install -m 644 deploy/firewalld/pxe.xml /etc/firewalld/zones/ && sudo firewall-cmd --reload
# LAN1 (eno1) 的主地址必须就是 pxe.env 的 PXE_IP, 且在 pxe 区; 否则三件套 active 也装不了机. 二选一:
#   车机「网口模式」把 LAN1 设成静态 PXE_IP (netmode 建的连接自带 pxe 区), 或者:
. /etc/ops-cart/pxe.env
sudo nmcli con add type ethernet ifname eno1 con-name Lan1-pxe ipv4.method manual \
    ipv4.addresses "$PXE_IP/$PXE_PREFIX" connection.zone pxe && sudo nmcli con up Lan1-pxe
sudo python3 -m venv /opt/pxe/venv && sudo /opt/pxe/venv/bin/pip install pillow pyusb   # cart-dash 用这个 venv
sudo bash scripts/setup-cart.sh      # 先把 Rocky minimal ISO 放到脚本里写的路径; 校验后铺 tftp 和 HTTP 仓库, 只写默认菜单
sudo install -m 644 examples/ks.cfg.example /opt/pxe/http/ks.cfg   # 改掉里面的密码 hash 和分区再用
sudo systemctl enable --now kiosk.service cart-dash.service kvm-broker.service \
    pxe-server.service pxe-http-boot.service pxe-http-repo.service \
    cart-boot-success.timer wifi-prefer.timer campus-auth.timer
sudo systemctl enable --now cart-fan-curve.service   # 认不出 it8625 时会失败, 可以先不开
```

### systemd 单元

| 单元 | 作用 | 启用 |
|---|---|---|
| `kiosk` · `cart-dash` · `kvm-broker` | 车机、副屏和快照、键鼠代理 | 开 |
| `pxe-server` · `pxe-http-boot` · `pxe-http-repo` | 装机三件套 | 开 |
| `cart-boot-success.timer` · `wifi-prefer.timer` · `campus-auth.timer` | 开机成功标记、无线优先级、校园网认证 | 开 |
| `cart-fan-curve` | 风扇曲线 | 开；内核认不出 it8625 时单元会失败，可以先不开 |
| `cart-notify.timer` · `cart-dream.timer` | 手机推送（没配只记日志）、夜间小结 | 按需 |
| `webkiosk` · `cart-serial-req.path` | 网页口、网页急停和拒绝 | 按需，见 DEPLOY_1.06.00 第 7 步和 DEPLOY_1.11.00 第 5 节。建好 `ops-web` 用户后再跑一次 tmpfiles |
| `cart-mcp` · `cart-mcp-ro` | MCP 的 HTTP 方式，动手档和只读档 | 按需，先配 `/etc/ops-cart/mcp.json`。stdio 方式不用单元，只读档身份配置见 DESIGN_agent_mcp 第十六节 |
| `cart-ocr` | OCR 常驻服务 | 可选，先 `sudo bash scripts/setup-ocr.sh deploy/ocr-requirements.txt` |
| `cart-revtunnel` | 备用远程通道 | 可选，见 [REMOTE_TUNNEL](docs/REMOTE_TUNNEL.md) |
| `cart-ble-input` | 蓝牙打字 | 默认不开，见 [BLE_INPUT](docs/BLE_INPUT.md) |

### 路径约定

| 路径 | 内容 |
|---|---|
| `/opt/pxe/` | 程序，所有模块平铺 |
| `/etc/ops-cart/` | 配置，样例在 `examples/` |
| `/run/ops-cart/` | 运行时状态，重启清空（租约、挂起、急停也随之清空） |
| `/var/lib/ops-cart/` | 需要跨重启的状态 |
| `/var/log/ops-cart/` | 日志，由 logrotate 管理 |

推车常被直接拔电挪走：SSD 带掉电保护，脏页 5 秒落盘，配置一律原子写，系统日志落盘，来电自动开机。挪车前仍建议短按电源键正常关机。

## 测试

```bash
python3 -m pytest -q                         # 离线全量, 不接硬件
bash scripts/smoke-kiosk.sh                  # Xvfb 无头冒烟
python3 scripts/check_public_safe.py .       # 公开仓库脱敏检查, 本地词表放 .public-safe.local
```

- `*_core.py` 是不碰硬件、不碰 Tk 的纯逻辑，是单测的主要对象。
- `test_py39_compat` 保证代码能在推车的 Python 3.9 上跑。
- macOS 上 Tk 用例默认跳过。不要在 macOS 上用 `OPS_TK_TESTS=1` 批量跑，进程会 abort。
- 真 Tk 用例在车机真屏上跑，命令见 [DEPLOY_1.16.01](docs/DEPLOY_1.16.01.md) 第 8 节。skip 不算通过。
- 单测全绿不代表界面正常。GUI 问题多半在真屏上才暴露，所以功能总览单列了“待真机验收”。

## 开发约定

- 先写失败的测试，再写实现。
- 纯逻辑放 `*_core.py`，硬件、Tk 和外部命令留在薄外壳。
- 车上代码只用标准库。新增第三方依赖要进独立 venv，并在部署文档中写明。
- 车机主线程不做 I/O，外部命令一律放后台线程。
- 配置和状态文件用 `atomicfile` 原子写。
- 判定类逻辑 fail-closed：读不懂、超时、拿不到状态时，按更严的一侧处理。
- 颜色、字体、斜边只在 `theme.py` 定义，业务代码引用名字，测试扫描兜底。
- 这是公开仓库，不放真实地址、MAC、主机名、人名和本机路径，约定见 [PUBLIC_SAFETY](docs/PUBLIC_SAFETY.md)。
- 改动走分支和 PR，经审查后由维护者合并。
- `python3 scripts/readme_due.py` 统计 README 上次改动后合并的 PR 数，到 10 个时整理一次。统计以 git 历史为准，没有单独的记录文件。

## 版本

`VERSION` 是人工维护的版本号（如 `1.16.05`），`scripts/stamp-build.sh` 生成 `BUILD`（`<提交总数> <短哈希>`，工作区不干净时带 `-dirty`）。车机显示这两个值，验收脚本用它们比对。改版本号时同步 `scripts/cart_accept.py` 的 `EXPECT_VERSION`。

## 代码地图

模块都在根目录，部署时平铺进 `/opt/pxe/`。

| 模块 | 职责 |
|---|---|
| `pxeserver.py` · `httpserve.py` | PXE 服务端和装机口守卫；引导和仓库的静态 HTTP |
| `kiosk.py` · `kiosk_core.py` · `kiosk_ui_core.py` | 车机界面；自检、状态采集、装机模式、PIN；界面状态机 |
| `ime_tk.py` · `ime_core.py` · `ime_proc.py` · `ime_worker.py` · `ime_rime.py` | 车机中文输入 |
| `theme.py` · `sun_core.py` · `lamps_core.py` | 配色和字体；日出日落；底部指示灯 |
| `ks_panel.py` | Kickstart 表单编辑 |
| `wifi_panel.py` · `wifi_core.py` · `wifi_prefer.py` · `ruijie_portal.py` | 无线管理、优先级、校园网认证 |
| `netmode_panel.py` · `netmode_core.py` · `netprof_core.py` | 网口模式 |
| `wol_panel.py` · `wol_core.py` | 网络唤醒 |
| `label_panel.py` · `label_core.py` · `label_render.py` · `label_device.py` | 车机打标签 |
| `mcp_label.py` · `cart_label.py` · `tspl_preview.py` | MCP 标签工具；TSPL 生成；开发机预览 |
| `ch9329.py` · `kvm_core.py` · `kvm_broker.py` · `kvm_broker_core.py` | CH9329 协议；画面源绑定；键鼠代理 |
| `serial_panel.py` · `serial_core.py` · `survey_core.py` | 串口面板、conbroker 接口、只读巡检 |
| `observe_core.py` · `observe_panel.py` | 串口观察模式 |
| `cart_snap.py` · `cart_ocr.py` · `ocr_rapid_worker.py` | 截帧；OCR；OCR 服务进程 |
| `recipe.py` · `recipe_core.py` | 屏幕配方 |
| `cart_mcp.py` · `mcp_core.py` | MCP 服务、档位、认证、审计 |
| `mcp_serial.py` · `serial_write_core.py` · `serial_gate_model.py` · `serial_allowlist.py` | 串口写：租约、闸门、决策模型、影子白名单 |
| `mcp_kvm.py` | MCP 键鼠、`screen_ocr`、`wol_send` |
| `mcp_pxe.py` · `pxe_req_core.py` · `pxe_req_panel.py` · `pxe_log_core.py` | PXE 解锁申请、`pxe_clients`、网页 PXE 日志 |
| `confirm_core.py` · `confirm_panel.py` | 写操作确认框和急停 |
| `bigfish_panel.py` · `bigfish_core.py` · `bigfish_task_core.py` | 大肥鱼面板，接口见 [BIGFISH_PANEL](docs/BIGFISH_PANEL.md) |
| `approval_core.py` · `totp_core.py` · `approval_channel.py` | 远程审批服务端核心，尚未接入 |
| `ble_input.py` · `ble_input_core.py` | 蓝牙打字 |
| `webkiosk.py` · `web_core.py` · `web/` | 网页口 |
| `dash_service.py` · `dash_core.py` · `dash_render.py` · `udisp.py` · `udisp_usb.py` | 副屏服务、快照、渲染、USB 协议 |
| `pet_core.py` · `pet_render.py` · `pet_sprites.py` | 副屏桌宠 |
| `ups_core.py` · `hidpdc.py` | UPS 判断；HID Power Device 解析 |
| `lldp_core.py` · `notify_core.py` · `clone_core.py` | LLDP；事件推送；整盘克隆 |
| `srv_data_core.py` · `samba_core.py` | 数据盘和 Samba |
| `cart_dream.py` · `cartlog.py` · `atomicfile.py` | 夜间小结；日志分层；原子写 |

| 目录 | 内容 |
|---|---|
| `deploy/` | systemd 单元、sysctl、journald、logrotate、tmpfiles、firewalld、SELinux、命令 |
| `scripts/` | 打包安装、构建号、冒烟、验收、脱敏检查、README 计数等 |
| `examples/` | 配置样例 |
| `fonts/` · `icons/` · `plymouth/` | 界面字体、网络图标、开机画面 |
| `windows/` | 在 Windows 上跑 PXE 服务端 |
| `android/` | 蓝牙打字的安卓 App |
| `tests/` | 离线测试 |

## 文档

| 文档 | 内容 |
|---|---|
| [DEPLOY_1.16.01](docs/DEPLOY_1.16.01.md) · [1.16.00](docs/DEPLOY_1.16.00.md) · [1.11.00](docs/DEPLOY_1.11.00.md) · [1.08.03](docs/DEPLOY_1.08.03.md) · [1.06.00](docs/DEPLOY_1.06.00.md) | 各版部署清单 |
| [MIGRATE_95](docs/MIGRATE_95.md) | 日志路径迁移和 smb 恢复 |
| [ACCEPT_1.11](docs/ACCEPT_1.11.md) | 人工验收单 |
| [DESIGN_agent_mcp](docs/DESIGN_agent_mcp.md) | Agent 和 MCP 接入设计 |
| [DESIGN_layer1_allowlist](docs/DESIGN_layer1_allowlist.md) | 串口闸门第 1 层白名单 |
| [REMOTE_APPROVAL](docs/REMOTE_APPROVAL.md) | 远程审批第一阶段 |
| [BIGFISH_PANEL](docs/BIGFISH_PANEL.md) | 大肥鱼面板接口约定 |
| [NETMODE](docs/NETMODE.md) | 网口模式 |
| [CLONE](docs/CLONE.md) · [DATA_DISK](docs/DATA_DISK.md) · [WINPE](docs/WINPE.md) | 克隆、数据盘、Windows PE |
| [IME_RIME](docs/IME_RIME.md) · [BLE_INPUT](docs/BLE_INPUT.md) | 中文输入、蓝牙打字 |
| [REMOTE_TUNNEL](docs/REMOTE_TUNNEL.md) · [udisp-protocol](docs/udisp-protocol.md) | 备用远程通道、USB 副屏协议 |
| [PUBLIC_SAFETY](docs/PUBLIC_SAFETY.md) | 公开仓库约定和救援环境风险 |
| [OPTIMIZE_PLAN_20261025](docs/OPTIMIZE_PLAN_20261025.md) | 10-25 优化方案 |
| [fonts/README](fonts/README.md) | 字体来源和授权 |

## License

MIT
