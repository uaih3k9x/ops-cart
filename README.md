# ops-cart

> 硬件级自动化运维推车 —— 装机、KVM、串口、以及替你蹲机房的那台机器。

一台二手 X 光智能识别盒,改造成了远程运维车:目标机器没系统、没网络、
卡在 BIOS,它都能到场处理。**人在被窝,车在机房。**

系统全部自研:一个跨平台 PXE 服务端、一套带键鼠透传的车机界面、
一个 CH9329 注入协议栈、以及串口会话面板。零厂商依赖,
零闭源驱动,零 Windows。

```
目标机器 ◄── HDMI ── 采集 ────┐
目标机器 ◄── USB ─── CH9329 ──┤ 推车 (Linux) ── tailnet ──► 你 (任意位置)
交换机   ◄── 串口 ── FT232R ──┘
```

## 组成

| 文件 | 职责 |
|---|---|
| [`pxeserver.py`](pxeserver.py) | PXE 服务端:DHCP(67) + proxyDHCP(4011) + TFTP(69),单文件,Windows/Linux 通用 |
| [`kiosk.py`](kiosk.py) | 车机 GUI(Tkinter,无 WM 直跑):状态板、双网卡配置、UVC 预览、键鼠透传(VMware 式 Ctrl+Alt 释放)、串口会话、KS 编辑、屏保 |
| [`kiosk_core.py`](kiosk_core.py) | GUI 可测核心层:nmcli 命令生成、服务状态、PXE 配置渲染、Kickstart 编辑引擎 |
| [`serial_panel.py`](serial_panel.py) | 串口会话面板(人在不在都能操作 console) |
| [`ks_panel.py`](ks_panel.py) | Kickstart 编辑面板:主机名/root 密码表单化,保存自动轮换备份,破坏性指令前置校验 |
| [`ch9329.py`](ch9329.py) | CH9329 键鼠注入协议栈:键盘 8 字节 / 鼠标 5 字节报告,组合键与长按 |
| `test_*.py` | **58 个测试**,协议层全部离线可测,不碰硬件也能全绿 |

## 快速开始

```bash
# 离线全测(无需任何硬件)
python3 -m pytest -q

# CH9329 注入(串口线 + CH9329 板)
python3 ch9329.py --port /dev/ttyACM0 info
python3 ch9329.py --port /dev/ttyACM0 type "hello from cart"
python3 ch9329.py --port /dev/ttyACM0 combo ctrl alt delete

# PXE 服务端(目标机器网线直插,全自动装 Rocky 9)
python3 pxeserver.py serve --ip 192.168.77.1 \
    --pool 192.168.77.10,192.168.77.20 --tftp-root /opt/pxe/tftp
```

推车整机布局:代码部署在 `/opt/pxe/`,systemd 三件套
(`pxe-server` / `pxe-http-repo` / `pxe-http-boot`)开机自启;
终端键入 `kiosk` 进入车机界面。
开机自检的项目在 `/opt/pxe/selftest.json` 里配(照 [`selftest.example.json`](selftest.example.json) 改网卡名、串口、PCI 卡 ID、要 ping 的主机),
没这个文件就用内置的默认七项;新检查写成 `kiosk_core.check_xxx(ctx, **参数)` 再登记进 `CHECKS`。同一套代码亦可在 Windows 上跑
(`setup.ps1` / `start-pxe.ps1`),单文件跨平台是设计目标之一。

## 协议栈

```
CH9329 帧:  57 AB ADDR CMD LEN DATA... SUM    —— 校验为整帧求和, 含帧头
键盘报告:   [modifier, 0, key×6]              —— 8 字节
鼠标报告:   [buttons, dx, dy, wheel, 0]      —— 5 字节, 相对位移 int8
```

datasheet 不写"校验含帧头"这件事 —— 真机的应答帧会。

## 设计原则

- **车是外设,脑在远端**:采集和注入在车上,OCR 与决策可以放在
  tailnet 任意一端。断网时本地 tesseract 降级兜底,不是瘫。
- **三层物理优先**:键鼠走 USB、画面走 HDMI、console 走串口 ——
  这三条不依赖任何网络,机房网络怎么炸都不断。
- **单测覆盖协议,实机验证行为**:今天所有 GUI 层 bug(参数顺序、
  前后台混淆、先清后取)都是实机暴露的,单测绿不代表能跑。
- **危险动作前置**:Lan1 永不进交换机(rogue DHCP)、改管理口必弹
  警告、`clearpart --all` 必亮校验条、备份轮换永远先于写入。

## 已实测

- 全自动 PXE 装 Rocky 9.8(目标机器:UEFI + AMI 固件,含全部三怪癖)
- CH9329 打字/组合键注入上屏
- Kiosk Xvfb 无头冒烟 + 实机运行
- Kickstart 编辑面板:改主机名、root 密码 hash、备份与恢复

## License

MIT
