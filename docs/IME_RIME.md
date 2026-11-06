# 车机中文输入（librime + Tk 自绘候选栏，#54）

车机 Tk 输入框默认只打 ASCII。这里不走 X 输入法协议（车机没有窗口管理器，候选框位置不稳），
而是 Tk 自己在输入框下面画一条候选栏，拼音引擎用 librime。

## 用法

- 默认英文，和以前一样。**单按一下 Shift**（按下到松开中间没有别的键）切中 / 英，输入框右边显示 `中` / `英`。
- 中文下：小写字母开始拼音；空格上首选（高亮的那个）、`1`-`9` 选本页第几个、回车把敲的字母原样上屏（上英文）、
  `Esc` 取消、`-` / `=`（或 PageUp / PageDown）翻页、退格删拼音。没在拼的时候数字、标点、大写字母照常输入。
- 第一次切中文才在后台起 librime 子进程；首次部署（编译词库）可能要几秒到几分钟，期间显示 `中…`，键盘照常打英文。
  加载完成时如果离那次 Shift 不到 3 秒、中间也没打字，就切到中文；否则保持英文，再按一次 Shift。
- 打不开（没装 librime / 没词库 / 方案没部署 / 方案不认按键）就显示 `英·无中文`，候选栏上提示一次原因，之后一直英文，
  不影响别的功能，字母也不会被吞。运行中子进程崩了或超时，同样回英文；下次 Shift 会重开，最多 2 次。
- 拼音期间吃掉的键不会冒泡到 kiosk 的全局按键绑定，所以每吃一个键补发虚拟事件 `<<OpsCartImeInput>>`，
  kiosk 拿它算"有人在操作"，连续打拼音不会被判成空闲锁屏。

目前只接了**网络唤醒面板的"名字"输入框**做示范（`attach(e, purpose="wol.name")`）。只接普通文字输入框；确认码、
PIN、密码、命令输入框一律不接（候选上屏等于替人打字，这几处要人亲手敲）：
- `attach` 必须带用途名；`tests/test_ime_tk.py` 递归扫全仓，对 (文件, 用途) 做白名单，新接一处要改那里；
- 运行时兜底：设了 `show=` 的输入框（密码框）`attach` 直接拒。

## 代码

| 文件 | 干什么 |
|---|---|
| `ime_core.py` | 状态机：哪些键交给引擎、哪些放回输入框；纯逻辑，可注入假引擎 |
| `ime_proc.py` | kiosk 这边的引擎：起 `ime_worker.py` 子进程，stdin/stdout 一行一个 JSON；子进程退出 / 超时 / 回复看不懂就杀掉收尸、判不可用 |
| `ime_worker.py` | 子进程：在这里加载 librime（经 `ime_rime`）。kiosk 主进程不 dlopen librime |
| `ime_rime.py` | ctypes 调 librime（只用 `rime_get_api()` 的函数表，按 `rime_api.h` 顺序声明，`data_size` 盖不到的项当没有）；缺库 / 缺词库返回 `(None, 原因)`，不抛 |
| `ime_tk.py` | `ime_tk.attach(entry)`：在输入框自己的绑定之前插一个 bindtag 拦按键、画候选栏和中英标记；出错返回 `None`，输入框照旧 |

## 进程隔离

librime 是 C++，在 kiosk（root）进程里崩了会带崩车机，还可能把 systemd 的重启次数用完。所以放子进程：

- kiosk 是 root 时用 `systemd-run --pipe` 起，沙箱属性仿照 `cart-ocr.service`：`DynamicUser=yes`、`PrivateNetwork=yes`、
  `CapabilityBoundingSet=`（空）、`NoNewPrivileges=yes`、`ProtectSystem=strict`、`ProtectHome=yes`、`MemoryMax=512M` 等；
  用户目录用 `StateDirectory=ops-cart-ime`（`/var/lib/ops-cart-ime`，部署产物留着下次不用重编）。没有 `systemd-run` 就不起。
- 非 root（开发机）直接用当前 python 起，只有崩溃隔离，没有降权断网。

## 配置（环境变量，都可不设）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPS_CART_LIBRIME` | 按 `librime.so.1` / `find_library("rime")` 找 | librime 动态库路径 |
| `OPS_CART_RIME_SHARED` | `/usr/share/rime-data` | 共享数据：方案 + 词库 |
| `OPS_CART_RIME_USER` | `~/.local/share/ops-cart/rime`（root 下经 systemd-run 时固定为 `/var/lib/ops-cart-ime`） | 用户目录：部署产物 (`build/`)、用户词频 |
| `OPS_CART_RIME_SCHEMA` | 不设 = `default.yaml` 里的第一个方案 | 方案 id，如 `luna_pinyin`、`rime_ice` |

注意：**方案要列进 `schema_list` 才会被部署**。不管指没指定方案，打开时都检查当前方案的部署产物
（`build/<方案>.schema.yaml`）在不在；不在就报"方案没部署"，而不是给一个按键全不认的引擎。
万一还是遇到引擎不认第一个字母（不进拼音也不上屏），也当坏了：那个字母还给输入框，回英文。

## 装 librime 和词库（推车上，都没进仓库，默认不装）

librime 本身是 BSD-3。词库和方案是另外的仓库，许可证各不相同，**装之前请再核对一遍**：

| 来源 | 内容 | 许可证（写这份文档时 GitHub 上标的） |
|---|---|---|
| `rime/librime` | 引擎 | BSD-3-Clause |
| `rime/rime-prelude`、`rime/rime-essay`、`rime/rime-luna-pinyin`、`rime/rime-stroke` | 基础配置、语料、朙月拼音（繁体为主，带简化开关）、笔画 | LGPL-3.0 |
| `iDvel/rime-ice`（雾凇拼音） | 简体常用词库更全，推荐给车机 | GPL-3.0（另含第三方词库，以仓库说明为准） |

待核对（rime-ice）：它的方案用到 Lua 脚本，需要 librime 带 **librime-lua** 插件，还要求较新的 librime；
发行版的 librime 包带不带 lua 插件、版本够不够，要实机确认。不带的话只能用朙月拼音或自己编译 librime + 插件。

这些都**只放在推车本地的数据目录**（如 `/usr/share/rime-data` 或 `OPS_CART_RIME_SHARED` 指向的目录），
不进这个仓库，也不跟着部署包分发。

待核对：Rocky 9（EPEL）里有没有现成的 `librime` / 数据包、版本多少；没有的话用 librime 官方 release 或自己编译。
本 PR 没写安装脚本，装法定下来再补。

## 测试

- `tests/test_ime_core.py`：状态机全路径（拼音、空格 / 数字选词、回车上英文、Esc、翻页、退格、Shift 切换、加载中 / 不可用 / 引擎出错降级）。
- `tests/test_ime_rime.py`：ctypes 结构布局对照头文件、`data_size` 兼容、缺库 / 缺词库 / 出错降级；
  真 librime 集成测试要显式给环境变量才跑：

  ```
  OPS_CART_TEST_LIBRIME=/path/to/librime.1.dylib OPS_CART_TEST_RIME_DATA=/path/to/rime-data \
  OPS_CART_TEST_RIME_USER=/path/to/scratch python3 -m pytest -q tests/test_ime_rime.py
  ```
- `tests/test_ime_proc.py`：子进程通信，用真 subprocess 跑假 worker，覆盖正常、打开失败、打开崩溃 / 超时、运行中崩溃 / 超时 / 坏回复、重开；
  起子进程的命令（root 走 systemd-run 沙箱、没有 systemd-run 不起）；worker 循环；主进程不加载 librime。
- `tests/test_ime_tk.py`：候选栏文字、接线（假输入框 / 假标签）、(文件, 用途) 白名单、密码框拒绝、补发输入事件、
  加载完成不乱切中文、加载期间关闭。
