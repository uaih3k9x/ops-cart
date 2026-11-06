package org.opscart.bleinput

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
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

    private val ui = object : CartLink.Listener {
        override fun onState(text: String) = runOnUiThread { status.text = text }
        override fun onLine(text: String) = runOnUiThread { append(text) }
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
        askPermissionsThenStart()
    }

    override fun onResume() {
        super.onResume()
        LinkService.listener = ui
        status.text = LinkService.state
        log.text = ""
        LinkService.lines.forEach(::append)
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
        if (link.send(text)) input.setText("")
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

    private fun needed(): List<String> {
        val p = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31) {
            p += Manifest.permission.BLUETOOTH_SCAN
            p += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            p += Manifest.permission.ACCESS_COARSE_LOCATION
            p += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        return p.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun askPermissionsThenStart() {
        val miss = needed()
        if (miss.isEmpty()) LinkService.start(this) else requestPermissions(miss.toTypedArray(), REQ_PERM)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode != REQ_PERM) return
        val bt = if (Build.VERSION.SDK_INT >= 31) Manifest.permission.BLUETOOTH_CONNECT else Manifest.permission.ACCESS_FINE_LOCATION
        if (checkSelfPermission(bt) == PackageManager.PERMISSION_GRANTED) LinkService.start(this)
        else status.text = "没有蓝牙权限, 连不了推车"
    }

    companion object {
        private const val REQ_PERM = 1
        private const val REQ_VOICE = 2
    }
}
