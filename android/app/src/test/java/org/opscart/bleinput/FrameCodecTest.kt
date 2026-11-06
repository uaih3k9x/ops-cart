package org.opscart.bleinput

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

private fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }
private fun unhex(s: String) = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

class FrameCodecTest {
    private val t0 = 1_700_000_000_000L

    private fun vectors(): JSONObject {
        val path = System.getProperty("bleVectors") ?: "../../docs/ble_input_vectors.json"
        return JSONObject(File(path).readText(Charsets.UTF_8))
    }

    private fun feedAll(rx: Reassembler, frames: List<ByteArray>, now: Long = t0) =
        frames.flatMap { rx.feed(it, now) }

    @Test
    fun startFrameLayoutByHand() {
        val f = FrameCodec.encode(0x1234, "hi".toByteArray(), 23).single()
        assertEquals("0112340002d8932aac6869", hex(f))
    }

    @Test
    fun framesFitMtu() {
        val frames = FrameCodec.encode(7, ByteArray(40) { it.toByte() }, 23)
        assertTrue(frames.all { it.size <= 20 })
        assertEquals("020007000b", hex(frames[1].copyOfRange(0, 5)))
    }

    @Test
    fun sharedVectorsEncodeAndDecode() {
        val vs = vectors().getJSONArray("vectors")
        assertTrue(vs.length() > 0)
        for (i in 0 until vs.length()) {
            val v = vs.getJSONObject(i)
            val payload = unhex(v.getString("payload_hex"))
            assertArrayEquals(v.getString("text").toByteArray(Charsets.UTF_8), payload)
            val frames = FrameCodec.encode(v.getInt("msg_id"), payload, v.getInt("mtu"))
            val want = v.getJSONArray("frames_hex")
            assertEquals(v.getString("name"), (0 until want.length()).map { want.getString(it) }, frames.map(::hex))
            val evs = feedAll(Reassembler(), frames)
            assertEquals(v.getString("name"), listOf(v.getString("text")), evs.map { (it as Reassembler.Event.Text).text })
        }
    }

    @Test
    fun sharedRejects() {
        val rs = vectors().getJSONArray("rejects")
        for (i in 0 until rs.length()) {
            val r = rs.getJSONObject(i)
            val fh = r.getJSONArray("frames_hex")
            val evs = feedAll(Reassembler(), (0 until fh.length()).map { unhex(fh.getString(it)) })
            val last = evs.last() as Reassembler.Event.Error
            assertEquals(r.getString("name"), r.getString("code"), last.code)
        }
    }

    @Test
    fun chineseEmojiRoundTripManyMtus() {
        for (mtu in listOf(23, 64, 185, 517)) {
            val text = "装机完成 ✅🚀👨‍👩‍👧 换行\n制表\t"
            val evs = feedAll(Reassembler(), FrameCodec.encode(5, text.toByteArray(), mtu))
            assertEquals(listOf<Reassembler.Event>(Reassembler.Event.Text(5, text)), evs)
        }
    }

    @Test
    fun outOfOrderAndDuplicateFragments() {
        val text = "乱序到达也要拼对".repeat(5)
        val frames = FrameCodec.encode(3, text.toByteArray(), 23)
        val shuffled = listOf(frames[0], frames[2]) + frames.drop(1).reversed()
        assertEquals(listOf<Reassembler.Event>(Reassembler.Event.Text(3, text)), feedAll(Reassembler(), shuffled))
    }

    @Test
    fun halfPacketTimeout() {
        val rx = Reassembler()
        val frames = FrameCodec.encode(11, ByteArray(50) { 'y'.code.toByte() }, 23)
        rx.feed(frames[0], t0)
        assertEquals(emptyList<Reassembler.Event>(), rx.expire(t0 + Reassembler.PARTIAL_TTL_MS - 1))
        assertEquals(listOf<Reassembler.Event>(Reassembler.Event.Error(11, "timeout")),
            rx.expire(t0 + Reassembler.PARTIAL_TTL_MS + 1))
    }

    @Test
    fun duplicateMessageIsDup() {
        val rx = Reassembler()
        val frames = FrameCodec.encode(9, "只收一次".toByteArray(), 23)
        feedAll(rx, frames)
        assertEquals(listOf<Reassembler.Event>(Reassembler.Event.Dup(9)), feedAll(rx, frames))
    }

    @Test(expected = IllegalArgumentException::class)
    fun tooLongRefusedAtEncode() {
        FrameCodec.encode(1, ByteArray(FrameCodec.MAX_LEN + 1), 247)
    }

    @Test
    fun checkTextRejectsControl() {
        assertEquals("ctrl", FrameCodec.textProblem("\u001b[2J"))
        assertEquals("ctrl", FrameCodec.textProblem("a‮b"))
        assertEquals(null, FrameCodec.textProblem("你好\n\t👍"))
        assertEquals("empty", FrameCodec.textProblem(""))
    }

    @Test
    fun statusParsing() {
        assertEquals(Status.Ack(5, 0xDEADBEEFL, false), Status.parse("""{"t":"ack","id":5,"crc":3735928559}"""))
        assertEquals(Status.Ack(5, 7L, true), Status.parse("""{"t":"ack","id":5,"crc":7,"dup":true}"""))
        assertEquals(Status.Err(5, 9L, "crc"), Status.parse("""{"t":"err","id":5,"crc":9,"code":"crc"}"""))
        assertEquals(Status.Err(null, null, "frame"), Status.parse("""{"t":"err","id":null,"crc":null,"code":"frame"}"""))
        assertEquals(Status.Reply("好的"), Status.parse("""{"t":"reply","text":"好的"}"""))
        assertEquals(null, Status.parse("not json"))
    }

    // ------------------------------------------------------------ 审查意见 (#63, 协议 v2)

    @Test
    fun frameNeverExceedsAttMax() {
        val frames = FrameCodec.encode(1, ByteArray(FrameCodec.MAX_LEN) { 'x'.code.toByte() }, 517)
        assertEquals(512, frames.maxOf { it.size })
        assertEquals(FrameCodec.MAX_LEN, (feedAll(Reassembler(), frames).single() as Reassembler.Event.Text).text.length)
    }

    @Test
    fun sameIdDifferentContentIsNew() {
        val rx = Reassembler()
        feedAll(rx, FrameCodec.encode(0, "first".toByteArray(), 185))
        assertEquals(listOf<Reassembler.Event>(Reassembler.Event.Text(0, "DIFFERENT")),
            feedAll(rx, FrameCodec.encode(0, "DIFFERENT".toByteArray(), 185)))
    }

    @Test
    fun sharedTextVectors() {
        val v = vectors()
        assertTrue(v.getString("protocol").startsWith("ops-cart BLE input v2"))
        val rj = v.getJSONArray("text_rejects")
        assertTrue(rj.length() >= 8)
        for (i in 0 until rj.length()) {
            val r = rj.getJSONObject(i)
            val text = String(unhex(r.getString("utf8_hex")), Charsets.UTF_8)
            assertEquals(r.getString("name"), r.getString("code"), FrameCodec.textProblem(text))
        }
        val ok = v.getJSONArray("text_accepts")
        for (i in 0 until ok.length()) {
            val r = ok.getJSONObject(i)
            assertEquals(r.getString("name"), null, FrameCodec.textProblem(String(unhex(r.getString("utf8_hex")), Charsets.UTF_8)))
        }
    }

    @Test
    fun sharedStatusVectors() {
        val st = vectors().getJSONArray("status")
        for (i in 0 until st.length()) {
            val v = st.getJSONObject(i)
            val crc = if (v.isNull("crc")) null else v.getLong("crc")
            val want = when (v.getString("kind")) {
                "text" -> Status.Ack(v.getInt("id"), crc, false)
                "dup" -> Status.Ack(v.getInt("id"), crc, true)
                else -> Status.Err(v.getInt("id"), crc, v.getString("code"))
            }
            assertEquals(v.getString("name"), want, Status.parse(v.getString("json")))
        }
    }

    @Test
    fun crcOfMatchesCart() {
        assertEquals(0xd8932aacL, FrameCodec.crc32("hi".toByteArray()))
    }
}
