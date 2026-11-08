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
 * - 发件箱持久化 (App 私有存储); 超过推车去重时效 (10 分钟) 没送达的丢掉并提示; 同一条的定时重发同时只有一个;
 *   发完 20 秒没回执同序号重发 (有陌生设备连着时推车不发回执).
 * - 不自动忘记推车: 连续失败、没有推车服务、认证失败 (推车上删过这台手机) 都停下等用户; 换了推车, 旧发件箱先问用户再发.
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
        fun askOutbox(count: Int)          // 换了推车: 旧发件箱 count 条要不要发给新推车 -> releaseOutbox()
    }

    data class Saved(val id: Int, val text: String, val created: Long, val held: Boolean = false)

    private class Out(val id: Int, val text: String, val created: Long, var held: Boolean = false) {
        val crc = FrameCodec.crc32(text.toByteArray(Charsets.UTF_8))
        var retryToken = 0
        var retrying = false
        var ackToken = 0
        var unpairedRetries = 0           // unpaired 后的定时重发次数, 最多 UNPAIRED_RETRIES
        var ackResends = 0                // 等不到回执的重发次数, 最多 ACK_RESENDS
        var sent = false                  // 整条写完过至少一次: 过期时推车可能已经收到
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
        const val AUTH_FAILS = 2
        const val ACK_WAIT_MS = 20_000L
        const val ACK_RESENDS = 2
        const val RETRY_MS = 3_000L
        const val UNPAIRED_RETRY_MS = 30_000L
        const val UNPAIRED_RETRIES = 10
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
    private var authFailures = 0
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
    val held get() = outbox.count { it.held }

    init {
        for (s in store.loadOutbox()) outbox += Out(s.id, s.text, s.created, s.held)
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
            failures = 0
            authFailures = 0
            backoffMs = 2_000L
            connect()
        }
    }

    /** "忘记推车": 清掉记住的地址, 断开, 重新扫描 (还要再确认). 系统里的配对记录让用户自己在设置里取消. */
    fun forget() {
        store.cart = null
        declined.clear()
        failures = 0
        authFailures = 0
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
        if (pending?.second != address) return     // pending 只在 CONFIRM 阶段非空: drop() 会清掉
        pending = null
        if (ok) {
            store.cart = address
            if (outbox.isNotEmpty()) {         // 换了推车: 旧的先扣着, 问用户
                outbox.forEach { it.held = true }
                save()
                ui.askOutbox(outbox.size)
            }
            open(address)
        } else {
            declined += address
            scan()
        }
    }

    /** 用户回答 askOutbox: true = 旧消息发给这台推车, false = 丢掉. */
    fun releaseOutbox(send: Boolean) {
        val old = outbox.filter { it.held }
        if (old.isEmpty()) return
        if (send) {
            old.forEach { it.held = false }
            save()
            if (ready) old.forEach { write(it) }
        } else {
            outbox.removeAll(old.toSet())
            save()
            ui.line("已丢掉 ${old.size} 条给旧推车的消息")
        }
    }

    // ------------------------------------------------------------ 平台事件

    fun onAdapter(on: Boolean) {
        if (phase == Phase.GAVE_UP) return    // 停下等用户的, 关开蓝牙也不自动恢复
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
            Bond.NONE -> if (port.createBond(a)) waitBond() else giveUp("配对起不来: 点\"重连\"再试")
        }
    }

    fun onBond(address: String, state: Bond) {
        if (phase != Phase.BONDING || address != addr) return
        val c = conn ?: return
        when (state) {
            Bond.BONDED -> askMtu(c)
            Bond.NONE -> giveUp("配对没成 (要在手机和车机上确认同一个六位数): 点\"重连\"再试")   // 不自动再弹配对框
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

    /** ok = 发现服务成功; hasCart = 有推车的服务和两个特征. 发现失败重试; 确实没有就停下, 不自动忘记. */
    fun onServices(c: Conn, ok: Boolean, hasCart: Boolean) {
        if (c !== conn || phase != Phase.DISCOVER) return
        if (!ok) return fail("发现服务失败")
        if (!hasCart) return giveUp("这台设备没有推车服务: 连错了的话点\"忘记推车\"重新找")
        phase = Phase.SUBSCRIBE
        if (c.subscribe()) arm(OP_TIMEOUT_MS) else fail("订阅起不来")
    }

    /** auth = 失败原因是认证 / 加密不够 (推车那边可能删过这台手机的配对). */
    fun onSubscribed(c: Conn, ok: Boolean, auth: Boolean = false) {
        if (c !== conn || phase != Phase.SUBSCRIBE) return
        disarm()
        if (!ok) return if (auth) authFail() else fail("订阅失败 (推车上配对 / trust 了吗)")
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

    fun onWritten(c: Conn, ok: Boolean, auth: Boolean = false) {
        if (c !== conn || phase != Phase.READY) return
        val op = inFlight ?: return
        inFlight = null
        disarm()
        if (!ok) return if (auth) authFail() else fail("写入失败")
        authFailures = 0
        if (op.last) {
            queued -= op.id
            outbox.firstOrNull { it.id == op.id }?.let(::awaitAck)
        }
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
                is Status.Reply -> FrameCodec.clean(s.text).split("\n").forEachIndexed { i, l ->
                    ui.line((if (i == 0) "大肥鱼: " else "大肥鱼│ ") + l)    // 每行带前缀, 冒充不了回执
                }
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
        arm(BOND_TIMEOUT_MS) { giveUp("配对超时: 点\"重连\"再试") }
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
        if (++failures >= MAX_FAILS) return giveUp("连续 $MAX_FAILS 次连不上推车 ($why): 靠近后点\"重连\"; 换了推车点\"忘记推车\"")
        retryLater(why)
    }

    /** 停下等用户 (重连 / 忘记推车). 不自动忘记推车, 免得给冒充者留路. */
    private fun giveUp(why: String) {
        drop()
        phase = Phase.GAVE_UP
        ui.state(why)
    }

    private fun authFail() {
        if (++authFailures >= AUTH_FAILS) {
            authFailures = 0
            return giveUp("推车不认这台手机的配对了 (车机上删过?): 到系统蓝牙设置里删掉推车的配对, 再点\"重连\"")
        }
        fail("认证失败")
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

    private fun arm(ms: Long, onTimeout: () -> Unit = { fail("蓝牙操作超时") }) {
        val t = ++watch
        later(ms) { if (t == watch) onTimeout() }
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
        if (!ready || m.held || m.id in queued) return
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

    /** 整条写完后等回执; 等不到 (推车有陌生设备连着时不发回执, 或通知丢了) 就同序号重发一次. */
    private fun awaitAck(m: Out) {
        m.sent = true
        val t = ++m.ackToken
        if (m.ackResends >= ACK_RESENDS) return
        if (m.lastCode == "unpaired" && m.retrying) return    // 已排了 unpaired 重发 (它先于最后一帧回调到了)
        later(ACK_WAIT_MS) {
            if (m.ackToken != t || m !in outbox) return@later
            m.ackResends++
            write(m)
        }
    }

    private fun onAck(s: Status.Ack) {
        val m = outbox.firstOrNull { it.id == s.id && it.crc == s.crc } ?: return   // 别的手机的回执
        outbox.remove(m)
        save()
        ui.line(if (s.dup) "✓ 已送达 (重复, 推车只收一次)" else "✓ 已送达")
    }

    private fun onErr(s: Status.Err) {
        // crc 为 null: 推车读不出是哪条, 只能按序号猜 -> 只许重发 (推车去重), 不许删
        // crc 为 null 只能按序号猜: 除了"重发一次"什么都不许动. unpaired 例外: 推车一律不给它带 crc (防指纹),
        // 而且只在写入方是唯一连着的设备时才发, 收到的就是写这条的手机.
        if (s.crc == null && s.code !in RETRY_CODES && s.code != "unpaired") return
        val m = outbox.firstOrNull { it.id == s.id && (s.crc == null || it.crc == s.crc) } ?: return
        m.ackToken++                      // 推车回话了, 不用再等回执超时
        val fresh = m.lastCode != s.code
        m.lastCode = s.code
        when {
            s.code == "unpaired" -> {
                if (fresh) ui.line("✗ 推车还没 trust 这台手机: 在车机上 trust 后自动重发")
                if (!m.retrying && m.unpairedRetries < UNPAIRED_RETRIES) {
                    m.unpairedRetries++
                    retry(m, UNPAIRED_RETRY_MS)
                }
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
        old.forEach {
            ui.line(
                if (it.sent) "✗ 超过 10 分钟没收到回执, 推车可能已收到但没确认 (先看车机, 别急着重发): ${it.text}"
                else "✗ 超过 10 分钟没送达, 不再自动发 (要发请重新输入): ${it.text}",
            )
        }
    }

    private fun save() = store.saveOutbox(outbox.map { Saved(it.id, it.text, it.created, it.held) })
}
