# 部署清单 1.16.01

从 **1.16.00**（BUILD `323 2b28f24`，以推车上 `cat /opt/pxe/VERSION /opt/pxe/BUILD` 为准）升级到 1.16.01。
本清单只写步骤，**由人在现场确认后执行**。`<推车>` 是推车的 ssh 地址，尖括号内容按现场填写，不进仓库。

和 1.16.00 一样一条命令装完（部署包里的 `install.sh` 就是 `scripts/install-1.16.01.sh`）：

```bash
sudo systemd-run --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.01/install.sh
journalctl -fu ops-cart-deploy          # 看进度; Ctrl-C 只停看日志, 不停安装
```

跑完看结果：`journalctl -u ops-cart-deploy --no-pager | tail -50`。

`install-1.16.01.sh` 是照 `install-1.16.00.sh` 复制出来改的，三轮审查的改进全部保留：systemd-run 下运行、INT/TERM/HUP 的 trap、`deploy.marker` 防重跑（`done=1` 区分"装完只是验收被打断"）、`rollback.sh` 写死部署前开着的服务名单、按 manifest 核对部署包、`python3 -I`、半截备份自动删、打包只打 `git ls-files`。
**为什么复制一份而不是泛化成带版本参数的脚本**：两版的中间步骤完全不同（1.16.00 是新单元 + 重写 PXE 菜单，1.16.01 是 #95 迁移），泛化要么塞一堆按版本开关的分支，要么再造一套插件机制，都比两份各自直白的脚本难审。公共的骨架（trap、marker、包核对）是逐行照抄的，测试 `tests/test_install_1601.py` 也照 `test_install_script.py` 的写法覆盖了同样的路径。

推车不是 1.16.00，脚本会停下；确认无误后可以 `sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.01/install.sh` 跳过版本检查（跳不过"上次装到一半"那道）。

> 怎么得出的：`git diff --name-status 2b28f24..<release/1.16.01 的提交>`。2026-11-08 用非 root 账号对过推车：`/opt/pxe` 下 77 个文件（`*.py` 和 `VERSION`）的 sha256 和 `2b28f24` 完全一致，没有现场手改；`kiosk.service`、`cart-dash.service`、`cart-mcp.service` 和仓库一致，`pxe-server.service` 是旧版（`--log /opt/pxe/pxeserver.log`）。

## 0. 这一版改了什么（相对 1.16.00）

| PR | 变化 | 对推车的影响 |
|---|---|---|
| #92 | 1.16.00 发版本身（安装 / 打包脚本最后两轮的小改） | 推车上跑的 `2b28f24` 是 #92 合并前的提交；后面两轮只动 `install-1.16.00.sh` / `pack-1.16.00.sh` / 清单，推车不受影响 |
| #90 | README 重写、`scripts/readme_due.py` | 不上车 |
| #86 | 串口观察模式（#85）：人登录后画设备前面板，端口灯跟真机 | 新 `observe_core.py`、`observe_panel.py`；换 `serial_panel.py`、`survey_core.py`、`dash_core.py`、`dash_render.py`、`kiosk.py`。**没有新单元、没有必需的配置、没有新依赖**（只用标准库）。可选覆盖文件 `/opt/pxe/observe_layouts.json`、`/opt/pxe/observe_profiles.json`，推车上都没有，不用建。快照目录 `/run/ops-cart/observe` 由 kiosk 自己建（`/run/ops-cart` 已由 tmpfiles 建好） |
| #94 | 确认框 Tk 测试夹具；验收 #38 没链路时不误报 | 只改测试和 `scripts/accept_core.py`、`cart_accept.py` |
| #96 | #95：pxeserver.log 挪进 `/var/log/ops-cart`、smbd 没链路不崩 | 换 `kiosk_core.py`、`lamps_core.py`；**`pxe-server.service` 改日志路径；`logrotate.d/ops-cart` 去掉 `/opt/pxe` 那行；手工 Samba 加 lo + lan1 drop-in + NM 钩子**（第 4、5 步，照 [`MIGRATE_95.md`](MIGRATE_95.md)） |
| #97 | 车机大肥鱼面板（#53） | 新 `bigfish_core.py`、`bigfish_panel.py`；换 `kiosk.py`、`kiosk_ui_core.py`、`dash_core.py`、`dash_render.py`。**不碰任何 bigfish 目录**，两个目录由大肥鱼的 `setup-root.sh` 建（接口见 [`BIGFISH_PANEL.md`](BIGFISH_PANEL.md)） |

**单元 / 系统件改动**（`git diff 2b28f24 -- deploy/`）：

- `pxe-server.service`：`--log /var/log/ops-cart/pxeserver.log`；
- `logrotate.d/ops-cart`：只剩 `/var/log/ops-cart/*.log`；
- `srv-data/smb-lan1.conf`（新）→ `/etc/systemd/system/smb.service.d/ops-cart-lan1.conf`；
- `srv-data/50-ops-cart-smb`（改）→ `/etc/NetworkManager/dispatcher.d/50-ops-cart-smb`（推车上原来没装）；
- 没有新单元；`tmpfiles.d`、firewalld、SELinux 模块、字体都没变。
- **不装** `srv-data/smb-ops-cart.conf`（数据盘那份 drop-in：没挂 `/srv/data` 时 smbd 永远不起），**不跑** `setup-samba.sh`（会用生成的 `smb.conf` 盖掉手工配置）。脚本里两样都没有，测试守着。

**依赖**：没有新的。观察模式和大肥鱼面板只用标准库；`testparm`、`restorecon` 推车上都有（samba 4.23.5）。

## 1. 部署前

开发机（在 `release/1.16.01` 上）：

```bash
git log -1 --oneline
python3 -m pytest -q                    # 全绿 (别设 OPS_TK_TESTS: Mac 上批量跑 Tk 会 abort)
python3 scripts/check_public_safe.py .
```

推车上（只读，2026-11-08 的结果写在后面）：

```bash
cat /opt/pxe/VERSION /opt/pxe/BUILD                 # 1.16.00 / 323 2b28f24
ls /opt/pxe/tftp/grub.cfg-* 2>/dev/null             # 应该没有; 有 = 有机器正在装, 脚本会拦
systemctl is-active kiosk cart-dash cart-mcp cart-mcp-ro webkiosk pxe-server
systemctl is-failed smb logrotate                   # 现在两个都是 failed (#95), 装完应该都不是
grep -nE '^\s*interfaces' /etc/samba/smb.conf       # 一行, 不带 lo
ls -ld /home/bigfish/status /run/bigfish-ctl        # 0711 bigfish:bigfish / 0750 root:bigfish
```

2026-11-08 只读结果：VERSION/BUILD 如上；没有解锁中的菜单；上面这些服务都 active（`cart-mcp-ro` 在 1.16.00 之后被手工开了，这次也要重启）；`smb` failed（eno1 unavailable，没插线）、`logrotate` failed（11-08 0 点 `error opening /opt/pxe/pxeserver.log: Permission denied`）；`smb.conf` 手工写的、`interfaces` 一行不带 `lo`、`bind interfaces only = yes`，当前文件 `testparm` 通过，加上 `lo` 的样稿 `testparm` 也通过；`smb.service.d/` 不存在，NM 钩子没装；`/var/log/ops-cart/pxeserver.log` 不存在。大肥鱼：`bigfish` 用户 / 组都在（uid 1001），`/home/bigfish/status` 0711 bigfish:bigfish，`bigfish.json` 0644 bigfish:bigfish、心跳正常（几秒内）、`schema 1`、带 v1.1 的 `estop.seq`（null）、`estop.seen` false；`/run/bigfish-ctl` 0750 root:bigfish（大肥鱼的 tmpfiles `bigfish.conf` 建的），非 root 进不去，正常。kiosk 和 cart-dash 都是 root、单元里没有 `ProtectHome`，读写这两处不受权限限制。

**执行前现场再确认一次（人工，脚本不查）**：

- 没有正在装机或正在 PXE 的机器（这版要停 / 起 pxe-server，脚本只拦"有 MAC 解锁着"）；
- 推车屏幕没人在用（kiosk 会回到锁屏）；
- 没有 Agent 正在用 MCP（`cart-mcp` / `cart-mcp-ro` 会重启，HTTP 会话断开）；
- 没人往 `\\<装机口>\win10` 拷东西（smb 会重启；LAN1 没插线时它本来就没在跑）。

这次**不重启** `kvm-broker`、`pxe-http-boot`、`pxe-http-repo`、`cart-ocr`（代码没变），键鼠租约不会丢。

## 2. 备份（脚本第 1 段）

照 1.16.00，备份在 `/opt/pxe/.bak-<时间戳>/`：`*.py`、`VERSION`、`BUILD`、`web/`、`icons/`、`/etc/ops-cart`（整份）、`etc/systemd/pxe-server.service`、`etc/logrotate.d/ops-cart`，以及这版新加的 `samba/smb.conf`（还有部署前已有的 lan1 drop-in / NM 钩子，这台推车上都没有）。
这版不动 PXE 菜单，不再备份 `tftp/grub.cfg*` 和 `pxe-mode.json`。

`rollback.sh` 写好后才落 `deploy.marker`（`from=1.16.00`、`to=1.16.01`）；装完追加 `done=1`。1.16.00 那次留下的 `.bak-20261108-181843/deploy.marker` 写的是 `to=1.16.00`，不会挡这次。

任何改动之前脚本先核对：

- 包里 `BUILD` 不带 `-dirty`，`VERSION` 是 1.16.01；包里该有的都在（`systemd/pxe-server.service`、`etc/logrotate.d/ops-cart`、`smb/` 两份、`accept/` 三份、`opt/kiosk.py` / `bigfish_core.py` / `observe_core.py`）；
- `accept/accept-manifest.json` 和包对得上：`/opt/pxe` 文件、单元、`/etc/logrotate.d/ops-cart` 的 sha256，`build` 字段；`files` 非空、`/opt/pxe` 不少于 60 个；
- `smb/` 两份不在验收清单里（`setup-samba.sh` 的推车不装 lan1 drop-in，进清单会误报），按包里的 `smb/SHA256SUMS` 核对；
- Samba：`smb.conf` 不是 `setup-samba.sh` 生成的、数据盘那份 drop-in 没装、`testparm` 在、**现在的 `smb.conf` 能过 `testparm`**、`interfaces` 恰好是 `[global]` 里的一行。任何一条不满足就停，什么都没动（`smb.conf` 不存在 = 没装 Samba，跳过 smb 那段）。

包里有、manifest 没列的文件会列成警告，照样装。这一版预期看到的：`approval_core.py`、`approval_channel.py`、`totp_core.py`（#77 还没接线）、`samba_core.py`、`srv_data_core.py`（数据盘那套）——以打包时的实际输出为准，记在 PR 里。

## 3. 部署包（开发机上打，传到推车 `/tmp`）

```bash
OUT=$(mktemp -d)
scripts/pack-1.16.01.sh $OUT          # 工作区不干净就拒绝; 生成 BUILD 和 manifest; 只打 git ls-files 里的文件
ssh <推车> 'test -e /tmp/deploy-1.16.01 && mv /tmp/deploy-1.16.01 /tmp/deploy-1.16.01.old-$(cut -d" " -f2 /tmp/deploy-1.16.01/opt/BUILD) || true'
COPYFILE_DISABLE=1 tar -C $OUT -cf - deploy-1.16.01 | ssh <推车> 'tar -C /tmp -xf -'
# 推车上非 root 比 sha256: 开发机上的清单 vs 推车上的
( cd $OUT/deploy-1.16.01 && find . -type f | sort | xargs shasum -a 256 ) > $OUT/sums
ssh <推车> 'cd /tmp/deploy-1.16.01 && sha256sum --quiet -c -' < $OUT/sums && echo 一致
```

包的布局：`opt/`（落到 `/opt/pxe`）、`systemd/pxe-server.service`、`etc/logrotate.d/ops-cart`、`smb/`（`smb-lan1.conf`、`50-ops-cart-smb`、`SHA256SUMS`）、`accept/`、`tk-test/`（`conftest.py`、`test_confirm_panel.py`、`test_bigfish_panel.py`）、`install.sh`。

## 4. 代码、pxeserver.log、logrotate（脚本第 2–4 段）

```bash
sudo install -m 644 opt/*.py opt/VERSION opt/BUILD /opt/pxe/
sudo cp -r opt/web/. /opt/pxe/web/ && sudo cp -r opt/icons/. /opt/pxe/icons/
# #95: 先停 pxe-server (开着文件时挪走, 进程会接着写旧位置)
sudo systemctl stop pxe-server
sudo install -m 644 systemd/pxe-server.service /etc/systemd/system/
sudo systemctl daemon-reload
[ -f /opt/pxe/pxeserver.log ] && sudo mv -n /opt/pxe/pxeserver.log /var/log/ops-cart/pxeserver.log
[ -f /var/log/ops-cart/pxeserver.log ] && sudo restorecon -v /var/log/ops-cart/pxeserver.log   # mv 带着 usr_t 过去
sudo systemctl start pxe-server            # 部署前开着才起
sudo install -m 644 etc/logrotate.d/ops-cart /etc/logrotate.d/ops-cart && sudo restorecon /etc/logrotate.d/ops-cart
```

`mv -n` 没挪（新位置已有文件）时脚本打警告，不停；看一眼留哪份，旧的不要就删。
`/etc/ops-cart` 一个文件都不写。

## 5. Samba（脚本第 5 段，#95）

### smb.conf 这一步为什么写进脚本自动做

选了**脚本自动做**，理由：

- 改动是机械的、可以精确判定：只在 `[global]` 里恰好一行 `interfaces` 时，在值前面加 `lo`，其他字节一个不动（测试按整份文件比对）。不是这个形状就整个不装，交给人。
- 两道 `testparm`：先校验改好的**样稿**（临时文件），过了才原地写回（`cat >`，属主、权限、SELinux 标签不变）；写回后再对正式文件跑一次，看 `interfaces = lo ...` 真的生效，不对就从备份换回并停下。部署前还先确认**现在的**文件能过 `testparm`，免得把原本就坏的配置算到这次头上。
- 手工 `sudoedit` 是这一步里最容易出错的环节（打错地址、删掉 `bind interfaces only`），而且要在 systemd-run 之外另开一段需要 sudo 的交互，ssh 断了没有 trap 兜底。
- 回滚能完整还原：`smb.conf` 从备份 `cp -a` 回去，lan1 drop-in 和 NM 钩子部署前没有就删掉，然后 `try-restart smb`。（`MIGRATE_95.md` 说过加了 `lo` 旧版本也能用，回滚时其实可以不撤；为了"回滚 = 回到部署前"这条简单的约定，脚本还是撤了。要保留 #95 的 smb 修复，回滚后照第 5 步的手工命令再装一遍即可。）

脚本做的事（手工时照这个做）：

```bash
[ -e /etc/samba/smb.conf.bak-95 ] || sudo cp -a /etc/samba/smb.conf /etc/samba/smb.conf.bak-95   # 手工时备份; 脚本备份在 .bak-<时间戳>/samba/
sudoedit /etc/samba/smb.conf            # interfaces = <装机口地址>/<前缀>  ->  interfaces = lo <装机口地址>/<前缀>
sudo testparm -s >/dev/null && testparm -s 2>/dev/null | grep -E '^\s*interfaces'     # 不过就换回备份
sudo install -d -m 755 /etc/systemd/system/smb.service.d
sudo install -m 644 smb/smb-lan1.conf /etc/systemd/system/smb.service.d/ops-cart-lan1.conf
sudo install -m 755 smb/50-ops-cart-smb /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo restorecon /etc/systemd/system/smb.service.d/ops-cart-lan1.conf /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
sudo systemctl daemon-reload
sudo systemctl reset-failed smb.service
sudo systemctl restart smb.service      # LAN1 没地址: ExecCondition 跳过, inactive (不算 failed)
```

**注意**：推车的 `smb.conf` 有 `hosts allow = <装机网段>.`，所以加了 `lo` 之后 smbd 会**监听** 127.0.0.1，但 127.0.0.1 来的连接仍被 `hosts allow` 拒掉。`lo` 在这里只是"永远有地方绑、不 `smb_panic`"的兜底，够用；`MIGRATE_95.md` 2.4 里的 `smbclient -L //127.0.0.1` 在这台推车上**会被拒**，用 `ss -ltn` 看监听来代替（第 8 步）。要不要把 `127.0.0.1` 加进 `hosts allow` 由维护者定，这次不改。

## 6. 默认不开的东西

这版没有新的。`cart-ble-input` 照旧不开；`cart-mcp-ro` 推车上已经手工开了，照常重启。

## 7. 重启（脚本第 6 段，开着的才重启）

按 import 关系算的（`2b28f24..HEAD` 改过的顶层模块：`bigfish_*`、`observe_*`、`dash_core`、`dash_render`、`kiosk`、`kiosk_core`、`kiosk_ui_core`、`lamps_core`、`serial_panel`、`survey_core`）：

| 服务 | 怎么处理 | 为什么 |
|---|---|---|
| kiosk | restart | 车机界面、观察模式、大肥鱼面板；回到锁屏 |
| cart-dash | restart | `dash_core` / `dash_render`（前面板、大肥鱼一行）、`lamps_core`（PXE 灯读新日志路径） |
| webkiosk | restart | import `kiosk_core`（PXE 日志路径） |
| cart-mcp、cart-mcp-ro | restart | import `kiosk_core` / `survey_core` / `dash_core` 等 |
| pxe-server | 第 4 步 stop → 换单元 → start | 只换单元（日志路径）；代码没变 |
| smb | 第 5 步 reset-failed + restart | 新 interfaces / drop-in |
| logrotate | 第 7 段 start 一次 | 清掉 11-08 的 failed |
| kvm-broker、pxe-http-boot、pxe-http-repo、cart-ocr | **不动** | 代码和单元都没变 |
| cart-serial-req、cart-notify、cart-dream、cart-boot-success | 不用 | path / timer 每次新起进程 |

部署前开着的服务里有没起来的，就停下并打印回滚命令；之后 smb 还是 failed、logrotate 还是失败只打印出来，不回滚（回滚也修不好它们）。

## 8. 验证

脚本最后自己跑一遍验收，报告存到 `/var/log/ops-cart/accept-1.16.01-<时间戳>.txt`；验收 FAIL 不会自动回滚。手工重跑：

```bash
cd /tmp/deploy-1.16.01/accept
sudo python3 cart_accept.py --expect-version 1.16.01 --expect-build "$(cat ../opt/BUILD)"
sudo python3 cart_accept.py --only '#95'      # LAN1 没线: smb SKIP; logrotate PASS
journalctl -u kiosk -u cart-dash -u webkiosk -u pxe-server -u cart-mcp -u cart-mcp-ro --since "-5 min" -p warning
```

必须 PASS：VERSION、BUILD、必带 py 模块（含 `bigfish_*`、`observe_*`）、系统文件 sha256、开着的单元、系统 python3 import 全部模块、MCP 工具表、`logrotate.service`；`smb.service` LAN1 没线时 SKIP，插线后 PASS。

车边：

- [ ] 车机回到锁屏，解锁后界面正常；锁屏右上角有"大肥鱼 · 空闲 / 在做事 · 第 N/M 步"一行；副屏锁屏页也有这一行，都不含大肥鱼的原话。
- [ ] 主菜单"大肥鱼"进面板：状态和 `bigfish.json` 对得上（心跳在，不显示"离线 / 不在线"）；大肥鱼的话每行带"大肥鱼说："前缀、等宽、缩进；顶上**没有**"急停通道未就绪"（`/run/bigfish-ctl` 在）；"确认 / 拒绝"按钮平时是灰的。**急停联调另外安排，这次不按急停。**
- [ ] 观察模式（#85）：在车机串口面板或终端里先人工登录交换机，到用户视图提示符 `<设备名>`；点"观察" →"开始观察"，确认框里设备名对；前面板端口数、排布和真机一致，插拔一根线灯跟着变；有人 attach 时提示"轮询暂停"；点"退出观察"后 console 上没有多出任何字节；人主动锁屏会停观察；副屏工作页温度区换成前面板，锁屏页不露设备名。
- [ ] smb 在 LAN1 没插线时不崩：`systemctl --failed` 里没有 smb，`systemctl status smb` 是 inactive、日志里有 `Skipped due to 'exec-condition'`，`coredumpctl list smbd` 没有新的；`systemctl show -p DropInPaths smb.service` 里有 `ops-cart-lan1.conf`。
- [ ] 插上 LAN1、等地址出来：`systemctl is-active smb` 是 active；`ss -ltn` 里 SMB 端口（`samba_core.SMB_PORT`，和 `smb.conf` 的 `smb ports` 一致）能看到 127.0.0.1 和装机口地址两条；`journalctl -u NetworkManager-dispatcher -b --no-pager | tail` 没报错；`sudo ausearch -m avc -ts recent | grep -E '50-ops-cart-smb|systemctl'` 没有拒绝。（`smbclient -L //127.0.0.1` 会被 `hosts allow` 拒，见第 5 步，不算失败。）
- [ ] logrotate 能正常跑：`ls -lZ /var/log/ops-cart/pxeserver.log` 是 `var_log_t`；`sudo logrotate -d /etc/logrotate.d/ops-cart 2>&1 | grep -i error` 没输出；`systemctl is-failed logrotate.service` 是 inactive；第二天 0 点后 `journalctl -u logrotate --since today` 没有 `Permission denied`。
- [ ] 车机装机面板和副屏 PXE 灯读到新日志：被测机 PXE 一次后，面板和灯有反应。

Tk 测试在车机真屏上跑（会在车机屏幕上短暂开关几个窗口，没人用车机时跑）：

```bash
cd /tmp/deploy-1.16.01/tk-test
sudo env DISPLAY=:0 XAUTHORITY=/root/.Xauthority PYTHONPATH=/opt/pxe python3 -m pytest -q -p no:cacheprovider test_confirm_panel.py test_bigfish_panel.py
```

- 预期 **21 passed、0 skipped**：`test_confirm_panel.py` 16 条（#94 修过夹具）、`test_bigfish_panel.py` 5 条（急停按钮只接到测试里的回调，不写 estop.json、不调 systemctl，不会真停大肥鱼）。**skip 不算过**——有 skip 说明没连上显示。
- `OPS_TK_TESTS=1` 只是 macOS 上的开关，推车是 Linux，不用设。推车 pytest 6.2.2 不认 `pytest.ini` 的 `pythonpath`，所以用 `PYTHONPATH=/opt/pxe` 测装好的代码。
- 待核对（1.16.00 留下的）：kiosk 是 root 用 `xinit` 起的 X（`xinit ... -- /usr/bin/X :0 vt7 ...`，没有 `-auth` 参数），授权在不在 `/root/.Xauthority`。连不上显示就 `sudo ls -a /root` 找 `.serverauth.*`，或者 `sudo xauth list`。

## 9. 回滚

```bash
sudo systemd-run --unit=ops-cart-rollback --collect bash /opt/pxe/.bak-<时间戳>/rollback.sh
journalctl -fu ops-cart-rollback
```

脚本出错停下时，会把这条命令连同具体时间戳一起打印出来。回滚中途失败会打印"回滚第 N 行失败"，修好后直接重跑同一条命令。

它做的事：

```bash
B=/opt/pxe/.bak-<时间戳>
sudo cp -a $B/*.py $B/VERSION $B/BUILD /opt/pxe/
sudo rm -rf /opt/pxe/web /opt/pxe/icons && sudo cp -a $B/web $B/icons /opt/pxe/
sudo install -m 644 $B/etc/systemd/pxe-server.service /etc/systemd/system/      # 旧单元写回 /opt/pxe/pxeserver.log
sudo cp -a $B/samba/smb.conf /etc/samba/smb.conf                              # 去掉 lo
sudo rm -f /etc/systemd/system/smb.service.d/ops-cart-lan1.conf /etc/NetworkManager/dispatcher.d/50-ops-cart-smb   # 部署前没有
sudo systemctl daemon-reload
sudo systemctl try-restart smb.service
# 重启部署前开着的那几个 (名单写死在 rollback.sh 里, 含 pxe-server)
cat /opt/pxe/VERSION    # 回到 1.16.00
```

- **保留新的 `logrotate.d/ops-cart`**（不带 `/opt/pxe` 那行）：装回旧的，logrotate 又会每次轮到 `/opt/pxe/pxeserver.log` 就失败。代价是回滚期间 `/opt/pxe/pxeserver.log` 不轮转（一周几百 KB，可以接受）。
- 挪到 `/var/log/ops-cart/pxeserver.log` 的那份留着；下次升级 `mv -n` 不会覆盖它，会提示两份都在。
- 撤掉 smb 修复后，LAN1 没线开机时 smbd 又会崩（回到 #95 之前）；要保留就照第 5 步再手工装一遍。
- 1.16.01 新加的 `.py`（`bigfish_*`、`observe_*`）留着无害，旧代码不 import。
- `/etc/ops-cart` 部署时没动，回滚也不用动；`$B/etc/ops-cart` 是保险。
