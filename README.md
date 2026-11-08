# ops-cart

硬件级运维推车：装机、KVM、串口，以及替你蹲机房的那台机器。

一台二手 X 光安检识别盒改成的远程运维车。目标机器没系统、没网络、卡在 BIOS，推过去插上线就能处理。

```
目标机器 ◄── HDMI ── SC0710 / MS2130 采集 ──┐
目标机器 ◄── USB ─── CH9329 键鼠注入 ───────┤
目标机器 ◄── 网线 ── PXE 装机口 (LAN1) ─────┤ 推车 (Rocky 9) ── 有线 / 无线 ── tailnet ──► 你
交换机   ◄── 串口 ── FT232R ───────────────┘
```

车上的程序只用 Python 3.9 标准库 + Tkinter。例外各自装在单独的 venv 里：副屏 `cart-dash` 要 Pillow / pyusb（`/opt/pxe/venv`），OCR 常驻服务 `cart-ocr` 要 RapidOCR（`/opt/pxe/ocr-venv`，可选）。

当前版本见 [`VERSION`](VERSION)。

## 能做什么

| 功能 | 说明 |
|---|---|
| 装机 | 自研 PXE 服务端（DHCP + proxyDHCP + TFTP + HTTP），网线直插自动装 Rocky 9，只支持 UEFI。只有车机上解锁、在倒计时内的那个 MAC 能装；其他机器的菜单超时后从本地盘启动，救援只能手选。装机口上有别人的 DHCP 就转代理模式；自己发地址时限时，默认 30 分钟 |
| KVM | HDMI 采集预览，CH9329 透传键鼠，开机自动选有信号的画面源。键鼠统一经常驻代理 `kvm-broker`，人优先 |
| 串口 | conbroker 串口 console 面板；一键只读巡检（华为 / H3C / 思科 / 锐捷，自动认厂商），比对配置漂移，可 `--export-topo` 导出 |
| 车机 | 无 WM 全屏 Kiosk：开机自检、状态板、PIN 锁屏、CDE 深浅两套配色（按日出日落自动切），屏幕底下一排虚拟指示灯；中文输入（librime，目前只接网络唤醒的名字框） |
| 网页口 | 同一套状态、画面、键鼠、串口经浏览器访问，另有只读的 PXE 日志页。tailnet 放行，局域网要开关 + PIN |
| 副屏 / UPS | USB 副屏常驻状态板和像素猫桌宠；APC UPS 直读 hidraw，低电量先预告，再倒计时关机 |
| 联网 | 有线优先，无线兜底；锐捷 ePortal 自动认证；LAN1 / LAN2 按口选模式，每次切换都能撤回 |
| Agent 接入 | MCP 服务开放串口、截屏、OCR、键鼠、WOL、PXE 申请、标签打印，分只读档和动手档（`--profile readonly / act`）。写操作要拿有期限的租约，键鼠和串口都过闸门，高危的挂起等人在车机上确认，挂起期间同一槽位不许直发。串口回车 / 空格只在设备最后一行是命令提示符或翻页提示时直发，确认类提示或看不到最后一行要人确认，确认框上另列"设备最后一行（不可信）"；全部急停，解除只能在本机。远程审批目前只有服务端核心，还没接上 |
| 蓝牙打字 | 手机经 BLE 把文字发到推车排队，不执行命令；安卓 App 在 `android/`，单元默认不开 |
| 机房辅助 | LLDP 看对端交换机口、TSC 线缆 / 资产标签、关键事件推到手机、Clonezilla 整盘克隆、屏幕配方（如 `boot-pxe`）、夜间日志小结 |

## 部件

车上运行的模块都在根目录，部署时平铺进 `/opt/pxe/`。`*_core.py` 是不碰硬件、不碰 Tk 的纯逻辑，测试主要测它们。

| 模块 | 职责 |
|---|---|
| `pxeserver.py` · `httpserve.py` | PXE 服务端和装机口守卫 `PxeGuard`；引导和仓库的静态 HTTP，ks 和装机菜单只给解锁的 MAC |
| `kiosk.py` · `kiosk_core.py` · `kiosk_ui_core.py` | 车机界面；自检计划表、状态采集、装机模式、PIN 闸门、默认菜单（`--write-rescue`）；界面状态机 |
| `ime_tk.py` · `ime_core.py` · `ime_proc.py` · `ime_worker.py` · `ime_rime.py` | 车机中文输入：Tk 候选栏，librime 放在沙箱子进程里 |
| `theme.py` · `sun_core.py` · `lamps_core.py` | 配色、字体、斜边（`DARK` / `LIGHT`）；日出日落定深浅；底部指示灯 |
| `ks_panel.py` | Kickstart 表单编辑，带备份和破坏性指令校验 |
| `wifi_panel.py` · `wifi_core.py` · `wifi_prefer.py` · `ruijie_portal.py` | 无线管理、优先级生效、校园网认证 |
| `netmode_panel.py` · `netmode_core.py` · `netprof_core.py` | 网口模式（关闭 / DHCP / 静态），可撤回 |
| `wol_panel.py` · `wol_core.py` | 网络唤醒 |
| `ch9329.py` · `kvm_core.py` · `kvm_broker.py` · `kvm_broker_core.py` | CH9329 协议栈和命令行；画面源与键鼠绑定；键鼠代理（`/run/ops-kvm/kvm.sock`） |
| `serial_panel.py` · `serial_core.py` · `survey_core.py` | 串口面板、conbroker 接口、多厂商只读巡检 |
| `observe_core.py` · `observe_panel.py` | 串口观察模式（#85，半自动）：人登录后确认设备名，推车只发 `display` 只读命令，画前面板和端口灯；有人 attach 就暂停，主动锁屏或到时长上限（默认 30 分钟）就停 |
| `cart_snap.py` · `cart_ocr.py` · `ocr_rapid_worker.py` | 截一帧；OCR（优先 `cart-ocr` 服务，没开退回 Tesseract）；OCR 服务进程 |
| `recipe.py` · `recipe_core.py` | 屏幕配方：截图 + OCR + 发键 |
| `cart_mcp.py` · `mcp_core.py` | MCP 服务（stdio / HTTP，`--profile readonly` 只注册只看的工具，默认 `act`），审计日志；车机上 `pending / confirm / reject / estop / resume` |
| `mcp_serial.py` · `serial_write_core.py` · `serial_gate_model.py` | 串口写：租约、闸门（只读规则 + 按键白名单 + 可选决策模型）、挂起、急停 |
| `serial_allowlist.py` | 第 1 层按厂商的命令前缀白名单，影子期：只记进 `gate.shadow`，放行仍按只读规则；MCP 工具 `gate_allowlist` 只读查看 |
| `mcp_kvm.py` | MCP 键鼠 `kvm_*`（租约 + 闸门，逐行逐键查高危）、`screen_ocr`、`wol_send` |
| `mcp_pxe.py` · `pxe_req_core.py` · `pxe_req_panel.py` · `pxe_log_core.py` | Agent 申请 PXE 解锁（人照抄确认码才生效）、`pxe_clients`；网页 PXE 日志页 |
| `mcp_label.py` · `cart_label.py` · `tspl_preview.py` | MCP 标签工具；TSPL 生成；开发机上预览 |
| `confirm_core.py` · `confirm_panel.py` | 串口 / 键鼠写操作的确认框和急停 UI |
| `bigfish_panel.py` · `bigfish_core.py` | 大肥鱼面板（#53）：只读看它在做什么，锁屏和副屏一行状态；确认 / 拒绝走现有确认框，全部急停同时写大肥鱼急停信号，10 秒没确认就硬停。接口约定见 [docs/BIGFISH_PANEL.md](docs/BIGFISH_PANEL.md) |
| `approval_core.py` · `totp_core.py` · `approval_channel.py` | 远程审批第一阶段：分级、digest、TOTP、通道接口；还没接进推车 |
| `ble_input.py` · `ble_input_core.py` | 蓝牙隔空打字：BlueZ GATT 服务，收文字排进 `/run/ops-cart/ble-input/in/` |
| `webkiosk.py` · `web_core.py` · `web/` | 网页口（专用用户 `ops-web`，默认不自启） |
| `dash_service.py` · `dash_core.py` · `dash_render.py` | `cart-dash`：采集状态快照 `/run/ops-cart/state.json`，画副屏 |
| `pet_core.py` · `pet_render.py` · `pet_sprites.py` | 副屏桌宠 |
| `udisp.py` · `udisp_usb.py` | USB 副屏协议和传输 |
| `ups_core.py` · `hidpdc.py` | UPS 读数和低电量判断；HID Power Device 解析 |
| `lldp_core.py` · `notify_core.py` · `clone_core.py` | LLDP 被动监听；手机推送（ntfy / 飞书）；整盘克隆 |
| `srv_data_core.py` · `samba_core.py` | 数据盘 `/srv/data` 和 Samba 装机库 |
| `cart_dream.py` · `cartlog.py` · `atomicfile.py` | 夜间日志小结；日志分层；防断电写文件 |

| 目录 | 内容 |
|---|---|
| `deploy/` | systemd 单元、sysctl、journald、logrotate、tmpfiles、firewalld 区、SELinux 小模块、`kiosk` 等命令 |
| `scripts/` | 铺引导文件、构建号、冒烟、验收、公开安全检查、OCR / Samba / 数据盘安装、README 计数 |
| `examples/` | 各配置文件的样例（自检、ks、MCP、闸门、推送、主题、换皮、标签缩写等） |
| `fonts/` · `icons/` · `plymouth/` | 界面字体 Unifont、顶栏网络图标、开机画面 |
| `windows/` | 在 Windows 上跑 PXE 服务端 |
| `android/` | 蓝牙打字的安卓 App |
| `tests/` | 离线单测，不接硬件 |

## 部署

升级已有推车：照 [`docs/DEPLOY_1.11.00.md`](docs/DEPLOY_1.11.00.md) 整包拷（备份、落位、SELinux 模块、重启顺序、验证、回滚）。1.11 之后新增的部件没有单独的清单，按各自文档装：中文输入见 [`docs/IME_RIME.md`](docs/IME_RIME.md)，蓝牙打字见 [`docs/BLE_INPUT.md`](docs/BLE_INPUT.md)，MCP 只读档见 [`docs/DESIGN_agent_mcp.md`](docs/DESIGN_agent_mcp.md) 第十六节。装完跑只读验收 `scripts/cart_accept.py`，用法和人工部分见 [`docs/ACCEPT_1.11.md`](docs/ACCEPT_1.11.md)。

系统包：python3-tkinter、xorg-x11-xinit、xorg-x11-server-Xorg、v4l-utils（`v4l2-ctl` 判断有没有信号）、libusbx（pyusb 要）、poppler-utils（资产标签的 pdftoppm）、tailscale；EPEL 里的 tesseract、ImageMagick（OCR 退回 Tesseract 时用 `convert` 预处理）；RPM Fusion 里的 mpv、ffmpeg。串口 console 用的 conbroker 目前在仓库外，见 #89。

从零装的骨架：

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

| 单元 | 作用 | 开不开 |
|---|---|---|
| `kiosk.service` · `cart-dash.service` · `kvm-broker.service` | 车机、副屏 + 快照、键鼠代理 | 开 |
| `pxe-server` · `pxe-http-boot` · `pxe-http-repo` | 装机三件套 | 开 |
| `cart-boot-success.timer` · `wifi-prefer.timer` · `campus-auth.timer` | 开机成功标记、无线优先级、校园网认证 | 开 |
| `cart-fan-curve.service` | 风扇曲线 | 开；依赖 hwmon 认出 it8625，内核驱动认不出时单元会失败，可以先不开 |
| `cart-notify.timer` · `cart-dream.timer` | 手机推送（没配只记日志）、夜间小结 | 按需 |
| `webkiosk.service` · `cart-serial-req.path` | 网页口，网页急停 / 拒绝 | 按需；见 DEPLOY_1.06.00 第 7 步 + DEPLOY_1.11.00 第 5 节。建好 `ops-web` 用户后再跑一次 `sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf` |
| `cart-mcp.service` · `cart-mcp-ro.service` | MCP 的 HTTP 方式，动手档 / 只读档各一个进程、令牌分开。stdio 不用单元：`claude mcp add cart -- ssh cart sudo python3 /opt/pxe/cart_mcp.py stdio --profile act`；只读档要专用身份 `agent-ro`，配法见 DESIGN_agent_mcp 第十六节 | 按需，先配 `/etc/ops-cart/mcp.json` |
| `cart-ocr.service` | OCR 常驻服务；`sudo bash scripts/setup-ocr.sh deploy/ocr-requirements.txt` 建 `/opt/pxe/ocr-venv` | 可选 |
| `cart-revtunnel.service` | 备用远程通道 | 可选，见 [`docs/REMOTE_TUNNEL.md`](docs/REMOTE_TUNNEL.md) |
| `cart-ble-input.service` | 蓝牙隔空打字 | 可选，默认不开，见 [`docs/BLE_INPUT.md`](docs/BLE_INPUT.md) |

网口名在代码和单元里写死：LAN1 装机口 `eno1`（`pxe-server.service`、`netmode_core`、自检），LAN2 管理口 `enp3s0`，无线 `wlp4s0`（`wifi_core`）。新机器的网口名不一样，就要改代码或单元文件。

配置都在 `/etc/ops-cart/`（照 `examples/` 改），运行时状态在 `/run/ops-cart/`（重启清空），要跨重启的在 `/var/lib/ops-cart/`，日志在 `/var/log/ops-cart/`。

推车常被直接拔电挪走：SSD 带掉电保护，脏页 5 秒落盘，配置一律原子写，系统日志落盘（`journalctl -b -1`），来电自动开机。能关还是先关：挪车前短按一下电源键。

## 开发和测试

```bash
python3 -m pytest -q                         # 离线全测; 代码要能在推车的 Python 3.9 上跑 (test_py39_compat 守着)
python3 scripts/check_public_safe.py .       # 公开仓库安全检查, 本地词表放 .public-safe.local
git fetch origin && python3 scripts/readme_due.py   # README 上次整理后 origin/main 合并了几个 PR, 到 10 个退出码 1
bash scripts/smoke-kiosk.sh                  # Xvfb 无头冒烟

python3 ch9329.py --port /dev/ttyACM0 type "hello"          # 键鼠代理在跑时经代理发
python3 pxeserver.py serve --ip 192.0.2.1 --prefix 24 --pool 192.0.2.10,192.0.2.20 \
    --tftp-root /opt/pxe/tftp --iface eno1
python3 cart_ocr.py shot.png --find "Boot Menu"             # 找到 0 / 没找到 1 / 出错 2
```

macOS 上默认不开 Tk 窗口，相关用例跳过；`OPS_TK_TESTS=1` 才真开。GUI 的毛病多半在实机上才暴露，单测全绿不等于能跑。

这是公开仓库，不放真实地址、MAC、主机名、人名和本机路径，约定见 [`docs/PUBLIC_SAFETY.md`](docs/PUBLIC_SAFETY.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [DEPLOY_1.11.00](docs/DEPLOY_1.11.00.md) · [1.08.03](docs/DEPLOY_1.08.03.md) · [1.06.00](docs/DEPLOY_1.06.00.md) | 各版部署清单 |
| [ACCEPT_1.11](docs/ACCEPT_1.11.md) | 人工验收单 |
| [NETMODE](docs/NETMODE.md) | 网口模式设计 |
| [CLONE](docs/CLONE.md) | 整盘克隆 |
| [DATA_DISK](docs/DATA_DISK.md) | 数据盘和装机库 |
| [WINPE](docs/WINPE.md) | Windows PE 引导 |
| [IME_RIME](docs/IME_RIME.md) | 车机中文输入，装 librime 和词库 |
| [BLE_INPUT](docs/BLE_INPUT.md) | 蓝牙隔空打字：协议、配对、单元 |
| [REMOTE_APPROVAL](docs/REMOTE_APPROVAL.md) | 远程审批第一阶段 |
| [DESIGN_agent_mcp](docs/DESIGN_agent_mcp.md) | Agent / MCP 接入设计，含只读档 / 动手档 |
| [DESIGN_layer1_allowlist](docs/DESIGN_layer1_allowlist.md) | 串口写闸门第 1 层白名单：格式、影子期、切换条件 |
| [REMOTE_TUNNEL](docs/REMOTE_TUNNEL.md) | 备用远程通道 |
| [udisp-protocol](docs/udisp-protocol.md) | USB 副屏协议 |
| [PUBLIC_SAFETY](docs/PUBLIC_SAFETY.md) | 公开仓库安全约定 |
| [OPTIMIZE_PLAN_20261025](docs/OPTIMIZE_PLAN_20261025.md) | 10-25 优化方案 |
| [fonts/README](fonts/README.md) | 字体来源和授权 |

## License

MIT
