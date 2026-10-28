# xfz1986 "udisp" USB 副屏协议（用户态发送用）

来源：开源仓库 chuanjinpang/win10_idd_xfz1986_usb_graphic_driver_display（Windows IDD 驱动 `Driver.cpp` / `enc_base.cpp`，
ESP32 固件示例 `device_porting/esp-idf5.3_udisp_s3_st7789_example/main/app_vendor.c`）。2026-10-28 读源码整理，**还没上真机验证**。

## 设备

- USB `303a:2986`（复合设备，用接口 0：`MI_00`）或 `303a:2987`。`303a` 是乐鑫（Espressif）的厂商号。
- 一个厂商自定义接口，带 bulk OUT（发画面）和 bulk IN。
- **iProduct 字符串描述符里带配置**，`_` 分隔的若干字段，主机按前缀认：

  | 字段 | 含义 |
  |---|---|
  | `R<宽>x<高>` | 分辨率，例如 `R800x1280` |
  | `Ejpg<n>` | 用 JPEG 编码，n 是质量档位 |
  | `Ergb<n>` | 用原始 RGB 编码 |
  | `Fps<n>` | 帧率上限 |
  | `Bl<n>` | 带宽限制 |
  | 第一个字段的最后一位数字 | 注册表配置序号（Windows 用，可忽略） |

  字段不足 2 个时 Windows 驱动用默认值。

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
- 固件示例里每帧缓冲是 **100 KB**（`UDISP_BUF_SIZE`），超了会丢帧。800×1280 的板子固件可能更大，**上机先确认**；JPEG 质量要压到单帧放得下。

## 用户态发送的最小流程（Python + pyusb）

1. 找 `303a:2986`，claim 接口 0，找 bulk OUT 端点和 `wMaxPacketSize`；
2. 读 iProduct，解析 `R..x..` 得分辨率；
3. 画图（PIL）→ JPEG（质量按 100KB 预算调）→ 拼帧头 → `ep.write(帧头 + jpeg)`；
4. 总长 % 包长 == 0 时补发 `type=0xff` 的 16 字节结束帧头。
