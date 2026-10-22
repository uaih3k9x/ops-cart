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

软件全部自研,只用 Python 标准库 + Tkinter,跑在车上的东西不装第三方包。

## 能干什么

- **装机**:自研 PXE 服务端(DHCP + proxyDHCP + TFTP),目标机器网线直插即全自动装 Rocky 9;
  按 MAC 下发,只有"装机模式"倒计时内的那台会被装,其他机器只能进救援
- **KVM**:HDMI 采集低延迟预览,CH9329 透传键鼠(绝对鼠标,VMware 式 Ctrl+Alt 释放);
  开机自动选有信号的画面源
- **串口**:串口 console 面板,交换机/防火墙/服务器在网络炸了的时候也够得着
- **车机界面**:无 WM 直跑的全屏 Kiosk —— 街机风开机自检、实时状态板、右侧二级菜单、PIN 锁屏
- **网页版 Kiosk**:同一套状态与串口经浏览器远程访问,tailnet 永远放行,局域网需开关 + PIN
- **联网**:有线优先、无线兜底(拔网线约 17 秒切换);内置无线管理面板,
  校园网锐捷 ePortal 无浏览器自动认证
- **OCR**:截图识字带坐标,给"看屏幕 → 找按钮 → 点"的自动化留接口

## 代码地图

车上运行的模块都在根目录,部署时平铺进 `/opt/pxe/`。

| 文件 | 职责 |
|---|---|
| [`pxeserver.py`](pxeserver.py) | PXE 服务端,单文件,Linux / Windows 通用 |
| [`httpserve.py`](httpserve.py) | PXE 用的静态 HTTP(引导文件 :80、仓库与 ks :8000);LAN1 没插线就等地址出现,不退出 |
| [`kiosk.py`](kiosk.py) | 车机 GUI(Tkinter) |
| [`kiosk_core.py`](kiosk_core.py) | GUI 的可测核心:自检计划表与检查函数、nmcli 命令、PXE 配置、菜单定义 |
| [`serial_panel.py`](serial_panel.py) | 串口会话面板 |
| [`ks_panel.py`](ks_panel.py) | Kickstart 编辑:表单化改主机名/密码,自动备份,破坏性指令校验 |
| [`wifi_panel.py`](wifi_panel.py) · [`wifi_core.py`](wifi_core.py) | 无线管理:扫描、连接、显示密码、优先级排序;面板底部改校园网认证账号、立即认证、看最近结果 |
| [`wifi_prefer.py`](wifi_prefer.py) | 定时让优先级真正生效:看得见更高优先级的已保存网络就切过去,失败冷却 30 分钟 |
| [`netprof_panel.py`](netprof_panel.py) · [`netprof_core.py`](netprof_core.py) | 网口配置文件:LAN1/LAN2 切换、一键回退、静态地址模板;发地址只在 LAN1,启用前探测对面有没有 DHCP |
| [`wol_panel.py`](wol_panel.py) · [`wol_core.py`](wol_core.py) | 网络唤醒:常用目标清单 + 临时输 MAC,从选定网口广播魔术包 |
| [`atomicfile.py`](atomicfile.py) | 防断电写文件:临时文件 → fsync → 改名,配置要么旧要么新 |
| [`cartlog.py`](cartlog.py) | 日志分层:机器事件进 `/var/log/ops-cart/`(logrotate 轮转),`CART-LOG.md` 只收一行摘要 |
| [`webkiosk.py`](webkiosk.py) · [`web_core.py`](web_core.py) · [`web/`](web/) | 网页版 Kiosk(标准库 http.server + xterm.js) |
| [`ruijie_portal.py`](ruijie_portal.py) | 锐捷 ePortal 自动登录,只在连着 CAMPUSNET 时跑;结果写 `/run/ops-cart/campusnet-status.json` 给面板和状态板 |
| [`ch9329.py`](ch9329.py) | CH9329 键鼠注入协议栈,也可当命令行工具用 |
| [`cart_ocr.py`](cart_ocr.py) | 截图 OCR(tesseract) |

| 目录 | 内容 |
|---|---|
| [`tests/`](tests/) | 离线单测,不接任何硬件也能全绿 |
| [`deploy/`](deploy/) | systemd 单元(含 PXE 三件套)、sysctl(含防断电脏页设置)、journald 日志落盘、logrotate / tmpfiles 配置、`kiosk` / `kiosk-setpin` 命令 |
| [`scripts/`](scripts/) | `setup-cart.sh` 铺 PXE 引导文件与仓库,`stamp-build.sh` 生成构建号,`smoke-kiosk.sh` 无头冒烟 |
| [`examples/`](examples/) | 自检计划表、Kickstart、换皮素材样例 |
| [`plymouth/`](plymouth/) | 开机画面主题 |
| [`windows/`](windows/) | 在 Windows 上跑 PXE 服务端的部署脚本 |

## 快速开始

```bash
# 离线全测
python3 -m pytest -q

# CH9329 注入
python3 ch9329.py --port /dev/ttyACM0 type "hello from cart"
python3 ch9329.py --port /dev/ttyACM0 combo ctrl alt delete

# PXE 服务端
python3 pxeserver.py serve --ip 192.168.200.1 \
    --pool 192.168.200.10,192.168.200.20 --tftp-root /opt/pxe/tftp

# 截图找字
python3 cart_ocr.py shot.png --find "Boot Menu"
```

## 部署到推车

```bash
scripts/stamp-build.sh                         # 生成 BUILD(提交数 + 哈希)
scp *.py VERSION BUILD cart:/opt/pxe/ && scp -r web cart:/opt/pxe/
sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo cp deploy/sysctl.d/* /etc/sysctl.d/ && sudo sysctl --system
sudo mkdir -p /etc/systemd/journald.conf.d && sudo cp deploy/journald.conf.d/* /etc/systemd/journald.conf.d/ && sudo systemctl restart systemd-journald
sudo cp deploy/logrotate.d/ops-cart /etc/logrotate.d/ && sudo cp deploy/tmpfiles.d/ops-cart.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf
sudo cp deploy/bin/kiosk-launcher /usr/local/bin/kiosk
sudo cp deploy/bin/kiosk-setpin /usr/local/bin/ && sudo kiosk-setpin
sudo systemctl enable --now kiosk.service cart-boot-success.timer campusnet-auth.timer wifi-prefer.timer
```

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
- **危险动作前置**:装机口永不进交换机(rogue DHCP)、改管理口必弹警告、
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
