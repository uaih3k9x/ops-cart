package org.opscart.bleinput

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** CartLink 的排队 / 收发 / 重连状态机 (LinkCore) 对假 GATT 的单测. 时间是假的, advance() 推着走. */
class LinkCoreTest {
    private val cart = "02:00:00:00:00:0A"

    class FakeConn(val n: Int) : LinkCore.Conn {
        val writes = ArrayList<ByteArray>()
        var mtuAsked = 0
        var discovered = false
        var subscribed = false
        var closed = false
        var writeOk = true
        override fun requestMtu(mtu: Int): Boolean { mtuAsked = mtu; return true }
        override fun discover(): Boolean { discovered = true; return true }
        override fun subscribe(): Boolean { subscribed = true; return true }
        override fun write(frame: ByteArray): Boolean { if (writeOk) writes += frame; return writeOk }
        override fun close() { closed = true }
    }

    class Fake : LinkCore.Port, LinkCore.Store, LinkCore.Ui {
        var t = 1_700_000_000_000L
        private val timers = ArrayList<Pair<Long, () -> Unit>>()
        var adapter = true
        var scanAllowed = true
        var scans = 0
        var scanStops = 0
        var connectReturnsNull = false
        val conns = ArrayList<FakeConn>()
        val bonds = HashMap<String, LinkCore.Bond>()
        val bondAsked = ArrayList<String>()
        override var cart: String? = null
        private var next = 100
        var saved: List<LinkCore.Saved> = emptyList()
        val states = ArrayList<String>()
        val lines = ArrayList<String>()
        val confirms = ArrayList<String>()
        val heldAsks = ArrayList<Int>()

        override fun now() = t
        override fun schedule(delayMs: Long, task: () -> Unit) { timers += (t + delayMs) to task }
        fun advance(ms: Long) {
            val end = t + ms
            while (true) {
                val due = timers.filter { it.first <= end }.minByOrNull { it.first } ?: break
                timers.remove(due)
                t = maxOf(t, due.first)
                due.second()
            }
            t = end
        }
        override fun adapterOn() = adapter
        override fun canScan() = scanAllowed
        override fun startScan(): Boolean { scans++; return true }
        override fun stopScan() { scanStops++ }
        override fun connect(addr: String): LinkCore.Conn? =
            if (connectReturnsNull) null else FakeConn(conns.size).also { conns += it }
        override fun bondState(addr: String) = bonds[addr] ?: LinkCore.Bond.NONE
        override fun createBond(addr: String): Boolean { bondAsked += addr; bonds[addr] = LinkCore.Bond.BONDING; return true }
        override fun nextId() = next++
        override fun loadOutbox() = saved
        override fun saveOutbox(items: List<LinkCore.Saved>) { saved = items }
        override fun state(text: String) { states += text }
        override fun line(text: String) { lines += text }
        override fun confirm(name: String?, addr: String) { confirms += addr }
        override fun askOutbox(count: Int) { heldAsks += count }
    }

    private fun bondedFake() = Fake().apply { cart = this@LinkCoreTest.cart; bonds[this@LinkCoreTest.cart] = LinkCore.Bond.BONDED }

    /** 走完 连接 -> MTU -> 发现 -> 订阅, 返回这条连接. */
    private fun bringUp(core: LinkCore, f: Fake, mtu: Int = 517): FakeConn {
        val c = f.conns.last()
        core.onConnected(c)
        assertEquals(517, c.mtuAsked)
        core.onMtu(c, mtu, true)
        assertTrue(c.discovered)
        core.onServices(c, true, true)
        assertTrue(c.subscribed)
        core.onSubscribed(c, true)
        assertTrue(core.ready)
        return c
    }

    /** 假推车: 把 conn 收到的帧重组, 回 ack (推车自己的发送序号从 txStart 开始). */
    private class FakeCart(var txId: Int = 0) {
        val rx = Reassembler()
        fun feed(frames: List<ByteArray>): List<Pair<Int, Long>> =
            frames.flatMap { rx.feed(it, 0) }.filterIsInstance<Reassembler.Event.Text>().map {
                it.id to FrameCodec.crc32(it.text.toByteArray(Charsets.UTF_8))
            }
        fun notify(json: String): List<ByteArray> = FrameCodec.encode(txId++ and 0xFFFF, json.toByteArray(), 23)
        fun ack(id: Int, crc: Long) = notify("""{"t":"ack","id":$id,"crc":$crc}""")
    }

    private fun drainWrites(core: LinkCore, c: FakeConn) {
        var seen = 0
        while (seen < c.writes.size) { seen = c.writes.size; core.onWritten(c, true) }
    }

    private fun crcOf(s: String) = FrameCodec.crc32(s.toByteArray(Charsets.UTF_8))

    @Test
    fun happyPathAndFramesCappedAt512() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        val text = "装".repeat(1300)                        // 3900 字节
        assertTrue(core.send(text))
        drainWrites(core, c)
        assertTrue(c.writes.size > 1 && c.writes.all { it.size <= 512 })
        assertEquals(512, c.writes.maxOf { it.size })
        val got = FakeCart().feed(c.writes)
        assertEquals(listOf(100 to crcOf(text)), got)
    }

    @Test
    fun oneWriteInFlightAtATime() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f, mtu = 23)
        core.send("一次只写一帧".repeat(5))
        assertEquals(1, c.writes.size)
        core.onWritten(c, true)
        assertEquals(2, c.writes.size)
    }

    @Test
    fun writeStartFailureIsDisconnectAndMessageResentWhole() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f, mtu = 23)
        core.send("写到一半起不来".repeat(4))
        core.onWritten(c, true)                              // 第一帧写完
        c.writeOk = false
        core.onWritten(c, true)                              // 第二帧起不来 -> 按断线处理, 不跳帧
        assertTrue(c.closed)
        assertFalse(core.ready)
        f.advance(2_000)
        val c2 = bringUp(core, f, mtu = 23)
        drainWrites(core, c2)
        assertEquals(listOf(100 to crcOf("写到一半起不来".repeat(4))), FakeCart().feed(c2.writes))
        assertEquals(FrameCodec.T_START, c2.writes[0][0].toInt())
    }

    @Test
    fun gattOperationWatchdog() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("没有回调")
        f.advance(LinkCore.OP_TIMEOUT_MS - 1)
        assertFalse(c.closed)
        f.advance(2)
        assertTrue(c.closed)
        assertFalse(core.ready)
        f.advance(2_000)
        assertEquals(2, f.conns.size)                        // 断了重来
    }

    @Test
    fun watchdogDuringHandshake() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = f.conns.last()
        core.onConnected(c)                                  // 发了 requestMtu, 一直没回
        f.advance(LinkCore.OP_TIMEOUT_MS + 1)
        assertTrue(c.closed)
    }

    @Test
    fun staleCallbacksFromOldConnectionIgnored() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val old = bringUp(core, f)
        core.onDisconnected(old)
        f.advance(2_000)
        val cur = bringUp(core, f)
        core.send("新连接")
        val before = cur.writes.size
        core.onDisconnected(old)                             // 旧连接迟到的回调
        core.onWritten(old, false)
        core.onSubscribed(old, false)
        core.onNotify(old, FakeCart().notify("""{"t":"reply","text":"假的"}""")[0])
        assertTrue(core.ready)
        assertFalse(cur.closed)
        assertEquals(before, cur.writes.size)
        assertFalse(f.lines.any { it.contains("假的") })
    }

    @Test
    fun cartRestartDoesNotSwallowAcks() {
        // H1 / P4: 推车重启后通知序号重来; 手机每次订阅成功都新建回执重组状态
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c1 = bringUp(core, f)
        core.send("第一条")
        drainWrites(core, c1)
        val cartA = FakeCart(txId = 0)
        val (id1, crc1) = cartA.feed(c1.writes).single()
        cartA.ack(id1, crc1).forEach { core.onNotify(c1, it) }
        assertTrue(f.saved.isEmpty())

        core.onDisconnected(c1)                              // 推车重启
        f.advance(2_000)
        val c2 = bringUp(core, f)
        core.send("第二条")
        drainWrites(core, c2)
        val cartB = FakeCart(txId = 0)                       // 新推车的通知序号又从 0 开始
        val (id2, crc2) = cartB.feed(c2.writes).single()
        cartB.ack(id2, crc2).forEach { core.onNotify(c2, it) }
        assertTrue("第二条的回执不能被当成重复丢掉", f.saved.isEmpty())
        assertEquals(2, f.lines.count { it.startsWith("✓") })
    }

    @Test
    fun cartRestartSameReplyBytesStillShown() {
        // 推车重启前后发了一模一样的帧 (同序号同内容): 订阅成功时重建重组状态, 不当重复
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c1 = bringUp(core, f)
        FakeCart(0).notify("""{"t":"reply","text":"好的"}""").forEach { core.onNotify(c1, it) }
        core.onDisconnected(c1)
        f.advance(2_000)
        val c2 = bringUp(core, f)
        FakeCart(0).notify("""{"t":"reply","text":"好的"}""").forEach { core.onNotify(c2, it) }
        assertEquals(2, f.lines.count { it == "大肥鱼: 好的" })
    }

    @Test
    fun ackForSomeoneElseIgnored() {
        // P3: 两台手机连着, 回执是广播; (序号, CRC) 对不上的不认
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("我的")
        drainWrites(core, c)
        val cartSim = FakeCart()
        cartSim.ack(100, crcOf("别人的")).forEach { core.onNotify(c, it) }
        assertEquals(1, f.saved.size)
        cartSim.notify("""{"t":"err","id":100,"crc":${crcOf("别人的")},"code":"len"}""").forEach { core.onNotify(c, it) }
        assertEquals(1, f.saved.size)
        cartSim.ack(100, crcOf("我的")).forEach { core.onNotify(c, it) }
        assertTrue(f.saved.isEmpty())
    }

    @Test
    fun retryScheduledOnlyOncePerMessage() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("重发一次")
        drainWrites(core, c)
        val crc = crcOf("重发一次")
        val cartSim = FakeCart()
        repeat(3) { cartSim.notify("""{"t":"err","id":100,"crc":$crc,"code":"busy"}""").forEach { core.onNotify(c, it) } }
        val before = c.writes.size
        f.advance(LinkCore.RETRY_MS)
        drainWrites(core, c)
        assertEquals("只重发一次", 1, c.writes.drop(before).count { it[0].toInt() == FrameCodec.T_START })
    }

    @Test
    fun reconnectFlushCancelsPendingRetry() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("别发两遍")
        drainWrites(core, c)
        FakeCart().notify("""{"t":"err","id":100,"crc":${crcOf("别发两遍")},"code":"full"}""").forEach { core.onNotify(c, it) }
        core.onDisconnected(c)
        f.advance(2_000)
        val c2 = bringUp(core, f)                             // 重连 flush 已经发了一遍
        drainWrites(core, c2)
        f.advance(LinkCore.RETRY_MS)
        drainWrites(core, c2)
        assertEquals(1, c2.writes.count { it[0].toInt() == FrameCodec.T_START })
    }

    @Test
    fun weakErrorWithoutCrcRetriesButNeverDrops() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("猜的")
        drainWrites(core, c)
        FakeCart().notify("""{"t":"err","id":100,"crc":null,"code":"frame"}""").forEach { core.onNotify(c, it) }
        assertEquals(1, f.saved.size)
    }

    @Test
    fun unpairedStaysInOutboxAndIsResent() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("等 trust")
        drainWrites(core, c)
        val n = c.writes.size
        FakeCart().notify("""{"t":"err","id":100,"crc":${crcOf("等 trust")},"code":"unpaired"}""").forEach { core.onNotify(c, it) }
        assertEquals(1, f.saved.size)
        assertTrue(f.lines.any { it.contains("trust") })
        f.advance(LinkCore.UNPAIRED_RETRY_MS)
        drainWrites(core, c)
        assertTrue(c.writes.size > n)
    }

    @Test
    fun outboxEntriesExpireAfterDedupWindow() {
        val f = bondedFake()
        f.bonds.clear()
        val core = LinkCore(f, f, f)
        core.start()
        core.send("一直没连上")
        assertEquals(1, f.saved.size)
        f.advance(LinkCore.OUTBOX_TTL_MS + LinkCore.TICK_MS)
        assertTrue(f.saved.isEmpty())
        assertTrue(f.lines.any { it.contains("10 分钟") && it.contains("一直没连上") })
    }

    @Test
    fun outboxSurvivesProcessDeath() {
        val f = bondedFake()
        LinkCore(f, f, f).also { it.start(); it.send("被杀之前") }
        val core = LinkCore(f, f, f)                          // 进程被杀, 服务重建
        assertTrue(f.lines.any { it.contains("未送达") && it.contains("被杀之前") })
        core.start()
        val c = bringUp(core, f)
        drainWrites(core, c)
        assertEquals(listOf(100 to crcOf("被杀之前")), FakeCart().feed(c.writes))
    }

    @Test
    fun mustBondBeforeWriting() {
        // H3: 没 bond 先 createBond 并等成功, 之前不写 RX
        val f = Fake().apply { cart = this@LinkCoreTest.cart }
        val core = LinkCore(f, f, f)
        core.start()
        core.send("先配对")
        val c = f.conns.last()
        core.onConnected(c)
        assertEquals(listOf(cart), f.bondAsked)
        assertEquals(0, c.mtuAsked)
        f.bonds[cart] = LinkCore.Bond.BONDED
        core.onBond(cart, LinkCore.Bond.BONDED)
        assertEquals(517, c.mtuAsked)
    }

    @Test
    fun bondFailureDisconnects() {
        val f = Fake().apply { cart = this@LinkCoreTest.cart }
        val core = LinkCore(f, f, f)
        core.start()
        val c = f.conns.last()
        core.onConnected(c)
        f.bonds[cart] = LinkCore.Bond.NONE
        core.onBond(cart, LinkCore.Bond.NONE)
        assertTrue(c.closed)
        assertFalse(core.ready)
    }

    @Test
    fun noReplyShownWhenLinkNotBonded() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        f.bonds[cart] = LinkCore.Bond.NONE                    // 配对被取消 / 地址被冒充
        FakeCart().notify("""{"t":"reply","text":"冒充的回复"}""").forEach { core.onNotify(c, it) }
        assertFalse(f.lines.any { it.contains("冒充的回复") })
        f.bonds[cart] = LinkCore.Bond.BONDED
        FakeCart(5).notify("""{"t":"reply","text":"真的"}""").forEach { core.onNotify(c, it) }
        assertTrue(f.lines.contains("大肥鱼: 真的"))
    }

    @Test
    fun firstConnectNeedsUserConfirm() {
        val f = Fake()
        val core = LinkCore(f, f, f)
        core.start()
        assertEquals(1, f.scans)
        core.onScanResult("02:00:00:00:00:0B", "ops-cart")
        assertEquals(listOf("02:00:00:00:00:0B"), f.confirms)
        assertTrue(f.conns.isEmpty())
        core.confirm("02:00:00:00:00:0B", false)              // 不是这台
        assertNull(f.cart)
        assertEquals(2, f.scans)
        core.onScanResult("02:00:00:00:00:0B", "ops-cart")     // 拒过的不再问
        assertEquals(1, f.confirms.size)
        core.onScanResult(cart, "ops-cart")
        core.confirm(cart, true)
        assertEquals(cart, f.cart)
        assertEquals(1, f.conns.size)
    }

    @Test
    fun connectGattNullRetries() {
        val f = bondedFake().apply { connectReturnsNull = true }
        val core = LinkCore(f, f, f)
        core.start()
        assertFalse(core.ready)
        f.connectReturnsNull = false
        f.advance(2_000)
        assertEquals(1, f.conns.size)
    }

    @Test
    fun adapterOffWaitsForBroadcast() {
        val f = bondedFake().apply { adapter = false }
        val core = LinkCore(f, f, f)
        core.start()
        f.advance(60_000)
        assertTrue(f.conns.isEmpty())
        f.adapter = true
        core.onAdapter(true)
        assertEquals(1, f.conns.size)
        bringUp(core, f)
        f.adapter = false
        core.onAdapter(false)
        assertFalse(core.ready)
        assertTrue(f.conns[0].closed)
    }

    @Test
    fun noScanPermissionNoScan() {
        val f = Fake().apply { scanAllowed = false }
        val core = LinkCore(f, f, f)
        core.start()
        f.advance(60_000)
        assertEquals(0, f.scans)
        assertTrue(f.states.last().contains("权限"))
    }

    @Test
    fun scanGivesUpThenUserKicks() {
        val f = Fake()
        val core = LinkCore(f, f, f)
        core.start()
        f.advance(10 * 60_000)
        assertEquals(LinkCore.SCAN_ROUNDS, f.scans)
        assertTrue(f.states.last().contains("重连"))
        core.kick()
        assertEquals(LinkCore.SCAN_ROUNDS + 1, f.scans)
    }

    @Test
    fun repeatedFailuresGiveUpButKeepCart() {
        // 中1: 不自动忘记推车 (那会给冒充者留路), 失败多了停下等用户
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        repeat(LinkCore.MAX_FAILS) {
            core.onDisconnected(f.conns.last())
            f.advance(30_000)
        }
        assertEquals(cart, f.cart)
        assertEquals(0, f.scans)
        assertTrue(f.states.last().contains("重连"))
        val n = f.conns.size
        f.advance(10 * 60_000)
        assertEquals(n, f.conns.size)
        core.kick()
        assertEquals(n + 1, f.conns.size)
    }

    @Test
    fun discoveryFailureRetriesAndKeepsCart() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = f.conns.last()
        core.onConnected(c)
        core.onMtu(c, 517, true)
        core.onServices(c, false, false)                     // 发现失败 != 没有推车服务
        assertTrue(c.closed)
        assertEquals(cart, f.cart)
        f.advance(2_000)
        assertEquals(2, f.conns.size)
    }

    @Test
    fun missingServiceStopsAndKeepsCart() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = f.conns.last()
        core.onConnected(c)
        core.onMtu(c, 517, true)
        core.onServices(c, true, false)                      // 确实没有推车服务: 停下让用户决定, 不自动忘
        assertEquals(cart, f.cart)
        assertTrue(f.states.last().contains("忘记推车"))
        f.advance(10 * 60_000)
        assertEquals(1, f.conns.size)
    }

    @Test
    fun oldOutboxHeldAfterSwitchingCart() {
        // 中1: 换了推车后, 旧发件箱先问用户再发
        val other = "02:00:00:00:00:0B"
        for (send in listOf(true, false)) {
            val f = bondedFake().apply { adapter = false; bonds[other] = LinkCore.Bond.BONDED }
            val core = LinkCore(f, f, f)
            core.start()
            core.send("给旧推车的")
            f.adapter = true
            core.forget()
            core.onScanResult(other, "ops-cart")
            core.confirm(other, true)
            assertEquals(listOf(1), f.heldAsks)
            assertEquals(1, core.held)
            val c = bringUp(core, f)
            core.send("给新推车的")
            drainWrites(core, c)
            assertEquals("旧的先扣着", listOf(101), FakeCart().feed(c.writes).map { it.first })
            core.releaseOutbox(send)
            drainWrites(core, c)
            val got = FakeCart().feed(c.writes).map { it.first }
            if (send) assertEquals(listOf(101, 100), got) else assertEquals(listOf(101), got)
            assertEquals(0, core.held)
            assertEquals(if (send) 2 else 1, f.saved.size)
        }
    }

    @Test
    fun heldSurvivesRestart() {
        val other = "02:00:00:00:00:0B"
        val f = bondedFake().apply { adapter = false }
        LinkCore(f, f, f).apply { start(); send("旧的"); f.adapter = true; forget(); onScanResult(other, null); confirm(other, true) }
        assertEquals(1, LinkCore(f, f, f).held)
    }

    @Test
    fun authFailuresStopAndPointToSystemSettings() {
        // 第 12 条: 推车上 remove 过这台手机, 手机本地还 BONDED -> 别死循环
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        repeat(LinkCore.AUTH_FAILS) {
            val c = f.conns.last()
            core.onConnected(c)
            core.onMtu(c, 517, true)
            core.onServices(c, true, true)
            core.onSubscribed(c, false, auth = true)
            f.advance(30_000)
        }
        assertTrue(f.states.last().contains("系统蓝牙设置"))
        val n = f.conns.size
        f.advance(10 * 60_000)
        assertEquals(n, f.conns.size)
    }

    @Test
    fun writeAuthFailureCounts() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        repeat(LinkCore.AUTH_FAILS) {
            val c = bringUp(core, f)
            core.send("x")
            core.onWritten(c, false, auth = true)
            f.advance(30_000)
        }
        assertTrue(f.states.last().contains("系统蓝牙设置"))
    }

    @Test
    fun resendOnceWhenNoReceipt() {
        // 第 6 条: 陌生设备连着时推车不发回执; 手机 20 秒没回执就同序号重发
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("等回执")
        drainWrites(core, c)
        f.advance(LinkCore.ACK_WAIT_MS - 1)
        drainWrites(core, c)
        assertEquals(1, c.writes.count { it[0].toInt() == FrameCodec.T_START })
        f.advance(2)
        drainWrites(core, c)
        assertEquals(2, c.writes.count { it[0].toInt() == FrameCodec.T_START })
        FakeCart().ack(100, crcOf("等回执")).forEach { core.onNotify(c, it) }
        f.advance(LinkCore.ACK_WAIT_MS * 3)
        drainWrites(core, c)
        assertEquals(2, c.writes.count { it[0].toInt() == FrameCodec.T_START })
    }

    @Test
    fun replyLinesPrefixedAndCleaned() {
        // 第 5 条: 回复里写"✓ 已送达"也冒充不了回执
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        FakeCart().notify("""{"t":"reply","text":"好\u200b的\n✓ 已送达"}""").forEach { core.onNotify(c, it) }
        assertEquals(listOf("大肥鱼: 好的", "大肥鱼│ ✓ 已送达"), f.lines.filter { it.startsWith("大肥鱼") })
        assertFalse(f.lines.any { it.startsWith("✓") })
    }

    // ------------------------------------------------------------ 变异后仍全绿的缺口

    @Test
    fun subscribeExpiresStaleBeforeFlush() {
        val f = bondedFake().apply { adapter = false }
        val core = LinkCore(f, f, f)
        core.start()
        core.send("过期了")
        f.advance(LinkCore.OUTBOX_TTL_MS + 10)              // 刚过时效, 下一个 tick 还没到
        assertEquals(1, f.saved.size)
        f.adapter = true
        core.onAdapter(true)
        val c = bringUp(core, f)
        assertTrue("过期的不能再发", c.writes.isEmpty())
        assertTrue(f.saved.isEmpty())
    }

    @Test
    fun retryWhileStillWritingDoesNotDuplicateFrames() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f, mtu = 23)
        val text = "还在写".repeat(10)
        core.send(text)
        FakeCart().notify("""{"t":"err","id":100,"crc":${crcOf(text)},"code":"busy"}""").forEach { core.onNotify(c, it) }
        f.advance(LinkCore.RETRY_MS)                          // 重发定时器到了, 第一遍还没写完
        drainWrites(core, c)
        assertEquals(1, c.writes.count { it[0].toInt() == FrameCodec.T_START })
    }

    @Test
    fun straySubscribedOutsidePhaseIgnored() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = f.conns.last()
        core.onConnected(c)                                  // 在 MTU 阶段
        core.onSubscribed(c, true)
        assertFalse(core.ready)
    }

    @Test
    fun weakNostartRetries() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.send("推车没见过开头")
        drainWrites(core, c)
        FakeCart().notify("""{"t":"err","id":100,"crc":null,"code":"nostart"}""").forEach { core.onNotify(c, it) }
        f.advance(LinkCore.RETRY_MS)
        drainWrites(core, c)
        assertEquals(2, c.writes.count { it[0].toInt() == FrameCodec.T_START })
    }

    @Test
    fun confirmOutsideConfirmPhaseIgnored() {
        val f = Fake()
        val core = LinkCore(f, f, f)
        core.start()
        core.confirm(cart, true)
        assertNull(f.cart)
        core.onScanResult(cart, null)
        f.adapter = false
        core.onAdapter(false)                                // 等确认时蓝牙关了
        core.confirm(cart, true)
        assertNull(f.cart)
        assertTrue(f.conns.isEmpty())
    }

    @Test
    fun forgetClearsAndRescans() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        core.start()
        val c = bringUp(core, f)
        core.forget()
        assertNull(f.cart)
        assertTrue(c.closed)
        assertEquals(1, f.scans)
    }

    @Test
    fun badTextRefusedLocally() {
        val f = bondedFake()
        val core = LinkCore(f, f, f)
        assertFalse(core.send("a​b"))
        assertTrue(f.saved.isEmpty())
    }
}
