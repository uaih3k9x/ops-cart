package org.opscart.bleinput

/**
 * 和推车一条 BLE 连接的状态机 (#52), 纯 Kotlin: 安卓蓝牙 API 经 [Port] / [Conn] 注入, 单元测试用假 GATT.
 * 所有入口都要在同一个线程 (主线程) 调.
 *
 * 流程: (没记住推车: 扫描 -> 用户确认名称和地址) -> 连接 -> 配对 (没 bond 先 createBond 等成功) -> MTU 517
 *      -> 发现服务 -> 订阅 TX (成功时新建回执重组状态, 推车重启也不吞回执) -> 就绪, 发件箱整条重发.
 * - 每个 GATT 操作有看门狗, 超时断开重来; 操作起不来按断线处理, 不跳帧.
 * - 回调带着是哪条连接, 不是当前连接的一律不理.
 * - 回执只认 (序号, CRC) 和发件箱对得上的; 回复只在已 bond 的连接上显示.
 * - 发件箱持久化 (App 私有存储); 超过推车去重时效 (10 分钟) 没送达的丢掉并提示; 同一条的定时重发同时只有一个.
 */
class LinkCore(private val port: Port, private val store: Store, private val ui: Ui) {
    enum class Bond { NONE, BONDING, BONDED }

    /** 一条 GATT 连接. 各操作返回 false = 没起来. 结果经 LinkCore.onXxx(conn, ...) 回来. */
    interface Conn {
        fun requestMtu(mtu: Int): Boolean
        fun discover(): Boolean
        fun subscribe(): Boolean          // 打开 TX 通知并写 CCCD
        fun write(frame: ByteArray): Boolean
        fun close()                       // disconnect + close
    }

    interface Port {
        fun now(): Long                   // 墙上时间毫秒 (发件箱时效跨进程要用)
        fun schedule(delayMs: Long, task: () -> Unit)
        fun adapterOn(): Boolean
        fun canScan(): Boolean            // 扫描权限 + (老系统) 定位开关
        fun startScan(): Boolean
        fun stopScan()
        fun connect(addr: String): Conn?
        fun bondState(addr: String): Bond
        fun createBond(addr: String): Boolean
    }

    interface Store {
        var cart: String?                 // 用户确认过的推车地址
        fun nextId(): Int
        fun loadOutbox(): List<Saved>
        fun saveOutbox(items: List<Saved>)
    }

    interface Ui {
        fun state(text: String)
        fun line(text: String)
        fun confirm(name: String?, addr: String)
    }

    data class Saved(val id: Int, val text: String, val created: Long)

    private class Out(val id: Int, val text: String, val created: Long) {
        val crc = FrameCodec.crc32(text.toByteArray(Charsets.UTF_8))
        var retryToken = 0
        var retrying = false
        var lastCode: String? = null      // 同一个错误码只提示一次, 免得 unpaired 每 30 秒刷屏
    }

    private class Op(val id: Int, val frame: ByteArray, val last: Boolean)

    private enum class Phase { IDLE, WAIT, WAIT_ADAPTER, SCANNING, CONFIRM, CONNECTING, BONDING, MTU, DISCOVER, SUBSCRIBE, READY, GAVE_UP }

    companion object {
        const val WANT_MTU = 517
        const val OP_TIMEOUT_MS = 10_000L
        const val CONNECT_TIMEOUT_MS = 20_000L
        const val BOND_TIMEOUT_MS = 60_000L
        const val SCAN_WINDOW_MS = 15_000L
        const val SCAN_ROUNDS = 4
        const val MAX_FAILS = 5
        const val RETRY_MS = 3_000L
        const val UNPAIRED_RETRY_MS = 30_000L
        const val OUTBOX_TTL_MS = 600_000L      // = 推车去重时效, 过了再发可能重复投递
        const val TICK_MS = 30_000L
        val RETRY_CODES = setOf("full", "busy", "timeout", "nostart", "crc")
    }

    private var running = false
    private var gen = 0                   // stop() 一次加一, 旧定时器全作废
    private var watch = 0
    private var phase = Phase.IDLE
    private var conn: Conn? = null
    private var addr: String? = null
    private var mtu = 23
    private var backoffMs = 2_000L
    private var failures = 0
    private var scanMisses = 0
    private val declined = HashSet<String>()
    private val outbox = ArrayList<Out>()
    private val ops = ArrayDeque<Op>()
    private var inFlight: Op? = null
    private val queued = HashSet<Int>()
    private var inbound = Reassembler()

    var pending: Pair<String?, String>? = null
        private set
    val ready get() = phase == Phase.READY

    init {
        for (s in store.loadOutbox()) outbox += Out(s.id, s.text, s.created)
        if (outbox.isNotEmpty()) {
            ui.line("上次有 ${outbox.size} 条未送达 (App 被关过), 连上推车后重发, 超过 10 分钟的丢弃:")
            outbox.forEach { ui.line("  未送达: ${it.text}") }
        }
    }

    // ------------------------------------------------------------ 对外

    fun start() {
        if (running) return
        running = true
        tick()
        connect()
    }

    fun stop() {
        running = false
        gen++
        drop()
        phase = Phase.IDLE
        ui.state("已断开")
    }

    /** 用户点"重连": 放弃等待 / 扫不到之后再来一轮. */
    fun kick() {
        if (!running) return start()
        if (phase in setOf(Phase.IDLE, Phase.WAIT, Phase.WAIT_ADAPTER, Phase.GAVE_UP)) {
            scanMisses = 0
            backoffMs = 2_000L
            connect()
        }
    }

    /** "忘记推车": 清掉记住的地址, 断开, 重新扫描 (还要再确认). 系统里的配对记录让用户自己在设置里取消. */
    fun forget() {
        store.cart = null
        declined.clear()
        failures = 0
        drop()
        ui.line("已忘记推车; 要彻底取消配对, 到系统蓝牙设置里删掉它")
        phase = Phase.IDLE
        if (running) connect()
    }

    /** 放进发件箱; 连着就马上发. 返回 false = 太长或有不该有的字符. */
    fun send(text: String): Boolean {
        val problem = FrameCodec.textProblem(text)
        if (problem != null || text.toByteArray(Charsets.UTF_8).size > FrameCodec.MAX_LEN) {
            ui.line("✗ 没发: " + if (problem != null) "有控制字符或看不见的字符" else "超过 ${FrameCodec.MAX_LEN} 字节")
            return false
        }
        val m = Out(store.nextId(), text, port.now())
        outbox += m
        save()
        ui.line("我: $text")
        if (ready) write(m) else ui.line("（还没连上推车, 连上后自动发）")
        return true
    }

    fun confirm(address: String, ok: Boolean) {
        if (phase != Phase.CONFIRM || pending?.second != address) return
        pending = null
        if (ok) {
            store.cart = address
            open(address)
        } else {
            declined += address
            scan()
        }
    }

    // ------------------------------------------------------------ 平台事件

    fun onAdapter(on: Boolean) {
        if (!on) {
            drop()
            phase = Phase.WAIT_ADAPTER
            ui.state("蓝牙关了, 打开后自动重连")
        } else if (running && phase == Phase.WAIT_ADAPTER) {
            backoffMs = 2_000L
            connect()
        }
    }

    fun onScanResult(address: String, name: String?) {
        if (phase != Phase.SCANNING || address in declined) return
        port.stopScan()
        scanMisses = 0
        phase = Phase.CONFIRM
        pending = name to address
        ui.state("找到 ${name ?: "推车"} ($address), 等你确认")
        ui.confirm(name, address)
    }

    fun onScanFailed(code: Int) {
        if (phase != Phase.SCANNING) return
        phase = Phase.IDLE
        retryLater("扫描失败 ($code)")
    }

    fun onConnected(c: Conn) {
        if (c !== conn || phase != Phase.CONNECTING) return
        val a = addr ?: return
        when (port.bondState(a)) {
            Bond.BONDED -> askMtu(c)
            Bond.BONDING -> waitBond()
            Bond.NONE -> if (port.createBond(a)) waitBond() else fail("配对起不来")
        }
    }

    fun onBond(address: String, state: Bond) {
        if (phase != Phase.BONDING || address != addr) return
        val c = conn ?: return
        when (state) {
            Bond.BONDED -> askMtu(c)
            Bond.NONE -> fail("配对没成 (要在手机和车机上确认同一个六位数)")
            Bond.BONDING -> {}
        }
    }

    fun onDisconnected(c: Conn) {
        if (c === conn) fail("连接断了")
    }

    fun onMtu(c: Conn, value: Int, ok: Boolean) {
        if (c !== conn || phase != Phase.MTU) return
        mtu = if (ok) value else 23
        phase = Phase.DISCOVER
        if (c.discover()) arm(OP_TIMEOUT_MS) else fail("发现服务起不来")
    }

    fun onServices(c: Conn, ok: Boolean) {
        if (c !== conn || phase != Phase.DISCOVER) return
        if (!ok) {
            store.cart = null
            ui.line("这台设备没有推车服务, 忘掉它重新找")
            return fail("不是推车")
        }
        phase = Phase.SUBSCRIBE
        if (c.subscribe()) arm(OP_TIMEOUT_MS) else fail("订阅起不来")
    }

    fun onSubscribed(c: Conn, ok: Boolean) {
        if (c !== conn || phase != Phase.SUBSCRIBE) return
        disarm()
        if (!ok) return fail("订阅失败 (推车上配对 / trust 了吗)")
        phase = Phase.READY
        inbound = Reassembler()           // 推车可能重启过, 它的通知序号重来
        failures = 0
        backoffMs = 2_000L
        ui.state("已连上推车 (MTU $mtu)")
        expire()
        for (m in outbox) {
            m.retryToken++
            m.retrying = false
            write(m)
        }
    }

    fun onWritten(c: Conn, ok: Boolean) {
        if (c !== conn || phase != Phase.READY) return
        val op = inFlight ?: return
        inFlight = null
        disarm()
        if (!ok) return fail("写入失败")
        if (op.last) queued -= op.id
        pump()
    }

    fun onNotify(c: Conn, frame: ByteArray) {
        if (c !== conn || phase != Phase.READY) return
        val a = addr ?: return
        if (port.bondState(a) != Bond.BONDED) return      // 没 bond 的连接上收到的一律不信
        for (ev in inbound.feed(frame, port.now())) {
            if (ev !is Reassembler.Event.Text) continue
            when (val s = Status.parse(ev.text)) {
                is Status.Ack -> onAck(s)
                is Status.Err -> onErr(s)
                is Status.Reply -> ui.line("大肥鱼: ${s.text}")
                null -> {}
            }
        }
    }

    // ------------------------------------------------------------ 连接

    private fun connect() {
        if (!running) return
        if (!port.adapterOn()) {
            phase = Phase.WAIT_ADAPTER
            ui.state("蓝牙关着, 打开后自动连")
            return
        }
        val a = store.cart
        if (a != null) open(a) else scan()
    }

    private fun scan() {
        if (!port.canScan()) {
            phase = Phase.IDLE
            ui.state("没有扫描权限 (老系统还要开定位), 找不到推车")
            return
        }
        if (!port.startScan()) {
            phase = Phase.IDLE
            return retryLater("扫描起不来")
        }
        phase = Phase.SCANNING
        ui.state("找推车…")
        later(SCAN_WINDOW_MS) {
            if (phase != Phase.SCANNING) return@later
            port.stopScan()
            phase = Phase.IDLE
            if (++scanMisses >= SCAN_ROUNDS) {
                phase = Phase.GAVE_UP
                ui.state("一直没找到推车, 靠近后点\"重连\"再找")
            } else {
                retryLater("没找到推车")
            }
        }
    }

    private fun open(a: String) {
        drop()
        addr = a
        phase = Phase.CONNECTING
        ui.state("连接推车…")
        conn = port.connect(a) ?: return fail("连接起不来")
        arm(CONNECT_TIMEOUT_MS)
    }

    private fun waitBond() {
        phase = Phase.BONDING
        ui.state("配对中: 手机和车机上的六位数一致再确认")
        arm(BOND_TIMEOUT_MS)
    }

    private fun askMtu(c: Conn) {
        phase = Phase.MTU
        if (c.requestMtu(WANT_MTU)) arm(OP_TIMEOUT_MS) else fail("MTU 协商起不来")
    }

    /** 关掉当前连接 / 扫描, 清掉连接上的状态 (发件箱留着). */
    private fun drop() {
        disarm()
        if (phase == Phase.SCANNING) port.stopScan()
        conn?.close()
        conn = null
        pending = null
        ops.clear()
        inFlight = null
        queued.clear()
    }

    private fun fail(why: String) {
        drop()
        phase = Phase.IDLE
        if (++failures >= MAX_FAILS && store.cart != null) {
            store.cart = null
            failures = 0
            ui.line("连续 $MAX_FAILS 次连不上记住的推车, 忘掉它重新扫描 (会再让你确认)")
        }
        retryLater(why)
    }

    private fun retryLater(why: String) {
        if (!running) return
        if (!port.adapterOn()) {
            phase = Phase.WAIT_ADAPTER
            ui.state("蓝牙关着, 打开后自动连")
            return
        }
        phase = Phase.WAIT
        ui.state("$why, ${backoffMs / 1000} 秒后重连")
        later(backoffMs) { if (phase == Phase.WAIT) connect() }
        backoffMs = minOf(backoffMs * 2, 30_000L)
    }

    // ------------------------------------------------------------ 定时器

    private fun later(ms: Long, task: () -> Unit) {
        val g = gen
        port.schedule(ms) { if (g == gen) task() }
    }

    private fun arm(ms: Long) {
        val t = ++watch
        later(ms) { if (t == watch) fail("蓝牙操作超时") }
    }

    private fun disarm() {
        watch++
    }

    private fun tick() {
        later(TICK_MS) {
            expire()
            if (running) tick()
        }
    }

    // ------------------------------------------------------------ 收发

    private fun write(m: Out) {
        if (!ready || m.id in queued) return
        val frames = FrameCodec.encode(m.id, m.text.toByteArray(Charsets.UTF_8), mtu)
        frames.forEachIndexed { i, f -> ops.addLast(Op(m.id, f, i == frames.lastIndex)) }
        queued += m.id
        pump()
    }

    private fun pump() {
        if (inFlight != null || ops.isEmpty()) return
        val c = conn ?: return
        val op = ops.removeFirst()
        if (!c.write(op.frame)) return fail("写入起不来")
        inFlight = op
        arm(OP_TIMEOUT_MS)
    }

    private fun onAck(s: Status.Ack) {
        val m = outbox.firstOrNull { it.id == s.id && it.crc == s.crc } ?: return   // 别的手机的回执
        outbox.remove(m)
        save()
        ui.line(if (s.dup) "✓ 已送达 (重复, 推车只收一次)" else "✓ 已送达")
    }

    private fun onErr(s: Status.Err) {
        // crc 为 null: 推车读不出是哪条, 只能按序号猜 -> 只许重发 (推车去重), 不许删
        val m = outbox.firstOrNull { it.id == s.id && (s.crc == null || it.crc == s.crc) } ?: return
        val fresh = m.lastCode != s.code
        m.lastCode = s.code
        when {
            s.code == "unpaired" -> {
                if (fresh) ui.line("✗ 推车还没 trust 这台手机: 在车机上 trust 后自动重发")
                retry(m, UNPAIRED_RETRY_MS)
            }
            s.code in RETRY_CODES -> {
                if (fresh) ui.line("✗ 推车回 ${s.code}, ${RETRY_MS / 1000} 秒后重发")
                retry(m, RETRY_MS)
            }
            s.crc != null -> {
                outbox.remove(m)
                save()
                ui.line("✗ 推车拒收 (${s.code}): ${m.text}")
            }
        }
    }

    private fun retry(m: Out, ms: Long) {
        if (m.retrying) return
        m.retrying = true
        val t = ++m.retryToken
        later(ms) {
            if (m.retryToken != t) return@later
            m.retrying = false
            if (m in outbox) write(m)
        }
    }

    private fun expire() {
        val now = port.now()
        val old = outbox.filter { now - it.created > OUTBOX_TTL_MS }
        if (old.isEmpty()) return
        outbox.removeAll(old.toSet())
        save()
        old.forEach { ui.line("✗ 超过 10 分钟没送达, 不再自动发 (要发请重新输入): ${it.text}") }
    }

    private fun save() = store.saveOutbox(outbox.map { Saved(it.id, it.text, it.created) })
}
