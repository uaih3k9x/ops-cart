# xfz1986 "udisp" USB 副屏协议（用户态发送用）

来源：开源仓库 chuanjinpang/win10_idd_xfz1986_usb_graphic_driver_display（Windows IDD 驱动 `Driver.cpp` / `enc_base.cpp`，
ESP32 固件示例 `device_porting/esp-idf5.3_udisp_s3_st7789_example/main/app_vendor.c`）。2026-10-28 读源码整理，
**2026-10-29 在真板上验证过**（ESP32-P4 驱动板，macOS 与推车两端都点得亮）。

## 设备

- USB `303a:2986`（复合设备，用接口 0：`MI_00`）或 `303a:2987`。`303a` 是乐鑫（Espressif）的厂商号。
- 一个厂商自定义接口，带 bulk OUT（发画面）和 bulk IN。
- **配置串在厂商接口的接口字符串描述符（iInterface）里**，`_` 分隔的若干字段，主机按前缀认：

  实测真板（2026-10-29，`303a:2987`）：

  ```
  esp32p4udisp0_R1280x800_Ejpg6_Fps46_Bl300000
  ```

  | 字段 | 含义 |
  |---|---|
  | `R<宽>x<高>` | 分辨率，报告的是**帧头/帧尺寸** |
  | `Ejpg<n>` | 用 JPEG 编码，n 是质量档位（驱动里 `n*10` 当 libjpeg quality） |
  | `Ergb<n>` | 用原始 RGB 编码（16→RGB565，32→RGB888） |
  | `Fps<n>` | 帧率上限 |
  | `Bl<n>` | 单帧负载上限（**单位存疑**，见下） |
  | 第一个字段的最后一位数字 | 注册表配置序号（Windows 用，可忽略） |

  字段不足 2 个时 Windows 驱动用默认值（fps 60 / JPEG 5 档）。

- **注意两个坑**（都是真机踩出来的）：

  1. Windows 驱动读的是 **iProduct**，但这块板子的 iProduct 只是产品名
     （`gezi-lakala USB Display`），配置在**接口字符串**里。用户态发送端要读 iInterface，
     读不到再退回 iProduct。非 root 拿不到字符串描述符时只能走默认值。
  2. `Bl` 的单位：ESP32 固件 README 说 `Bl20` = 20 KB，Windows 驱动解析后 `×1024`；
     但这块 P4 板写的是 `Bl300000`，按 KB 算就是 300 MB，显然作者是当**字节**写的。
     两种解释都不影响仪表板（实测单帧 87 KB 很轻松），`udisp.py` 原样返回数值，
     预算按单帧 ≤100 KB 设计。

## 一帧怎么发

一次 bulk OUT 传输 = **16 字节帧头 + 负载**，小端、紧凑排列：

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | u16 | crc16 | 主机填 0，固件不校验 |
| 2 | u8 | type | 0=RGB565，1=RGB888，2=YUV420，**3=JPEG**；0xff 见下 |
| 3 | u8 | cmd | 填 0 |
| 4 | u16 | x | 这块区域左上角 |
| 6 | u16 | y | |
| 8 | u16 | width | 区域宽 |
| 10 | u16 | height | 区域高 |
| 12 | u32 | frame_id \| payload_total << 10 | 低 10 位是帧序号（递增、回绕），高 22 位是负载字节数（最大 4MB） |

- **可以只发一块矩形区域**（x/y/width/height），仪表板只重画变了的那部分（比如一盏灯），省带宽。
- **帧结束的判断**：固件靠"短包"或"收满 payload_total"判断一帧结束。ESP32-S2/S3 不支持零长度包，所以**总长度恰好是端点最大包长的整数倍时**，主机要再补发一个 16 字节帧头、`type=0xff`、其余为 0，当作结束标记（Windows 驱动就这么做）。
- 固件示例里每帧缓冲是 **100 KB**（`UDISP_BUF_SIZE`），超了会丢帧。真板（P4）没测到上限，仪表板按单帧 ≤100 KB 设计最稳。

## 坐标与屏幕朝向（真机踩出来的）

- 帧头里的 x/y/width/height 一律按**配置串报告的那个尺寸**算（这块板是 `R1280x800`，横的），
  不管面板物理上怎么转。
- 这块板子是**竖装**的：横屏内容顺时针转 90° 才正。所以横屏画面的**左上角落到竖屏的左下角**
  （横屏的右下角落到竖屏左上角）。
- 整张图发之前先在主机转好；只发脏矩形时按 `udisp.rot90_rect()` 换算：
  横屏 `(x, y, w, h)` → 竖屏 `(y, 1280-(x+w), h, w)`。

## 用户态发送的最小流程（Python + pyusb）

1. 找 `303a:2986`，claim 接口 0，找 bulk OUT 端点和 `wMaxPacketSize`；推车上 root 才能开设备，
   要常驻的话加一条 udev 规则（`TAG+=\"uaccess\"` 或给服务用户组）。
2. 读 iInterface（读不到退 iProduct），解析 `R..x..` 得分辨率；`udisp.parse_config()` 已实现。
3. 画图（PIL）→ JPEG（质量按 100KB 预算调）→ 拼帧头 → `ep.write(帧头 + jpeg)`；`udisp.build_frame()`。
4. 总长 % 包长 == 0 时补发 `type=0xff` 的 16 字节结束帧头；`udisp.needs_zlp()` / `udisp.zlp_frame()`。

协议层在 [`udisp.py`](../udisp.py)，测试在 `tests/test_udisp.py`（不碰 USB）；带 USB 的实机脚本
见 issue #6 评论。推车没装 Pillow，绘图那一侧要另想办法（或只靠 JPEG 素材）。
