# 部署清单 1.16.00

从 **1.15.01**（BUILD `304 f6541fd`，以推车上 `cat /opt/pxe/VERSION /opt/pxe/BUILD` 为准）升级到 1.16.00。
本清单只写步骤，**由人在现场确认后执行**。`<推车>` 是推车的 ssh 地址，尖括号内容按现场填写，不进仓库。

这一版把第 3–7 步收进了一个脚本 `scripts/install-1.16.00.sh`（部署包里叫 `install.sh`），在推车上一条命令装完：

```bash
sudo bash /tmp/deploy-1.16.00/install.sh
```

下面逐步写清楚脚本做了什么，脚本跑不了时照着手工做。

> 怎么得出的：`git diff --name-status f6541fd..<release/1.16.00 的提交>`。部署前对过推车：`/opt/pxe` 下 66 个文件（`*.py` 和 `VERSION`）的 sha256 和 `f6541fd` 完全一致，没有现场手改。
> 推车如果不是 1.15.01，脚本会停下（`FORCE=1` 才跳过检查）。代码是整包拷的，旧版本也不会漏文件，但单元和配置的变化只按 1.15.01 → 1.16.00 写。

## 0. 这一版改了什么（相对 1.15.01）

| PR | 变化 | 对推车的影响 |
|---|---|---|
| #66 | dash_service 测试不再偶发失败 | 只改测试 |
| #84 | 副屏红条不再压住昨夜整理；车机开关按钮显示当前状态 | 换 `dash_render.py`、`kiosk*.py`、各 `*_panel.py` |
| #60 | MCP 按档位启动，只读档不注册动手工具 | 换 `cart_mcp.py`、`mcp_*.py`；**新单元 `cart-mcp-ro.service`（默认不开）**；`cart-mcp.service` 加 `TasksMax=64` |
| #77 | 远程审批第一阶段：服务端纯逻辑 | 新 `approval_core.py`、`approval_channel.py`、`totp_core.py`，**这一阶段不接线、不部署**（拷过去也没人 import） |
| #57 | 串口巡检补 H3C / 锐捷，默认厂商改成 `auto` | 换 `survey_core.py`、`serial_write_core.py`；登记成 ruijie 的槽位第 1 层放行 show |
| #67 | 键鼠闸门修绕过：逐行、逐键、secret 计入 | 换 `mcp_kvm.py`、`kvm_broker*.py`、`confirm_*.py` |
| #68 | 串口挂起期间拒绝 Agent 直发，批准绑定发送序号 | 换 `serial_write_core.py`、`mcp_serial.py`、`confirm_*.py` |
| #72 | 车机中文输入（librime + Tk 候选栏），只接网络唤醒的"名字"框 | 新 `ime_*.py`；**推车上没有 librime，切中文显示"英·无中文"，见第 6 步** |
| #76 | PXE 默认菜单超时从本地盘启动，救援只能手选 | 换 `pxeserver.py`、`kiosk_core.py`；**要重写 `tftp/grub.cfg`（第 5 步）** |
| #61 | 蓝牙隔空打字·推车侧（BLE GATT 收文字排队，不执行命令） | 新 `ble_input.py`、`ble_input_core.py`；**新单元 `cart-ble-input.service`（默认不开）** |
| #63 | 蓝牙隔空打字·安卓 App | 只有 `android/`，推车上不用 |
| #78 | 串口写闸门第 1 层白名单·影子期：只记录 `gate.shadow`，放行不变 | 新 `serial_allowlist.py`；`serial-gate.json` 可选 `allowlist.disable` |
| #91 | 测试数据避开禁用词 | 只改测试 |
| #74 | 串口按键闸门：设备停在确认类提示（`[Y/N]`、`Are you sure`、以问号收尾……）上、或读不到最后一行时，Agent 发回车 / 空格要人确认 | 换 `serial_write_core.py`、`mcp_serial.py`、`confirm_core.py`、`confirm_panel.py`、`kiosk.py`；确认框多一块"设备最后一行（不可信）"。**没有新单元、新配置，也不用迁移**；靠重启 kiosk 生效（MCP stdio 每次新起进程，cart-mcp 开着也会重启） |

**单元 / 系统件改动**（`git diff f6541fd -- deploy/`）：

- 新增 `cart-mcp-ro.service`、`cart-ble-input.service`，**都只装不开**；
- `cart-mcp.service`：多一行 `TasksMax=64`；
- `tmpfiles.d`、firewalld、SELinux、字体都没变。

**依赖**（2026-11-08 只读查过推车，Rocky 9.8，Python 3.9.25）：

| 依赖 | 给谁 | 推车上 | 怎么办 |
|---|---|---|---|
| `python3-dbus`、`python3-gobject` | #61 `ble_input` | 已装（1.2.18 / 3.40.1），`import dbus, gi` 正常 | 不用动 |
| BlueZ | #61 | 已装 5.87，`bluetooth.service` 开着，有 `hci0` | 不用动；`main.conf` 的 `SecureConnections` 还是注释掉的默认值（见第 6 步） |
| librime + 词库 | #72 车机中文输入 | **没装**；Rocky 9 / EPEL 9 的源里都没有 `librime`、`rime-data` | 这次不装，车机退回英文（设计如此）。要装另开任务：自己编 librime（依赖 boost、yaml-cpp、leveldb、marisa、opencc、glog，基本都在 EPEL）或拿 Fedora 的 SRPM 重编，词库放 `/usr/share/rime-data`（见 [`IME_RIME.md`](IME_RIME.md)） |
| `systemd-run` | #72 起 librime 子进程 | 有（systemd 252） | 不用动 |

推车能上外网，dnf 的源可用（用户态查询要 `--disablerepo=tailscale-stable`，那个源的 GPG 在普通用户下验不过，和这次部署无关）。

## 1. 部署前

开发机（在 `release/1.16.00` 上）：

```bash
git log -1 --oneline                    # 部署哪个提交就记哪个
python3 -m pytest -q                    # 全绿
python3 scripts/check_public_safe.py .  # 通过
scripts/stamp-build.sh                  # BUILD 必须是真实提交哈希, 不带 -dirty
python3 scripts/cart_accept.py --make-manifest scripts/accept-manifest.json
```

推车上（只读）：

```bash
cat /opt/pxe/VERSION /opt/pxe/BUILD                 # 预期 1.15.01 / 304 f6541fd
ls /opt/pxe/tftp/grub.cfg-* 2>/dev/null             # 应该没有; 有 = 有机器正解锁着, 先上锁 (脚本也会拦)
systemctl is-active kiosk cart-dash cart-mcp webkiosk kvm-broker pxe-server
```

**执行前现场再确认一次**：推车屏幕没人在用、没有正在装的机器、串口会话没人在用、没有 Agent 拿着串口写租约或键鼠租约。
脚本会重启 kiosk（回到锁屏）、kvm-broker（键鼠租约丢）、pxe-server 和两个 HTTP 服务（正在 PXE 的机器会断）。

## 2. 备份（脚本第 1 段）

```bash
TS=$(date +%Y%m%d-%H%M%S)
B=/opt/pxe/.bak-$TS
sudo mkdir -p $B/etc/systemd $B/tftp
sudo cp -a /opt/pxe/*.py /opt/pxe/VERSION /opt/pxe/BUILD /opt/pxe/web /opt/pxe/icons $B/
sudo cp -a /opt/pxe/tftp/grub.cfg $B/tftp/
sudo cp -a /etc/systemd/system/cart-mcp.service $B/etc/systemd/
sudo cp -a /etc/ops-cart $B/etc/ops-cart          # 配置原样留一份 (含 0600 的)
```

脚本同时在 `$B/rollback.sh` 写好回滚脚本（第 9 步），出错时直接打印这条回滚命令。

## 3. 部署包（开发机上打，传到推车 `/tmp`）

推车没有 rsync，用 tar 管道。

```bash
P=$(mktemp -d)/deploy-1.16.00
mkdir -p $P/opt $P/systemd $P/accept
cp *.py VERSION BUILD $P/opt/ && cp -r web icons $P/opt/
cp deploy/systemd/cart-mcp.service deploy/systemd/cart-mcp-ro.service deploy/systemd/cart-ble-input.service $P/systemd/
cp scripts/cart_accept.py scripts/accept_core.py scripts/accept-manifest.json $P/accept/
cp scripts/install-1.16.00.sh $P/install.sh
mkdir -p $P/tk-test && cp tests/conftest.py tests/test_confirm_panel.py $P/tk-test/   # 第 8 步车机真屏跑
tar -C $(dirname $P) -cf - deploy-1.16.00 | ssh <推车> 'rm -rf /tmp/deploy-1.16.00 && tar -C /tmp -xf -'
```

包的布局：`opt/`（落到 `/opt/pxe`）、`systemd/`、`accept/`（单放，别混进 `/opt/pxe`）、`tk-test/`、`install.sh`。

## 4. 落位（脚本第 2–4 段）

```bash
D=/tmp/deploy-1.16.00
sudo install -m 644 $D/opt/*.py $D/opt/VERSION $D/opt/BUILD /opt/pxe/
sudo cp -r $D/opt/web/. /opt/pxe/web/ && sudo cp -r $D/opt/icons/. /opt/pxe/icons/
sudo install -m 644 $D/systemd/cart-mcp.service $D/systemd/cart-mcp-ro.service $D/systemd/cart-ble-input.service /etc/systemd/system/
sudo systemctl daemon-reload
```

`/etc/ops-cart` **一个文件都不写**：pxe.env、pet-skin.json、serial.conf、kvm.json、mcp.json、serial-gate.json 都保留原样，这一版也没有必需的新配置。

## 5. 重写 PXE 默认菜单（脚本第 5 段，#76）

```bash
sudo python3 /opt/pxe/kiosk_core.py --write-rescue --root /opt/pxe/tftp
grep -n "Boot from local disk" /opt/pxe/tftp/grub.cfg     # 第 0 项是本地盘, 超时 10 秒
```

命令名还叫 `--write-rescue`（没改名），写出来的已经是"超时本地盘、救援手选"的新菜单。它会顺手删掉所有按 MAC 解锁的菜单，所以有机器正解锁着时脚本直接停。

## 6. 默认不开的东西（要用再手工开）

| 单元 / 功能 | 不开时 | 开法 |
|---|---|---|
| `cart-mcp-ro.service`（#60） | 只读档 HTTP 不起；stdio 默认档照旧 | 先在 `/etc/ops-cart/mcp.json` 加 `readonly` 段（端口默认 8766，令牌和动手档分开，见 `examples/mcp.example.json`），再 `sudo systemctl enable --now cart-mcp-ro` |
| `cart-ble-input.service`（#61） | 蓝牙不广播，不收文字 | 按 [`BLE_INPUT.md`](BLE_INPUT.md)：建议先在 `/etc/bluetooth/main.conf` 写 `SecureConnections = only`；手机配对、在车机上 `trust`；再 `sudo systemctl enable --now cart-ble-input`。队列文字现在还没有取件方 |
| 车机中文输入（#72） | 按 Shift 显示"英·无中文"，一直英文 | 装 librime + 词库（见第 0 节依赖表），不用改单元 |
| 串口白名单影子期（#78） | 只记 `gate.shadow`，放行规则不变 | 不用开；要禁某条用 `serial-gate.json` 的 `allowlist.disable` |

**待核对**：推车 `/etc/ops-cart/serial-gate.json` 里如果还有旧样例的 `ping` / `tracert` / `traceroute` 追加动词，新样例已经不再开它们（参数没上限）。部署不改这个文件，要不要删由维护者定（`sudo cat` 看一眼）。

## 7. 重启（脚本第 6 段，开着的才重启）

```bash
for s in cart-dash kiosk webkiosk kvm-broker pxe-server pxe-http-boot pxe-http-repo cart-mcp; do
    systemctl is-active --quiet $s && sudo systemctl restart $s
done
```

- cart-dash：`dash_render.py`（#84）；kiosk：车机界面、IME、确认框；webkiosk：`web/index.html`；
- kvm-broker：键鼠闸门（#67）；pxe-server / pxe-http-*：`pxeserver.py`、`httpserve.py`；cart-mcp：MCP 档位（#60）。
- 不用重启：cart-ocr（`cart_ocr.py` 没变）、cart-notify / cart-dream / cart-boot-success（timer 每次新起进程）、cart-serial-req（path 触发的 oneshot）。

脚本重启完等 3 秒，开机自启的单元有没起来的就停下，打印回滚命令。

## 8. 验证

脚本最后自己跑一遍验收，报告存到 `/var/log/ops-cart/accept-1.16.00-<时间戳>.txt`；验收 FAIL 不会自动回滚。手工重跑：

```bash
cd /tmp/deploy-1.16.00/accept
sudo python3 cart_accept.py --expect-version 1.16.00 --expect-build "$(cat ../opt/BUILD)"
journalctl -u kiosk -u cart-dash -u webkiosk -u kvm-broker -u pxe-server -u cart-mcp --since "-5 min" -p warning
```

必须 PASS：VERSION、BUILD、必带 py 模块、静态资源、系统文件、sha256 一致、各个开着的单元、系统 python3 import 全部模块、MCP 工具表。
`cart-mcp-ro` / `cart-ble-input` 应该是"默认不开"。

人工看（车边）：

- [ ] 车机回到锁屏，解锁后界面正常；网络唤醒面板的"名字"框按 Shift 显示"英·无中文"（librime 没装，正常）。
- [ ] 车机开关按钮（UVC 预览 / 串口会话 / PXE）文字跟着开关变（#84）；副屏红条不压昨夜整理（#84）。
- [ ] 被测机 PXE 进默认菜单，不按键等 10 秒，从本地盘启动；没有回到 PXE 循环，也没停在固件设置界面（#76，多试几个品牌）。
- [ ] 手选 Rescue shell（HTTP / TFTP）还能进救援（#76）。
- [ ] 串口发一条命令后，`serial-slot<N>.log` 里有 `gate.shadow`（#78）。
- [ ] 车机确认框里挂起项带设备上下文、带 `<secret>` 的行显示正常，不挤掉"确认发送"按钮（#67 #68）。
- [ ] 键鼠经 MCP 操作 BIOS 启动菜单（F12 + 上下键 + 回车）不会被挂起（#67）。
- [ ] 串口确认框里有"设备最后一行（不可信）"这一块（灰字），"确认发送"按钮没被挤掉（#74）。
- [ ] 串口设备停在 `[Y/N]` / `Destination filename [...]?` 这类提示上时，Agent 发回车被挂起；停在 `---- More ----` 上时空格照常翻页（#74）。

确认框的 Tk 测试（#74 / #65）在车机真屏上跑一次。包里 `tk-test/` 带了 `test_confirm_panel.py` 和 `conftest.py`。会在车机屏幕上短暂开关几个窗口，没人用车机时跑：

```bash
cd /tmp/deploy-1.16.00/tk-test
sudo env DISPLAY=:0 XAUTHORITY=/root/.Xauthority PYTHONPATH=/opt/pxe python3 -m pytest -q -p no:cacheprovider test_confirm_panel.py
```

- `OPS_TK_TESTS=1` 只是 macOS 上的开关（`tests/conftest.py`），推车是 Linux，不用设，有 `DISPLAY` 就真跑；没有显示会全部 skip，**skip 不算过**。
- 推车的 pytest 是 6.2.2，不认 `pytest.ini` 的 `pythonpath`，所以用 `PYTHONPATH=/opt/pxe` 测装好的代码。
- 待核对：kiosk 是 root 用 `xinit` 起的 X，授权文件在不在 `/root/.Xauthority`。连不上显示就 `sudo ls -a /root` 找 `.serverauth.*`，或者 `sudo xauth list` 看看。

## 9. 回滚

```bash
sudo bash /opt/pxe/.bak-<时间戳>/rollback.sh
```

它做的事：

```bash
B=/opt/pxe/.bak-<时间戳>
sudo cp -a $B/*.py $B/VERSION $B/BUILD /opt/pxe/
sudo rm -rf /opt/pxe/web /opt/pxe/icons && sudo cp -a $B/web $B/icons /opt/pxe/
sudo cp -a $B/tftp/grub.cfg /opt/pxe/tftp/grub.cfg                     # 回到默认救援菜单
sudo systemctl disable --now cart-mcp-ro cart-ble-input 2>/dev/null
sudo rm -f /etc/systemd/system/cart-mcp-ro.service /etc/systemd/system/cart-ble-input.service
sudo install -m 644 $B/etc/systemd/cart-mcp.service /etc/systemd/system/cart-mcp.service
sudo systemctl daemon-reload
# 重启第 7 步那几个开着的服务
cat /opt/pxe/VERSION    # 回到 1.15.01
```

- 1.16 新加的 `.py`（`ime_*`、`ble_input*`、`serial_allowlist`、`approval_*`、`totp_core`）留着无害，旧代码不 import 它们。
- `/etc/ops-cart` 部署时没动，回滚也不用动；`$B/etc/ops-cart` 是保险。
