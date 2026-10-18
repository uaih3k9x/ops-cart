# ops-cart — 运维推车全家桶

X 光机智能识别盒改造的**硬件级自动化运维推车**:能看(UVC 采集)、能操作(CH9329 键鼠注入)、
能装机(自研 PXE)、能淦交换机(conbroker 串口)。运行于 Rocky Linux 9(推车 `rocky9`),
纯 SSH/终端使用,无桌面依赖(除 Kiosk GUI)。

## 组成

| 文件 | 职责 |
|---|---|
| `pxeserver.py` + `test_pxeserver.py` | 自研 PXE 服务端:DHCP(67) + proxyDHCP(4011) + TFTP(69),单文件跨平台(Win/Linux),21 测 |
| `kiosk.py` | 车机全屏 GUI(Tkinter,无 WM 直跑):状态板 / PXE 开关 / 双网卡配置 / UVC 预览 / 键鼠透传 / 串口会话 / KS 编辑 / 奶龙屏保 |
| `kiosk_core.py` + `test_kiosk_core.py` | Kiosk 可测核心层:服务状态 / nmcli 命令生成 / PXE 配置渲染 / **KS 编辑(读、写、备份轮换、hash、校验)**,19 测 |
| `serial_panel.py` + `test_serial_panel.py` | Kiosk 串口会话面板,后端走 conbroker(子命令在前,root 下以 uaih3k9x 身份跑),6 测 |
| `ks_panel.py` | Kiosk Kickstart 编辑面板(表单改主机名/密码 + 全文编辑 + 备份 + 校验) |
| `ch9329.py` + `test_ch9329.py` | CH9329 键鼠注入(帧 `57 AB … SUM`,校验含帧头;键盘 8 字节 + 鼠标 5 字节报告),12 测 |
| `setup-cart.sh` | 推车部署:挂 ISO → 提取引导文件 → 铺 HTTP 仓库(幂等) |
| `setup.ps1` / `config-net.ps1` / `start-pxe.ps1` / `http-serve.bat` / `http80-serve.bat` | GPD(Windows 备用装机头)侧部署件 |
| `grub.cfg` | PXE 引导菜单(HTTP 快速通道优先,TFTP 兜底) |
| `ks.cfg.example` | Kickstart 模板(**真实 ks.cfg 不进 git**,hash 用 Kiosk 面板或 `openssl passwd -6` 生成) |
| `kiosk-launcher` | `/usr/local/bin/kiosk` 启动器(xinit 直跑,退出回终端,**刻意不自启**) |
| `smoke-kiosk.sh` | Xvfb 无头冒烟测试 |

## 推车部署布局(`/opt/pxe/`)

代码全部 scp 到 `/opt/pxe/`;systemd 三件套(`pxe-server` / `pxe-http-repo` / `pxe-http-boot`)
开机自启;`/usr/local/bin/kiosk` 手动启动;`/usr/local/bin/ch9329.py` CLI。

## 开发循环

```bash
cd ~/code/pxeserver
python3 -m pytest -q                 # 全部测试 (58 个)
# 改完 → scp 到推车 /opt/pxe/ → 实机验证 (GUI 类 bug 单测抓不到, 必须实机)
```

## 工程文档

`~/code/CiderVapourized/推车工程文档.md` — 硬件/网络/PXE 使用流程/风险纪律/采购清单全在内。

## 相关

- conbroker(串口共享+留言板):`~/code/conbroker/`,接入文档在 `接入文档-rocky推车.md`
- 目标机器固件的 PXE 三怪癖(option 60 / proxyDHCP 4011 / grub 走 80 端口拉大 initrd)已内置于 pxeserver.py
