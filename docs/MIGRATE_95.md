# #95 迁移：pxeserver.log 挪进日志目录、smbd 没链路不起

下一次部署（含 #95 的版本）时要多做的事。版本自己的 `DEPLOY_x.md` / `install.sh` 写进来之前，照这里手工做。
命令都在推车上跑，`<包>` 是部署包或仓库的目录。

## 改了什么

| 项 | 以前 | 现在 | 为什么 |
|---|---|---|---|
| 装机口日志 | `/opt/pxe/pxeserver.log` | `/var/log/ops-cart/pxeserver.log` | `/opt/pxe` 是 `usr_t`，`logrotate_t` 打不开，每次轮到它 logrotate 整体失败（`error opening ... Permission denied`）。日志目录本来就是 `var_log_t`、本来就在 `*.log` 规则里 |
| `logrotate.d/ops-cart` | 单列 `/opt/pxe/pxeserver.log` | 去掉，`/var/log/ops-cart/*.log` 已经包含 | 同上 |
| 读这份日志的 | 车机装机面板（`kiosk_core.PXE_LOG`）、副屏 PXE 灯（`lamps_core.PXE_LOG`）、验收 | 都换新路径 | — |
| smbd | LAN1 没地址时照起，找不到接口 `smb_panic`，留 core 和 failed 单元 | `smb.service.d/ops-cart-lan1.conf`：没地址就跳过（inactive，不算失败）；NM 钩子 `50-ops-cart-smb` 在 LAN1 来地址时（重）启 | 见下 |
| 验收 | 不看 smb、logrotate | 多两项：`smb.service`（LAN1 没地址时 SKIP，failed 一律 FAIL）、`logrotate.service`（上次没失败） | — |

网页口的「PXE 日志」页读的是 `pxe-req.log` 和 `events.log`，不读 `pxeserver.log`，不受影响。

smbd 为什么用「跳过」不用「等地址」：等地址要在开机时干等（拖慢 `multi-user.target`，等不到超时又是 failed），而且
等完之后再拔插线还是要靠钩子拉起。跳过 + 钩子两头都覆盖：开机没线 → 跳过；开机有线但地址晚到 → 跳过，地址一到钩子拉起；
跑着时重新来地址 → 钩子重启绑上。

## 1. pxeserver.log（所有推车）

pxe-server 要先停：开着文件时挪走，进程会继续写旧位置。

```bash
WAS=$(systemctl is-active pxe-server)
sudo systemctl stop pxe-server
sudo install -m 644 <包>/deploy/systemd/pxe-server.service /etc/systemd/system/
sudo install -m 644 <包>/deploy/logrotate.d/ops-cart /etc/logrotate.d/ops-cart
# 旧日志接着用 (-n: 新位置已经有就不覆盖); mv 在同一个文件系统里会带着 usr_t 标签过去, 所以要 restorecon
[ -f /opt/pxe/pxeserver.log ] && sudo mv -n /opt/pxe/pxeserver.log /var/log/ops-cart/pxeserver.log
sudo restorecon -v /var/log/ops-cart/pxeserver.log
sudo systemctl daemon-reload
[ "$WAS" = active ] && sudo systemctl start pxe-server
# 新的 kiosk_core.py / lamps_core.py 落位后重启 kiosk、cart-dash 才读新路径 (部署本来就会重启)
```

`/opt/pxe/pxeserver.log` 还在（`mv -n` 没挪）就是新位置已有文件：看一眼留哪份，旧的不要就删。

验证：

```bash
ls -lZ /var/log/ops-cart/pxeserver.log            # var_log_t
sudo logrotate -d /etc/logrotate.d/ops-cart 2>&1 | grep -i error     # 没输出
sudo systemctl start logrotate.service && systemctl is-failed logrotate.service   # 清掉上次的 failed; 应输出 inactive
```

回滚到旧版本：旧单元写回 `/opt/pxe/pxeserver.log`，新位置那份留着不动就行（下次升级 `mv -n` 不会覆盖它）。

## 2. smbd（装了 Samba 的推车）

```bash
sudo install -d -m 755 /etc/systemd/system/smb.service.d
sudo install -m 644 <包>/deploy/srv-data/smb-lan1.conf /etc/systemd/system/smb.service.d/ops-cart-lan1.conf
sudo install -m 755 <包>/deploy/srv-data/50-ops-cart-smb /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo restorecon -v /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo systemctl daemon-reload
sudo systemctl reset-failed smb.service
sudo systemctl start smb.service
systemctl status smb.service --no-pager -n 0      # 没插线: inactive (dead), "skipped"; 插着线: active (running)
```

用 `setup-samba.sh` 装的推车（数据盘 + 仓库生成的 `smb.conf`）重跑 `setup-samba.sh` 也行，它会装上这两份。

**Samba 是手工配的推车不要跑 `setup-samba.sh`**，也不要装 `smb-ops-cart.conf`（数据盘那份 drop-in）：
前者会用仓库生成的 `smb.conf` 盖掉现有共享，后者在没挂 `/srv/data` 时让 smbd 永远不起。只装上面两个文件。

验证：拔着 LAN1 重启推车，`systemctl --failed` 里没有 smb，`coredumpctl list smbd` 没有新的；
插上 LAN1 等地址出来，`systemctl is-active smb` 是 active。
