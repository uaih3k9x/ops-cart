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
COAT = "ginger"         # 主猫; 外部皮肤换的就是它
PERSONA = {"cow": "timid", "stray": "bold", "tabby": "watcher"}   # 路人的性格
TERRITORY = 190         # 进这个距离才哈气
FIGHT_CHANCE = .35      # 橘猫僵持时打起来的概率
VISIT_GAP = (240, 600)  # 路人多久来一次 (秒)

ANIM = {
    "walk": {"f": ["walk%d" % i for i in range(8)], "beat": .125},
    "antic": {"f": ["half", "crouch", "crouch", "crouch"], "beat": .125, "once": True},
    "landing": {"f": ["land", "land", "land", "half", "half"], "beat": .1, "once": True},
    "run": {"f": ["run0", "run1", "run2", "run1"], "beat": .125},
    "sleep": {"f": ["sleep0", "sleep1"], "beat": 1.2},
    "knead": {"f": ["knead0", "knead1"], "beat": .5},
    "puff": {"f": ["puff0", "puff1"], "beat": .25},
    "lick": {"f": ["lick0", "lick1", "lick2", "lick1"], "beat": .25},
}


NO_BLINK = {"sleep", "knead", "puff", "lick", "landing"}     # 这些动作眼睛闭着, 或者张嘴不能换表情
NO_BLINK_EYES = {"happy", "closed", "sleep", "squint"}


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
        self.eye = None         # 当前心情; None = 姿势自带的眼睛
        self.blink_left = 2.0   # 还有多久眨眼
        self.blink = []         # 眨眼还剩哪几帧
        self.hidden = False     # 打架时藏在烟雾里
        self.signal = None      # 主猫发给路人的: flee / puff / rush
        self.bump = 0.0         # 打完架头上的星星还剩几秒

    def shown_eye(self):
        """这一帧实际画什么眼睛; None = 姿势自带."""
        return self.blink[0] if self.blink else self.eye

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
    """主猫常驻, 路人猫随机来, 各走各的状态机. sync() 每次采集调一次, tick() 按 interval() 调."""

    def __init__(self, rng=None, strays=("cow", "stray", "tabby")):
        self.rng = rng or random.Random()
        self.cat = Cat(COAT)
        self.other = Cat(strays[0] if strays else "cow")
        self.strays = strays
        self.smoke = None       # 打架烟雾 {x, y, t}
        self.reacting = False   # 主猫正在应付路人
        self.forced_fight = None
        self.next_visit = self.rng.uniform(*VISIT_GAP)
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
                self.other.hidden = self.cat.hidden = False
                self.reacting, self.smoke = False, None
                self.cat.set_script(self._reland(self.cat))
        if ok and not self.wanted:
            self.wanted = True
            self.cat.set_script(self._come_back(self.cat))
        elif not ok and self.wanted:
            self.wanted = False
            if self.cat.visible:
                self.cat.set_script(self._leave(self.cat))
            self.reacting, self.smoke = False, None
            self.cat.hidden = self.other.hidden = False
            if self.other.visible:
                self.other.set_script(self._flee(self.other))

    def tick(self, dt):
        dt = min(dt, MAX_DT)
        self._blink(dt)
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
            if c.bump > 0:
                c.bump -= dt
            if c.script:
                try:
                    if c.started:
                        c.script.send(dt)
                    else:
                        c.started = True
                        next(c.script)
                except StopIteration:
                    c.script = None
        if self.smoke:
            self.smoke["t"] += dt
        self._director(dt)

    def visit(self, coat=None, fight=None):
        """叫一只路人来. coat/fight 不给就随机. 已经有路人就不叫."""
        o = self.other
        if o.visible or o.script or not self.strays:
            return
        o.coat = coat or self._pick(self.strays)
        o.signal, o.say, o.hidden = None, "", False
        self.forced_fight = fight
        o.set_script(self._stray(o))
        self.next_visit = self.rng.uniform(*VISIT_GAP)

    def effects(self):
        """烟雾这类不属于哪只猫的东西."""
        return self.smoke

    def _director(self, dt):
        """路人什么时候来; 路人落地后主猫就开始盯."""
        if not self.wanted or not self.cat.visible:
            return
        o = self.other
        if o.visible or o.script:
            if not self.reacting and o.visible and not o.air:
                self.reacting = True
                self.cat.set_script(self._react(self.cat, o))
            return
        self.next_visit -= dt
        if self.next_visit <= 0:
            self.visit()

    def _blink(self, dt):
        """睁着眼才眨: 隔几秒来一次, 半眯 → 闭眼 → 睁开, 各一帧. 本来就半眯的只闭一帧."""
        c = self.cat
        if c.blink:
            c.blink = c.blink[1:]
            return
        c.blink_left -= dt
        if c.blink_left > 0:
            return
        c.blink_left = self.rng.uniform(2, 6)
        if c.pose in NO_BLINK or c.eye in NO_BLINK_EYES:
            return
        c.blink = ["closed"] if c.eye == "half" else ["half", "closed"]

    def interval(self):
        """下一帧隔多久; 没猫 None (不用推)."""
        if not self.cat.visible and not self.other.visible and not self.cat.script and not self.smoke:
            return None
        if self.cat.pose == "sleep" and self.cat.visible and not self.other.visible and not self.smoke:
            return SLEEP_S
        return MOVE_S

    def draws(self):
        """要画的猫, 后画的在上面."""
        out = []
        for c in (self.other, self.cat):
            if c.visible and not c.hidden:
                x, y = c.snapped()
                out.append({"pose": c.frame(), "state": c.pose, "coat": c.coat, "x": x, "y": y, "dir": c.dir,
                            "t": c.anim_t, "eye": c.shown_eye(), "say": c.say, "dust_t": c.dust_t, "alarm": c.alarm > 0, "bump": c.bump > 0})
        return out

    # ---------------------------------------------------- 工具
    def _vanish(self):
        for c in (self.cat, self.other):
            c.visible, c.script, c.say, c.run, c.hidden = False, None, "", None, False
        self.reacting, self.smoke = False, None

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
        c.eye = self._pick([None, None, None, "half"])    # 偶尔没睡醒
        yield from _walk_to(c, r["lo"] if c.x - r["lo"] > r["hi"] - c.x else r["hi"])
        yield from _hold(c, "stand", .5)
        yield from _walk_to(c, r["lo"] + (r["hi"] - r["lo"]) * self.rng.uniform(.3, .7))
        c.eye = None
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
        c.eye = self._pick([None, None, "half", "down"])
        yield from _hold(c, "knead", 5)
        c.eye = None
        yield from _hold(c, "stand", .5)

    def _act_lick(self, c):
        """坐下舔两下爪子, 一副什么都没发生的样子."""
        yield from _hold(c, "sit", .5)
        c.eye = self._pick(["half", "half", "closed", "open", None])     # 面无表情: 刚才不是我
        yield from _hold(c, "lick", self.rng.uniform(3, 4))
        yield from _hold(c, "sit", .5)
        c.eye = None
        c.set_pose("stand")

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

    def _near(self, a, b):
        return math.hypot(a.x - b.x, (a.y - b.y) * 1.2) < TERRITORY

    def _face(self, c, o):
        if abs(o.x - c.x) > 1:
            c.dir = 1 if o.x > c.x else -1

    def _stray(self, c):
        """路人自己的状态机: 进场 → 溜达 → 听主猫的信号. 不用管主猫在干嘛."""
        persona = PERSONA.get(c.coat, "timid")
        runs = self.lay["runs"]
        me = self.cat
        if persona == "watcher":                # 梨花找个离主猫够远的地方蹲
            yield from self._watch_from_afar(c)
            return
        run = me.run or self._pick([r for r in runs if r["span"] >= 90] or runs)
        from_left = me.x > (run["lo"] + run["hi"]) / 2
        g = run["glyphs"][0 if from_left else -1]
        self._spawn(c, -90 if from_left else W + 90, g["top"] + 200)
        c.dir = 1 if from_left else -1
        c.set_pose("rise")
        lx = run["lo"] if from_left else run["hi"]
        yield from _arc(c, lx, g["top"], T=1.5, h=90)
        c.place(g, lx)
        c.dust_t = 0.0
        yield from _hold(c, "landing", .5)
        t = 0.0
        c.set_pose("walk")
        while c.signal is None and t < 45:      # 往主猫那边走; 够不着就算了
            dt = (yield)
            t += dt
            tx = min(run["hi"], max(run["lo"], me.x - (1 if from_left else -1) * 120))
            if abs(tx - c.x) > 2:
                c.dir = 1 if tx > c.x else -1
                c.x += c.dir * min(56 * dt, abs(tx - c.x))
                c.y = _surface_y(run, c.x)
                c.set_pose("walk")
            else:
                c.set_pose("stand")
        if c.signal == "puff":
            self._face(c, me)
            c.set_pose("puff")
            while c.signal == "puff":
                yield
        while c.signal == "rush":
            dt = (yield)
            self._face(c, me)
            c.set_pose("run")
            c.x += c.dir * min(160 * dt, abs(me.x - c.x))
        c.hidden, c.say = False, ""
        yield from self._flee(c)

    def _watch_from_afar(self, c):
        """梨花: 跳到领地外, 蹲着盯一会儿, 自己走. 主猫靠近地盘外不会哈它."""
        me, margin = self.cat, TERRITORY + 40
        spots = [(g, x) for g in self.lay["perches"] for x in (g["lo"], g["hi"])
                 if math.hypot(x - me.x, (g["top"] - me.y) * 1.2) >= margin]
        if not spots:                           # 整屏都太近, 那今天就不来了
            c.visible, c.script = False, None
            return
        g, x = self._pick(spots)
        from_left = x < W / 2
        self._spawn(c, -90 if from_left else W + 90, g["top"] + 200)
        c.dir = 1 if from_left else -1
        c.set_pose("rise")
        yield from _arc(c, x, g["top"], T=1.5, h=90)
        c.place(g, x)
        c.dust_t = 0.0
        yield from _hold(c, "landing", .5)
        c.set_pose("sit")
        self._face(c, me)
        t, stay = 0.0, self.rng.uniform(15, 30)
        while t < stay and c.signal is None:
            self._face(c, me)
            t += (yield)
        yield from self._flee(c, 120)

    def _react(self, c, o):
        """主猫: 醒 → 盯 → 靠近才哈 → 哈跑 → 舔爪 → 接着过日子."""
        if c.air or not c.run:
            yield from self._ground(c)
        if c.pose == "sleep":
            c.eye = "half"
            yield from _hold(c, "sit", .5)
        c.eye = "half"
        c.set_pose("sit")
        while o.visible and not self._near(c, o):
            self._face(c, o)
            yield
        if o.visible:
            yield from self._hiss(c, o)
        c.eye = None
        self.reacting = False
        yield from self._act_lick(c)
        yield from self._life(c)

    def _hiss(self, c, o):
        persona = PERSONA.get(o.coat, "timid")
        c.eye = None
        c.set_pose("stand")
        self._face(c, o)
        c.alarm = .5
        yield from _wait(.5)
        c.set_pose("puff")
        c.say = "哈!"
        yield from _wait(1)
        if persona == "bold":
            o.signal = "puff"
            self._face(o, c)
            c.say, o.say = "", "哈!"
            yield from _wait(1)
            c.say, o.say = "哈!", ""
            yield from _wait(1)
            c.say = ""
            fight = self.forced_fight
            if fight is None:
                fight = self.rng.random() < FIGHT_CHANCE
            if fight:
                yield from self._fight(c, o)
                return
        c.say = ""
        o.signal = "flee"
        yield from _wait(1)
        c.set_pose("stand")

    def _fight(self, c, o):
        """两只都钻进一团烟里, 飞点爪子和星星, 散开后对方跑了, 主猫头上多颗星."""
        o.signal = "rush"
        self._face(c, o)
        c.set_pose("run")
        while abs(c.x - o.x) > 60:
            dt = (yield)
            c.x += c.dir * 160 * dt
        c.hidden = o.hidden = True
        self.smoke = {"x": (c.x + o.x) / 2, "y": c.y, "t": 0.0}
        yield from _wait(self.rng.uniform(2, 3))
        self.smoke = None
        c.hidden, c.bump = False, 6.0
        c.set_pose("sit")
        o.signal = "flee"
        yield from _wait(1)

    def _life(self, c):
        acts = {"walk": self._act_walk, "knead": self._act_knead, "hop": self._act_hop, "jump": self._act_jump}
        while True:
            yield from self._nap(c, self.rng.uniform(24, 42))
            act = self._pick(["walk", "walk", "knead", "knead", "knead", "hop", "hop", "hop", "jump"])
            yield from self._wake(c)
            if self.rng.random() < .4:      # 睡醒先舔爪
                yield from self._act_lick(c)
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
