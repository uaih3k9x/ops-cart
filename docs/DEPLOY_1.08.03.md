# 部署清单 1.08.03

从 1.08.02 升级到 1.08.03。本清单只写步骤，**由人在现场确认后执行**。
`<推车>` 是推车的 ssh 地址，尖括号内容按现场填写，不进仓库。

远程写 root 文件：先 scp 到临时目录，再 `sudo install`。不要把密码 pipe 给 `sudo -S tee`（抢 stdin，文件可能被写空）。

## 0. 这一版改了什么

| 任务 | 变化 | 对推车的影响 |
|---|---|---|
| #4 UPS | 有 hidraw 状态就以它为准，OL 压过滞后的 discharging；车机判断走电池也认 hidraw 的 OB | 换 `ups_core.py`、`dash_core.py`；无需额外操作 |
| #8 静态地址 | 网口面板静态地址改人工填写并校验；LAN1 / LAN2 切换都弹确认框，写明「原来/改为/回滚」 | 换 `netprof_core.py`、`netprof_panel.py`、`kiosk_core.py` |
| #9 键鼠 | 车机键鼠透传改用 `IdleCh`（懒打开 + 空闲 3 秒放锁），被占用时横幅提示、丢这次输入 | 换 `kvm_core.py`、`kiosk.py` |
| #11 代理模式 | 守卫探到别人的 DHCP 不再停服，转 proxyDHCP 代理模式（状态 `/run/ops-cart/pxe-guard.json`）；自检里 LAN1 / PXE 变 WARN 一行、写明对方网段和推车够不够得着 | 换 `pxeserver.py`、`kiosk_core.py`；**换 `pxe-server.service` 单元**（见下） |
| #11 WinPE | 解锁对话框二选一：装 Rocky（自动装机）/ 进 WinPE（进 PE）；按 `win/` 是否齐全放 WinPE 项，上锁一并清掉；新增 `boot.ipxe` 模板 | 换 `pxeserver.py`、`httpserve.py`、`kiosk.py`、`kiosk_core.py`；推车需已铺好 `win/` 文件 |

**单元改动**：`pxe-server.service` 的 `RestartPreventExitStatus` 由 `2 3 4` 改为 `2 4`（exit 3 从前是「停服」，现在守卫转代理模式、不再用 exit 3 停服）。必须同步到 `/etc` 并 `daemon-reload`，否则重启行为跟新代码对不上。

## 1. 部署前（本次已在开发机 + 推车只读核对，结果记录于此）

- 开发机：`git log -1`=`dcee996`；`pytest`=**983 passed, 1 skipped**；`check_public_safe`=通过。
- 推车现状：版本 `1.08.02`（BUILD `83 4de5b45`）；`pxe-server/boot/repo`、`kiosk`、`cart-dash` 均 active（`webkiosk`、`dash` inactive，符合预期）；`eno1` 主地址 = 装机地址（第一条，守卫会回包）；UDP/68 只有自己的守卫；`wheel` 组含操作账号；`who` 无登录会话。
- **执行前现场再确认一次**：推车屏幕没人在用、没有正在装的机器、串口会话没人在用。**重启 kiosk 会回到锁屏。**LAN1 上如果接着被测机/编码盒，重启 pxe/kiosk 不影响该设备，但会清掉旧装机菜单（`pxe_disarm`）。

## 2. 备份

```bash
ssh <推车>
TS=$(date +%Y%m%d-%H%M)
cd /opt/pxe
sudo mkdir -p /opt/pxe/.bak-$TS
sudo cp -a dash_core.py httpserve.py kiosk.py kiosk_core.py kvm_core.py \
          netprof_core.py netprof_panel.py pxeserver.py ups_core.py VERSION /opt/pxe/.bak-$TS/
sudo cp -a /etc/systemd/system/pxe-server.service /opt/pxe/.bak-$TS/pxe-server.service
```

## 3. 传文件（在开发机上，`<推车>` 换成 ssh 地址）

```bash
cd ~/code/pxeserver
scripts/stamp-build.sh                 # 生成干净 BUILD(提交数+哈希，不带 dirty)
scp dash_core.py httpserve.py kiosk.py kiosk_core.py kvm_core.py \
    netprof_core.py netprof_panel.py pxeserver.py ups_core.py VERSION BUILD <推车>:/tmp/ops-cart-1.08.03/
scp deploy/systemd/pxe-server.service <推车>:/tmp/ops-cart-1.08.03/
```

在推车上落位：

```bash
ssh <推车>
cd /tmp/ops-cart-1.08.03
sudo install -m 644 dash_core.py httpserve.py kiosk.py kiosk_core.py kvm_core.py \
     netprof_core.py netprof_panel.py pxeserver.py ups_core.py VERSION BUILD /opt/pxe/
sudo install -m 644 pxe-server.service /etc/systemd/system/pxe-server.service
sudo systemctl daemon-reload
```

## 4. 重启（顺序：先 pxe 三件套，再副屏，最后 kiosk）

```bash
sudo systemctl restart pxe-server pxe-http-boot pxe-http-repo
sudo systemctl restart cart-dash
sudo systemctl restart kiosk          # 会回到锁屏
```

## 5. 验证

```bash
cat /opt/pxe/VERSION                   # 1.08.03
cat /opt/pxe/BUILD                     # 不带 dirty
for s in pxe-server pxe-http-boot pxe-http-repo cart-dash kiosk; do systemctl is-active $s; done   # 全 active
systemctl cat pxe-server | grep RestartPreventExitStatus    # 2 4
cat /run/ops-cart/pxe-guard.json 2>/dev/null                # 守卫状态
journalctl -u pxe-server -u kiosk -u cart-dash --since "-3 min" -p warning   # 没有异常
```

**现场验证清单（只能在推车前做）**：
1. 解锁对话框出现「装 Rocky / 进 WinPE」二选一；选 WinPE 时菜单里有 PE 项（前提 `win/` 齐全），上锁后菜单清掉。
2. 网口面板改 LAN1 / LAN2 静态地址，确认框写明「原来 / 改为 / 回滚」，点「否」什么都不变。
3. 自检那行「代理模式」WARN 文字在车机和副屏上**不被截断**。
4. CH9329：拔插一次不会把键打到别的机器；车机侧空闲 3 秒放锁，被占用时有横幅。
5. 副屏 UPS 那行：市电/电池切换跟随（#4）。

## 6. 回滚

```bash
ssh <推车>
TS=<上面备份的时间戳>
cd /opt/pxe
sudo cp -a /opt/pxe/.bak-$TS/*.py /opt/pxe/.bak-$TS/VERSION /opt/pxe/
sudo install -m 644 /opt/pxe/.bak-$TS/pxe-server.service /etc/systemd/system/pxe-server.service
sudo systemctl daemon-reload
sudo systemctl restart pxe-server pxe-http-boot pxe-http-repo cart-dash kiosk
cat /opt/pxe/VERSION    # 回到 1.08.02
```
