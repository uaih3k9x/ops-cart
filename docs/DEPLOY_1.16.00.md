# 部署清单 1.16.00

从 **1.15.01**（BUILD `304 f6541fd`，以推车上 `cat /opt/pxe/VERSION /opt/pxe/BUILD` 为准）升级到 1.16.00。
本清单只写步骤，**由人在现场确认后执行**。`<推车>` 是推车的 ssh 地址，尖括号内容按现场填写，不进仓库。

这一版把第 3–7 步收进了一个脚本 `scripts/install-1.16.00.sh`（部署包里叫 `install.sh`），在推车上一条命令装完：

```bash
sudo systemd-run --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.00/install.sh
journalctl -fu ops-cart-deploy          # 看进度; Ctrl-C 只停看日志, 不停安装
```

用 `systemd-run` 跑，安装进程不挂在 ssh 会话上，ssh 断了也照样跑完（推车上没有 tmux / screen；systemd 252 支持 `--collect`，已在推车上核对过）。
脚本**没有任何交互**，全程不用敲键。万一还是被打断（`systemctl stop ops-cart-deploy`、直接 `sudo bash` 跑时 ssh 断线或按 Ctrl-C），它会打印回滚命令。
再跑一次时，只要推车已经是 1.16.00，或者已经有这次的备份记录（`.bak-*/deploy.marker`），脚本就停下并给出那次的回滚命令，**不会叠着装**。
跑完看结果：`journalctl -u ops-cart-deploy --no-pager | tail -40`（`--collect` 只卸掉单元，日志还在）。

下面逐步写清楚脚本做了什么，脚本跑不了时照着手工做。

> 怎么得出的：`git diff --name-status f6541fd..<release/1.16.00 的提交>`。部署前对过推车：`/opt/pxe` 下 66 个文件（`*.py` 和 `VERSION`）的 sha256 和 `f6541fd` 完全一致，没有现场手改。
> 推车如果不是 1.15.01，脚本会停下。要跳过这道检查，用 `sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.00/install.sh`。代码是整包拷的，旧版本也不会漏文件，但单元和配置的变化只按 1.15.01 → 1.16.00 写。

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
systemctl is-active kiosk cart-dash cart-mcp webkiosk kvm-broker pxe-server pxe-http-boot pxe-http-repo
ls /etc/systemd/system/cart-mcp-ro.service /etc/systemd/system/cart-ble-input.service 2>/dev/null   # 应该没有 (脚本也会拦)
```

**执行前现场再确认一次（人工，脚本不查）**：**没有正在装机或正在 PXE 的机器**（脚本只拦"有 MAC 解锁着"，查不出别的 PXE 访问）、推车屏幕没人在用、串口会话没人在用、没有 Agent 拿着串口写租约或键鼠租约。
脚本会重启 kiosk（回到锁屏）、kvm-broker（键鼠租约丢）、pxe-server 和两个 HTTP 服务（正在 PXE 的机器会断）。

## 2. 备份（脚本第 1 段）

```bash
TS=$(date +%Y%m%d-%H%M%S)
B=/opt/pxe/.bak-$TS
sudo mkdir -p $B/etc/systemd $B/tftp
sudo cp -a /opt/pxe/*.py /opt/pxe/VERSION /opt/pxe/BUILD /opt/pxe/web /opt/pxe/icons $B/
( cd /opt/pxe/tftp && sudo find . -name 'grub.cfg*' -type f ) | while read -r f; do   # 菜单整批, 含 EFI/BOOT 下的
    sudo mkdir -p "$B/tftp/$(dirname "$f")" && sudo cp -a "/opt/pxe/tftp/$f" "$B/tftp/$f"
done
[ -f /opt/pxe/pxe-mode.json ] && sudo cp -a /opt/pxe/pxe-mode.json $B/
sudo cp -a /etc/systemd/system/cart-mcp.service $B/etc/systemd/
sudo cp -a /etc/ops-cart $B/etc/ops-cart          # 配置原样留一份 (含 0600 的)
```

脚本在备份前先记下第 7 步那几个服务里**当时开着的**，写进 `$B/rollback.sh`（第 9 步）。出错停下时（命令失败、脚本自己判的失败、被信号打断）都会打印这条回滚命令。
同一目录里还写一个 `deploy.marker`（`from` / `to` / `build`），用来认出"上次装到一半"；回滚成功后它会被改名成 `deploy.marker.rolled-back`，之后可以再装。

任何改动之前，脚本先按 `accept/accept-manifest.json` 核对包里每个文件的 sha256，再核对包里的 `BUILD` 和 manifest 的 `build` 是否一致。对不上就停。
- 它还会检查 manifest 的 `files` 不能为空，`/opt/pxe` 条目不能少于 60 个，包里的 3 个单元都得列在 manifest 里。
- 核对时先打印 manifest 自己的 sha256。
- 包里有、manifest 没列的文件会列成警告，照样装，不核对。这一版会看到 `approval_core.py`、`approval_channel.py`、`totp_core.py`（#77 还没接线，不在验收清单里），属于正常。
- 内联的 python 用 `python3 -I` 跑，不受包目录和环境变量影响。

`deploy.marker` 在 `rollback.sh` 写好之后才落，所以有 marker 就一定有能用的回滚。备份做到一半（`rollback.sh` 还没写好）就出错的话，推车上的文件还没动，半截备份会被删掉。
装完（重启检查通过）后，marker 里会追加 `done=1`。之后再被打断，就只是验收没跑完，脚本会提示"已经装完，只是验收被打断"，不用回滚。重跑会提示"已经装完了"。
它还会检查 `cart-mcp-ro.service`、`cart-ble-input.service` 是否已经存在（1.15.01 上不该有），存在也停。

备份前脚本还会检查部署包齐不齐：3 个单元、`accept/cart_accept.py` 等都要在，包里 `VERSION` 必须是 1.16.00。缺了就停，什么都不动。版本检查要跳过，用 `sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.00/install.sh`（FORCE 只跳过 from 版本检查，跳不过"上次装到一半"那道）。别用 `sudo -E`，它会把开发机上测试用的 `OPT` / `SYS` / `ETC` / `LOG` 变量带进 root 环境。

## 3. 部署包（开发机上打，传到推车 `/tmp`）

推车没有 rsync，用 tar 管道。

```bash
OUT=$(mktemp -d)
scripts/pack-1.16.00.sh $OUT          # 工作区不干净就拒绝; 生成 BUILD 和 manifest; 只打 git ls-files 里的文件
ssh <推车> 'test -e /tmp/deploy-1.16.00 && mv /tmp/deploy-1.16.00 /tmp/deploy-1.16.00.old-$(cut -d" " -f2 /tmp/deploy-1.16.00/opt/BUILD) || true'
COPYFILE_DISABLE=1 tar -C $OUT -cf - deploy-1.16.00 | ssh <推车> 'tar -C /tmp -xf -'
```

旧包挪走保留，不删。`opt/` 里只有 git 跟踪的顶层 `*.py`、`VERSION`、`web/`、`icons/`，再加生成的 `BUILD`。

包的布局：`opt/`（落到 `/opt/pxe`）、`systemd/`、`accept/`（单放，别混进 `/opt/pxe`）、`tk-test/`、`install.sh`。

## 4. 落位（脚本第 2–4 段）

```bash
D=/tmp/deploy-1.16.00
sudo install -m 644 $D/opt/*.py $D/opt/VERSION $D/opt/BUILD /opt/pxe/
sudo cp -r $D/opt/web/. /opt/pxe/web/ && sudo cp -r $D/opt/icons/. /opt/pxe/icons/
sudo install -m 644 $D/systemd/cart-mcp.service $D/systemd/cart-mcp-ro.service $D/systemd/cart-ble-input.service /etc/systemd/system/
sudo systemctl daemon-reload
```

`install -m 644` 装出来是 root:root 0644，和推车上原来一致（2026-11-08 用非 root 账号 `stat` 过，`/opt/pxe` 下 68 个文件都是 root:root 644，`web` / `icons` 目录是 root:root 755）。

`/etc/ops-cart` **一个文件都不写**：pxe.env、pet-skin.json、serial.conf、kvm.json、mcp.json、serial-gate.json 都保留原样，这一版也没有必需的新配置。

## 5. 重写 PXE 默认菜单（脚本第 5 段，#76）

```bash
sudo python3 /opt/pxe/kiosk_core.py --write-rescue --root /opt/pxe/tftp --state /opt/pxe/pxe-mode.json
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

脚本重启完等 3 秒。部署前开着的服务里有没起来的，就停下并打印回滚命令；部署前就没在跑的服务只提一句，不算失败。

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
sudo systemd-run --unit=ops-cart-rollback --collect bash /opt/pxe/.bak-<时间戳>/rollback.sh
journalctl -fu ops-cart-rollback
```

脚本出错停下时，会把这条命令连同具体时间戳一起打印出来。回滚中途失败会打印"回滚第 N 行失败"；把问题修好后，直接再跑同一条命令即可。

它做的事：

```bash
B=/opt/pxe/.bak-<时间戳>
sudo cp -a $B/*.py $B/VERSION $B/BUILD /opt/pxe/
sudo rm -rf /opt/pxe/web /opt/pxe/icons && sudo cp -a $B/web $B/icons /opt/pxe/
sudo find /opt/pxe/tftp -name 'grub.cfg*' -type f -delete              # 菜单整批换回备份那一套
( cd $B/tftp && find . -type f ) | while read -r f; do
    sudo mkdir -p "/opt/pxe/tftp/$(dirname "$f")" && sudo cp -a "$B/tftp/$f" "/opt/pxe/tftp/$f"
done
[ -f $B/pxe-mode.json ] && sudo cp -a $B/pxe-mode.json /opt/pxe/ || sudo rm -f /opt/pxe/pxe-mode.json
sudo systemctl disable --now cart-mcp-ro cart-ble-input 2>/dev/null
sudo rm -f /etc/systemd/system/cart-mcp-ro.service /etc/systemd/system/cart-ble-input.service
sudo install -m 644 $B/etc/systemd/cart-mcp.service /etc/systemd/system/cart-mcp.service
sudo systemctl daemon-reload
# 重启部署前开着的那几个 (名单在 rollback.sh 里写死, 不看现在状态: 新代码起不来停掉的也要拉回来)
cat /opt/pxe/VERSION    # 回到 1.15.01
```

- 1.16 新加的 `.py`（`ime_*`、`ble_input*`、`serial_allowlist`、`approval_*`、`totp_core`）留着无害，旧代码不 import 它们。
- `/etc/ops-cart` 部署时没动，回滚也不用动；`$B/etc/ops-cart` 是保险。
