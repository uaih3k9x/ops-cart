package org.opscart.bleinput

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * 前台服务: 切到后台、锁屏也保持和推车的连接. 界面经 [link] / [listener] 拿连接和消息;
 * 最近的消息留在 [lines], 界面重开时补上.
 */
class LinkService : Service() {
    companion object {
        const val ACTION_STOP = "org.opscart.bleinput.STOP"
        private const val CHANNEL = "link"
        private const val NOTE_ID = 1
        private const val KEEP_LINES = 200

        var link: CartLink? = null
            private set
        var listener: CartLink.Listener? = null
        var state = "未启动"
            private set
        val lines = ArrayList<String>()

        fun start(ctx: Context) {
            val i = Intent(ctx, LinkService::class.java)
            ctx.startForegroundService(i)
        }
    }

    private val relay = object : CartLink.Listener {
        override fun onState(text: String) {
            state = text
            notifyState(text)
            listener?.onState(text)
        }

        override fun onLine(text: String) {
            lines += text
            if (lines.size > KEEP_LINES) lines.removeAt(0)
            listener?.onLine(text)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            link?.stop()
            link = null
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        val note = note(state)
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTE_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTE_ID, note)
        }
        if (link == null) link = CartLink(applicationContext, relay).also { it.start() }
        return START_STICKY
    }

    override fun onDestroy() {
        link?.stop()
        link = null
        super.onDestroy()
    }

    private fun note(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, getString(R.string.channel), NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun notifyState(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTE_ID, note(text))
    }
}
