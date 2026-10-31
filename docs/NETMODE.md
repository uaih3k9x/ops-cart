# 网口模式（#12 第一批，含 #25；10/31 现场反馈修订）

LAN1 / LAN2 按网口管：每个口同一时间一个**模式**，每个模式都能 **undo**。
取代原来的「网口配置文件」面板和「LAN2 管理口配置」两个入口（#8：两套入口改同一个口，手感不对）。

代码：[`netmode_core.py`](../netmode_core.py)（纯逻辑，单测全覆盖）· [`netmode_panel.py`](../netmode_panel.py)（Tk 面板）。

## 模式

| 模式 | 网口 | 建的连接 | 要点 |
|---|---|---|---|
| 关闭 | LAN1 | `cart-eno1-off` | `ipv4.method disabled`、`ipv6.method disabled`；LAN1 的干净状态 |
| DHCP 客户端 | LAN1 / LAN2 | `cart-<口>-dhcp` | LAN2 的干净状态；LAN1 上加 `never-default`（装机口不当出口） |
| 静态 IP | LAN1 / LAN2 | `cart-<口>-static` | 一页表单：地址/前缀、网关、经网关去的网段、DNS、出口 |

「交还旧配置」10/31 去掉了：旧连接在第一次接管这个口时备份后删掉（见下面「接管时删旧配置」）。

PXE、共享上网、透明桥、热点是后续批次；框架已留好：记录的 `ports` 可写两个口（占用期间另一个口不能单独切），
占用里可记内核参数原值（`set_sysctl` / `sysctl_res`，undo 时写回）。

所有推车建的连接：`connection.autoconnect yes`、`autoconnect-priority 50`（高过旧连接的默认 0，重启后 NM 先连推车托管的那条），
uuid 由推车定好写进状态文件，undo 只按 uuid 删，**不按名字前缀匹配**。

### 静态表单与出口优先级（#25）

- 地址/前缀走 `netprof_core.static_form`：必须写前缀，不猜 /24；对端地址可选，只防撞址和「按对端预填」。
- 网关、经网关去的网段、DNS 留空时显式写 `""`，不留旧值。
- **主出口只有 Wi-Fi**（10/31）。有线出口二选一，默认「不当出口」：

| 出口 | nmcli | 说明 |
|---|---|---|
| 不当出口 | `ipv4.never-default yes`、`ipv6.never-default yes`、`ipv4.gateway ""` | 只连同网段（加上「经网关去的网段」） |
| 备用出口（只有 LAN2） | `ipv4.gateway <网关>` + `never-default no` + `route-metric 1000`（v4/v6 同） | 大于 Wi-Fi 的 900，无线断了才走它 |

- **网关框在静态模式下永远能填**（10/31 现场反馈：原来「不当出口」时网关置灰，LAN1 全不能点）。
- **LAN2**：填了网关而出口还是「不当出口」→ 自动改成「备用出口」，提示行写明；清空网关 → 自动回到「不当出口」。
  只在网关框变化时纠正；核心层同样把「不当出口 + 网关」当备用出口（`static_params` 规整，`_settings` 对老记录也兜一次），
  `never-default yes` 和 `ipv4.gateway` 不会同时写出去。备用出口网关必填。
- **LAN1 永不当出口**（连备用都不当，面板上「备用出口」置灰，核心层拒绝），免得推车默认从装机网出去。
  它的网关只给「经网关去的网段」用：逗号分隔的 `网段/前缀`，写成 `ipv4.routes "198.51.100.0/24 192.0.2.254, …"`，
  同时 `never-default yes`、`ipv4.gateway ""`。校验：必须是网段号/前缀；不许 `0.0.0.0/0`（那就是当出口）；
  不许和本口网段重叠；填了网段没填网关报错。填了网关没填网段不拦，提示行写「网关现在不起作用，要去哪些网段写进…」。
  LAN2 也能填这一栏（备用出口时多余但无害）。
- 1.09.00 记下的 LAN2「主出口」老记录：列表显示「主出口 (旧版, 再建按备用)」，失败恢复要重建它时按备用出口建（不至于切不走）；
  面板表单落回「不当出口」，新填的表单不给主出口。
- metric 常量在 `wifi_core`：`WIFI_METRIC` / `WIRED_BACKUP_METRIC`；`WIRED_PRIMARY_METRIC` 网口模式不再用，留给 #7。
- IPv6 只对齐 `never-default` / `route-metric`，`ipv6.method auto`，不配地址。

## 切换流程

```
写状态文件 (新模式 status=applying, stale=当前模式的占用)    <- 断电也知道要撤什么
undo 当前模式 (按占用反序; 旧连接这时不动)
apply 新模式 (LAN1 先停 PXE 三件套; con add 定好 uuid; con up —— 确定没插线就不 up, 插上 NM 自动连)
  成功 -> 状态文件写 status=ok, 记 prev (「回到上一个」用; 旧连接不进 prev)
          原来是未托管 (接管) -> 备份这个口的旧连接 -> 删 (备份不成就不删)
  失败 -> undo 新模式 -> 恢复原模式 (托管的用记下的参数重建; 未托管的旧连接还没删, 直接 up 回去)
          恢复也失败 -> 撤掉恢复的那份 -> 落到干净状态 (LAN1 关闭 / LAN2 DHCP)
```

和 issue 原文"undo → 干净状态 → apply"的差别：干净状态只在失败兜底时显式落；正常切换不先落一次干净状态再切，
免得管理口多断一次。undo 之后、apply 之前那一刻就是"推车自己的东西都撤了"的干净态。

每次 apply / undo / 删旧配置写一行 `events.log`（`[netmode]`），apply 成功那行带上撤销命令，远程断了也能在日志里找到。

### 接管时删旧配置（10/31）

某个口第一次从「未托管」切到推车的模式（含「撤到干净状态」）时，**新模式 apply 成功之后**删掉这个口上的旧连接；
失败回滚时旧连接还在，直接 up 回去。已托管的口再切换不删任何旧连接。

删哪些（`netmode_core.old_cons`，保守）：以太网连接、不在状态文件的占用里（推车自己建的不算），并且满足其一：

- `connection.interface-name` 就是这个口（面板刷新时对每条以太网连接跑 `nmcli -t con show uuid <u>` 读）；
- 正在这个口上生效（那它不可能绑在别的口上；没绑网口的也算，它就在这儿用着）。

不删：没绑网口又没在这个口上生效的（如 `Wired connection 1`，可能在别的口上起）、绑在别的口上的、Wi-Fi、tun 等非以太网。
读不到详情的连接只认正在这个口上生效的那条。

删之前备份（`netmode_core.backup_cons`，目录可注入）：`/var/lib/ops-cart/nm-backup/<YYYYmmdd-HHMMSS>/`（0700），
keyfile 原样复制（先找 `/etc/NetworkManager/system-connections/<名>.nmconnection`，对不上 uuid 就在目录里按 `uuid=` 找）；
没有 keyfile 就存 `nmcli -t con show uuid <u>` 的输出为 `<名>.nmcli.txt`。**任何一条备份不成就一条不删**（fail-closed），
状态行写「旧配置 X 没删: 备份失败 (…)」——留着不碍事，推车的连接 `autoconnect-priority` 更高。
每删一条记一行 `events.log`：`删旧配置 LAN1 Lan1-pxe (<uuid>): 成功, 备份在 …`。

现场 LAN1 的 `Lan1-pxe` 给装机口提供地址（PXE 服务绑的那个）：删了以后用静态 IP 在 LAN1 重建同样的地址。
要删的旧连接名字带 `pxe`、或者接管 LAN1 时 `pxe-server` 在跑，确认框加一句「装机口地址要用静态 IP 重建, PXE 服务会停.」。

「回到上一个」：1.09.00 可能在 `prev` 里记了旧连接，现在直接提示「交还旧配置去掉了…」，不弹框。

Wi-Fi（`wlp4s0`）永远不碰：不在可选网口里，生成的命令里不会出现它（有单测守着）。

## 状态文件 `/var/lib/ops-cart/netmode.json`

原子写（`atomicfile`），路径可注入。没有文件 = 首次运行：两个口按现状显示「未托管 · 旧配置「…」」，**不自动改任何东西**。
文件坏了按未托管显示并提示，下一次切换会覆盖。

```json
{
 "version": 1,
 "modes": [
  {"mode": "static", "ports": ["enp3s0"],
   "params": {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "", "egress": "backup", "routes": ""},
   "res": [{"con": "<uuid>", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
   "status": "ok", "since": 1793300000}
 ],
 "prev": {"enp3s0": {"mode": "dhcp", "ports": ["enp3s0"], "params": {}}}
}
```

- `res`：这个模式占的东西，undo 按反序撤。`{"con": uuid}` 删连接；`{"sysctl": key, "orig": 原值}` 写回原值（后续批次用）。
- `status`：`ok` / `applying`（切到一半断电，面板标"上次切换没做完"，下次切换连 `stale` 一起撤）/ `failed`（连干净状态都没起来）。
- `stale`：上次没撤掉的占用（切到一半断电，或 undo 命令真失败了），下次切换一起撤，面板上一行黄字提示。
  切换失败回到旧连接但还有遗留时，留一条 `"mode": "legacy"` 的记录专门带着它。
- 未托管且没有遗留的口不进 `modes`。

## 面板（车机：网络 → 网口模式；顶栏 LAN1 / LAN2 灯点进来直接选中那个口）

```
网口模式 · 每个口同一时间一个模式, 都能撤 · 无线不归这里管(留作后路)
+--------------------------------------------------------------------------------+
| LAN1 eno1    未托管 · 旧配置「Lan1-pxe」   192.0.2.1/24      已插线  不出网       |
| LAN2 enp3s0  静态 IP 192.0.2.5/24 ... 备用出口  192.0.2.5/24  已插线  备用出口 (metric 1000) |
+--------------------------------------------------------------------------------+
(选中的口和记录对不上时这里一行黄字: 记录是 X, 实际生效的是 Y / 上次切换没做完)
+- LAN2 (enp3s0) 改模式 ---------------------------------------------------------+
| 模式: ( )关闭  ( )DHCP 客户端  (o)静态 IP                                        |
| 模板: [手填 v]  本机 地址/前缀: [192.0.2.5/24 ]  对端(可选): [      ] [按对端预填] |
| 网关: [192.0.2.1   ]  DNS(可空, 逗号分隔): [                ]                     |
| 经网关去的网段(可空, 逗号分隔): [                                    ]            |
| 出口: ( )不当出口  (o)备用出口                                                    |
| 推车用 192.0.2.5/24 (网段 192.0.2.0/24) · 备用出口: metric 1000 大于无线的 900, 无线断了才走它; 清空网关回到不当出口; 切之前还会确认 |
| [应用]  [撤到干净状态]  [回到上一个]  [刷新]                                       |
+--------------------------------------------------------------------------------+
状态行 (成功绿 / 失败红)
```

- 不能用的选项置灰：LAN2 没有「关闭」；LAN1 的「备用出口」；非静态时地址、网关、经网关去的网段、DNS。网关在静态模式下永远能填。
- 点「应用」先在本地校验（错了直接在状态行说错在哪，不弹框），再弹确认框：

```
LAN2 (enp3s0) 切换网口模式

原来: 未托管 · 旧配置「enp3s0」 · 198.51.100.7/24
改为: 静态 IP 192.0.2.5/24 网关 192.0.2.1 · 备用出口

LAN2 是管理口, 切换时经它的远程连接会断 (无线和 tailscale 还在).
将删除旧配置: enp3s0 (新模式起来之后才删, 删之前备份到 /var/lib/ops-cart/nm-backup/<时间>/; 备份不成就不删).

将执行的命令:
  nmcli con add type ethernet ifname enp3s0 con-name cart-enp3s0-static connection.uuid <新> ...
  nmcli con up uuid <新>
  成功之后, 备份再删旧配置:
    nmcli con delete uuid <旧连接>
出问题时的 undo 命令 (切换失败会自动执行; 车边终端也能照抄):
  撤掉新模式:
    nmcli con delete uuid <新>
  回到原来:
    nmcli con up uuid <旧连接>

要继续吗?
```

## 和旧入口的迁移

- 车机「网络」页：「网口配置文件」和「LAN2 管理口配置」都撤了，只剩「网口模式」。顶栏 LAN1 / LAN2 灯也指向它。
- 推车上已有的旧连接（`Lan1-pxe`、`enp3s0`、旧面板建的 `cart-<口>-<种类>-<名字>`）：打开面板不动，显示「未托管 · 旧配置「…」」；
  第一次接管那个口时（新模式起来之后）备份到 `/var/lib/ops-cart/nm-backup/` 再删（10/31 改，原来是一条不删 + 「交还旧配置」）。
- 想回到旧的装机地址：LAN1 选静态 IP，填原来的地址/前缀（不当出口）；PXE 服务不自动开，去「装机」页开。
  真要原样恢复旧连接：把备份目录里的 `.nmconnection` 拷回 `/etc/NetworkManager/system-connections/`（0600），`nmcli con reload`。
- 旧面板的能力：新建 DHCP / 静态、切换、回退上一个、设备出厂地址模板、按对端预填都并进新面板；「删除配置文件」没迁。
- `kiosk_core.nics_status` 改成读网口上实际生效的连接，网页口「网卡」卡片显示的连接名 / 获取方式跟着变。

## 部署注意

- 换 `netmode_core.py`、`netmode_panel.py`、`netprof_core.py`、`kiosk.py`、`kiosk_core.py`、`kiosk_ui_core.py`、`wifi_core.py`；
  **删 `/opt/pxe/netprof_panel.py`**（没人再 import 它，留着也跑不起来）。
- `pxeserver.py` 只用 `netprof_core.dhcp_probe`，没变；`pxe-server` 不用为此重启。
- 重启 `kiosk`（会回锁屏）；`webkiosk` / `cart-dash` 在跑的话也重启一下，`nics_status` 换了读法。
- `/var/lib/ops-cart/`（含 `nm-backup/`）第一次切换时自动建；不用改 tmpfiles。
- 10/31 这版只换 `netmode_core.py`、`netmode_panel.py`、`wifi_core.py`（只改了注释），重启 `kiosk`。
- 现场验证（只能在推车前做）：
  1. 打开「网口模式」：两个口都显示「未托管 · 旧配置「…」」，地址和默认出口对得上 `ip route`；什么都没动。
  2. LAN1 选静态，填装机口原来的地址/前缀（不当出口）→ 确认框列出 `systemctl stop pxe-*`、`nmcli con add … cart-eno1-static`、
     「将删除旧配置: Lan1-pxe (…备份到 /var/lib/ops-cart/nm-backup/<时间>/…)」「装机口地址要用静态 IP 重建, PXE 服务会停.」、undo 命令；
     点「否」什么都不变；点「是」后 `nmcli dev` 里 eno1 连着 `cart-eno1-static`、地址对，`nmcli con` 里没有 `Lan1-pxe`，
     `/var/lib/ops-cart/nm-backup/<时间>/Lan1-pxe.nmconnection` 在，`events.log` 有 undo / apply / 删旧配置三行 `[netmode]`；
     再去「装机」页开 PXE，确认 pxe-server 起得来。
  3. LAN1 填网关不填网段：提示行说网关不起作用；再填一个网段 → `ip route` 里有 `<网段> via <网关> dev eno1`，没有 eno1 的 default。
  4. LAN2 静态填网关（人在车边，Wi-Fi 连着）：出口自动跳到「备用出口」；应用后 `ip route` 里 LAN2 默认路由 metric 1000、排在 Wi-Fi 900 后面，
     旧连接 `enp3s0` 已备份并删除；再「撤到干净状态」回到 DHCP。
  5. LAN1 拔着线选静态（按设备出厂网段）：确认框写「没插线: 只把配置建好」，`nmcli con` 里有 `cart-eno1-static`；插上线后 NM 自动连上、地址出来。
  6. 故意切一个起不来的：LAN1 插着线但对面没有 DHCP，选 DHCP —— 约 45 秒后失败，自动回到原来的，状态行写「已回到原来的 …」，`events.log` 有 apply 失败 / 自动撤 / 恢复三行。
- 回滚：恢复备份的 `.py`；推车建的连接按 `netmode.json` 里的 uuid `nmcli con delete uuid <uuid>`；
  已删的旧连接从 `/var/lib/ops-cart/nm-backup/<时间>/` 拷回 `/etc/NetworkManager/system-connections/`（0600）、`nmcli con reload`，
  再 `nmcli con up id Lan1-pxe` / `nmcli con up id enp3s0`。
