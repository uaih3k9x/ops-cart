# ops-cart 优化方案（2026-10-25 夜）

基线：分支 `night/20261025-kiosk`，提交 64f1b99（= 推车上正在跑的 1.05.01）。
本机 `python3 -m pytest -q`：307 passed / 1 skipped。

来源：五路审计（Tk 车机界面、网络与装机、网页管理口、串口/键鼠/画面、仓库与脱敏）。
本文只记录结论和分工，代码改动由三条并行实现线完成，合并阶段统一改 README / VERSION / BUILD。

## 0. 约束（所有实现线都要遵守）

- 不改推车本身：不 scp、不 sudo、不重启推车上的服务。部署由人确认后进行，本文第 6 节是部署清单。
- 不在开发机上真跑 pxeserver / DHCP / RA。网络相关一律 mock 加单元测试。
- 公开仓库：不写真实地址、MAC、序列号、真名、本机路径、内部主机名和内部 SSID。
  示例地址用 192.0.2.x / 198.51.100.x / 203.0.113.x，示例 MAC 用 02:00:00:xx:xx:xx。
- 测试先行，任何一次提交 pytest 都必须全绿。
- 代码要兼容 Python 3.9：不用 match、运行时的 `X | Y`、3.10 以后才有的标准库 API。
- 界面文案用中文，不用 emoji 和符号图标。
- 提交信息用中文，结尾加 Co-Authored-By 行，不 push。

## 1. 抽查结论

架构负责人逐条读代码核对了以下高严重度发现，全部属实：

| 发现 | 核对位置 | 结论 |
|---|---|---|
| LAN1 配置必然失败，且失败前已经改了网卡 | kiosk.py `cfg_lan1` 先跑 nmcli，后调 `apply_pxe_conf`；后者要求 ip 带 `/`，而传入的 `ip_body` 不带 | 属实。另外 `render_pxe_service` 不带 `--iface`；测试 `test_apply_pxe_conf_writes_conf_and_unit` 把错误约定固化了 |
| 状态板从后台线程调 `self.after` | kiosk.py `refresh` 里的 `checks()` / `work()` | 属实。Tk 不是线程安全的，无论泄漏的具体机理如何都要改。泄漏速率采用审计员在推车上只读测得的数据 |
| `_pxe_tick` 没有异常兜底，退出时不上锁 | kiosk.py `_pxe_tick`、`quit_kiosk` | 属实 |
| `pxe_disarm` 只处理根目录的 grub.cfg 和 `grub.cfg-01-*` | kiosk_core.py `pxe_disarm` | 属实。EFI/BOOT/grub.cfg 和按 IP 命名的菜单不管 |
| setup-cart.sh 会写出默认自动装机的菜单 | scripts/setup-cart.sh 的 heredoc：`set default=0`，菜单项带 `inst.ks=`，并复制到 EFI/BOOT | 属实 |
| DHCP REQUEST 不看 option 54；地址池用完后重复发第一个地址 | pxeserver.py `DhcpServer.handle` / `offer_ip` | 属实 |
| TFTP 块号超过 65535 时 `struct.pack("!H")` 抛异常；整文件读进内存 | pxeserver.py `_rrq` | 属实 |
| DHCP 广播地址和掩码写死 | pxeserver.py 回包循环与 OFFER/ACK 选项 | 属实 |
| 网页口不校验 Host/Origin；PIN 读不到就当作没设 | webkiosk.py `App.handle`；kiosk_core.py `pin_conf_read` 遇异常返回 None | 属实 |
| `PinGate.attempt` 没有锁 | kiosk_core.py `PinGate` | 属实（代码结构上成立，并发测试数据采用审计员的结果） |
| 网页键鼠不看绑定，用 by-id 排第一的设备 | webkiosk.py `_real_ch_open` → `kc.ch9329_port()` | 属实 |
| Ch9329 没有独占，每帧 TCIOFLUSH 并同步等 ACK 0.4 秒 | ch9329.py `__init__` / `_send_raw` | 属实 |
| 公开文件里有真实地址 | `git grep` 命中 20 多个被跟踪文件、80 多行 | 属实 |

没有剔除整条的发现，但调整了几条的修法：

- LAN1 改地址：不修 `cfg_lan1`，直接删掉。在车机上重写 /etc/systemd 单元这件事本身就是风险来源，装机网段改为放进部署配置（EnvironmentFile）。
- “pxe-server 默认不 enable”：改成由 pxeserver 自己兜底（见 A2、A4）。enable 与否作为部署决定，留给维护者。
- “网页 PIN 至少 6 位”：暂不做。PIN 哈希里看不出长度，强制要求会让现有 PIN 失效。改用指数退避和按来源计数来压低穷举速度。
- 审计员提出的数字（RSS 增长、并发试错次数、OCR 耗时）没有逐条复测，按“审计员实测”引用；验收时以修复后的测试和部署后观测为准。

## 2. 优先级

- P0 安全：装机误清盘、在别人的网段上发 DHCP、网页口被劫持或绕过 PIN、键鼠发错机器、公开仓库泄露。
- P1 缺陷：LAN1 配置、TFTP 大文件、DHCP 地址池、自检卡死、拔线异常、面板互斥、黑边误点。
- P2 性能与体验：后台线程刷界面导致内存增长、主线程阻塞、子进程过多、提示文案、emoji。
- P3 新功能：网页留言页、部署工具等，本轮基本放进 deferred。

## 3. 三条实现线

三条线的文件归属互不重叠，各自写测试、各自保持全绿。README.md、VERSION、BUILD 不归任何线。
跨线只靠“不改签名”的约定衔接，第 4 节列出这些约定。

### A 线：PXE 与装机安全（pxeserver / httpserve / kiosk_core / 部署件）

文件：pxeserver.py、httpserve.py、kiosk_core.py、scripts/setup-cart.sh、windows/*、
deploy/systemd/pxe-server.service、deploy/systemd/pxe-http-boot.service、deploy/systemd/pxe-http-repo.service、
deploy/systemd/kiosk.service、deploy/tmpfiles.d/ops-cart.conf、deploy/pxe.env.example（新）、
tests/test_pxeserver.py、tests/test_httpserve.py、tests/test_kiosk_core.py、tests/test_grub_templates.py（新）。

- A1（P0）DHCP 守规矩：REQUEST 带的 option 54 不是自己时静默；没有 option 54 且 (mac, ip) 不在自己租约里时静默，不发 NAK；地址池用完时不发 OFFER，记 warning；租约按 3600 秒过期回收；处理 RELEASE 和 DECLINE（DECLINE 的地址隔离一段时间）。
- A2（P0）防在别人的网段上发 DHCP：启动时和 eno1 carrier 从 0 变 1 时，在 eno1 上探测一次（复用 netprof_core.dhcp_probe，通过参数注入以便测试）；看到别人的 OFFER 就停止应答。另开一个被动监听，看到 server-id 不是自己的 OFFER/ACK 也停止应答。只有 eno1 确实持有 `--ip` 时才回包。状态写入 /run/ops-cart/pxe-guard.json，用专门的退出码配合单元里的 `RestartPreventExitStatus`，防止 systemd 反复拉起。
- A3（P0）装机菜单只从一处生成：`pxe_disarm` 扫整个 tftp 树，凡是含 `inst.ks=` 的 grub.cfg*（包括 EFI/BOOT/grub.cfg 和按 IP 命名的），除了当前有效的已解锁 MAC，一律删除或改写为救援菜单。setup-cart.sh 不再写 grub.cfg，改为调用 kiosk_core 的救援菜单写出入口（新增 `python3 kiosk_core.py --write-rescue`），并删除 EFI/BOOT/grub.cfg。windows/grub.cfg 改成救援模板。新增 tests/test_grub_templates.py：遍历被跟踪的 grub 模板和脚本里的 heredoc，断言默认项不含 `inst.ks`。
- A4（P0）服务端兜底：TFTP 发 `grub.cfg*`、httpserve 发 ks 文件之前先读 pxe-mode.json，未解锁、已过期或 MAC 不匹配时一律按救援菜单回应或拒绝。这样 kiosk 不在时也不会误装。用 mock 测。
- A5（P1）删除 LAN1 改地址的路径：删 `apply_pxe_conf`、`render_pxe_service`、`pxe_conf_read/write` 及对应测试；装机网段只来自部署配置。pxeserver 新增 `--prefix`（或接受 CIDR），用 ipaddress 算掩码和广播地址；grub 菜单里的 HTTP 地址从同一份配置读取。deploy 单元改为 `EnvironmentFile=/etc/ops-cart/pxe.env`，并保证 `--iface eno1` 写死在单元里；加测试守住 `--iface eno1`（对应 LAN1 铁律）。
- A6（P1）TFTP：块号按 `block & 0xFFFF` 回绕；按块 seek+read 流式读取；blksize 限制在 8..65464，解析失败回 ERROR 包。测试用假 socket 自动回 ACK，传一个超过 65536 块的文件。
- A7（P1）pxeserver 不静默死线程：各 serve 循环把 recv、处理、send 整体包在 try 里；main 每隔几秒检查线程 `is_alive()`，有线程死了就记日志并 `sys.exit(1)` 交给 systemd 重启（与 A2 的退出码区分开）。
- A8（P0）PinGate 加固：内部加 `threading.Lock`；冷却改成指数递增（60s 起，设上限）；失败计数持久化到 /run 下的状态文件，kiosk 重启后不清零；进入冷却时调用 `cartlog.event("pin", ...)`。`attempt()` 的签名和返回值不变。
- A9（P1）自检不卡死：`selftest_run` 把上下文采集也纳入保护，任何异常都产出一行 `SELFTEST / FAIL`，不往外抛。
- A10（P2）少起子进程：uptime 读 /proc/uptime；carrier 读 sysfs；`services_status` 合并成一次 `systemctl show -p ActiveState,UnitFileState ...`，写解析函数并测试。
- A11（P0 脱敏）本线文件里的真实地址、MAC、序列号全部换成示例值；pxe/装机相关的事件（解锁、菜单被取走、上锁）写 `cartlog.event("pxe", ...)`；tmpfiles 把日志目录改成 0750 root:wheel，方便只读排障。

### B 线：车机 Tk 界面与网络面板（kiosk / 面板 / wifi / 脱敏工具）

文件：kiosk.py、kiosk_ui_core.py（新）、tests/test_kiosk_ui_core.py（新）、ks_panel.py、wifi_panel.py、netprof_panel.py、
wol_panel.py、wifi_core.py、wifi_prefer.py、ruijie_portal.py、netprof_core.py、wol_core.py、
tests/test_wifi_core.py、tests/test_ruijie_portal.py、tests/test_netprof_core.py、tests/test_wol_core.py、tests/test_wifi_prefer.py（新）、
scripts/check_public_safe.py（新）、tests/test_public_safe.py（新）、tests/test_py39_compat.py（新）、
docs/PUBLIC_SAFETY.md（新）、.gitignore、scripts/smoke-kiosk.sh。

- B1（P2，但影响长期稳定）UiBridge：kiosk_ui_core 里实现，后台线程只往 `queue.Queue` 放可调用对象，主线程 `after(50)` 轮询执行。kiosk 的 `refresh`、开机自检，以及 wifi/netprof/ks/wol 面板的 `_bg/_async` 全部改用它。用假 root 测：后台放入后，执行只发生在 drain 里。
- B2（P0）装机模式定时器：判断逻辑抽成 `pxe_tick_decide(state, log, now)`；`_pxe_tick` 用 try/finally 保证重新排期；`quit_kiosk` 和窗口销毁前先 `pxe_disarm`。
- B3（P1）开机自检兜底：`work()` 整体 try；加 30 秒看门狗强制进入主界面；`_boot_done` 做成幂等。
- B4（P1）左侧面板状态机 `LeftPane`：只有一个 current 字段，`show(name)` 返回要关的和要开的；6 个 toggle 合并为 `toggle_pane(name)`，UVC 预览也纳入（修复串口与预览并排的问题）。MENU 搬进 kiosk_ui_core 并改成参数化写法，去掉“LAN1 装机口配置”项，删除 `cfg_lan1`。
- B5（P2）主线程不跑外部命令：toggle_pxe、cfg_lan2、预览、无线密码读取、ks 改密码等全部经 UiBridge 放到后台，界面显示“处理中…”并置灰按钮；`default_video` 推迟到自检线程计算。kiosk.py 不再直接 import subprocess（mpv 的 Popen 除外）。
- B6（P1）键鼠透传：CH9329 发送放到专门的工作线程和有界队列，主线程只入队；相对位移超过 127 时保留余量到下一帧（纯函数，守恒测试）；相对模式下把指针 warp 回中心并忽略 warp 产生的事件；绝对模式下按下和滚轮落在黑边时不发帧，松开照发（纯函数决策并测试）。断线时关闭句柄，不只是置空。
- B7（P2）装机解锁对话框：`grab_set`；重复点击只把已有窗口提到最前；输错验证码原地换码并保留提示。
- B8（P2）小键盘数字：`kp_digits_as_digits(root)` 按 X keycode 插入数字，不依赖 NumLock 状态；参照现有 KP_Enter 的测试写法。
- B9（P1）ks_panel：保存和恢复包 `try/except OSError` 并弹错误；`ks_validate` 不通过时要二次确认才写盘；恢复备份时关闭文件。
- B10（P0）无线不切断唯一出口：`wifi_core.plan_replace(saved, ssid)` 返回命令序列和回滚序列，先建后删；改密码失败写回旧密码；对当前出口连接执行忘记或改密码时弹出“远程会断”的确认。
- B11（P0）校园网认证白名单：portal 主机名按白名单校验，首次成功后记住（TOFU）；302 跳到陌生主机时不发凭据。测试用 mock curl。
- B12（P1）`dhcp_probe` 先检查 carrier，没插线时返回“未知”，不当作安全放行。返回值的形状保持兼容（A 线会调用它）。
- B13（P0 脱敏工具）`scripts/check_public_safe.py`：只用标准库，导出 `scan_text(text, deny_prefixes, deny_words)`；内置规则查公网 IP、CGNAT 段、非本地管理位的 MAC、用户主目录形式的本机路径，行内 `public-safe-ignore` 可豁免；额外的 deny 前缀和词表放在不进仓库的 `.public-safe.local`。tests/test_public_safe.py 本轮只测 `scan_text`，扫整个仓库的集成测试由合并阶段加上（见第 5 节）。
- B14（P1）tests/test_py39_compat.py：对被跟踪的 `*.py` 做 `ast.parse(feature_version=(3, 9))`，并用 AST 访问器拦 match 和运行时 `X | Y`。
- B15（P2）界面去掉 emoji 和符号字符（kiosk.py、wifi_panel、ks_panel），改为“[透传中]”“[加密]”“警告:”“校验通过”这类文字。
- B16（P1）`.gitignore` 加上推车运行时文件（CART-LOG.md、`*.bak*`、kiosk.conf、selftest.json、kvm.json、serial.conf、notice.txt、wol.json、pxe.conf、pxe-mode.json、pxeserver.log、.public-safe.local）；wifi_prefer 的 main 改为可注入 run/now/冷却路径并补 2-3 个测试；smoke-kiosk.sh 接受目录参数、日志写到 mktemp；本线测试文件脱敏。

### C 线：网页管理口、键鼠与串口（webkiosk / web_core / ch9329 / kvm / serial）

文件：webkiosk.py、web_core.py、web/index.html、ch9329.py、kvm_core.py、serial_panel.py、cart_snap.py、cart_ocr.py、cartlog.py、
deploy/systemd/webkiosk.service（新）、tests/test_webkiosk.py、tests/test_web_core.py、tests/test_ch9329.py、tests/test_kvm_core.py、
tests/test_serial_panel.py、tests/test_cart_snap.py、tests/test_cart_ocr.py、tests/test_cartlog.py。

- C1（P0）Host/Origin 校验：`handle()` 最前面校验 Host 白名单（从 web.conf 读，仓库里用示例值）；POST 要求同源的 Origin 或 `Sec-Fetch-Site: same-origin`。先写测试：Host 不在白名单返回 403。
- C2（P0）PIN 必须存在：web_core 新增 `pin_state(path)`，区分“已设置 / 未设置 / 读不到”；网页口只有在“已设置”时才允许登录，其余情况对所有来源都拒绝。按来源 IP 各用一个 PinGate，tailnet 和局域网分开计数。
- C3（P0）读请求体之前先做来源检查；Content-Length 超过 64 KiB 返回 413（/api/type 单独设上限）；Handler 设 timeout；同一采集设备只起一个 ffmpeg。
- C4（P0）会话：记录签发时的 PIN 代号，哈希变了全部作废；加绝对 TTL；签发时清理过期令牌，Sessions 内部加锁。所有长连接按会话登记，退出登录、关闭局域网开关、PIN 变更时统一关闭；流循环里每隔几秒重新检查鉴权。
- C5（P0）局域网开关排除装机网段：`client_allowed` 增加排除网段参数（来自配置，默认排除 LAN1 装机网段）。
- C6（P0）键鼠按绑定走：`/api/hid`、`/api/type` 带上当前画面源，由 kvm_core 解析出绑定的那块；没绑定或不在线返回 409，不调用 `ch_open`。`/api/videos` 改用 `kc.video_nodes`，返回名称和绑定状态。
- C7（P0）Ch9329 加固：`__init__` 里 `fcntl.flock(LOCK_EX | LOCK_NB)`，和 pyserial 的独占方式一致，拿不到锁就报“键鼠被占用”；`termios.error` 统一转成 `Ch9329Gone(OSError)`；flush 只清输入；支持传入已打开的 fd；波特率表用 `getattr` 按需取，使 macOS 上也能构造；新增 `parse_ack`；ACK 超时缩短。用 `os.openpty()` 做真 tty 测试，覆盖拔线和双开互斥。
- C8（P1）键名表：`win`/`super` 改为修饰位 gui，`del` 作为 delete 的别名，补 printscreen、scrolllock、pause、numlock、f13 到 f24；`type_text` 先整体校验，有打不出的字就整串拒绝并列出。`mouse_chunks` 挪到 ch9329 供两端共用。
- C9（P1）网页打字和鼠标：/api/type 限长，返回 `{sent, skipped}`，分块发送且块间释放锁，结束时真正发一次全部松开；网页鼠标默认改为绝对坐标（`abs_coords` 纯函数测试），相对模式留作可选。
- C10（P1）网页串口：去掉连上就发回车，改为“唤醒”按钮；接入时回放最近若干行；串口连接按“令牌加连接序号”管理，第二个标签页不挤掉第一个。前端：去掉 emoji；401 时弹登录；403 时显示“未开放局域网访问”。
- C11（P1）网页审计日志：用 cartlog 写结构化事件（来源、类别、会话前缀、动作）；/api/type 只记长度，串口只记连接、断开和字节数，PIN 不进日志。测试注入日志回调。
- C12（P1）serial_panel：后台线程不再直接调 `after`，统一走面板已有的队列加 `_drain`；新增 `parse_list(text)`，只把还活着的服务算作服务，标题显示服务实际的波特率；开服务加 busy 守卫，stderr 落到临时文件，进程提前退出时把错误显示出来；优先使用 by-id 路径；断开后定时检查并自动重连；粘贴改到后台按行限速发送；列表里标出 CH9329 所在的口，不允许当控制台开服务。
- C13（P2）cart_snap：mpv IPC 等到对应 request_id 的回包才返回；捕获 TimeoutExpired；默认输出改用 mkstemp 或 /run 下的路径；设备被别的进程占用时给出明确提示。cart_ocr：子进程加 timeout，出错退出码为 2（1 只表示没找到），放大倍数按输入宽度自适应。
- C14（P1）新增 deploy/systemd/webkiosk.service：专用用户、NoNewPrivileges、ProtectSystem=strict，只开放所需的写目录，Restart=on-failure，不依赖 kiosk.service。本线测试文件脱敏。

## 4. 跨线约定

- A 线不改这些对外签名：`PinGate(stored, ...)` 和 `attempt(pin, now=None)` 的返回值；`video_nodes`、`selftest_run`、`pxe_disarm`、`pxe_mode_read`、`kp_enter_as_return`、`ch9329_port`、`misc_status` 的签名和返回形状。
- A 线不动 `kc.MENU`；B 线在 kiosk_ui_core 里自建 MENU，kiosk.py 改用它。合并阶段删掉 `kc.MENU`。
- A 线删除 `apply_pxe_conf`、`render_pxe_service`、`pxe_conf_read`；B 线同时删除 kiosk.py 里的 `cfg_lan1`。合并阶段用 grep 确认 kiosk.py 不再引用这些名字。
- B 线保持 `netprof_core.dhcp_probe` 的调用方式兼容，只增加 carrier 检查；A 线以参数注入的方式调用它。
- C 线把 `termios.error` 转成 `OSError` 的子类，所以 B 线在 kiosk.py 里只需捕获 `OSError`。
- C 线不改 `cartlog.event` 的签名，A、B 线照常调用。
- 三条线都不改 README.md、VERSION、BUILD。

## 5. 合并阶段

1. 三条线依次合入，每合一条跑一次全量 pytest。
2. 删除 `kc.MENU`、`kc.ch9329_port` 和 `misc_status` 里的 ch9329 判断（改为显示绑定数和在线数），并修相应测试。
3. 在 tests/test_public_safe.py 里加扫整个仓库的集成测试（`git ls-files`），要求零发现。
4. README：补 cart_snap、网页口需单独启用、pxe.env 的说明；部署段改写；setup-cart.sh 的说明同步。VERSION 升到 1.06.00，重写 BUILD。
5. 用真 3.9 解释器跑一遍测试（如果本机 3.9 能装上 pytest）。

## 6. 部署清单（留给人确认，本轮不执行）

1. 部署前在推车上创建 /etc/ops-cart/pxe.env，填入真实的装机网段（A5 的前提）；否则 pxe-server 起不来。
2. 删除推车上的 /opt/pxe/tftp/EFI/BOOT/grub.cfg（现在是自动装机菜单）。
3. 决定 pxe-server 是否继续开机自启，以及 Lan1-pxe 连接的 autoconnect。
4. 安装 webkiosk.service 前确认专用用户和 PIN 文件的读权限。
5. 部署后验收：kiosk 运行数小时后 RSS 不再线性增长；装机模式到期后 tftp 树里没有任何含 `inst.ks=` 的菜单；网页口 Host 不在白名单时返回 403。
6. 在 CART-LOG（推车本地，不进仓库）记录版本、改动和回滚方式。

## 7. 暂不做（deferred）及理由

- 重写 git 历史清除已泄露内容：会影响已有 clone 和 fork，需要维护者决定。本轮只修 HEAD，并在 docs/PUBLIC_SAFETY.md 里写明“历史中有已知泄露，未重写”。
- kiosk_core.py 按领域拆分、kiosk.py 拆出 LockScreen/BootScreen/Passthrough 等类：三条线并行时拆分会制造大量冲突，等本轮合并后单独做。
- 一键部署工具（releases 目录加 current 软链接、manifest、自动写 CART-LOG）：涉及推车目录结构调整，需要和维护者一起定方案。推车上 77 个 .bak 和旧测试文件的清理也一并放到那时。
- conbroker 改用 systemd-run 或 `conbroker@.service` 脱离 kiosk 的 cgroup：需要推车上的 sudo 规则和单元配合，部署要维护者确认。本轮 C12 只做 busy 守卫和错误回显。
- 网页留言页（读写 conbroker 的 chat.log）：新功能，优先级最低。
- 网页口强制 6 位以上 PIN：见第 1 节，改用退避和按来源计数。
- 校园网认证的 BSSID OUI 白名单、cart_ocr 的 `--region`：收益小，等 B11、C13 落地后再看。
- 内部 SSID 和认证单元名的泛化：改名要在推车上迁移单元和配置文件，放到部署工具那一轮。本轮由 `.public-safe.local` 的词表在本地拦截。
- pxeserver.log 迁到统一日志目录：涉及推车上的路径和 logrotate，放到部署工具那一轮。

## 8. 验收方式

- 每条线：先提交会红的测试，再提交让它变绿的代码；最终 `python3 -m pytest -q` 全绿，数量不少于 307 passed。
- 关键测试（必须有）：
  - DHCP：带别人 server-id 的 REQUEST 返回 None；12 个 MAC 依次 DISCOVER 时第 12 个没有 OFFER；外来 OFFER 注入后停止应答并写出守护状态。
  - TFTP：超过 65536 块的文件能传完，块号发生回绕。
  - 装机：disarm 之后 tftp 树里没有含 `inst.ks` 的 grub.cfg*；pxe-mode 过期时 TFTP 对 `grub.cfg-01-*` 回救援菜单；被跟踪的 grub 模板默认项不含 `inst.ks`；pxe 单元带 `--iface eno1`。
  - PIN：并发 N 个线程试错，计为 bad 的次数不超过 max_fails；冷却时长逐次递增。
  - 网页：Host 不在白名单返回 403；PIN 文件不可读时登录失败；来源被拒时请求体一个字节都不读；PIN 变更后旧令牌失效；画面源未绑定时 /api/hid 返回 409 且不打开设备。
  - CH9329：pty 关掉 master 后发送抛 OSError 子类；两个实例打开同一口时第二个被拒。
  - 车机：UiBridge 的执行只发生在 drain 里；LeftPane 任意两个面板切换后只剩一个；`pxe_tick_decide` 覆盖到期和取走两种上锁；相对位移累计 (300, -300) 时余量守恒；黑边上按下不发帧。
  - 公开安全：`scan_text` 能识别公网 IP、CGNAT 段、真实 MAC、本机路径，放过文档地址段、版本号和本地管理位 MAC。
  - 兼容性：所有被跟踪的 .py 能按 3.9 语法解析。
- 部署后观测（人确认后）：见第 6 节第 5 条。
