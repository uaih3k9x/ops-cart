#!/usr/bin/env python3
"""pet_core: 副屏桌宠的行为 (#35). 纯逻辑: 不碰屏幕、不依赖 Pillow, 时间全靠 tick(dt) 喂.

只在锁屏且确认没事时出现 (allowed). 动的时候 8 fps, 睡着 2 fps, 不在就不推帧.
这块屏局部刷新不显示 (见 docs/udisp-protocol.md), 所以每帧都是整帧.
行为用生成器写: 一只猫一个脚本, 每 tick 往里 send(dt).
"""

import math
import random

S = 5                   # 美术像素放大倍数
CAT_W = S * 21          # 猫占的矩形, 判断某个字头顶站不站得下
CAT_H = S * 20
CEILING = 560           # 猫头最高到这儿: 站分隔线时盖一点下排灯, 维护者说无所谓
W = 800
M = 40                  # 跟 dash_render 的左右边距一致
DIVIDER_Y = 680         # 版面分隔线: 能当跑道
FLOOR_Y = 1130          # 公告下面的空地, 底部提示上方
MOVE_S = 1 / 8          # 动的时候 8 fps (#35 真机整帧 8 fps 目视顺滑)
SLEEP_S = 1 / 2         # 睡着退回副屏原来的 2 fps
MAX_DT = 0.6            # 采集卡一下别让猫瞬移
EPS = 1e-6
COAT, VISITOR = "ginger", "cow"

ANIM = {
    "walk": {"f": ["walk%d" % i for i in range(8)], "beat": .125},
    "antic": {"f": ["half", "crouch", "crouch", "crouch"], "beat": .125, "once": True},
    "landing": {"f": ["land", "land", "land", "half", "half"], "beat": .1, "once": True},
    "run": {"f": ["run0", "run1", "run2", "run1"], "beat": .125},
    "sleep": {"f": ["sleep0", "sleep1"], "beat": 1.2},
    "knead": {"f": ["knead0", "knead1"], "beat": .5},
    "puff": {"f": ["puff0", "puff1"], "beat": .25},
}


def allowed(view):
    """这一帧猫能不能待着: 锁屏, 且没有自检 / 掉线 / 发地址 / UPS 告警 / Agent 待确认 / 操作头在动.
    跟 #34 的"当前动词"接上之前, 就只认这些副屏本来就知道的信号."""
    if not view.get("locked") or view.get("boot") or view.get("stale"):
        return False
    if view.get("serve") or view.get("ups_alert") or view.get("agent"):
        return False
    return not any(h.get("active") for h in view.get("heads") or [])


def red_lamp(view):
    """有红灯: 猫别上分隔线挡着. 只算 FAIL: 黄灯车上常年亮着 (Wi-Fi 占线之类), 算上猫就永远上不去了."""
    return any(x.get("health") == "FAIL" for x in (view.get("lower") or []) + (view.get("heads") or []))


def platforms(red=False):
    """不是字的落脚面. li 用负数, 不跟公告行连成一段; 红灯时拿掉分隔线."""
    floor = {"ch": "地", "li": -1, "left": M, "right": W - M, "top": FLOOR_Y, "bottom": FLOOR_Y + 2, "flat": True}
    if red:
        return [floor]
    return [{"ch": "线", "li": -2, "left": M, "right": W - M, "top": DIVIDER_Y, "bottom": DIVIDER_Y + 2, "flat": True},
            floor]


def layout(glyphs, plats=()):
    """字框 + 落脚面 → 落脚点和能走的连续字段. 每个字都能站, 站下面一行就挡住上一行 (维护者要的);
    只有头会顶破 CEILING 的字不算."""
    glyphs = [dict(g) for g in glyphs] + [dict(p) for p in plats]
    for g in glyphs:
        pad = CAT_W / 2 if g.get("flat") else min(14, (g["right"] - g["left"]) / 3)
        ok = g["top"] - CAT_H >= CEILING
        g["lo"], g["hi"] = (g["left"] + pad, g["right"] - pad) if ok else (None, None)
        g["x"] = (g["lo"] + g["hi"]) / 2 if ok else None
    runs = []
    for g in glyphs:
        if g["x"] is None:
            continue
        r = runs[-1] if runs else None
        p = r["glyphs"][-1] if r else None
        if p and p["li"] == g["li"] and abs(p["top"] - g["top"]) <= 12 and g["left"] - p["right"] <= 24:
            r["glyphs"].append(g)
            r["hi"] = g["hi"]
        else:
            runs.append({"li": g["li"], "glyphs": [g], "lo": g["lo"], "hi": g["hi"]})
    for r in runs:
        r["span"] = r["hi"] - r["lo"]
        for g in r["glyphs"]:
            g["run"] = r
    return {"runs": runs, "perches": [g for g in glyphs if g["x"] is not None]}


def _surface_y(run, x):
    best, dist = run["glyphs"][0], 1e9
    for g in run["glyphs"]:
        d = g["left"] - x if x < g["left"] else x - g["right"] if x > g["right"] else 0
        if d < dist:
            best, dist = g, d
    return best["top"]


class Cat:
    def __init__(self, coat):
        self.coat = coat
        self.x = self.y = 0
        self.dir = 1
        self.pose = "stand"
        self.anim_t = 0.0
        self.visible = False
        self.script = None
        self.started = False
        self.run = None
        self.say = ""
        self.air = False
        self.dust_t = -1.0
        self.alarm = 0.0
        self.arrived = False

    def set_pose(self, p):
        if self.pose != p:
            self.pose, self.anim_t = p, 0.0

    def set_script(self, gen):
        self.script, self.started = gen, False

    def frame(self):
        a = ANIM.get(self.pose)
        if not a:
            return self.pose
        i = int((self.anim_t + EPS) / a["beat"])
        return a["f"][min(i, len(a["f"]) - 1) if a.get("once") else i % len(a["f"])]

    def place(self, g, x=None):
        self.x = g["x"] if x is None else x
        self.y, self.run, self.air = g["top"], g["run"], False

    def snapped(self):
        """位置落到美术像素格上: 横向和空中按 S 取整; 站着时 y 就是字顶."""
        x = int(round(self.x / S)) * S
        return x, int(round(self.y / S)) * S if self.air else int(round(self.y))


def _wait(sec):
    t = 0.0
    while t < sec - EPS:
        t += (yield)


def _hold(c, pose, sec):
    c.set_pose(pose)
    yield from _wait(sec)


def _walk_to(c, tx, speed=56, anim="walk"):
    c.set_pose(anim)
    if abs(tx - c.x) > .5:
        c.dir = 1 if tx > c.x else -1
    while abs(tx - c.x) > .5:
        dt = (yield)
        c.x += c.dir * min(speed * dt, abs(tx - c.x))
        if c.run:
            c.y = _surface_y(c.run, c.x)


def _arc(c, tx, ty, T=None, h=None):
    """一段抛物线. 当拍就离地; 按真实时间走, 采集卡一下也落在同一个点."""
    x0, y0 = c.x, c.y
    T = T if T is not None else (1.5 if math.hypot(tx - x0, ty - y0) > 160 else 1)
    h = h if h is not None else 46 + abs(y0 - ty) * .6
    if abs(tx - x0) > 1:
        c.dir = 1 if tx > x0 else -1
    c.air, c.run = True, None
    t = MOVE_S
    while t < T - EPS:
        u = t / T
        c.x = x0 + (tx - x0) * u
        c.y = y0 + (ty - y0) * u - 4 * h * u * (1 - u)
        c.set_pose("rise" if u < .4 else "apex" if u <= .6 + EPS else "fall")
        t += (yield)
    c.x, c.y, c.air = tx, ty, False


class Pet:
    """橘猫一只常驻, 奶牛猫偶尔路过吵架. sync() 每次采集调一次, tick() 按 interval() 调."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.cat = Cat(COAT)
        self.other = Cat(VISITOR)
        self.lay = layout([])
        self.glyph_key = None
        self.wanted = False
        self.next_at = None     # 推帧节奏, dash_service 用
        self.last_tick = None
        self.drawn = False      # 上一帧画过猫, 猫走了要补一帧擦掉

    # ---------------------------------------------------- 外部接口
    def sync(self, locked, ok, glyphs, red=False):
        """locked: 当前是锁屏页; ok: allowed(view); glyphs: 公告字框; red: red_lamp(view)."""
        if not locked:                  # 换页了, 猫站的字都没了: 立刻消失, 不演
            self._vanish()
            self.wanted = False
            self.glyph_key = None
            return
        key = (red, tuple((g["left"], g["top"]) for g in glyphs))
        if key != self.glyph_key:
            self.glyph_key = key
            self.lay = layout(glyphs, platforms(red))
            if self.cat.visible and self.wanted:
                self.cat.air, self.cat.run = True, None
                self.other.visible, self.other.script, self.other.say = False, None, ""
                self.cat.set_script(self._reland(self.cat))
        if ok and not self.wanted:
            self.wanted = True
            self.cat.set_script(self._come_back(self.cat))
        elif not ok and self.wanted:
            self.wanted = False
            if self.cat.visible:
                self.cat.set_script(self._leave(self.cat))
            if self.other.visible:
                self.other.set_script(self._flee(self.other))

    def tick(self, dt):
        dt = min(dt, MAX_DT)
        for c in (self.cat, self.other):
            if not c.visible and not c.script:
                continue
            c.anim_t += dt
            if c.dust_t >= 0:
                c.dust_t += dt
                if c.dust_t > .6:
                    c.dust_t = -1.0
            if c.alarm > 0:
                c.alarm -= dt
            if c.script:
                try:
                    if c.started:
                        c.script.send(dt)
                    else:
                        c.started = True
                        next(c.script)
                except StopIteration:
                    c.script = None

    def interval(self):
        """下一帧隔多久; 没猫 None (不用推)."""
        if not self.cat.visible and not self.other.visible and not self.cat.script:
            return None
        if self.cat.pose == "sleep" and self.cat.visible and not self.other.visible:
            return SLEEP_S
        return MOVE_S

    def draws(self):
        """要画的猫, 后画的在上面."""
        out = []
        for c in (self.other, self.cat):
            if c.visible:
                x, y = c.snapped()
                out.append({"pose": c.frame(), "state": c.pose, "coat": c.coat, "x": x, "y": y, "dir": c.dir,
                            "t": c.anim_t, "say": c.say, "dust_t": c.dust_t, "alarm": c.alarm > 0})
        return out

    # ---------------------------------------------------- 工具
    def _vanish(self):
        for c in (self.cat, self.other):
            c.visible, c.script, c.say, c.run = False, None, "", None

    def _widest(self):
        return max(self.lay["runs"], key=lambda r: r["span"])

    def _nearest(self, x, y):
        return min(self.lay["perches"], key=lambda g: (g["x"] - x) ** 2 + ((g["top"] - y) * 1.5) ** 2)

    def _spawn(self, c, x, y):
        c.x, c.y, c.visible, c.air, c.run, c.say = x, y, True, True, None, ""

    def _pick(self, seq):
        return seq[self.rng.randrange(len(seq))]

    # ---------------------------------------------------- 动作
    def _jump_to(self, c, g, x=None):
        if x is None and g.get("flat"):     # 跑道那么长, 别每次都落在正中间
            x = self.rng.uniform(g["lo"], g["hi"])
        yield from _hold(c, "antic", .5)
        yield from _arc(c, g["x"] if x is None else x, g["top"])
        c.place(g, x)
        c.dust_t = 0.0
        yield from _hold(c, "landing", .5)
        yield from _hold(c, "stand", .5)

    def _wake(self, c):
        if c.pose == "sleep":
            yield from _hold(c, "sit", .5)
        c.set_pose("stand")

    def _ground(self, c):
        if c.air or not c.run:
            g = self._nearest(c.x, c.y)
            yield from _arc(c, g["x"], g["top"], T=1)
            c.place(g)
            c.dust_t = 0.0
            yield from _hold(c, "landing", .5)

    def _nap(self, c, sec):
        if c.pose != "sleep":
            yield from _hold(c, "stand", .5)
            yield from _hold(c, "sit", .5)
        c.set_pose("sleep")
        yield from _wait(sec)

    def _act_walk(self, c):
        if c.run["span"] < 90:
            w = self._widest()["glyphs"]
            yield from self._jump_to(c, w[1] if len(w) > 1 else w[0])
        r = c.run
        yield from _walk_to(c, r["lo"] if c.x - r["lo"] > r["hi"] - c.x else r["hi"])
        yield from _hold(c, "stand", .5)
        yield from _walk_to(c, r["lo"] + (r["hi"] - r["lo"]) * self.rng.uniform(.3, .7))
        yield from _hold(c, "stand", .5)

    def _act_jump(self, c):
        yield from _hold(c, "antic", .5)
        x, y, run = c.x, c.y, c.run
        yield from _arc(c, x, y, T=1, h=84)
        c.run, c.dust_t = run, 0.0
        yield from _hold(c, "landing", .5)
        yield from _hold(c, "stand", .5)

    def _act_knead(self, c):
        yield from _hold(c, "stand", .5)
        yield from _hold(c, "knead", 5)
        yield from _hold(c, "stand", .5)

    def _other_perch(self, c):
        others = [g for g in self.lay["perches"] if g["run"] is not c.run]
        if others:
            return self._pick(others)
        far = [g for g in self.lay["perches"] if abs(g["x"] - c.x) >= (g["right"] - g["left"]) * .9]
        return self._pick(far) if far else None

    def _act_hop(self, c):
        for _ in range(self._pick([1, 2, 2])):
            g = self._other_perch(c)
            if g is None:
                yield from self._act_jump(c)
                return
            yield from self._jump_to(c, g)

    def _flee(self, c, speed=240):
        left = c.x < W / 2
        if c.run:
            yield from _walk_to(c, c.run["lo"] if left else c.run["hi"], speed, "run")
        c.dir = -1 if left else 1
        c.set_pose("rise")
        yield from _arc(c, -90 if left else W + 90, c.y + 200, T=1, h=40)
        c.visible, c.script, c.say, c.run = False, None, "", None

    def _visitor(self, c, run, tx, from_left):
        self._spawn(c, -90 if from_left else W + 90, run["glyphs"][0]["top"] + 200)
        c.dir = 1 if from_left else -1
        c.set_pose("rise")
        g = run["glyphs"][0 if from_left else -1]
        lx = run["lo"] if from_left else run["hi"]
        yield from _arc(c, lx, g["top"], T=1.5, h=90)
        c.place(g, lx)
        c.dust_t = 0.0
        yield from _hold(c, "landing", .5)
        yield from _walk_to(c, tx, 56)
        c.set_pose("stand")
        c.arrived = True
        while True:
            yield

    def _act_visitor(self, c):
        run = self._widest()
        mid = (run["lo"] + run["hi"]) / 2
        if c.run is not run:
            first = c.x < mid
            yield from self._jump_to(c, run["glyphs"][0 if first else -1], run["lo"] if first else run["hi"])
        side = -1 if c.x <= mid else 1
        ax = run["lo"] if side < 0 else run["hi"]
        yield from _walk_to(c, ax, 90)
        c.dir = -side
        yield from _hold(c, "sit", .5)
        b = self.other
        b.arrived = False
        b.set_script(self._visitor(b, run, ax - side * min(150, run["hi"] - run["lo"]), side > 0))
        while not b.arrived:
            yield
        c.set_pose("stand")             # 对视一拍, 然后炸毛
        c.dir, b.dir = -side, side
        c.alarm = b.alarm = .5
        yield from _wait(.5)
        c.set_pose("puff")
        b.set_pose("puff")
        c.say = "哈!"
        yield from _wait(1)
        c.say, b.say = "", "哈!"
        yield from _wait(1)
        c.say = "哈!"
        yield from _wait(1)
        c.say = b.say = ""
        b.set_script(self._flee(b))
        yield from _wait(1)
        yield from _hold(c, "stand", .5)
        yield from _hold(c, "sit", 1)
        yield from _walk_to(c, (run["lo"] + run["hi"]) / 2)

    def _life(self, c):
        acts = {"walk": self._act_walk, "knead": self._act_knead, "hop": self._act_hop,
                "jump": self._act_jump, "visitor": self._act_visitor}
        while True:
            yield from self._nap(c, self.rng.uniform(24, 42))
            act = self._pick(["walk", "walk", "knead", "knead", "knead", "hop", "hop", "hop", "jump", "visitor"])
            yield from self._wake(c)
            yield from acts[act](c)

    def _reland(self, c):
        yield from self._ground(c)
        yield from self._life(c)

    def _leave(self, c):
        yield from self._ground(c)
        yield from self._wake(c)
        yield from self._flee(c, 200)

    def _come_back(self, c):
        if not c.visible:
            g = self.lay["perches"][0]
            self._spawn(c, -90, g["top"] + 200)
            c.dir = 1
            c.set_pose("rise")
            yield from _arc(c, g["x"], g["top"], T=1.5, h=90)
            c.place(g)
            c.dust_t = 0.0
            yield from _hold(c, "landing", .5)
        else:
            yield from self._ground(c)
        yield from _hold(c, "stand", .5)
        yield from _walk_to(c, (c.run["lo"] + c.run["hi"]) / 2)
        yield from self._life(c)
