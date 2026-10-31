# 网口模式（#12 第一批，含 #25）

LAN1 / LAN2 按网口管：每个口同一时间一个**模式**，每个模式都能 **undo**。
取代原来的「网口配置文件」面板和「LAN2 管理口配置」两个入口（#8：两套入口改同一个口，手感不对）。

代码：[`netmode_core.py`](../netmode_core.py)（纯逻辑，单测全覆盖）· [`netmode_panel.py`](../netmode_panel.py)（Tk 面板）。

## 模式

| 模式 | 网口 | 建的连接 | 要点 |
|---|---|---|---|
| 关闭 | LAN1 | `cart-eno1-off` | `ipv4.method disabled`、`ipv6.method disabled`；LAN1 的干净状态 |
| DHCP 客户端 | LAN1 / LAN2 | `cart-<口>-dhcp` | LAN2 的干净状态；LAN1 上加 `never-default`（装机口不当出口） |
| 静态 IP | LAN1 / LAN2 | `cart-<口>-static` | 一页表单：地址/前缀、网关、DNS、出口优先级 |
| 交还旧配置 | LAN1 / LAN2 | 不建 | 撤掉推车托管的，把选中的旧连接 up；= 退出托管 |

PXE、共享上网、透明桥、热点是后续批次；框架已留好：记录的 `ports` 可写两个口（占用期间另一个口不能单独切），
占用里可记内核参数原值（`set_sysctl` / `sysctl_res`，undo 时写回）。

所有推车建的连接：`connection.autoconnect yes`、`autoconnect-priority 50`（高过旧连接的默认 0，重启后 NM 先连推车托管的那条），
uuid 由推车定好写进状态文件，undo 只按 uuid 删，**不按名字前缀匹配**。

### 静态表单与出口优先级（#25）

- 地址/前缀走 `netprof_core.static_form`：必须写前缀，不猜 /24；对端地址可选，只防撞址和「按对端预填」。
- 网关、DNS 留空时显式写 `""`，不留旧值。
- 出口三选一，默认「不当出口」：

| 出口 | nmcli | 说明 |
|---|---|---|
| 不当出口 | `ipv4.never-default yes`、`ipv6.never-default yes` | 只连同网段；网关置灰、不写 |
| 备用出口 | `never-default no` + `route-metric 1000`（v4/v6 同） | 大于 Wi-Fi 的 900，无线断了才走它 |
| 主出口 | `never-default no` + `route-metric 100`（v4/v6 同） | 小于 Wi-Fi，默认路由走这个口；确认框提示 tailscale 可能断一下 |

- 选备用 / 主出口时网关必填。**LAN1 不允许当出口**（面板上两项置灰，核心层也拒绝）。
- metric 常量在 `wifi_core`：`WIFI_METRIC` / `WIRED_PRIMARY_METRIC` / `WIRED_BACKUP_METRIC`，#7 复用这一处。
- IPv6 只对齐 `never-default` / `route-metric`，`ipv6.method auto`，不配地址。

## 切换流程

```
写状态文件 (新模式 status=applying, stale=当前模式的占用)    <- 断电也知道要撤什么
undo 当前模式 (按占用反序; 旧连接不归推车删)
apply 新模式 (LAN1 先停 PXE 三件套; con add 定好 uuid; con up —— 确定没插线就不 up, 插上 NM 自动连)
  成功 -> 状态文件写 status=ok, 记 prev (「回到上一个」用)
  失败 -> undo 新模式 -> 恢复原模式 (托管的用记下的参数重建; 未托管的把原来那条旧连接 up)
          恢复也失败 -> 撤掉恢复的那份 -> 落到干净状态 (LAN1 关闭 / LAN2 DHCP)
```

和 issue 原文"undo → 干净状态 → apply"的差别：干净状态只在失败兜底时显式落；正常切换不先落一次干净状态再切，
免得管理口多断一次。undo 之后、apply 之前那一刻就是"推车自己的东西都撤了"的干净态。

每次 apply / undo 写一行 `events.log`（`[netmode]`），apply 成功那行带上撤销命令，远程断了也能在日志里找到。
交还装机配置（`Lan1-pxe`）前照旧先探测链路上有没有别人的 DHCP，有（或没插线、探测不算数）就不切。

Wi-Fi（`wlp4s0`）永远不碰：不在可选网口里，生成的命令里不会出现它（有单测守着）。

## 状态文件 `/var/lib/ops-cart/netmode.json`

原子写（`atomicfile`），路径可注入。没有文件 = 首次运行：两个口按现状显示「未托管 · 旧配置「…」」，**不自动改任何东西**。
文件坏了按未托管显示并提示，下一次切换会覆盖。

```json
{
 "version": 1,
 "modes": [
  {"mode": "static", "ports": ["enp3s0"],
   "params": {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "", "egress": "backup"},
   "res": [{"con": "<uuid>", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
   "status": "ok", "since": 1793300000}
 ],
 "prev": {"enp3s0": {"mode": "legacy", "ports": ["enp3s0"], "params": {"uuid": "<uuid>", "name": "enp3s0"}}}
}
```

- `res`：这个模式占的东西，undo 按反序撤。`{"con": uuid}` 删连接；`{"sysctl": key, "orig": 原值}` 写回原值（后续批次用）。
- `status`：`ok` / `applying`（切到一半断电，面板标"上次切换没做完"，下次切换连 `stale` 一起撤）/ `failed`（连干净状态都没起来）。
- `stale`：上次没撤掉的占用（切到一半断电，或 undo 命令真失败了），下次切换一起撤，面板上一行黄字提示。
  口回到旧连接但还有遗留时，留一条 `"mode": "legacy"` 的记录专门带着它。
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
| 模式: ( )关闭  ( )DHCP 客户端  (o)静态 IP  ( )交还旧配置: [enp3s0       v]      |
| 模板: [手填 v]  本机 地址/前缀: [192.0.2.5/24 ]  对端(可选): [      ] [按对端预填] |
| 网关: [192.0.2.1   ]  DNS(可空, 逗号分隔): [                ]                     |
| 出口: (o)不当出口  ( )备用出口  ( )主出口                                         |
| 推车用 192.0.2.5/24 (网段 192.0.2.0/24) · 不当出口: 只连同网段, 网关不写; 切之前还会确认 |
| [应用]  [撤到干净状态]  [回到上一个]  [刷新]                                       |
+--------------------------------------------------------------------------------+
状态行 (成功绿 / 失败红)
```

- 不能用的选项置灰：LAN2 没有「关闭」；LAN1 的「备用 / 主出口」；非静态时地址、网关、DNS；「不当出口」时网关。
- 点「应用」先在本地校验（错了直接在状态行说错在哪，不弹框），再弹确认框：

```
LAN2 (enp3s0) 切换网口模式

原来: 未托管 · 旧配置「enp3s0」 · 198.51.100.7/24
改为: 静态 IP 192.0.2.5/24 网关 192.0.2.1 · 主出口

LAN2 是管理口, 切换时经它的远程连接会断 (无线和 tailscale 还在).
选了主出口: 推车默认出网会走这个口, tailscale / 远程可能断一下.

将执行的命令:
  nmcli con add type ethernet ifname enp3s0 con-name cart-enp3s0-static connection.uuid <新> ...
  nmcli con up uuid <新>
出问题时的 undo 命令 (切换失败会自动执行; 车边终端也能照抄):
  撤掉新模式:
    nmcli con delete uuid <新>
  回到原来:
    nmcli con up uuid <旧连接>

要继续吗?
```

## 和旧入口的迁移

- 车机「网络」页：「网口配置文件」和「LAN2 管理口配置」都撤了，只剩「网口模式」。顶栏 LAN1 / LAN2 灯也指向它。
- 推车上已有的旧连接（`Lan1-pxe`、`enp3s0`、旧面板建的 `cart-<口>-<种类>-<名字>`）**一条不删**：
  面板把它们列在「交还旧配置」里；切到推车托管的模式时它们只是不再生效（推车的那条优先级更高）。
- 想回到旧的装机配置：LAN1 选「交还旧配置: Lan1-pxe」—— 撤掉推车建的、up 旧连接、先探测别人的 DHCP；PXE 服务不自动开，去「装机」页开。
- 旧面板的能力：新建 DHCP / 静态、切换、回退上一个、设备出厂地址模板、按对端预填都并进新面板；
  **「删除配置文件」没迁**（旧连接只认不删，要删车边 `nmcli con delete`）。
- `kiosk_core.nics_status` 改成读网口上实际生效的连接，网页口「网卡」卡片显示的连接名 / 获取方式跟着变。

## 部署注意

- 换 `netmode_core.py`、`netmode_panel.py`、`netprof_core.py`、`kiosk.py`、`kiosk_core.py`、`kiosk_ui_core.py`、`wifi_core.py`；
  **删 `/opt/pxe/netprof_panel.py`**（没人再 import 它，留着也跑不起来）。
- `pxeserver.py` 只用 `netprof_core.dhcp_probe`，没变；`pxe-server` 不用为此重启。
- 重启 `kiosk`（会回锁屏）；`webkiosk` / `cart-dash` 在跑的话也重启一下，`nics_status` 换了读法。
- `/var/lib/ops-cart/` 第一次切换时自动建；不用改 tmpfiles。
- 现场验证（只能在推车前做）：
  1. 打开「网口模式」：两个口都显示「未托管 · 旧配置「…」」，地址和默认出口对得上 `ip route`；什么都没动。
  2. LAN1 选「关闭」→ 确认框列出 `systemctl stop pxe-*`、`nmcli con add … cart-eno1-off`、undo 命令；点「否」什么都不变；点「是」后 `nmcli dev` 里 eno1 连着 `cart-eno1-off`、没有地址，`/var/lib/ops-cart/netmode.json` 有记录，`events.log` 有两行 `[netmode]`。
  3. LAN1「交还旧配置: Lan1-pxe」→ 回到装机地址，`cart-eno1-off` 被删。
  4. LAN2 静态 + 备用出口（人在车边，Wi-Fi 连着）：`ip route` 里 LAN2 默认路由 metric 1000、排在 Wi-Fi 900 后面；再「撤到干净状态」回到 DHCP。
  5. LAN1 拔着线选静态（按设备出厂网段）：确认框写「没插线: 只把配置建好」，`nmcli con` 里有 `cart-eno1-static`；插上线后 NM 自动连上、地址出来。
  6. 故意切一个起不来的：LAN1 插着线但对面没有 DHCP，选 DHCP —— 约 45 秒后失败，自动回到原来的，状态行写「已回到原来的 …」，`events.log` 有 apply 失败 / 自动撤 / 恢复三行。
- 回滚：恢复备份的 `.py`；推车建的连接按 `netmode.json` 里的 uuid `nmcli con delete uuid <uuid>`，再 `nmcli con up id Lan1-pxe` / `nmcli con up id enp3s0`。
