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
| 装机菜单 | `pxe_disarm`（kiosk 启动时会调）删掉 tftp 树里所有带 `inst.ks=` 的 `grub.cfg` / `grub.cfg-*`，包括 `EFI/BOOT/grub.cfg`；`grub.cfg.bak-*` 这类手工备份不动 | 推车上现在 `EFI/BOOT/grub.cfg` 是自动装机菜单，kiosk 重启后会被删掉（第 2 步先备份） |
| 校园网认证 | 单元改名 `campus-auth.service/.timer`，配置 `/etc/ops-cart/campus-auth.json`，状态 `/run/ops-cart/campus-auth-status.json`；代码里不再有默认 SSID，**配置里没写 `ssid` 就不认证** | 迁移配置、换单元（第 4、6 步） |
| 串口留言 | 留言署名从 `serial.conf` 的 `chat_name` 读，默认 `operator` | 想保留原署名就在 serial.conf 里加上（第 5 步） |
| 网页管理口 | 第一次部署到推车。会话绑定 PIN、关局域网开关时断开局域网会话和长连接、输错锁定按来源分开（局域网指数退避）、装机口网段来源一律拒绝、键鼠按画面源绑定走、畸形请求回 400、HTTP/1.1；新增 `webkiosk.service` | 拷文件但默认不启用；开不开、开机自启与否由维护者决定（第 7 步） |
| PinGate | 加锁；车机锁屏行为不变（固定 60 秒冷却） | 无 |
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
```

```bash
# 推车
S=/tmp/ops-cart-1.06.00
sudo install -m 644 -o root -g root $S/*.py $S/VERSION $S/BUILD /opt/pxe/
sudo cp -r $S/web /opt/pxe/ && sudo chown -R root:root /opt/pxe/web

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

文件在第 6 步已经拷好（`webkiosk.py`、`web/`、`webkiosk.service`）。要启用：

```bash
sudo systemctl enable --now webkiosk.service      # 或只 start 不 enable
```

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
```

车机上：

1. 开机自检 / 状态板正常，PXE 三个服务绿。
2. 「装机 ▸」里没有「LAN1 装机口配置」。
3. 装机模式解锁一次（不接被测机），`/opt/pxe/tftp/grub.cfg-01-<mac>` 里的 HTTP 地址是装机地址；上锁后该文件消失。
4. 「网络 ▸ 无线网络」底部认证框标题写的是配置里的 SSID，「立即认证」结果正常。
5. 串口会话留言署名符合第 5 步。
6. 画面预览、键鼠透传照常（Tk 侧本版没改透传逻辑）。

启用了网页口的话（从 tailnet 上的机器）：

```bash
curl -s http://<推车 tailnet 地址>:8088/api/whoami      # 200, kind=trusted
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
sudo mv /etc/ops-cart/campus-auth.json /etc/ops-cart/$OLD_CONF
sudo systemctl daemon-reload
sudo systemctl enable --now $OLD.timer
sudo systemctl restart pxe-http-boot pxe-http-repo pxe-server kiosk
```

说明：

- `webkiosk.py`、`web_core.py`、`web/` 是这次新拷上去的（1.05.01 推车上没有），回滚时直接删。
- `/etc/ops-cart/pxe.env` 留着无害（旧单元不读）。
- `EFI/BOOT/grub.cfg` 是旧的自动装机菜单，**不建议恢复**；确实要恢复就从 `/opt/pxe/tftp-attic/` 拷回去。
- 回滚后在 CART-LOG 记一条。
