package org.opscart.bleinput

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
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
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.util.UUID

/**
 * LinkCore 的安卓外壳: 蓝牙扫描 / GATT / 配对广播 / 蓝牙开关广播 / 私有存储. 状态机和规矩都在 [LinkCore].
 * 每条连接一个 [GattConn], 回调先核对 g == 这条连接的 gatt, 再转到主线程交给 LinkCore (它再核对是不是当前连接).
 */
@SuppressLint("MissingPermission")     // 权限由 MainActivity 在启动服务前要到; 运行中被收回时各调用包 try
class CartLink(private val ctx: Context, private val listener: Listener) : LinkCore.Port, LinkCore.Store {
    interface Listener {
        fun onState(text: String)
        fun onLine(text: String)
        fun onConfirm(name: String?, addr: String)
        fun onAskOutbox(count: Int)
    }

    private val main = Handler(Looper.getMainLooper())
    private val manager = ctx.getSystemService(BluetoothManager::class.java)
    private val adapter: BluetoothAdapter? get() = manager?.adapter
    private val prefs = ctx.getSharedPreferences("cart", Context.MODE_PRIVATE)   // App 私有存储, 不落外部存储
    private val serviceUuid = UUID.fromString(FrameCodec.SERVICE_UUID)
    private val rxUuid = UUID.fromString(FrameCodec.RX_UUID)
    private val txUuid = UUID.fromString(FrameCodec.TX_UUID)
    private val cccd = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    val core = LinkCore(this, this, object : LinkCore.Ui {
        override fun state(text: String) = listener.onState(text)
        override fun line(text: String) = listener.onLine(text)
        override fun confirm(name: String?, addr: String) = listener.onConfirm(name, addr)
        override fun askOutbox(count: Int) = listener.onAskOutbox(count)
    })

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.action) {
                BluetoothAdapter.ACTION_STATE_CHANGED -> when (i.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)) {
                    BluetoothAdapter.STATE_ON -> core.onAdapter(true)
                    BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> core.onAdapter(false)
                }
                BluetoothDevice.ACTION_BOND_STATE_CHANGED -> {
                    @Suppress("DEPRECATION")
                    val dev: BluetoothDevice = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE) ?: return
                    core.onBond(dev.address, bond(i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.BOND_NONE)))
                }
            }
        }
    }
    private var registered = false

    fun start() {
        if (!registered) {
            val f = IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED).apply { addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED) }
            if (Build.VERSION.SDK_INT >= 33) ctx.registerReceiver(receiver, f, Context.RECEIVER_NOT_EXPORTED)
            else ctx.registerReceiver(receiver, f)
            registered = true
        }
        core.start()
    }

    fun stop() {
        core.stop()
        main.removeCallbacksAndMessages(null)
        if (registered) {
            ctx.unregisterReceiver(receiver)
            registered = false
        }
    }

    // ------------------------------------------------------------ LinkCore.Port

    override fun now() = System.currentTimeMillis()

    override fun schedule(delayMs: Long, task: () -> Unit) {
        main.postDelayed(task, delayMs)
    }

    override fun adapterOn() = try { adapter?.isEnabled == true } catch (e: SecurityException) { false }

    override fun canScan(): Boolean {
        if (Build.VERSION.SDK_INT >= 31) return granted(Manifest.permission.BLUETOOTH_SCAN)
        val loc = if (Build.VERSION.SDK_INT >= 29) Manifest.permission.ACCESS_FINE_LOCATION else Manifest.permission.ACCESS_COARSE_LOCATION
        return granted(loc) && locationOn(ctx)
    }

    override fun startScan(): Boolean = try {
        val scanner = adapter?.bluetoothLeScanner
        if (scanner == null) false
        else {
            scanner.startScan(
                listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(serviceUuid)).build()),
                ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_BALANCED).build(),
                scanCb,
            )
            true
        }
    } catch (e: SecurityException) {
        false
    } catch (e: IllegalStateException) {
        false
    }

    override fun stopScan() {
        try {
            adapter?.bluetoothLeScanner?.stopScan(scanCb)
        } catch (e: SecurityException) {
        } catch (e: IllegalStateException) {
        }
    }

    /** connectGatt 返回 null、地址不对、权限被收回都返回 null, LinkCore 按连不上退避重试. */
    override fun connect(addr: String): LinkCore.Conn? {
        val c = GattConn()
        c.gatt = try {
            adapter?.getRemoteDevice(addr)?.connectGatt(ctx, false, c.cb, BluetoothDevice.TRANSPORT_LE)
        } catch (e: IllegalArgumentException) {
            null
        } catch (e: SecurityException) {
            null
        }
        return if (c.gatt != null) c else null
    }

    override fun bondState(addr: String): LinkCore.Bond = try {
        bond(adapter?.getRemoteDevice(addr)?.bondState ?: BluetoothDevice.BOND_NONE)
    } catch (e: RuntimeException) {
        LinkCore.Bond.NONE
    }

    override fun createBond(addr: String): Boolean = try {
        adapter?.getRemoteDevice(addr)?.createBond() == true
    } catch (e: RuntimeException) {
        false
    }

    private fun bond(s: Int) = when (s) {
        BluetoothDevice.BOND_BONDED -> LinkCore.Bond.BONDED
        BluetoothDevice.BOND_BONDING -> LinkCore.Bond.BONDING
        else -> LinkCore.Bond.NONE
    }

    private fun granted(p: String) = ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val dev = result.device
            val name = result.scanRecord?.deviceName
            main.post { core.onScanResult(dev.address, name) }
        }

        override fun onScanFailed(errorCode: Int) {
            main.post { core.onScanFailed(errorCode) }
        }
    }

    // ------------------------------------------------------------ LinkCore.Store (私有 SharedPreferences)

    override var cart: String?
        get() = prefs.getString("cart", null)
        set(v) {
            prefs.edit().apply { if (v == null) remove("cart") else putString("cart", v) }.apply()
        }

    override fun nextId(): Int {
        val id = prefs.getInt("nextId", (0..0xFFFF).random())     // 首次随机起点, 之后跨重启持久化
        prefs.edit().putInt("nextId", (id + 1) and 0xFFFF).apply()
        return id
    }

    override fun loadOutbox(): List<LinkCore.Saved> = try {
        val a = JSONArray(prefs.getString("outbox", "[]"))
        (0 until a.length()).map { a.getJSONObject(it).let { o -> LinkCore.Saved(o.getInt("id"), o.getString("text"), o.getLong("t"), o.optBoolean("held", false)) } }
    } catch (e: JSONException) {
        emptyList()
    }

    override fun saveOutbox(items: List<LinkCore.Saved>) {
        val a = JSONArray()
        items.forEach { a.put(JSONObject().put("id", it.id).put("text", it.text).put("t", it.created).put("held", it.held)) }
        prefs.edit().putString("outbox", a.toString()).apply()
    }

    // ------------------------------------------------------------ 一条 GATT 连接

    private inner class GattConn : LinkCore.Conn {
        var gatt: BluetoothGatt? = null
        private var rx: BluetoothGattCharacteristic? = null
        private var tx: BluetoothGattCharacteristic? = null

        private fun on(g: BluetoothGatt, what: () -> Unit) {
            main.post { if (g === gatt) what() }
        }

        val cb = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) = on(g) {
                if (newState == BluetoothProfile.STATE_CONNECTED) core.onConnected(this@GattConn)
                else core.onDisconnected(this@GattConn)
            }

            override fun onMtuChanged(g: BluetoothGatt, value: Int, status: Int) = on(g) {
                core.onMtu(this@GattConn, value, status == BluetoothGatt.GATT_SUCCESS)
            }

            override fun onServicesDiscovered(g: BluetoothGatt, status: Int) = on(g) {
                val svc = g.getService(serviceUuid)
                rx = svc?.getCharacteristic(rxUuid)
                tx = svc?.getCharacteristic(txUuid)
                core.onServices(this@GattConn, status == BluetoothGatt.GATT_SUCCESS, rx != null && tx != null)
            }

            override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) = on(g) {
                if (d.uuid == cccd) core.onSubscribed(this@GattConn, status == BluetoothGatt.GATT_SUCCESS, status in AUTH_STATUS)
            }

            override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) = on(g) {
                core.onWritten(this@GattConn, status == BluetoothGatt.GATT_SUCCESS, status in AUTH_STATUS)
            }

            @Deprecated("Android 13 以下走这个")
            override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
                @Suppress("DEPRECATION") val v = c.value?.copyOf() ?: return
                on(g) { core.onNotify(this@GattConn, v) }
            }

            override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) =
                on(g) { core.onNotify(this@GattConn, value) }
        }

        override fun requestMtu(mtu: Int) = guard { gatt?.requestMtu(mtu) == true }

        override fun discover() = guard { gatt?.discoverServices() == true }

        override fun subscribe(): Boolean = guard {
            val g = gatt ?: return@guard false
            val t = tx ?: return@guard false
            val d = t.getDescriptor(cccd) ?: return@guard false
            if (!g.setCharacteristicNotification(t, true)) return@guard false
            val v = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeDescriptor(d, v) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                d.value = v
                @Suppress("DEPRECATION")
                g.writeDescriptor(d)
            }
        }

        override fun write(frame: ByteArray): Boolean = guard {
            val g = gatt ?: return@guard false
            val c = rx ?: return@guard false
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeCharacteristic(c, frame, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                @Suppress("DEPRECATION")
                c.value = frame
                @Suppress("DEPRECATION")
                g.writeCharacteristic(c)
            }
        }

        override fun close() {
            val g = gatt ?: return
            gatt = null                       // 之后这条连接的回调全被 on() 挡掉
            guard { g.disconnect(); g.close(); true }
        }

        private inline fun guard(op: () -> Boolean): Boolean = try {
            op()
        } catch (e: SecurityException) {
            false
        }
    }

    companion object {
        /** 认证 / 加密不够: 5 / 8 / 15 是 ATT 错误码, 137 (0x89) 是安卓的 GATT_AUTH_FAIL. */
        private val AUTH_STATUS = setOf(5, 8, 15, 137)     // 写数字: GATT_INSUFFICIENT_AUTHORIZATION 常量到 API 33 才有

        /** 定位开关 (Android 11 及以下扫 BLE 要开). */
        fun locationOn(ctx: Context): Boolean {
            if (Build.VERSION.SDK_INT >= 31) return true
            val lm = ctx.getSystemService(LocationManager::class.java) ?: return false
            return if (Build.VERSION.SDK_INT >= 28) lm.isLocationEnabled
            else lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER) || lm.isProviderEnabled(LocationManager.GPS_PROVIDER)
        }
    }
}
