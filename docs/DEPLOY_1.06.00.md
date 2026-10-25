# 部署清单 1.06.00

从 1.05.01 升级到 1.06.00。本清单只写步骤，**由人在现场确认后执行**。
文中 `<推车>` 是推车的 ssh 地址，`<装机网段>` 等尖括号内容按现场填写，不进仓库。

远程写 root 文件的方法：先 scp 到临时目录，再用 `sudo install`。不要把密码 pipe 给 `sudo -S tee`，
sudo 和 tee 会抢 stdin，文件可能被写成空的。

## 0. 这一版改了什么

| 方面 | 变化 | 对推车的影响 |
|---|---|---|
| 装机网段 | `pxe-server` / `pxe-http-boot` / `pxe-http-repo` 三个单元改读 `EnvironmentFile=/etc/ops-cart/pxe.env`，车机生成的 grub 菜单也读这份 | **必须先建 pxe.env**，否则三个 PXE 服务起不来 |
| LAN1 | 车机菜单删掉「LAN1 装机口配置」（它会重写 systemd 单元，而且原本就会失败）；`--iface eno1` 写死在单元里 | 改装机网段只能改 pxe.env 然后重启服务 |
| pxeserver | 新参数 `--prefix`，掩码和广播地址按它算；地址池用完不再重复发第一个地址；客户端选了别的 DHCP 服务器 (option 54) 时不回 | 单元已带上 |
| pxeserver 租约 | 租约表：ACK 给 3600 秒，没人 REQUEST 的 OFFER 留 60 秒，到期回收；不是自己的租约不回，只在客户端选了自己时才 NAK；处理 RELEASE / DECLINE（DECLINE 的地址隔离 600 秒）；修了 xid 读错位置的 bug | 无需操作 |
| pxeserver 守卫 | 启动时和 eno1 从没插线变成插线时主动探测一次别人的 DHCP，平时被动监听 UDP/68；发现别人就停止应答、写 cartlog、以退出码 3 退出且**不再自动拉起**（单元 `RestartPreventExitStatus=2 3 4`）。只有 eno1 的**主地址**等于 `PXE_IP` 才回包。状态写 `/run/ops-cart/pxe-guard.json` | 依赖 `cartlog.py`、`netprof_core.py` 和 pxeserver.py 放在同一目录（`*.py` 一起拷就满足）；第 1 步先确认主地址 |
| TFTP | 块号 16 位回绕、按块读文件（大镜像不再整个读进内存）、blksize 限 8..65464、畸形请求回 ERROR；工作线程死了进程以退出码 1 退出交给 systemd 拉起 | 无需操作 |
| 装机两头核对 | TFTP 只在装机模式已解锁、没过期、文件名对得上解锁的 MAC 时才发带 `inst.ks=` 的菜单，否则发救援菜单；HTTP 只在请求头 `X-RHN-Provisioning-MAC` 含解锁的 MAC 时才给 ks 和装机菜单，否则 403；`*.bak*` 一律 404。装机菜单加了 `inst.ks.sendmac` | kiosk 重启时 `pxe_disarm` 会清掉旧菜单，之后解锁生成的都带 sendmac。**Windows 脚本和手工放的自动装机菜单从此装不了机**（故意的） |
| 菜单模板 | grub 菜单模板只在 `pxeserver.grub_menu` 一处；`setup-cart.sh` 改调 `kiosk_core.py --write-rescue`，并删掉 `EFI/BOOT/grub.cfg` | 以后重铺 tftp 树也只会有救援菜单 |
| 装机菜单 | `pxe_disarm`（kiosk 启动时会调）删掉 tftp 树里所有带 `inst.ks=` 的 `grub.cfg` / `grub.cfg-*`，包括 `EFI/BOOT/grub.cfg`；`grub.cfg.bak-*` 这类手工备份不动 | 推车上现在 `EFI/BOOT/grub.cfg` 是自动装机菜单，kiosk 重启后会被删掉（第 2 步先备份） |
| 校园网认证 | 单元改名 `campus-auth.service/.timer`，配置 `/etc/ops-cart/campus-auth.json`，状态 `/run/ops-cart/campus-auth-status.json`；代码里不再有默认 SSID，**配置里没写 `ssid` 就不认证** | 迁移配置、换单元（第 4、6 步） |
| 串口留言 | 留言署名从 `serial.conf` 的 `chat_name` 读，默认 `operator` | 想保留原署名就在 serial.conf 里加上（第 5 步） |
| 网页管理口 | 第一次部署到推车。会话绑定 PIN、关局域网开关时断开局域网会话和长连接、输错锁定按来源 IP 分开（局域网指数退避）、装机口网段来源一律拒绝、键鼠按画面源绑定走、畸形请求回 400、HTTP/1.1。Host 只认 IP 字面量、localhost、本机名和 `web.conf` 的 `hosts`；POST 必须同源，否则 403（在读请求体之前）。打字分块发送并返回发了几个、跳过哪些；鼠标默认绝对坐标；串口不再连上就发回车（改为「唤醒」按钮），多个标签页各自一条连接。审计日志写 `/var/log/ops-cart-web/audit.log`（PIN、令牌、打字内容不记）。`webkiosk.service` 以专用用户 `ops-web` 跑（`dialout`、`video` 组 + 只读能力 `CAP_DAC_READ_SEARCH`，只能写 `/opt/pxe/web.conf`） | 拷文件但默认不启用；要启用得先建用户、预建 `web.conf`（第 7 步）；用 MagicDNS 名字访问要把名字写进 `web.conf` 的 `hosts` |
| PinGate | 加锁；冷却从 60 秒起逐次翻倍、封顶 15 分钟，进入冷却写 cartlog；车机的输错计数落 `/run/ops-cart/pin-gate.json`，重启 kiosk 不清零（重启推车清零） | 需要 tmpfiles 建好 `/run/ops-cart`（第 6 步） |
| 车机界面 | 外部命令、开关装机服务、关机重启都放到后台线程，结果经队列回主线程；左侧面板改成状态机（同时只开一个）；开机自检加 30 秒看门狗，出错变成一行 SELFTEST FAIL；退出时只上锁一次装机模式；小键盘数字不再依赖 NumLock；界面文案去掉了符号字符 | 无需操作 |
| 键鼠 | CH9329 独占打开（双开第二个报"键鼠被占用"），拔线只报一次；车机侧发送移到工作线程，相对位移每帧拆到 ±127 以内；win/super 改成修饰键，补了 F13-F24 等键名；有打不出的字时整串不发 | 无需操作 |
| 状态板 CH9329 行 | 不再看"那一块" CH9329，改报 `N ONLINE / M BOUND`（在线块数 / 绑了键鼠的画面源个数）；网页状态卡同步 | 无需操作 |
| 串口面板 | 只认在跑的 conbroker 服务，标题显示服务实际波特率；开服务失败把报错显示出来；优先 by-id 路径；断开后自动重连；粘贴按行限速；CH9329 所在的口不许开服务 | 无需操作 |
| 无线 | 重建连接先建后删、改密码连不上自动写回旧密码；动当前出口那条连接前弹"远程会断"的确认 | 无需操作 |
| 网口配置文件 | `dhcp_probe` 没插线时不发包、结果记"未知"，面板遇到未知拒绝启用发地址 | 无需操作 |
| 校园网认证 | 认证页主机白名单 `portal_hosts`：配置里没有时，第一次登录成功就记住并写回 `campus-auth.json`；以后被跳到陌生主机一个字节都不发，结果记 `untrusted` | 第一次认证要在真校园网上；换认证服务器时手工改 `portal_hosts` |
| cart_snap / cart_ocr | 截图等 mpv 回包才算成功，超时、设备被占用分别给原因；不给 `-o` 时输出到 `/run/ops-cart` 下的 0600 临时文件；cart_ocr 出错退出码改为 2（1 只表示没找到） | 调用方如果靠退出码判断，留意 1 和 2 的区别 |
| 日志目录 | tmpfiles 把 `/var/log/ops-cart` 和 `reports` 改为 `0750 root:wheel`，新建 `/run/ops-cart`（0755 root） | 第 6 步装 tmpfiles 配置并 `systemd-tmpfiles --create` |
| kiosk_core.run | 命令不存在或超时时不再把错误文字当输出 | 状态板上不会再出现把报错当 IP 显示的情况 |

## 1. 部署前

1. 确认现场：推车屏幕没人在用、没有正在装的机器、串口会话没人在用。重启 kiosk 会回到锁屏。
2. 确认 LAN1 (eno1) 没接交换机。
3. 在开发机上：

   ```bash
   git log -1 --oneline                 # 确认是要部署的提交
   python3 -m pytest -q                  # 全绿
   python3 scripts/check_public_safe.py .
   scripts/stamp-build.sh                # 生成 BUILD
   ```

4. 记下现在的装机网段（第 3 步要用）：

   ```bash
   ssh <推车> 'systemctl cat pxe-server | grep ExecStart'
   ```

5. 这一版有三处依赖推车现状，开发时没能在推车上核对，部署前先看：

   ```bash
   ssh <推车> 'ip -4 -o addr show dev eno1'      # 第一条 (主地址) 必须就是装机地址; 若是第二条, 守卫会一直不回包
   ssh <推车> 'getent group wheel'               # tmpfiles 要用 wheel 组
   ssh <推车> 'ss -ulpn | grep ":68 "'           # UDP/68 上有没有别的程序 (有也不致命: 守卫绑不上会每 30 秒重试, 照常服务)
   ```

## 2. 备份（全部 `.bak-日期`）

1.05.01 的校园网认证单元和配置文件名带内部名字，仓库里不写，用变量代替。先在推车上查出来：

```bash
systemctl list-unit-files --no-legend '*-auth.timer'    # 不是 campus-auth 的那个
sudo ls /etc/ops-cart/                                  # 对应的 .json
```

后面所有步骤（包括验证和回滚）都在同一个 shell 里设好这两个变量：

```bash
ssh <推车>
OLD=<旧认证单元名, 不带 .service/.timer>
OLD_CONF=<旧配置文件名, 如 xxx.json>
TS=$(date +%Y%m%d-%H%M)
cd /opt/pxe
for f in *.py VERSION BUILD; do sudo cp -a "$f" "$f.bak-$TS"; done
sudo cp -a serial.conf "serial.conf.bak-$TS"
for u in pxe-server.service pxe-http-boot.service pxe-http-repo.service $OLD.service $OLD.timer; do
  sudo cp -a /etc/systemd/system/$u /etc/systemd/system/$u.bak-$TS
done
sudo cp -a /etc/tmpfiles.d/ops-cart.conf "/etc/tmpfiles.d/ops-cart.conf.bak-$TS"
# tftp 里的自动装机菜单搬出 tftp 树 (TFTP 能按名字取到树里的任何文件)
sudo install -d -m 700 /opt/pxe/tftp-attic
sudo cp -a tftp/EFI/BOOT/grub.cfg "tftp-attic/EFI-BOOT-grub.cfg.bak-$TS"
sudo mv tftp/grub.cfg.bak-* tftp-attic/ 2>/dev/null || true
sudo cp -a /etc/ops-cart/$OLD_CONF "/etc/ops-cart/$OLD_CONF.bak-$TS"
echo "$TS"                               # 记下来, 回滚要用
```

## 3. 建 /etc/ops-cart/pxe.env

照 `deploy/pxe.env.example`，填第 1 步记下的值（`PXE_IP` 不带前缀长度，`PXE_POOL` 起止用逗号隔开，池子不能包含 `PXE_IP`）：

```bash
# 开发机
cp deploy/pxe.env.example /tmp/pxe.env && $EDITOR /tmp/pxe.env
scp /tmp/pxe.env <推车>:/tmp/pxe.env
# 推车
sudo install -m 644 -o root -g root /tmp/pxe.env /etc/ops-cart/pxe.env && rm /tmp/pxe.env
sudo cat /etc/ops-cart/pxe.env
```

## 4. 迁移校园网认证配置

```bash
sudo mv /etc/ops-cart/$OLD_CONF /etc/ops-cart/campus-auth.json
sudo chmod 600 /etc/ops-cart/campus-auth.json
sudo python3 -c 'import json; c = json.load(open("/etc/ops-cart/campus-auth.json")); print("ssid:", repr(c.get("ssid")))'
```

如果打印出来是 `None` 或空串，要补上：在车机「网络 ▸ 无线网络」里连着校园网的时候点一次「保存」，
或者手工编辑（临时文件 + `sudo install -m 600`），加上 `"ssid": "<校园网 SSID>"`。

## 5. serial.conf 留言署名（可选）

想让车机留言继续用原来的署名，在 `/opt/pxe/serial.conf` 里加 `"chat_name": "<署名>"`，其他键保留：

```bash
sudo python3 - <<'EOF'
import json
p = "/opt/pxe/serial.conf"
c = json.load(open(p)); c["chat_name"] = "<署名>"
open(p + ".new", "w").write(json.dumps(c, ensure_ascii=False))
EOF
sudo install -m 644 /opt/pxe/serial.conf.new /opt/pxe/serial.conf && sudo rm /opt/pxe/serial.conf.new
```

## 6. 拷文件、换单元、重启

```bash
# 开发机: 先放到推车临时目录
ssh <推车> 'rm -rf /tmp/ops-cart-1.06.00 && mkdir -p /tmp/ops-cart-1.06.00/systemd'
scp *.py VERSION BUILD <推车>:/tmp/ops-cart-1.06.00/
scp -r web <推车>:/tmp/ops-cart-1.06.00/
scp deploy/systemd/{pxe-server,pxe-http-boot,pxe-http-repo,campus-auth,webkiosk}.service \
    deploy/systemd/campus-auth.timer <推车>:/tmp/ops-cart-1.06.00/systemd/
scp deploy/tmpfiles.d/ops-cart.conf <推车>:/tmp/ops-cart-1.06.00/
```

```bash
# 推车
S=/tmp/ops-cart-1.06.00
sudo install -m 644 -o root -g root $S/*.py $S/VERSION $S/BUILD /opt/pxe/
sudo cp -r $S/web /opt/pxe/ && sudo chown -R root:root /opt/pxe/web

# 日志目录权限 + /run/ops-cart (守卫状态、PIN 计数、mpv socket)
sudo install -m 644 $S/ops-cart.conf /etc/tmpfiles.d/ops-cart.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf

# 旧认证单元下线, 新单元装上
sudo systemctl disable --now $OLD.timer
sudo rm /etc/systemd/system/$OLD.service /etc/systemd/system/$OLD.timer
sudo install -m 644 $S/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now campus-auth.timer

# 重启顺序: PXE 三件套 (都先读 pxe.env) -> kiosk (启动时 pxe_disarm 重写救援菜单)
sudo systemctl restart pxe-http-boot pxe-http-repo
sudo systemctl restart pxe-server
sudo systemctl restart kiosk
```

`pxe-server` 是否继续开机自启、`Lan1-pxe` 是否自动连接，部署时由维护者决定，本版不改。

## 7. 网页管理口（可选，由维护者决定）

文件在第 6 步已经拷好（`webkiosk.py`、`web/`、`webkiosk.service`）。要启用，先建专用用户、预建 `web.conf`
（单元里 `ReadWritePaths=/opt/pxe/web.conf` 指向的文件不存在时服务直接起不来，这步不能省）：

```bash
sudo useradd -r -s /sbin/nologin ops-web
sudo install -o ops-web -g ops-web -m 0600 /dev/null /opt/pxe/web.conf    # 已有就只 chown/chmod
sudo systemctl enable --now webkiosk.service      # 或只 start 不 enable
journalctl -u webkiosk -n 20 --no-pager           # 看有没有权限报错
```

以 `ops-web` 跑时没在推车上验证过的几处，启用后顺手看一眼：PIN 文件能读（登录正常）、
conbroker 的状态和日志能读（网页串口列表不空）、`/var/log/ops-cart-web/audit.log` 有新行。
网页口里 PIN 输错进入冷却时的 cartlog 事件写不进 `/var/log/ops-cart`（ops-web 没权限），只会少这一条，不影响校验。

局域网访问默认关闭，只在 tailnet 和本机可用；推车没设 PIN 时局域网永远拒绝。

## 8. 验证

```bash
cat /opt/pxe/VERSION                                   # 1.06.00
systemctl is-active pxe-server pxe-http-boot pxe-http-repo kiosk
systemctl is-enabled campus-auth.timer                 # enabled
systemctl list-unit-files "$OLD*"                      # 没有了
ps -o args= -C python3 | grep pxeserver                # --ip <装机地址> --prefix .. --pool .. --iface eno1
sudo ss -ulpn | grep ':67 '                            # 0.0.0.0%eno1:67
sudo grep -rl 'inst.ks=' /opt/pxe/tftp | grep -v '/grub.cfg\.' || echo '没有自动装机菜单'
sudo head -8 /opt/pxe/tftp/grub.cfg                    # Rescue shell, (http,<装机地址>)
journalctl -u pxe-server -n 20 --no-pager              # "PXE 服务启动 ip=<装机地址>/<前缀>"
sudo systemctl start campus-auth.service; cat /run/ops-cart/campus-auth-status.json
cat /run/ops-cart/pxe-guard.json                       # 插着被测机时 "state": "serving"; 没插线 "no-link"
stat -c '%a %U:%G' /var/log/ops-cart /run/ops-cart     # 750 root:wheel / 755 root:root
```

`pxe-guard.json` 是 `foreign` 时说明 LAN1 上有别的 DHCP（多半接到了交换机），`pxe-server` 已经停下且不会自己拉起；
拔线、确认 LAN1 只接被测机，再 `sudo systemctl restart pxe-server`。

车机上：

1. 开机自检 / 状态板正常，PXE 三个服务绿。
2. 「装机 ▸」里没有「LAN1 装机口配置」。
3. 装机模式解锁一次（不接被测机），`/opt/pxe/tftp/grub.cfg-01-<mac>` 里的 HTTP 地址是装机地址、带 `inst.ks.sendmac`；上锁后该文件消失。
   解锁期间在推车上 `curl -s -o /dev/null -w '%{http_code}\n' http://<装机地址>:8000/ks.cfg` 应为 403（没带 MAC 头）。
4. 「网络 ▸ 无线网络」底部认证框标题写的是配置里的 SSID，「立即认证」结果正常。
5. 串口会话留言署名符合第 5 步。
6. 画面预览、键鼠透传照常；相对鼠标模式下指针不再跑出画面。状态板 CH9329 行显示 `N ONLINE / M BOUND`。
7. 锁屏连续输错 PIN：第二次冷却比第一次长；`/run/ops-cart/pin-gate.json` 里有计数。

启用了网页口的话（从 tailnet 上的机器）：

```bash
curl -s http://<推车 tailnet 地址>:8088/api/whoami      # 200, kind=trusted
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://<推车 tailnet 地址>:8088/   # 403
```

从 LAN1 上的被测机访问 8088 应当返回 403「装机口 (LAN1) 不提供管理访问」。
画面源没绑键鼠时，网页上的画面源会标「未绑键鼠, 只看」，键鼠操作提示原因。

## 9. 回滚

```bash
TS=<第 2 步记下的时间戳>; OLD=<同第 2 步>; OLD_CONF=<同第 2 步>
cd /opt/pxe
for f in *.bak-$TS; do sudo cp -a "$f" "${f%.bak-$TS}"; done
sudo systemctl disable --now webkiosk.service 2>/dev/null
sudo rm -rf webkiosk.py web_core.py web        # 1.05.01 时推车上没有这几个
sudo systemctl disable --now campus-auth.timer
sudo rm -f /etc/systemd/system/campus-auth.service /etc/systemd/system/campus-auth.timer /etc/systemd/system/webkiosk.service
for u in pxe-server.service pxe-http-boot.service pxe-http-repo.service $OLD.service $OLD.timer; do
  sudo cp -a /etc/systemd/system/$u.bak-$TS /etc/systemd/system/$u
done
sudo cp -a /etc/tmpfiles.d/ops-cart.conf.bak-$TS /etc/tmpfiles.d/ops-cart.conf
sudo mv /etc/ops-cart/campus-auth.json /etc/ops-cart/$OLD_CONF
sudo systemctl daemon-reload
sudo systemctl enable --now $OLD.timer
sudo systemctl restart pxe-http-boot pxe-http-repo pxe-server kiosk
```

说明：

- `webkiosk.py`、`web_core.py`、`web/` 是这次新拷上去的（1.05.01 推车上没有），回滚时直接删。
- `/etc/ops-cart/pxe.env` 留着无害（旧单元不读）。
- `kiosk_ui_core.py` 也是新文件，留着无害（1.05.01 的 kiosk.py 不 import 它）。
- `ops-web` 用户、`/opt/pxe/web.conf`、`/run/ops-cart/` 下的状态文件留着无害；要清就 `sudo userdel ops-web`。
- 日志目录的权限 tmpfiles 恢复后下次开机才改回；要立刻改回就 `sudo systemd-tmpfiles --create /etc/tmpfiles.d/ops-cart.conf`。
- `EFI/BOOT/grub.cfg` 是旧的自动装机菜单，**不建议恢复**；确实要恢复就从 `/opt/pxe/tftp-attic/` 拷回去。
- 回滚后在 CART-LOG 记一条。
