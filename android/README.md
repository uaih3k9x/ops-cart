# 推车打字（安卓）

手机经蓝牙 BLE 给推车"隔空打字"的最小 App（#52）。协议见 [`../docs/BLE_INPUT.md`](../docs/BLE_INPUT.md)。

- 按服务 UUID 扫描推车，连上后请求 MTU 517、订阅通知；记住推车地址，下次直接连
- 输入框用系统输入法（输入法自带的语音键也行）；"语音"按钮调系统语音识别，结果填进输入框
- 发送后显示推车的回执、错误、大肥鱼回复
- 前台服务保持连接；断线按 2–30 秒退避自动重连；没收到回执的消息重连后用同一序号重发（推车去重）
- 分包编解码 `FrameCodec.kt` 是纯 Kotlin，单元测试读 `../docs/ble_input_vectors.json`，和推车侧 Python 用同一组向量

## 编译

要 JDK 17+ 和 Android SDK（platform 35、build-tools 35）。`local.properties` 写 `sdk.dir=...`（不进仓库）。

```
./gradlew test assembleDebug
```

APK 在 `app/build/outputs/apk/debug/app-debug.apk`。最低 Android 8.0（API 26）。

## 第一次配对

推车上先按 `docs/BLE_INPUT.md` 打开配对并起好 agent。App 第一次写入时系统弹配对框，手机和车机上的六位数一致再确认；
然后在车机上 `trust` 这台手机。没 trust 的手机推车回 `unpaired`。
