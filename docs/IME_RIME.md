# 车机中文输入（librime + Tk 自绘候选栏，#54）

车机 Tk 输入框默认只打 ASCII。这里不走 X 输入法协议（车机没有窗口管理器，候选框位置不稳），
而是 Tk 自己在输入框下面画一条候选栏，拼音引擎用 librime。

## 用法

- 默认英文，和以前一样。**单按一下 Shift**（按下到松开中间没有别的键）切中 / 英，输入框右边显示 `中` / `英`。
- 中文下：小写字母开始拼音；空格上首选（高亮的那个）、`1`-`9` 选本页第几个、回车把敲的字母原样上屏（上英文）、
  `Esc` 取消、`-` / `=`（或 PageUp / PageDown）翻页、退格删拼音。没在拼的时候数字、标点、大写字母照常输入。
- 第一次切中文才在后台打开 librime；首次部署（编译词库）可能要几秒到几分钟，期间显示 `中…`，键盘照常打英文。
- 打不开（没装 librime / 没词库 / 方案没部署）就显示 `英·无中文`，候选栏上提示一次原因，之后一直英文，不影响别的功能。

目前只接了**网络唤醒面板的"名字"输入框**做示范。只接普通文字输入框；确认码、PIN、密码、命令输入框一律不接
（候选上屏等于替人打字，这几处要人亲手敲）。`tests/test_ime_tk.py` 里有一条白名单测试，新接一处要改那里。

## 代码

| 文件 | 干什么 |
|---|---|
| `ime_core.py` | 状态机：哪些键交给引擎、哪些放回输入框；纯逻辑，可注入假引擎 |
| `ime_rime.py` | ctypes 调 librime（只用 `rime_get_api()` 的函数表，按 `rime_api.h` 顺序声明，`data_size` 盖不到的项当没有）；缺库 / 缺词库返回 `(None, 原因)`，不抛 |
| `ime_tk.py` | `ime_tk.attach(entry)`：在输入框自己的绑定之前插一个 bindtag 拦按键、画候选栏和中英标记；出错返回 `None`，输入框照旧 |

## 配置（环境变量，都可不设）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPS_CART_LIBRIME` | 按 `librime.so.1` / `find_library("rime")` 找 | librime 动态库路径 |
| `OPS_CART_RIME_SHARED` | `/usr/share/rime-data` | 共享数据：方案 + 词库 |
| `OPS_CART_RIME_USER` | `~/.local/share/ops-cart/rime` | 用户目录：部署产物 (`build/`)、用户词频 |
| `OPS_CART_RIME_SCHEMA` | 不设 = `default.yaml` 里的第一个方案 | 方案 id，如 `luna_pinyin`、`rime_ice` |

注意：**方案要列进 `schema_list` 才会被部署**。指定了一个没部署的方案，`open_engine` 会直接报"方案没部署"，
而不是给一个按键全不认的引擎。

## 装 librime 和词库（推车上，都没进仓库，默认不装）

librime 本身是 BSD-3。词库和方案是另外的仓库，许可证各不相同，**装之前请再核对一遍**：

| 来源 | 内容 | 许可证（写这份文档时 GitHub 上标的） |
|---|---|---|
| `rime/librime` | 引擎 | BSD-3-Clause |
| `rime/rime-prelude`、`rime/rime-essay`、`rime/rime-luna-pinyin`、`rime/rime-stroke` | 基础配置、语料、朙月拼音（繁体为主，带简化开关）、笔画 | LGPL-3.0 |
| `iDvel/rime-ice`（雾凇拼音） | 简体常用词库更全，推荐给车机 | GPL-3.0（另含第三方词库，以仓库说明为准） |

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
- `tests/test_ime_tk.py`：候选栏文字、接线（假输入框 / 假标签）、接入白名单。
