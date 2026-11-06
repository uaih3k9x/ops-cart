package org.opscart.bleinput

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import java.util.UUID

/**
 * 和推车的一条 BLE 连接: 扫描 (按服务 UUID) -> 连接 -> 请求大 MTU -> 发现服务 -> 订阅 TX -> 发 RX.
 * 断了自动重连 (退避 2..30 秒); 没收到 ack 的消息留在发件箱, 重连后用同一序号重发 (推车按序号去重).
 * 所有 GATT 操作排队一次一个; 回调都转到主线程.
 */
@SuppressLint("MissingPermission")     // 权限由 MainActivity 在启动服务前要到
class CartLink(private val ctx: Context, private val listener: Listener) {
    interface Listener {
        fun onState(text: String)
        fun onLine(text: String)
    }

    private class Outgoing(val id: Int, val text: String)

    private val main = Handler(Looper.getMainLooper())
    private val adapter = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
    private val prefs = ctx.getSharedPreferences("cart", Context.MODE_PRIVATE)
    private val serviceUuid = UUID.fromString(FrameCodec.SERVICE_UUID)
    private val rxUuid = UUID.fromString(FrameCodec.RX_UUID)
    private val txUuid = UUID.fromString(FrameCodec.TX_UUID)
    private val cccd = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    private var gatt: BluetoothGatt? = null
    private var rx: BluetoothGattCharacteristic? = null
    private var mtu = 23
    private var ready = false
    private var running = false
    private var scanning = false
    private var backoffMs = 2_000L
    private val ops = ArrayDeque<() -> Boolean>()
    private var opBusy = false
    private val outbox = ArrayList<Outgoing>()
    private val inbound = Reassembler()

    // ------------------------------------------------------------ 对外

    fun start() {
        if (running) return
        running = true
        connect()
    }

    fun stop() {
        running = false
        main.removeCallbacksAndMessages(null)
        stopScan()
        gatt?.close()
        gatt = null
        ready = false
        listener.onState("已断开")
    }

    /** 放进发件箱; 连着就马上发. 返回 false = 太长或有不该有的字符. */
    fun send(text: String): Boolean {
        val bytes = text.toByteArray(Charsets.UTF_8)
        val problem = FrameCodec.textProblem(text)
        if (problem != null || bytes.size > FrameCodec.MAX_LEN) {
            listener.onLine("✗ 没发: " + if (problem != null) "有控制字符" else "超过 ${FrameCodec.MAX_LEN} 字节")
            return false
        }
        val id = nextId()
        outbox += Outgoing(id, text)
        listener.onLine("我: $text")
        if (ready) write(outbox.last())
        else listener.onLine("（还没连上推车, 连上后自动发）")
        return true
    }

    private fun nextId(): Int {
        val id = prefs.getInt("nextId", (0..0xFFFF).random())
        prefs.edit().putInt("nextId", (id + 1) and 0xFFFF).apply()
        return id
    }

    // ------------------------------------------------------------ 连接

    private fun connect() {
        if (!running) return
        val addr = prefs.getString("cart", null)
        if (addr != null) {
            listener.onState("连接推车…")
            open(adapter.getRemoteDevice(addr))
        } else {
            scan()
        }
    }

    private fun scan() {
        val scanner = adapter?.bluetoothLeScanner ?: return retryLater("蓝牙没开")
        listener.onState("找推车…")
        scanning = true
        scanner.startScan(
            listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(serviceUuid)).build()),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCb,
        )
        main.postDelayed({ if (scanning) { stopScan(); retryLater("没找到推车") } }, 15_000)
    }

    private fun stopScan() {
        if (scanning) {
            scanning = false
            adapter?.bluetoothLeScanner?.stopScan(scanCb)
        }
    }

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            main.post {
                if (!scanning) return@post
                stopScan()
                listener.onState("找到推车, 连接…")
                open(result.device)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            main.post { scanning = false; retryLater("扫描失败 ($errorCode)") }
        }
    }

    private fun open(dev: BluetoothDevice) {
        gatt?.close()
        ready = false
        ops.clear()
        opBusy = false
        gatt = dev.connectGatt(ctx, false, cb, BluetoothDevice.TRANSPORT_LE)
    }

    private fun retryLater(why: String) {
        ready = false
        if (!running) return
        listener.onState("$why, ${backoffMs / 1000} 秒后重连")
        main.postDelayed({ connect() }, backoffMs)
        backoffMs = minOf(backoffMs * 2, 30_000L)
    }

    // ------------------------------------------------------------ GATT 回调 (都转主线程)

    private val cb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post {
                if (g != gatt) return@post
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    listener.onState("已连接, 协商…")
                    g.requestMtu(517)
                } else {
                    g.close()
                    gatt = null
                    retryLater("连接断了")
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, value: Int, status: Int) {
            main.post {
                if (status == BluetoothGatt.GATT_SUCCESS) mtu = value
                g.discoverServices()
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post {
                val svc = g.getService(serviceUuid)
                val r = svc?.getCharacteristic(rxUuid)
                val t = svc?.getCharacteristic(txUuid)
                if (r == null || t == null) {
                    prefs.edit().remove("cart").apply()      // 记错了设备, 下次重新扫
                    g.disconnect()
                    return@post
                }
                prefs.edit().putString("cart", g.device.address).apply()
                rx = r
                g.setCharacteristicNotification(t, true)
                enqueue { writeDescriptor(g, t.getDescriptor(cccd)) }
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            main.post {
                opDone()
                if (d.uuid == cccd && status == BluetoothGatt.GATT_SUCCESS) {
                    ready = true
                    backoffMs = 2_000L
                    listener.onState("已连上推车 (MTU $mtu)")
                    outbox.forEach { write(it) }
                } else if (d.uuid == cccd) {
                    listener.onState("订阅失败 ($status): 要先在推车上配对")
                    g.disconnect()
                }
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            main.post {
                opDone()
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    // 多半是没配对 / 加密不够: 系统会弹配对, 断开重连后整条重发
                    listener.onLine("✗ 写入失败 ($status), 重连后重发")
                    ops.clear()
                    g.disconnect()
                }
            }
        }

        @Deprecated("Android 13 以下走这个")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION") val v = c.value ?: return
            main.post { onNotify(v.copyOf()) }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            main.post { onNotify(value) }
        }
    }

    // ------------------------------------------------------------ 收发

    private fun onNotify(frame: ByteArray) {
        for (ev in inbound.feed(frame, SystemClock.elapsedRealtime())) {
            if (ev !is Reassembler.Event.Text) continue
            when (val s = Status.parse(ev.text)) {
                is Status.Ack -> {
                    outbox.removeAll { it.id == s.id }
                    listener.onLine(if (s.dup) "✓ 已送达 (重复, 推车只收一次)" else "✓ 已送达")
                }
                is Status.Err -> onError(s)
                is Status.Reply -> listener.onLine("大肥鱼: ${s.text}")
                null -> {}
            }
        }
    }

    private fun onError(s: Status.Err) {
        val msg = outbox.firstOrNull { it.id == s.id }
        val retry = s.code in setOf("full", "busy", "timeout", "nostart", "crc")
        listener.onLine("✗ 推车回错误 ${s.code}" + if (msg != null && retry) ", 3 秒后重发" else "")
        if (msg == null) return
        if (retry) main.postDelayed({ if (ready && msg in outbox) write(msg) }, 3_000)
        else outbox.remove(msg)
    }

    private fun write(m: Outgoing) {
        val g = gatt ?: return
        val c = rx ?: return
        for (f in FrameCodec.encode(m.id, m.text.toByteArray(Charsets.UTF_8), mtu)) {
            enqueue { writeChar(g, c, f) }
        }
    }

    private fun enqueue(op: () -> Boolean) {
        ops.addLast(op)
        pump()
    }

    private fun pump() {
        while (!opBusy && ops.isNotEmpty()) {
            if (ops.removeFirst()()) opBusy = true
        }
    }

    private fun opDone() {
        opBusy = false
        pump()
    }

    private fun writeChar(g: BluetoothGatt, c: BluetoothGattCharacteristic, v: ByteArray): Boolean =
        if (Build.VERSION.SDK_INT >= 33) {
            g.writeCharacteristic(c, v, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION")
            c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            @Suppress("DEPRECATION")
            c.value = v
            @Suppress("DEPRECATION")
            g.writeCharacteristic(c)
        }

    private fun writeDescriptor(g: BluetoothGatt, d: BluetoothGattDescriptor?): Boolean {
        d ?: return false
        val v = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        return if (Build.VERSION.SDK_INT >= 33) {
            g.writeDescriptor(d, v) == BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION")
            d.value = v
            @Suppress("DEPRECATION")
            g.writeDescriptor(d)
        }
    }
}
