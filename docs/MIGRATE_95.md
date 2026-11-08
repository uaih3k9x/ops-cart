# #95 迁移：pxeserver.log 挪进日志目录、smbd 没链路不起

下一次部署（含 #95 的版本）时要多做的事。版本自己的 `DEPLOY_x.md` / `install.sh` 写进来之前，照这里手工做。
命令都在推车上跑，`<包>` 是部署包或仓库的目录。

## 改了什么

| 项 | 以前 | 现在 | 为什么 |
|---|---|---|---|
| 装机口日志 | `/opt/pxe/pxeserver.log` | `/var/log/ops-cart/pxeserver.log` | `/opt/pxe` 是 `usr_t`，`logrotate_t` 打不开，每次轮到它 logrotate 整体失败（`error opening ... Permission denied`）。日志目录本来就是 `var_log_t`、本来就在 `*.log` 规则里 |
| `logrotate.d/ops-cart` | 单列 `/opt/pxe/pxeserver.log` | 去掉，`/var/log/ops-cart/*.log` 已经包含 | 同上 |
| 读这份日志的 | 车机装机面板（`kiosk_core.PXE_LOG`）、副屏 PXE 灯（`lamps_core.PXE_LOG`）、验收 | 都换新路径 | — |
| smbd（手工配置、`interfaces` 不带 `lo` 的推车） | 本机没有 `interfaces` 里那个地址时（LAN1 没插线，或网口模式面板把 LAN1 切到 DHCP / 换了静态地址）smbd 找不到接口 `smb_panic`，留 core 和 failed 单元 | `interfaces` 手工加上 `lo`（兜底：永远有地方绑）；再装 `smb.service.d/ops-cart-lan1.conf`：LAN1 没地址就跳过（inactive，不算失败）；NM 钩子 `50-ops-cart-smb` 在 LAN1 来地址时（重）启 | 见下 |
| 验收 | 不看 smb、logrotate | 多两项：`smb.service`（LAN1 没地址时 SKIP，failed 一律 FAIL，没装 lan1 drop-in 时提示）、`logrotate.service`（上次没失败） | — |

网页口的「PXE 日志」页读的是 `pxe-req.log` 和 `events.log`，不读 `pxeserver.log`，不受影响。

smbd 为什么用「跳过」不用「等地址」：等地址要在开机时干等（拖慢 `multi-user.target`，等不到超时又是 failed），而且
等完之后再拔插线还是要靠钩子拉起。跳过 + 钩子两头都覆盖：开机没线 → 跳过；开机有线但地址晚到 → 跳过，地址一到钩子拉起；
跑着时重新来地址 → 钩子重启绑上。

崩溃的真正条件是「`interfaces` 里列的地址一个都不在本机」，不是「eno1 没有 IPv4」：drop-in 只看后者，
LAN1 换了地址时会放行，smbd 照崩。所以手工配置一定要加 `lo`，drop-in 只是让没插线时不白起一回。

**lan1 drop-in 只给手工配置的推车装。** 仓库生成的 `smb.conf`（`setup-samba.sh`）本来就是 `interfaces = lo eno1`，
LAN1 没地址时 smbd 绑 127.0.0.1 照常跑，不会崩；再装这份 drop-in 反而没插线时把 smbd 整个跳过，连 127.0.0.1 都不可用，
没有好处。所以 `setup-samba.sh` 不装它，只装钩子。

## 1. pxeserver.log（所有推车）

pxe-server 要先停：开着文件时挪走，进程会继续写旧位置。

```bash
WAS=$(systemctl is-active pxe-server)
sudo systemctl stop pxe-server
sudo install -m 644 <包>/deploy/systemd/pxe-server.service /etc/systemd/system/
sudo install -m 644 <包>/deploy/logrotate.d/ops-cart /etc/logrotate.d/ops-cart
# 旧日志接着用 (-n: 新位置已经有就不覆盖); mv 在同一个文件系统里会带着 usr_t 标签过去, 所以要 restorecon
[ -f /opt/pxe/pxeserver.log ] && sudo mv -n /opt/pxe/pxeserver.log /var/log/ops-cart/pxeserver.log
[ -f /var/log/ops-cart/pxeserver.log ] && sudo restorecon -v /var/log/ops-cart/pxeserver.log
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
**回滚时保留新的 `logrotate.d/ops-cart`**（不带 `/opt/pxe` 那行），别把旧的装回去：装回旧的，logrotate 又会每次轮到
`/opt/pxe/pxeserver.log` 就失败。代价是回滚期间 `/opt/pxe/pxeserver.log` 不轮转（装机口日志一周几百 KB，可以接受）。

## 2. smbd（装了 Samba 的推车）

**Samba 是手工配的推车（`/etc/samba/smb.conf` 里没有 `setup-samba.sh` 字样）：不要跑 `setup-samba.sh`，
也不要装 `smb-ops-cart.conf`（装到推车上叫 `smb.service.d/ops-cart.conf`，数据盘那份）。**
前者会用仓库生成的 `smb.conf` 盖掉现有共享，后者在没挂 `/srv/data` 时让 smbd 永远不起。手工配置的推车照下面做。

用 `setup-samba.sh` 装的推车（数据盘 + 仓库生成的 `smb.conf`，带 `lo`）只要重跑 `setup-samba.sh` 装上新钩子，
不装 lan1 drop-in（理由见上）。

### 2.1 smb.conf 的 interfaces 加 lo（手工配置的推车）

```bash
sudo cp -a /etc/samba/smb.conf /etc/samba/smb.conf.bak-95
grep -n '^ *interfaces' /etc/samba/smb.conf        # 现在是 interfaces = <装机口地址>/<前缀>
sudoedit /etc/samba/smb.conf                        # 改成  interfaces = lo <装机口地址>/<前缀>   (地址照原样, 只在前面加 lo)
testparm -s 2>/dev/null | grep -E '^\s*(interfaces|bind interfaces only)'   # interfaces = lo <装机口地址>/<前缀>, bind interfaces only = Yes
```

`testparm` 报错就 `sudo cp -a /etc/samba/smb.conf.bak-95 /etc/samba/smb.conf` 换回去再看。

### 2.2 drop-in 和钩子（手工配置的推车）

```bash
sudo install -d -m 755 /etc/systemd/system/smb.service.d
sudo install -m 644 <包>/deploy/srv-data/smb-lan1.conf /etc/systemd/system/smb.service.d/ops-cart-lan1.conf
sudo install -m 755 <包>/deploy/srv-data/50-ops-cart-smb /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo restorecon -v /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo systemctl daemon-reload
sudo systemctl reset-failed smb.service
sudo systemctl start smb.service
systemctl status smb.service --no-pager -n 0      # 没插线: inactive (dead), 日志里 "Skipped due to 'exec-condition'"; 插着线: active (running)
systemctl show -p DropInPaths smb.service         # 里面有 ops-cart-lan1.conf
```

### 2.3 验证

- 拔着 LAN1 重启推车：`systemctl --failed` 里没有 smb，`coredumpctl list smbd` 没有新的。
- 插上 LAN1，等地址出来（`ip -4 addr show dev eno1`）：
  - `systemctl is-active smb` 是 active；`ss -ltn` 里 SMB 端口（`samba_core.SMB_PORT`）能看到 127.0.0.1 和装机口地址两条。
  - `journalctl -u NetworkManager-dispatcher -b --no-pager | tail` 没有报错（钩子跑过、没被拒）。
  - `sudo ausearch -m avc -ts recent` 没有 `50-ops-cart-smb` / `systemctl` 相关的拒绝（SELinux 没拦钩子重启 smb）。
- lo 兜底：`smbclient -L //127.0.0.1 -U <装机账号>` 插着线时能列出共享。
- 验收：`sudo python3 <包>/accept/cart_accept.py --only '#95'`（仓库里是 `scripts/cart_accept.py`），拔线时 smb 是 SKIP，插线时 PASS。
