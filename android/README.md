# 推车打字（安卓）

手机经蓝牙 BLE 给推车"隔空打字"的最小 App（#52）。协议见 [`../docs/BLE_INPUT.md`](../docs/BLE_INPUT.md)。

- 协议 v2：单帧 `min(MTU-3, 512)`；回执按 `(序号, CRC)` 认领，别的手机的回执不理；正文过滤和推车一致（拒 Cf/标签字符/Zl/Zp/Cn/Co 等）
- 第一次（或"忘记推车"之后）按服务 UUID 扫描（省电模式，扫几轮没找到就停，点"重连"再找），**弹框让你核对名称和地址**再连；之后记住地址直连，连续 5 次连不上就忘掉地址重新扫描（再确认一次）
- 写 RX 前要求系统里已经和推车 bond：没 bond 先 `createBond` 等成功，否则不发；没 bond 的连接上收到的回复一律不显示
- 连接 → 配对 → MTU 517 → 发现服务 → 订阅；每个 GATT 操作 10 秒看门狗，超时或操作起不来按断线处理（整条重发，不跳帧）；旧连接迟到的回调不理
- 订阅成功时新建回执重组状态（推车重启不吞回执）；蓝牙关了等开关广播再连；`connectGatt` 返回空也退避重试
- 输入框用系统输入法（输入法自带的语音键也行）；"语音"按钮调系统语音识别，结果填进输入框
- 发件箱存在 App 私有存储（不进备份、不随换机迁移）：没回执的重连后同序号重发；App 被关过重开时列出"未送达"；超过 10 分钟没送达的丢掉并提示；`unpaired` 留着等 trust 后每 30 秒重发；同一条的定时重发同时只有一个
- 状态机 `LinkCore.kt` 是纯 Kotlin，`LinkCoreTest` 用假 GATT 测排队、超时、陈旧回调、推车重启、回执归属、重发去重；`CartLink.kt` 只是安卓外壳
- 分包编解码 `FrameCodec.kt` 是纯 Kotlin，单元测试读 `../docs/ble_input_vectors.json`，和推车侧 Python 用同一组向量

## 权限

- Android 12+：`BLUETOOTH_CONNECT` 必需；没记住推车时 `BLUETOOTH_SCAN` 也必需，缺了不启动服务
- Android 10–11：扫描要精确定位权限和定位开关；Android 8–9 只申请粗略定位。定位开关关着会提示去打开（App 不读位置）
- Android 13+：通知权限（前台服务通知），不给也能用

## 编译

要 JDK 17+ 和 Android SDK（platform 35、build-tools 35）。`local.properties` 写 `sdk.dir=...`（不进仓库）。

```
./gradlew test assembleDebug lintDebug
```

APK 在 `app/build/outputs/apk/debug/app-debug.apk`。最低 Android 8.0（API 26）。

## 第一次配对

推车上先按 `docs/BLE_INPUT.md` 打开配对并起好 agent。App 扫到推车先让你核对名称和地址，确认后发起配对，
手机和车机上的六位数一致再确认；然后在车机上 `trust` 这台手机。没 trust 时推车回 `unpaired`，消息留在发件箱，trust 后自动重发。
要换车或怀疑连错了：点"忘记推车"，再到系统蓝牙设置里取消配对。
