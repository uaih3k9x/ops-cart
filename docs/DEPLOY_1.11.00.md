# 部署清单 1.11.00

从 **1.09.01**（推车上次部署的版本，以 `cat /opt/pxe/VERSION /opt/pxe/BUILD` 为准）升级到 1.11.00。
本清单只写步骤，**由人在现场确认后执行**。`<推车>` 是推车的 ssh 地址，尖括号内容按现场填写，不进仓库。

远程写 root 文件：先 scp 到临时目录，再 `sudo install`。不要把密码 pipe 给 `sudo -S tee`（抢 stdin，文件可能被写空）。

> 怎么得出的：`git diff --name-status 06545fa..a294905`（`06545fa` = VERSION 1.09.01 那个提交，`a294905` = 1.11.00 之后最后一个部署件提交）。
> 推车如果不是 1.09.01（比如还停在 1.08.x），下面"只换改过的"那部分不够用，**照第 3 步整包拷**就不会漏（本清单就是按整包拷写的）。

## 0. 这一版改了什么（相对 1.09.01）

| 版本 | 任务 | 变化 | 对推车的影响 |
|---|---|---|---|
| 1.09.02 | #12 网口模式 | LAN2 填了网关当备用出口（metric 1000，排在无线后面），不再抢主出口 | 换 `netmode_core.py`、`netmode_panel.py` |
| 1.09.02 | #26 副屏开机自检 | 进了自检逐行亮完，至少停 4 秒才切状态板 | 换 `dash_core.py` |
| 1.10.00 | #22 串口写闸门第 2 层 | 决策模型（**默认关**，`serial-gate.json` 的 `model.enabled`） | 新 `serial_gate_model.py`；换 `serial_write_core.py`、`mcp_serial.py`、`cart_dream.py` |
| 1.10.00 | #23 确认 UI | 车机操作头方块：急停红 / 待确认黄闪 / Agent 紫闪，点开确认框；网页只能急停和拒绝 | 新 `confirm_core.py`、`confirm_panel.py`；换 `kiosk.py`、`webkiosk.py`、`web/index.html`、`dash_core.py`、`dash_render.py`；**新单元 `cart-serial-req.path/.service`，tmpfiles 加 `/run/ops-cart/serial-req`，`webkiosk.service` 改了** |
| 1.10.00 | #24 PXE 解锁申请 | Agent 经 MCP 只能申请，人照抄确认码才解锁 | 新 `mcp_pxe.py`、`pxe_req_core.py`、`pxe_req_panel.py`；换 `cart_mcp.py`、`mcp_core.py` |
| 1.11.00 | #27 / #28 主题 | 颜色字体收进 `theme.py`；CDE 深色 + Unifont 16px 点阵，执行器条挪到屏幕底下 | 新 `theme.py`；换 `kiosk*.py`、各 `*_panel.py`、`lamps_core.py`、`dash_render.py`；**要装 Unifont 字体** |
| 1.11.00 | #31 日出日落 | `sun_core` 算深 / 浅，cart-dash 写进快照 `theme`（**界面还不会跟着切**，那是 #32） | 新 `sun_core.py`；可选配 `/etc/ops-cart/theme.json` |
| 1.11.00 | #24 后续 | 网页「PXE 日志」页（只读，按 MAC 分组） | 新 `pxe_log_core.py`；换 `webkiosk.py`、`web/index.html` |
| 1.11.00 后 | 换皮 | 深色界面先找 `branding/logo-dark.png` | 换 `kiosk_core.py`（branding 目录本身不进仓库） |
| 1.11.00 后 | SELinux | 小模块 `ops_cart_mounton`：让 systemd 能遮掉 shadow，webkiosk 在 Enforcing 下才起得来 | **装 SELinux 模块**（第 5 步） |

**1.09.01 之后没改、不用重启的**：`pxeserver.py`、`httpserve.py`（pxe-server / pxe-http-* 三件套）、`kvm_broker*.py` / `ch9329.py`（kvm-broker）、`notify_core.py`、`ups_core.py`。

**单元 / 系统件改动**：

- 新增 `cart-serial-req.path`、`cart-serial-req.service`（网页急停 / 拒绝，#23）；
- `webkiosk.service`：多一行 `ReadWritePaths=-/run/ops-cart/serial-req`；
- `tmpfiles.d/ops-cart.conf`：多一行建 `/run/ops-cart/serial-req`（ops-web 0700；没有 ops-web 用户这一行报错跳过，不影响别的行）；
- 字体：`/usr/local/share/fonts/ops-cart/unifont-18.0.01.otf`（提交 0b063c4）；
- SELinux 模块 `ops_cart_mounton`（提交 a294905）。

## 1. 部署前

开发机：

```bash
cd ~/code/pxeserver
git log -1 --oneline                    # 部署哪个提交就记哪个
python3 -m pytest -q                    # 全绿
python3 scripts/check_public_safe.py .  # 通过
scripts/stamp-build.sh                  # 生成干净 BUILD (不带 dirty), 记下输出
python3 scripts/cart_accept.py --make-manifest scripts/accept-manifest.json   # 每个文件的 sha256, 验收时比对
```

推车上（只读，先留一份升级前的底）：

```bash
ssh <推车>
cat /opt/pxe/VERSION /opt/pxe/BUILD      # 预期 1.09.01; 不是就停下来问 维护者
getenforce; sudo semodule -l | grep ops_cart_mounton    # 待核对: webkiosk 在 1.09.01 时是不是已经因为 226 起不来、有没有人手装过模块
systemctl is-enabled webkiosk cart-mcp kvm-broker cart-notify.timer 2>&1
id ops-web                              # 网页口用户在不在 (决定第 4 步 serial-req 那几行)
rpm -q checkpolicy                      # 第 5 步编译 SELinux 模块要 checkmodule / semodule_package (待核对包名)
```

**执行前现场再确认一次**：推车屏幕没人在用、没有正在装的机器、串口会话没人在用、没有 Agent 拿着串口写租约。
**重启 kiosk 会回到锁屏；`/run/ops-cart` 下的挂起命令和 PXE 申请重启推车才清，重启服务不清。**

## 2. 备份

```bash
ssh <推车>
TS=$(date +%Y%m%d-%H%M)
B=/opt/pxe/.bak-$TS
sudo mkdir -p $B/etc
sudo cp -a /opt/pxe/*.py /opt/pxe/VERSION /opt/pxe/BUILD /opt/pxe/web /opt/pxe/icons $B/
sudo cp -a /etc/systemd/system/webkiosk.service /etc/tmpfiles.d/ops-cart.conf $B/etc/
sudo cp -a /etc/ops-cart $B/etc/ops-cart          # 配置原样留一份 (含 0600 的, 目录权限照旧)
echo $TS                                          # 记下来, 回滚要用
```

## 3. 传文件（开发机上）

整包拷：58 个模块全拷（清单 = `scripts/accept_core.py` 的 `REQUIRED_PY`，由测试保证和 import 关系一致），没改的拷过去也一样。

```bash
cd ~/code/pxeserver
D=/tmp/ops-cart-1.11.00
ssh <推车> mkdir -p $D/systemd $D/accept
scp *.py VERSION BUILD <推车>:$D/
scp -r web icons fonts <推车>:$D/
scp deploy/systemd/webkiosk.service deploy/systemd/cart-serial-req.path deploy/systemd/cart-serial-req.service <推车>:$D/systemd/
scp deploy/tmpfiles.d/ops-cart.conf deploy/selinux/ops_cart_mounton.te <推车>:$D/
scp scripts/cart_accept.py scripts/accept_core.py scripts/accept-manifest.json <推车>:$D/accept/   # 单放, 别混进 /opt/pxe
```

别漏 `tspl_preview.py`（看着像开发机工具，但 `mcp_label` import 它）；`dash_render.py` / `udisp_usb.py` 是 cart-dash 在 venv 里懒加载的，也要在。

## 4. 推车上落位

```bash
ssh <推车>
D=/tmp/ops-cart-1.11.00
cd $D
sudo install -m 644 *.py VERSION BUILD /opt/pxe/
sudo mkdir -p /opt/pxe/web /opt/pxe/icons /opt/pxe/fonts
sudo cp -r web/. /opt/pxe/web/ && sudo cp -r icons/. /opt/pxe/icons/ && sudo cp -r fonts/. /opt/pxe/fonts/

# 单元 + tmpfiles
sudo install -m 644 systemd/webkiosk.service systemd/cart-serial-req.path systemd/cart-serial-req.service /etc/systemd/system/
sudo install -m 644 ops-cart.conf /etc/tmpfiles.d/ops-cart.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf      # 没 ops-web 用户时 serial-req 那行报错, 其余照建
sudo systemctl daemon-reload
```

### 字体（#28，见提交 0b063c4）

只装 OTF（Rocky 9 的 FreeType 2.10.4 读不了 OTB）；**别装 `bitmap-fangsongti-fonts`**（ASCII 全是全角，还会把自己设成全系统默认 monospace）。

```bash
sudo install -D -m 644 /opt/pxe/fonts/unifont-18.0.01.otf /usr/local/share/fonts/ops-cart/unifont-18.0.01.otf
sudo fc-cache -f
fc-list : family | grep -x Unifont          # 有一行 Unifont 才算装上
rpm -q bitmap-fangsongti-fonts              # 应该是 "not installed"
```

装不上也不致命：车机退回 Noto（1.10.00 的样子），开机自检 `UI FONT` 一行变 WARN。

## 5. SELinux 模块（webkiosk 要，提交 a294905）

`webkiosk.service` 的 `InaccessiblePaths=/etc/shadow /etc/gshadow /root` 要 systemd 在 shadow_t 文件上挂载遮挡，
Rocky 9 默认策略不给，Enforcing 下 webkiosk 起不来（`status=226/NAMESPACE`，`avc: denied { mounton }`）。模块只加这一条。

```bash
cd /tmp/ops-cart-1.11.00
checkmodule -M -m -o ops_cart_mounton.mod ops_cart_mounton.te
semodule_package -o ops_cart_mounton.pp -m ops_cart_mounton.mod
sudo semodule -i ops_cart_mounton.pp
sudo semodule -l | grep ops_cart_mounton    # 有这一行
```

`checkmodule` / `semodule_package` 命令不在：`sudo dnf install checkpolicy`（待核对：推车能不能联网装包；不能的话在开发机的 Rocky 9 容器里编好 `.pp` 拷过来，`semodule -i` 只要 `.pp`）。
`getenforce` 是 Permissive / Disabled 时也装上，免得以后切回 Enforcing 网页口起不来。

## 6. 可选配置（不配也能跑）

| 文件 | 不配时 | 配法 |
|---|---|---|
| `/etc/ops-cart/theme.json`（#31） | 一直深色，快照 `theme.setting=auto`、`next_switch=null` | `sudo install -m 644 examples/theme.example.json /etc/ops-cart/theme.json`，**把经纬度改成推车所在地**（样例是格林尼治，坐标不进仓库） |
| `/etc/ops-cart/serial-gate.json`（#21/#22） | 内置默认：华为提示符自动认，只放行 display / show，决策模型不开 | 推车上已有的话对一下新样例（多了 `ruijie` / `linux` 厂商和 `model` 段）；**`model.enabled` 先别开** |
| 网页急停（#23） | 网页「Agent」页的急停 / 拒绝投不出去 | 有 ops-web 用户才开：`sudo systemctl enable --now cart-serial-req.path` |

## 7. 重启（顺序：副屏 → 车机 → 网页口 → MCP）

```bash
sudo systemctl restart cart-dash                 # 新快照字段 theme / agent, 副屏 Unifont
sudo systemctl restart kiosk                     # 会回到锁屏
systemctl is-enabled webkiosk && sudo systemctl restart webkiosk     # 开着才重启
systemctl is-enabled cart-mcp && sudo systemctl restart cart-mcp     # 开着才重启; 只用 stdio 的话每次 ssh 拉起都是新代码
```

不用重启：pxe-server / pxe-http-boot / pxe-http-repo、kvm-broker、cart-notify（timer 每 30 秒新起进程）。
（待核对：推车 BUILD 若比 1.09.01 还旧，这几个也一起重启。）

## 8. 验证

```bash
cd /tmp/ops-cart-1.11.00/accept
sudo python3 cart_accept.py | tee ~/accept-1.11-$(date +%m%d-%H%M).txt
```

脚本只读，逐项 PASS / FAIL / SKIP / 人工。部署相关的这些必须 PASS：VERSION、BUILD、必带 py 模块、静态资源、系统文件、sha256 一致、
`单元 cart-dash.service` / `kiosk.service`、`fc-list 能取到 Unifont`、`SELinux 模块`、`系统 python3 import 全部模块`、`venv import 副屏模块`、
`快照里的版本号`、`深浅模式进快照`。webkiosk 开着的话 `单元 webkiosk.service` 也要 PASS（不是 226）。

再看一眼日志：

```bash
journalctl -u cart-dash -u kiosk -u webkiosk --since "-5 min" -p warning
```

然后照 [`ACCEPT_1.11.md`](ACCEPT_1.11.md) 做人工验收。

## 9. 回滚

```bash
ssh <推车>
TS=<第 2 步记下的时间戳>
B=/opt/pxe/.bak-$TS
sudo cp -a $B/*.py $B/VERSION $B/BUILD /opt/pxe/
sudo rm -rf /opt/pxe/web && sudo cp -a $B/web /opt/pxe/web
sudo install -m 644 $B/etc/webkiosk.service /etc/systemd/system/webkiosk.service
sudo install -m 644 $B/etc/ops-cart.conf /etc/tmpfiles.d/ops-cart.conf
sudo systemctl disable --now cart-serial-req.path 2>/dev/null
sudo rm -f /etc/systemd/system/cart-serial-req.path /etc/systemd/system/cart-serial-req.service
sudo systemctl daemon-reload
sudo systemctl restart cart-dash kiosk
systemctl is-enabled webkiosk && sudo systemctl restart webkiosk
cat /opt/pxe/VERSION    # 回到 1.09.01
```

- 1.11 新加的 `.py`（`theme.py`、`sun_core.py`、`confirm_*.py`、`pxe_req_*.py`、`mcp_pxe.py`、`pxe_log_core.py`、`serial_gate_model.py`）留着无害，旧代码不 import 它们。
- 字体留着无害（旧版车机用 Noto，不认 Unifont）。
- **SELinux 模块别卸**：1.09.01 的 webkiosk.service 里就已经有 `InaccessiblePaths`，卸了 Enforcing 下网页口照样起不来。真要卸：`sudo semodule -r ops_cart_mounton`。
- `/etc/ops-cart/theme.json` 旧版不读，留着无害。
