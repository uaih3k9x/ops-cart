package org.opscart.bleinput

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.speech.RecognizerIntent
import android.widget.Button
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** 输入框 + 发送. 手机输入法 (包括输入法自带的语音) 直接在输入框里用; "语音"按钮调系统语音识别, 结果填进输入框. */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var log: TextView
    private lateinit var input: EditText
    private lateinit var scroll: ScrollView

    private var asking: AlertDialog? = null

    private val ui = object : CartLink.Listener {
        override fun onState(text: String) = runOnUiThread { status.text = text }
        override fun onLine(text: String) = runOnUiThread { append(text) }
        override fun onConfirm(name: String?, addr: String) = runOnUiThread { askConfirm(name, addr) }
    }

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        log = findViewById(R.id.log)
        input = findViewById(R.id.input)
        scroll = findViewById(R.id.scroll)
        findViewById<Button>(R.id.send).setOnClickListener { send() }
        findViewById<Button>(R.id.voice).setOnClickListener { voice() }
        findViewById<Button>(R.id.stop).setOnClickListener {
            startService(Intent(this, LinkService::class.java).setAction(LinkService.ACTION_STOP))
            status.text = "已断开"
        }
        findViewById<Button>(R.id.reconnect).setOnClickListener {
            LinkService.link?.core?.kick() ?: askPermissionsThenStart()
        }
        findViewById<Button>(R.id.forget).setOnClickListener {
            AlertDialog.Builder(this)
                .setMessage("忘记记住的推车? 下次要重新扫描并确认.")
                .setPositiveButton("忘记") { _, _ -> LinkService.link?.core?.forget() ?: forgetOffline() }
                .setNegativeButton("取消", null)
                .show()
        }
        askPermissionsThenStart()
    }

    override fun onResume() {
        super.onResume()
        LinkService.listener = ui
        status.text = LinkService.state
        log.text = ""
        LinkService.lines.forEach(::append)
        LinkService.link?.core?.pending?.let { (name, addr) -> askConfirm(name, addr) }
    }

    /** 第一次连 (或忘记之后): 让用户核对扫到的设备名称和地址, 防连上冒充的设备. */
    private fun askConfirm(name: String?, addr: String) {
        if (asking?.isShowing == true) return
        asking = AlertDialog.Builder(this)
            .setTitle("连这台推车吗?")
            .setMessage("名称: ${name ?: "(没有名称)"}\n地址: $addr\n\n和车机上 bluetoothctl show 显示的地址一致再确认.")
            .setCancelable(false)
            .setPositiveButton("是这台") { _, _ -> LinkService.link?.core?.confirm(addr, true) }
            .setNegativeButton("不是") { _, _ -> LinkService.link?.core?.confirm(addr, false) }
            .show()
    }

    private fun forgetOffline() {
        getSharedPreferences("cart", MODE_PRIVATE).edit().remove("cart").apply()
        append("已忘记推车")
    }

    override fun onPause() {
        if (LinkService.listener === ui) LinkService.listener = null
        super.onPause()
    }

    private fun append(line: String) {
        log.append(line + "\n")
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun send() {
        val text = input.text.toString()
        if (text.isBlank()) return
        val link = LinkService.link
        if (link == null) {
            askPermissionsThenStart()
            Toast.makeText(this, "连接还没起来, 稍后再发", Toast.LENGTH_SHORT).show()
            return
        }
        if (link.core.send(text)) input.setText("")
    }

    // ------------------------------------------------------------ 语音

    private fun voice() {
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
        if (i.resolveActivity(packageManager) == null) {
            Toast.makeText(this, "没有系统语音识别, 用输入法里的语音键", Toast.LENGTH_SHORT).show()
            return
        }
        @Suppress("DEPRECATION")
        startActivityForResult(i, REQ_VOICE)
    }

    @Deprecated("Activity 基类只有这个")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_VOICE || resultCode != RESULT_OK) return
        val said = data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull() ?: return
        input.text.insert(input.selectionStart.coerceAtLeast(0), said)
    }

    // ------------------------------------------------------------ 权限

    private fun remembered() = getSharedPreferences("cart", MODE_PRIVATE).getString("cart", null) != null

    /** 扫描要的权限: 12+ 是 BLUETOOTH_SCAN; 10–11 精确定位; 8–9 粗略定位就够. 已记住推车直连不用扫. */
    private fun scanPermission(): String = when {
        Build.VERSION.SDK_INT >= 31 -> Manifest.permission.BLUETOOTH_SCAN
        Build.VERSION.SDK_INT >= 29 -> Manifest.permission.ACCESS_FINE_LOCATION
        else -> Manifest.permission.ACCESS_COARSE_LOCATION
    }

    private fun required(): List<String> {
        val p = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31) p += Manifest.permission.BLUETOOTH_CONNECT
        if (!remembered()) p += scanPermission()
        return p
    }

    private fun needed(): List<String> {
        val p = ArrayList(required())
        if (Build.VERSION.SDK_INT >= 31) p += Manifest.permission.BLUETOOTH_SCAN     // 记住了也要: 忘记 / 换车时要扫
        else p += scanPermission()
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        return p.distinct().filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun askPermissionsThenStart() {
        val miss = needed()
        if (miss.isEmpty()) startIfAllowed() else requestPermissions(miss.toTypedArray(), REQ_PERM)
    }

    /** 必需的权限缺了不启动服务; 老系统要扫描时定位开关关着就提示. */
    private fun startIfAllowed() {
        val missing = required().filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            status.text = if (Build.VERSION.SDK_INT >= 31) "没有蓝牙权限, 连不了推车" else "没有定位权限 (老系统扫蓝牙要), 找不到推车"
            return
        }
        if (!remembered() && !CartLink.locationOn(this)) {
            status.text = "定位开关关着, 这个系统版本扫不到蓝牙设备"
            AlertDialog.Builder(this)
                .setMessage("Android 11 及以下扫描蓝牙要打开定位开关 (App 不读位置).")
                .setPositiveButton("去打开") { _, _ -> startActivity(Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)) }
                .setNegativeButton("取消", null)
                .show()
            return
        }
        LinkService.start(this)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode == REQ_PERM) startIfAllowed()
    }

    companion object {
        private const val REQ_PERM = 1
        private const val REQ_VOICE = 2
    }
}
