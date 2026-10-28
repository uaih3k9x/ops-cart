"""MIDI 两手 -> 蜂鸣器琶音: 每个时间片取 最高(旋律)/中间/最低(低音) 三个音, STEP ms 轮换, 假装和弦."""
import sys, json, mido
STEP = int(sys.argv[3]) if len(sys.argv) > 3 else 25
SHIFT = int(sys.argv[4]) if len(sys.argv) > 4 else 0        # 整首升几个八度
FLOOR = int(sys.argv[5]) if len(sys.argv) > 5 else 0        # 低于这个 Hz 的再往上搬八度 (压电片低频发不出)
m = mido.MidiFile(sys.argv[1])
notes, tempo = [], 500000
for ti, track in enumerate(m.tracks):
    now, on, tempo = 0.0, {}, 500000
    for msg in mido.merge_tracks([m.tracks[0], track]) if ti else track:   # 每轨都要带上 track0 的 tempo
        now += mido.tick2second(msg.time, m.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0 and (ti == 0 or msg in track):
            on[msg.note] = now
        elif msg.type in ("note_off", "note_on") and msg.note in on:
            notes.append((on.pop(msg.note), now, msg.note))
def hz(n):
    f = 440 * 2 ** ((n + 12 * SHIFT - 69) / 12)
    while f < FLOOR:
        f *= 2
    return round(f)
edges = sorted({x for a, b, _ in notes for x in (a, b)})
out = []
for a, b in zip(edges, edges[1:]):
    live = sorted({n for s, e, n in notes if s <= a and e >= b}, reverse=True)
    ms = (b - a) * 1000
    if ms < 1:
        continue
    if not live:
        out.append([0, ms]); continue
    pick = [live[0]] + ([live[len(live) // 2]] if len(live) > 2 else []) + ([live[-1]] if len(live) > 1 else [])
    if len(pick) == 1 or ms < STEP * 1.5:
        out.append([hz(pick[0]), ms]); continue
    t, i = 0.0, 0
    while t < ms:
        d = min(STEP, ms - t)
        out.append([hz(pick[i % len(pick)]), d]); t += d; i += 1
out = [[f, round(d, 1)] for f, d in out]
json.dump(out, open(sys.argv[2], "w"))
print("events", len(out), "total %.1fs" % (sum(d for _, d in out) / 1000))
