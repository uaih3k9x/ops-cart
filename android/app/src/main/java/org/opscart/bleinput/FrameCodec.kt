package org.opscart.bleinput

import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.util.zip.CRC32

/**
 * 推车 BLE 输入协议的分包编解码 (#52), 和推车侧 ble_input_core.py 同一套规矩, 见 docs/BLE_INPUT.md.
 * 纯逻辑, 不碰安卓 API, 本地单元测试直接跑.
 *
 * START: 01 | 序号 u16 | 总长 u16 | CRC32 u32 | 正文开头
 * CONT : 02 | 序号 u16 | 偏移 u16 | 正文片
 */
object FrameCodec {
    const val T_START = 0x01
    const val T_CONT = 0x02
    const val START_HDR = 9
    const val CONT_HDR = 5
    const val ATT_OVERHEAD = 3
    const val MAX_LEN = 4096

    const val SERVICE_UUID = "c7fa50cf-d4f6-4e23-a71d-7b725c250660"
    const val RX_UUID = "d0b9f7e0-8914-4d78-9501-69062f225d38"
    const val TX_UUID = "a1e1ae6b-abf5-4e3d-9e69-19bafa051e86"

    fun crc32(b: ByteArray): Long = CRC32().apply { update(b) }.value

    fun encode(msgId: Int, payload: ByteArray, mtu: Int): List<ByteArray> {
        require(msgId in 0..0xFFFF) { "序号是 16 位" }
        require(payload.isNotEmpty() && payload.size <= MAX_LEN) { "消息长度要在 1..$MAX_LEN 字节" }
        val room = mtu - ATT_OVERHEAD
        require(room - START_HDR >= 1) { "MTU $mtu 太小" }
        val first = minOf(room - START_HDR, payload.size)
        val out = ArrayList<ByteArray>()
        out += ByteBuffer.allocate(START_HDR + first)
            .put(T_START.toByte()).putShort(msgId.toShort()).putShort(payload.size.toShort())
            .putInt(crc32(payload).toInt()).put(payload, 0, first).array()
        var off = first
        while (off < payload.size) {
            val n = minOf(room - CONT_HDR, payload.size - off)
            out += ByteBuffer.allocate(CONT_HDR + n)
                .put(T_CONT.toByte()).putShort(msgId.toShort()).putShort(off.toShort())
                .put(payload, off, n).array()
            off += n
        }
        return out
    }

    /** 正文有问题返回错误码 (ctrl / empty), 没问题 null. 只收可见字符加换行 / 制表. */
    fun textProblem(text: String): String? {
        if (text.isEmpty()) return "empty"
        var i = 0
        while (i < text.length) {
            val cp = text.codePointAt(i)
            val bidi = cp in 0x202A..0x202E || cp in 0x2066..0x2069
            val ctrl = Character.getType(cp) == Character.CONTROL.toInt() && cp != '\n'.code && cp != '\t'.code
            if (bidi || ctrl) return "ctrl"
            i += Character.charCount(cp)
        }
        return null
    }

    fun decodeUtf8(b: ByteArray): String? = try {
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(b)).toString()
    } catch (e: CharacterCodingException) {
        null
    }

    internal fun u16(b: ByteArray, at: Int) = ((b[at].toInt() and 0xFF) shl 8) or (b[at + 1].toInt() and 0xFF)
    internal fun u32(b: ByteArray, at: Int) = (u16(b, at).toLong() shl 16) or u16(b, at + 2).toLong()
}

/** 收推车的 notify (也能收任何同协议的帧). 时间单位毫秒, 调用方给. */
class Reassembler {
    sealed class Event {
        data class Text(val id: Int, val text: String) : Event()
        data class Dup(val id: Int) : Event()
        data class Error(val id: Int?, val code: String) : Event()
    }

    companion object {
        const val PARTIAL_TTL_MS = 10_000L
        const val MAX_PARTIAL = 2
        const val DEDUP_KEEP = 64
        const val DEDUP_TTL_MS = 600_000L
    }

    private class Partial(val total: Int, val crc: Long, var last: Long) {
        val pieces = sortedMapOf<Int, ByteArray>()
        var have = 0
    }

    private val partial = LinkedHashMap<Int, Partial>()
    private val done = LinkedHashMap<Int, Long>()

    fun expire(now: Long): List<Event> {
        val out = ArrayList<Event>()
        partial.entries.removeAll { (id, p) -> (now - p.last > PARTIAL_TTL_MS).also { if (it) out += Event.Error(id, "timeout") } }
        done.entries.removeAll { now - it.value > DEDUP_TTL_MS }
        return out
    }

    fun feed(frame: ByteArray, now: Long): List<Event> {
        val out = ArrayList(expire(now))
        val type = if (frame.isNotEmpty()) frame[0].toInt() and 0xFF else -1
        val peek = if (frame.size >= 3 && (type == FrameCodec.T_START || type == FrameCodec.T_CONT)) FrameCodec.u16(frame, 1) else null
        val isStart = type == FrameCodec.T_START && frame.size > FrameCodec.START_HDR
        val isCont = type == FrameCodec.T_CONT && frame.size > FrameCodec.CONT_HDR
        if (!isStart && !isCont) return out + Event.Error(peek, "frame")
        val id = peek!!
        val p: Partial
        val off: Int
        val chunk: ByteArray
        if (isStart) {
            if (id in done) return out + Event.Dup(id)
            partial.remove(id)
            val total = FrameCodec.u16(frame, 3)
            chunk = frame.copyOfRange(FrameCodec.START_HDR, frame.size)
            if (total == 0 || total > FrameCodec.MAX_LEN || chunk.size > total) return out + Event.Error(id, "len")
            while (partial.size >= MAX_PARTIAL) {
                val old = partial.keys.first()
                partial.remove(old)
                out += Event.Error(old, "busy")
            }
            p = Partial(total, FrameCodec.u32(frame, 5), now).also { partial[id] = it }
            off = 0
        } else {
            p = partial[id] ?: return if (id in done) out else out + Event.Error(id, "nostart")
            off = FrameCodec.u16(frame, 3)
            chunk = frame.copyOfRange(FrameCodec.CONT_HDR, frame.size)
        }
        val end = off + chunk.size
        val same = p.pieces[off]?.contentEquals(chunk) == true
        if (!same) {
            val code = when {
                end > p.total -> "len"
                p.pieces.any { (o, c) -> off < o + c.size && o < end } -> "overlap"
                else -> null
            }
            if (code != null) {
                partial.remove(id)
                return out + Event.Error(id, code)
            }
            p.pieces[off] = chunk
            p.have += chunk.size
        }
        p.last = now
        if (p.have < p.total) return out
        partial.remove(id)
        val data = p.pieces.values.fold(ByteArray(0)) { a, b -> a + b }
        if (FrameCodec.crc32(data) != p.crc) return out + Event.Error(id, "crc")
        val text = FrameCodec.decodeUtf8(data) ?: return out + Event.Error(id, "utf8")
        FrameCodec.textProblem(text)?.let { return out + Event.Error(id, it) }
        done[id] = now
        while (done.size > DEDUP_KEEP) done.remove(done.keys.first())
        return out + Event.Text(id, text)
    }
}

/** 推车 notify 回来的 JSON. */
sealed class Status {
    data class Ack(val id: Int, val dup: Boolean) : Status()
    data class Err(val id: Int?, val code: String) : Status()
    data class Reply(val text: String) : Status()

    companion object {
        fun parse(s: String): Status? = try {
            val o = org.json.JSONObject(s)
            when (o.optString("t")) {
                "ack" -> Ack(o.getInt("id"), o.optBoolean("dup", false))
                "err" -> Err(if (o.isNull("id")) null else o.getInt("id"), o.getString("code"))
                "reply" -> Reply(o.getString("text"))
                else -> null
            }
        } catch (e: org.json.JSONException) {
            null
        }
    }
}
