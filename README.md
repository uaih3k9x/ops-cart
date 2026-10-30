# ops-cart

> 硬件级自动化运维推车 —— 装机、KVM、串口、以及替你蹲机房的那台机器。

一台二手 X 光安检识别盒,改造成了远程运维车。目标机器没系统、没网络、
卡在 BIOS,推过去插上线就能处理。**人在被窝,车在机房。**

```
目标机器 ◄── HDMI ── SC0710 / MS2130 采集 ──┐
目标机器 ◄── USB ─── CH9329 键鼠注入 ───────┤
目标机器 ◄── 网线 ── PXE 装机口 ────────────┤ 推车 (Rocky 9) ── 有线 / 无线 ── tailnet ──► 你
交换机   ◄── 串口 ── FT232R ───────────────┘
```

软件全部自研,只用 Python 标准库 + Tkinter,跑在车上的东西不装第三方包;
唯一例外是副屏服务 `cart-dash`,画图和推 USB 要 Pillow / pyusb,装在单独的 `/opt/pxe/venv` 里。

## 能干什么

- **装机**:自研 PXE 服务端(DHCP + proxyDHCP + TFTP),目标机器网线直插即全自动装 Rocky 9;
  按 MAC 下发,只有"装机模式"倒计时内的那台会被装,其他机器只能进救援。
  服务端自己也守规矩:租约到期回收、池满不发、不是自己的租约不吭声;装机口上看见别人的 DHCP 就转代理模式(不发地址、只给引导),
  网口上没有自己的地址就不回包;TFTP / HTTP 发装机菜单和 ks 前再核一遍装机模式和 MAC
- **KVM**:HDMI 采集低延迟预览,CH9329 透传键鼠(绝对鼠标,VMware 式 Ctrl+Alt 释放);
  开机自动选有信号的画面源
- **串口**:串口 console 面板,交换机/防火墙/服务器在网络炸了的时候也够得着
- **车机界面**:无 WM 直跑的全屏 Kiosk —— 街机风开机自检、实时状态板、右侧二级菜单、PIN 锁屏
  (输错冷却逐次翻倍、封顶 15 分钟,计数落 `/run`,重启 kiosk 不清零);外部命令和串口收发都在后台线程,
  结果经队列回主线程
- **网页版 Kiosk**:同一套状态、画面、键鼠与串口经浏览器远程访问,tailnet 永远放行,局域网需开关 + PIN;
  只认白名单里的 Host、POST 必须同源,输错冷却按来源 IP 分开;以专用用户跑,操作写审计日志(PIN、打字内容不记)
- **联网**:有线优先、无线兜底(拔网线约 17 秒切换);内置无线管理面板,
  校园网锐捷 ePortal 无浏览器自动认证(认证页主机首次记住,之后跳到陌生主机不发账号);
  改当前出口那条无线前先确认,换连接先建后删、新密码连不上自动回退
- **副屏与 UPS**:USB 副屏常驻状态板(`cart-dash` 统一采集,车机和副屏读同一份快照);
  APC UPS 直读 hidraw 拿电量、负载、市电状态,放电到 15% 正常关机;顶栏一排虚拟指示灯,颜色看自检、闪烁看活动
- **OCR**:截图识字带坐标,给"看屏幕 → 找按钮 → 点"的自动化留接口

## 代码地图

车上运行的模块都在根目录,部署时平铺进 `/opt/pxe/`。

| 文件 | 职责 |
|---|---|
| [`pxeserver.py`](pxeserver.py) | PXE 服务端:DHCP 租约表、TFTP(块号回绕、流式读)、装机口守卫 `PxeGuard`(状态写 `/run/ops-cart/pxe-guard.json`)、grub 菜单模板(全仓库只此一处);Linux / Windows 通用 |
| [`httpserve.py`](httpserve.py) | PXE 用的静态 HTTP(引导文件 :80、仓库与 ks :8000);ks 和装机菜单只给报出解锁 MAC 的请求(防误装,MAC 是客户端自报的,不是访问控制),`.bak` 一律 404;LAN1 没插线就等地址出现,不退出 |
| [`kiosk.py`](kiosk.py) | 车机 GUI(Tkinter) |
| [`kiosk_ui_core.py`](kiosk_ui_core.py) | GUI 的界面逻辑:`UiBridge`(后台结果回主线程)、左侧面板状态机、右侧菜单定义、装机定时器判定、CH9329 发送线程与鼠标位移拆分 |
| [`kiosk_core.py`](kiosk_core.py) | GUI 的可测核心:自检计划表与检查函数、状态采集、nmcli 命令、装机模式与装机网段(读 `pxe.env`)、PIN 闸门;`python3 kiosk_core.py --write-rescue` 给部署脚本写救援菜单 |
| [`ks_panel.py`](ks_panel.py) | Kickstart 编辑:表单化改主机名/密码,自动备份,破坏性指令校验 |
| [`wifi_panel.py`](wifi_panel.py) · [`wifi_core.py`](wifi_core.py) | 无线管理:扫描、连接、显示密码、优先级排序;面板底部改校园网认证账号、立即认证、看最近结果 |
| [`wifi_prefer.py`](wifi_prefer.py) | 定时让优先级真正生效:看得见更高优先级的已保存网络就切过去,失败冷却 30 分钟 |
| [`netprof_panel.py`](netprof_panel.py) · [`netprof_core.py`](netprof_core.py) | 网口配置文件:LAN1/LAN2 切换、一键回退、静态地址模板;发地址只在 LAN1,启用前探测对面有没有 DHCP(没插线不探测,结果算"未知",不许启用) |
| [`wol_panel.py`](wol_panel.py) · [`wol_core.py`](wol_core.py) | 网络唤醒:常用目标清单 + 临时输 MAC,从选定网口广播魔术包 |
| [`atomicfile.py`](atomicfile.py) | 防断电写文件:临时文件 → fsync → 改名,配置要么旧要么新 |
| [`cartlog.py`](cartlog.py) | 日志分层:机器事件进 `/var/log/ops-cart/`(logrotate 轮转),`CART-LOG.md` 只收一行摘要 |
| [`webkiosk.py`](webkiosk.py) · [`web_core.py`](web_core.py) · [`web/`](web/) | 网页版远程管理口(标准库 http.server + xterm.js);单独的 `webkiosk.service`,专用用户 `ops-web`,默认不开机自启 |
| [`ruijie_portal.py`](ruijie_portal.py) | 锐捷 ePortal 自动登录,只在连着配置里写的那个 SSID 时跑(配置没写 SSID 就不跑);认证页主机白名单 `portal_hosts`(空时首次成功自动记住);结果写 `/run/ops-cart/campus-auth-status.json` 给面板和状态板 |
| [`kvm_core.py`](kvm_core.py) | 画面源 ↔ 键鼠绑定:CH9329 按 CH343 序列号认身份;没绑或不在线 = 只看不操作,绝不退回随便一块 |
| [`ch9329.py`](ch9329.py) | CH9329 键鼠注入协议栈(独占打开,被占用报 `Ch9329Busy`、拔线报 `Ch9329Gone`),也可当命令行工具用 |
| [`serial_panel.py`](serial_panel.py) · [`serial_core.py`](serial_core.py) | 串口会话面板:只认在跑的 conbroker 服务,断了自动重连,粘贴按行限速;CH9329 所在的口不许开服务。纯逻辑(读 `serial.conf`、conbroker 命令与输出解析、选口、终端字节)在 `serial_core`,不碰 Tk,无界面的服务都用它 |
| [`cart_snap.py`](cart_snap.py) | 采集卡截一帧(推车锁屏时也能拿;不给 `-o` 时输出到 `/run/ops-cart` 下的私有文件) |
| [`cart_ocr.py`](cart_ocr.py) | 截图 OCR(tesseract);退出码 0 找到 / 1 没找到 / 2 出错 |
| [`cart_label.py`](cart_label.py) | TSC 标签机 TSPL 生成器(只出字节流,打印由调用方 `cat > /dev/usb/lp0`):旗形线缆标签(上下对折、下半份旋转 180°)、推车线标、刻度尺、资产标签(PDF → pdftoppm → BITMAP);校准常量集中在文件头;缩写对照表校验见 [`examples/label_abbrev.json`](examples/label_abbrev.json) |
| [`dash_core.py`](dash_core.py) | 状态快照的可测核心:采集(慢项各有节奏)、原子写 `/run/ops-cart/state.json`(带 ts/seq)、过期判定、副屏版面模型;车机也读这份快照 |
| [`dash_service.py`](dash_service.py) · [`dash_render.py`](dash_render.py) | `cart-dash` 常驻进程:每 200 ms 采集,版面变了才重画(Pillow,竖屏画、横屏发),副屏拔了照写快照、3 秒重连;`--once --preview` 导出画面不推屏 |
| [`udisp.py`](udisp.py) · [`udisp_usb.py`](udisp_usb.py) | USB 副屏(xfz1986 / ESP32-P4)协议:16 字节帧头打包、假 zlp 收尾、从 iInterface 读分辨率;传输层走 pyusb,协议层不碰 USB。协议笔记见 [`docs/udisp-protocol.md`](docs/udisp-protocol.md) |
| [`ups_core.py`](ups_core.py) · [`hidpdc.py`](hidpdc.py) | 推车 UPS:`hidpdc` 用标准库解析 HID Power Device 描述符、直读 hidraw(不装 NUT),读不到退回 upower;有 hidraw 就以它的 OL/OB 为准;按百分比判低电量 |
| [`lamps_core.py`](lamps_core.py) | 顶栏虚拟指示灯:每灯盯一个单调计数器(日志大小、网卡字节数、键鼠发送次数),变了就闪;串口操作头 `/dev/con-1~4` 收到 ^G 亮红 |
| [`cart_dream.py`](cart_dream.py) | 推车"做梦":每晚 02:00 把 `CART-LOG.md` 新增部分交给车上的 Claude Code 整理成小结(不给工具、有花费上限、失败不前进),标题显示在锁屏底部 |

| 目录 | 内容 |
|---|---|
| [`tests/`](tests/) | 离线单测,不接任何硬件也能全绿 |
| [`icons/`](icons/) | 顶栏网络标(通外网 / 连上但出不去 / 未连接),部署到 `/opt/pxe/icons/` |
| [`deploy/`](deploy/) | systemd 单元(含 PXE 三件套)、sysctl(含防断电脏页设置)、journald 日志落盘、logrotate / tmpfiles 配置、`kiosk` / `kiosk-setpin` 命令 |
| [`scripts/`](scripts/) | `setup-cart.sh` 铺 PXE 引导文件与仓库(只写救援菜单),`stamp-build.sh` 生成构建号,`smoke-kiosk.sh` 无头冒烟,`check_public_safe.py` 公开仓库脱敏检查;`beeper/` 主板蜂鸣器放旋律(`midi2arp.py` 在开发机上把 MIDI 转曲谱,要 mido) |
| [`docs/`](docs/) | 优化方案、版本部署清单、[副屏协议](docs/udisp-protocol.md)、[公开仓库脱敏约定](docs/PUBLIC_SAFETY.md)、[Windows PE 引导](docs/WINPE.md) |
| [`examples/`](examples/) | 自检计划表、Kickstart、换皮素材样例、标签缩写对照表(只放通用缩写,真实资产名映射留在推车本地) |
| [`plymouth/`](plymouth/) | 开机画面主题 |
| [`windows/`](windows/) | 在 Windows 上跑 PXE 服务端的部署脚本 |

## 快速开始

```bash
# 离线全测(本机什么版本都行,但代码要能在推车的 Python 3.9 上跑,tests/test_py39_compat.py 守着语法)
python3 -m pytest -q

# CH9329 注入
python3 ch9329.py --port /dev/ttyACM0 type "hello from cart"
python3 ch9329.py --port /dev/ttyACM0 combo ctrl alt delete

# PXE 服务端(地址是示例;推车上由 /etc/ops-cart/pxe.env 给出)
python3 pxeserver.py serve --ip 192.0.2.1 --prefix 24 \
    --pool 192.0.2.10,192.0.2.20 --tftp-root /opt/pxe/tftp --iface eno1

# 公开仓库脱敏检查(本地词表放 .public-safe.local,不进仓库)
python3 scripts/check_public_safe.py .

# 截图找字
python3 cart_ocr.py shot.png --find "Boot Menu"
```

## 部署到推车

升级已有推车按 `docs/DEPLOY_<版本>.md` 一步步来(备份、重启顺序、验证、回滚)。从零装:

```bash
scripts/stamp-build.sh                         # 生成 BUILD(提交数 + 哈希)
scp *.py VERSION BUILD cart:/opt/pxe/ && scp -r web icons cart:/opt/pxe/
sudo install -d -m 755 /etc/ops-cart
sudo install -m 644 deploy/pxe.env.example /etc/ops-cart/pxe.env   # 改成现场的装机网段
sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo cp deploy/sysctl.d/* /etc/sysctl.d/ && sudo sysctl --system
sudo mkdir -p /etc/systemd/journald.conf.d && sudo cp deploy/journald.conf.d/* /etc/systemd/journald.conf.d/ && sudo systemctl restart systemd-journald
sudo cp deploy/logrotate.d/ops-cart /etc/logrotate.d/ && sudo cp deploy/tmpfiles.d/ops-cart.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf
sudo cp deploy/bin/kiosk-launcher /usr/local/bin/kiosk
sudo cp deploy/bin/kiosk-setpin /usr/local/bin/ && sudo kiosk-setpin
sudo systemctl enable --now kiosk.service cart-boot-success.timer campus-auth.timer wifi-prefer.timer
# 网页管理口按需 (专用用户 + 预建 web.conf, 见单元注释):
#   sudo useradd -r -s /sbin/nologin ops-web
#   sudo install -o ops-web -g ops-web -m 0600 /dev/null /opt/pxe/web.conf
#   sudo systemctl enable --now webkiosk.service
```

tftp 树由 `scripts/setup-cart.sh` 铺,它只写救援菜单(调 `kiosk_core.py --write-rescue`)。
装机菜单只在车机上解锁装机模式时按 MAC 生成,TFTP / HTTP 两头都会核对装机模式;
所以 Windows 脚本和手工放进 tftp 的自动装机菜单都装不了机,这是故意的。

运行时状态都在 `/run/ops-cart/`(tmpfiles 建,重启推车清空):`pxe-guard.json` 是装机口守卫状态,
`pin-gate.json` 是车机 PIN 输错计数,`mpv.sock` 给 `cart_snap.py` 用。
`pxe-server` 发现装机口上有别的 DHCP 不再停服,转**代理模式**(`pxe-guard.json` 里 `"state": "proxy"`、`"leases": false`):
67 口不发地址,只对 x86-64 UEFI 的 PXE 客户端回 yiaddr=0 的引导 OFFER,4011 / TFTP / HTTP 照常;
LAN1 拔掉再插上会重新探测,没有别人就恢复自己发地址。探测出错或拿不准一律按代理模式。
代理模式要生效,推车的 `PXE_IP` 得在那个网段里(别人发的地址见 `pxe-guard.json` 的 `offered`)。

装机网段只在 `/etc/ops-cart/pxe.env` 里定(`pxe-*` 三个单元和车机生成的 grub 菜单共用),车机界面不再改 LAN1 地址;
`pxe-server` 的 `--iface eno1` 写死在单元里。校园网认证配置在 `/etc/ops-cart/campus-auth.json`(要写 `ssid`)。

开机自检和状态板共用一张计划表 `/opt/pxe/selftest.json`
(照 [`examples/selftest.example.json`](examples/selftest.example.json) 改网卡名、串口、PCI ID、要 ping 的主机),
没有就用内置默认。加检查项:写一个 `kiosk_core.check_xxx(ctx, **参数)` 返回 `(状态, 说明)`,登记进 `CHECKS`。

换皮:把 `branding.json` 和 `logo.png` 放进 `/opt/pxe/branding/`,见 [`examples/branding/`](examples/branding/)。

## CH9329 协议笔记

```
帧:        57 AB ADDR CMD LEN DATA... SUM      校验 = 整帧求和, 含帧头
键盘 0x02: [modifier, 0, key×6]                8 字节
绝对 0x04: [0x02, 按键, xL, xH, yL, yH, 滚轮]   坐标 0~4095
相对 0x05: [0x01, 按键, dx, dy, 滚轮]           位移 int8
```

datasheet 不写"校验含帧头"—— 真机的应答帧会。

## 设计原则

- **车是外设,脑在远端**:采集和注入在车上,OCR 与决策可以放在 tailnet 任意一端
- **物理层优先**:键鼠走 USB、画面走 HDMI、console 走串口,机房网络怎么炸都不断
- **单测覆盖协议,实机验证行为**:GUI 层的 bug 几乎都是实机暴露的,单测绿不代表能跑
- **危险动作前置**:装机口永不进交换机(rogue DHCP,服务端自己也会盯着,看见别人就停)、改管理口必弹警告、
  `clearpart --all` 必亮校验条、备份永远先于写入

## 已实测

- 全自动 PXE 装 Rocky 9.8(UEFI + AMI 固件)
- SC0710 / MS2130 采集预览,CH9329 键鼠透传上屏
- 有线 ↔ 无线自动切换,锐捷 ePortal 自动认证
- Kiosk 实机常驻 + Xvfb 无头冒烟

## License

MIT

## 断电与挪车

推车常被直接拔电挪走,做了几层兜底:盘是带掉电保护的企业级 SSD;脏页 5 秒内落盘;配置一律原子写;
系统日志落盘(`journalctl -b -1` 看上一次断电前);BIOS 设了来电自动开机;GRUB 在上次没正常启动时自动出菜单。
**但能关就关**:挪车前短按一下电源键,十来秒正常关机,不用进菜单。
