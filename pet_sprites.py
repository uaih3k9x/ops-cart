#!/usr/bin/env python3
"""pet_sprites: 副屏桌宠的原创像素精灵 (#35). 纯数据 + 拼图, 不依赖 Pillow.

矩阵字符: . 透明  K 描边  W 主毛色  D 远侧腿(暗)  S 条纹  B 浅色肚子/爪尖
          P 粉  C 腮红  E 眼  H 高光  # 花斑
每个姿势拼成 24x24 的字符网格; 全部原创, 不含第三方素材.
"""

HEAD = [
"..K........K..",
".KWK......KWK.",
".KPWK....KWPK.",
".KPWWKKKKWWPK.",
"KWWWWSWWSWWWWK",
"KWWWWWSSWWWWWK",
"KWWWWWWWWWWWWK",
"KWWWWWWWWWWWWK",
"KWWWWWWWWWWWWK",
"KCCWWWPPWWWCCK",
"KWWWWKWWKWWWWK",
".KWWWWKKWWWWK.",
"..KKKKKKKKKK.."]
EARS_FLAT = [
"..............",
"KK..........KK",
"KWK........KWK",
"KWPKKKKKKKKPWK"]
EYES = {
    "open": ((4, 6, ["HE", "EE", "EE"]), (10, 6, ["HE", "EE", "EE"])),
    "sleep": ((2, 7, ["K..K", ".KK."]), (8, 7, ["K..K", ".KK."])),
    "happy": ((2, 7, [".KK.", "K..K"]), (8, 7, [".KK.", "K..K"])),
    "squint": ((3, 8, ["KKK"]), (8, 8, ["KKK"])),
    "angry": ((3, 6, ["K..", "HEK", "HE."]), (8, 6, ["..K", "KEH", ".EH"])),
}
HISS = (4, 10, ["KKKKKK", "KHPPHK", ".KKKK."])

TORSO = [
"...KKKKKKKKKKK..",
"..KWWSWWSWWSWWK.",
".KWWWSWWSWWSWWWK",
".KWWWWWWWWWWWWWK",
".KWWWWWWWWWBBBWK",
".KWWWWWWWWBBBBWK",
"..KKKKKKKKKKKKK."]
TAIL_UP = [
".KK.",
"KWWK",
"KWSK",
"KWWK",
"KSWK",
"KWWK",
"KWSK",
".KWWK",
".KWWK",
"..KWWK"]
TAIL_SWAY = [
"KK..",
"KWK.",
"KSWK",
".KWK",
".KWSK",
".KWWK",
".KSWK",
".KWWK",
".KWWK",
"..KWWK"]
TAIL_SWAY_R = [
"..KK",
".KWK",
"KWSK",
"KWWK",
"KSWK",
"KWWK",
"KWSK",
".KWWK",
".KWWK",
"..KWWK"]
TORSO_RISE = [
"..........KKKKKK",
".........KWWSWWWK",
".......KKWWSWWWBK",
"......KWWSWWWWBBK",
"....KKWWSWWWWBBK.",
"...KWWWSWWWWBBK..",
"..KWWWWWWWKKKK...",
"..KWWWWWKK.......",
"...KKKKK........."]
TORSO_FALL = [
"....KKKK........",
"...KWWWWKK......",
"...KWWSWWWKK....",
"...KWWSWWWWWK...",
"....KWWSWWWWBK..",
"....KKWWSWWWBBK.",
".....KWWWWWBBBK",
"......KKKWWWBBK",
"........KKKKKK."]
TORSO_SQUASH = [
"...KKKKKKKKKKKK",
"..KWWSWWSWWSWWWK",
".KWWWSWWSWWSWWWWK",
"KWWWWWWWWWWWWBBBWK",
"KWWWWWWWWWWWBBBBWK",
"KKKKKKKKKKKKKKKKKK"]
TAIL_LOW = [
".KKKKK",
"KWSWSW",
"KKKKKK"]
TAIL_PUFF = [
"..K..K..",
".KWKKWK.",
"KWWSWWSK",
".KWWWWK.",
"KWSWWSWK",
".KWWWWK.",
"KWWSWWWK",
".KWWWWK.",
".KWSWWK.",
"..KWWWK.",
"..KWWWK."]
TORSO_PUFF0 = [
"....W...W...W...",
"...KWK.KWK.KWK..",
"..KWWWKWWWKWWWK.",
".KWWWWWSWWSWWWWK",
"WKWWWWWSWWSWWWWWK",
".KWWWWWWWWWWWWWWK",
"WKWWWWWWWWWWBBBWK",
".KWWWWWWWWWBBBBWK",
"WKWWWWWWWWWBBBBWK",
".KKKKKKKKKKKKKKK."]
TORSO_PUFF1 = [
"..W...W...W...W.",
".KWK.KWK.KWK.KWK",
"..KWWKWWWKWWWKWK",
"WKWWWWWSWWSWWWWK",
".KWWWWWSWWSWWWWWK",
"WKWWWWWWWWWWWWWWK",
".KWWWWWWWWWWBBBWK",
"WKWWWWWWWWWBBBBWK",
".KWWWWWWWWWBBBBWK",
".KKKKKKKKKKKKKKK."]
TORSO_LOAF = [
".....KKKKKKK",
"...KKWWSWWSK",
"..KWWWWSWWSW",
".KWWSWWSWWSW",
".KWWSWWWWWWW",
"KWWWWWWWWWWW",
"KWWWWWWWWWWW",
"KWWWWWWWWWWW",
"KWWWWWWWWWWW",
"KWWWWWWWWWWW",
"KWWWWWWWWWWWWWWWWWWWWK",
".KKKKKKKKKKKKKKKKKKKK."]
TAIL_WRAP = [
"KKKKKKKK",
"KWSWWSWK",
"KKKKKKKK"]
PAWS = ["KBBBKBBBK","KKKKKKKKK"]
TORSO_SIT = [
"......KWWWWWWWWWWK",
".....KWWSWWWWBBBBWK",
".....KWWSWWWBBBBBWK",
"....KWWWSWWWBBBBBWK",
"....KWWWWWWWWBBBWWK",
"...KWWWWWWWWWKWWWWK",
"...KWWWWWWWWKWWKWWK",
"...KWWWWWWWWKWWKWWK",
"KKKKWWWWWWWWKBBKBBK",
"KSWSKKKKKKKKKKKKKK"]

# 单条腿一个周期 8 相: 0 前伸着地 → 1-3 撑地后移 → 4 后蹬 → 5-7 抬腿前摆.
# 矩阵第 3-5 列是髋. 第 0 行对齐身体底线, 最后一行是描边 (贴地时被去掉).
LEG8 = [
["..KWWWK..","...KWWWK.","....KWWWK","....KBBBK","....KKKKK"],
["..KWWWK..","...KWWWK.","...KWWWK.","...KBBBK.","...KKKKK."],
["..KWWWK..","..KWWWK..","..KWWWK..","..KBBBK..","..KKKKK.."],
["..KWWWK..",".KWWWK...",".KWWWK...",".KBBBK...",".KKKKK..."],
["..KWWWK..",".KWWWK...","KWWWK....","KBBBK....","KKKKK...."],
["..KWWWK..",".KWWWK...","KBBBK....","KKKKK...."],
["..KWWWK..","..KBBBK..","..KKKKK.."],
["..KWWWK..","...KWWWK.","....KBBBK","....KKKKK"]]


def far_leg(rows):
    return [r.replace("W", "D").replace("B", "D") for r in rows]


LEGS = {
"stand": [
"......WWW.....WWW",
".....KWWWK...KWWWK",
".....KWWWK...KWWWK",
".....KBBBK...KBBBK",
".....KKKKK...KKKKK"],
"crouch": [
".....KWWWWK..KWWWWK",
".....KKKKKK..KKKKKK"],
"half": [
"......WWW.....WWW",
".....KBBBK...KBBBK",
".....KKKKK...KKKKK"],
"land": ["KBBBK............KBBBK"],
"riseBack": ["..KWWK",".KWWK.",".KWWK.","KWWK..","KBBK..","KKKK.."],
"riseFront": ["KWWWWK..",".KWWWWK.","..KBBBK.","..KKKKK."],
"fallFront": ["KWWWK","KWWWK","KWWWK","KBBBK","KKKKK"],
"fallBack": ["KWWK","KBBK","KKKK"],
"k0": [
".............WWW.WWW",
"............KWWWKWWWK",
"............KWWWKBBBK",
"............KBBBKKKKK",
"............KKKKK"],
"k1": [
".............WWW.WWW",
"............KWWWKWWWK",
"............KBBBKWWWK",
"............KKKKKBBBK",
".................KKKKK"],
"puff": [
"......WWW.....WWW",
".....KWWWK...KWWWK",
".....KWWWK...KWWWK",
".....KWWWK...KWWWK",
".....KBBBK...KBBBK",
".....KKKKK...KKKKK"],
"runOut": [
"......WWW.....WWW",
"...KKWWWK.....KWWWKK",
"..KWWWKK.......KKWWWK",
".KBBKK...........KKBBK",
".KKK...............KKK"],
"runIn": [
"........WWW..WWW",
".........KWWKKWWK",
"..........KWWWWK",
"..........KBBBBK",
"..........KKKKKK"]}



POSES = {}


def _head(eye="open", flat=False, hiss=False):
    return {"eye": eye, "flat": flat, "hiss": hiss}


def _pose(name, head, hx, hy, *parts, over=()):
    """parts: (矩阵, 列, 行, 种类); 种类 b=身体 t=尾巴, 其它不上花斑. 先画的在后面. over 画在脸前面."""
    POSES[name] = {"parts": parts, "head": head, "hx": hx, "hy": hy, "over": over}


_pose("stand", _head(), 10, 5, (TAIL_UP, 1, 5, "t"), (TORSO, 2, 13, "b"), (LEGS["stand"], 0, 19, ""))
_pose("sit", _head(), 7, 3, (TORSO_SIT, 0, 14, "b"))


def _walk_poses():
    """走路: 对角步态, 前近+后远 与 前远+后近 差半个周期; 身体 1 像素起伏; 尾巴比身体晚一拍."""
    tails = [TAIL_SWAY, TAIL_SWAY, TAIL_UP, TAIL_UP, TAIL_SWAY_R, TAIL_SWAY_R, TAIL_UP, TAIL_UP]
    for i in range(8):
        up, lag = i in (1, 2, 5, 6), i in (2, 3, 6, 7)

        def leg(ph, extra=0):
            m = LEG8[(ph + 8) % 8]
            for _ in range((1 if up else 0) + extra):   # 身体抬高时腿往上多接一行
                m = [m[0]] + m
            return m
        y, b = (18 if up else 19), i + 4
        _pose("walk%d" % i, _head(), 10, 4 if up else 5, (tails[i], 1, 4 if lag else 5, "t"),
              (far_leg(leg(i)), 6, y, ""), (far_leg(leg(b, 2)), 13, y - 2, ""),   # 远侧前腿接到下巴后面
              (TORSO, 2, 12 if up else 13, "b"), (leg(b), 3, y, ""), (leg(i), 11, y, ""))


_walk_poses()
_stand_half = [r[:11] for r in LEGS["stand"]]
_pose("half", _head(), 10, 7, (TAIL_UP, 1, 6, "t"), (TORSO, 2, 15, "b"), (LEGS["half"], 0, 21, ""))
_pose("crouch", _head(), 10, 9, (TAIL_LOW, 0, 17, "t"), (TORSO, 2, 16, "b"), (LEGS["crouch"], 0, 22, ""))
_pose("rise", _head(), 10, 0, (TAIL_LOW, 0, 13, "t"), (far_leg(LEGS["riseBack"]), 3, 16, ""),
      (far_leg(LEGS["riseFront"]), 15, 12, ""), (TORSO_RISE, 1, 8, "b"),
      (LEGS["riseBack"], 1, 16, ""), (LEGS["riseFront"], 14, 14, ""))
_pose("apex", _head(), 10, 3, (TAIL_LOW, 0, 12, "t"), (TORSO, 2, 10, "b"), (LEGS["runIn"], 0, 16, ""))
_pose("fall", _head(), 10, 7, (TAIL_UP, 0, 0, "t"), (far_leg(LEGS["fallBack"]), 8, 13, ""),
      (TORSO_FALL, 0, 5, "b"), (LEGS["fallBack"], 6, 12, ""), (far_leg(LEGS["fallFront"]), 18, 18, ""),
      (LEGS["fallFront"], 14, 19, ""))
_pose("land", _head("squint"), 10, 10, (TAIL_UP, 1, 8, "t"), (TORSO_SQUASH, 0, 17, "b"), (LEGS["land"], 0, 22, ""))
_pose("sleep0", _head("sleep"), 8, 9, (TORSO_LOAF, 1, 12, "b"), (TAIL_WRAP, 1, 20, ""), (PAWS, 12, 22, ""))
_pose("sleep1", _head("sleep"), 8, 10, (TORSO_LOAF[1:], 1, 13, "b"), (TAIL_WRAP, 1, 20, ""), (PAWS, 12, 22, ""))
_pose("knead0", _head("happy"), 10, 7, (TAIL_UP, 1, 5, "t"), (TORSO, 2, 13, "b"), (_stand_half, 0, 19, ""), (LEGS["k0"], 0, 19, ""))
_pose("knead1", _head("happy"), 10, 8, (TAIL_SWAY, 1, 5, "t"), (TORSO, 2, 13, "b"), (_stand_half, 0, 19, ""), (LEGS["k1"], 0, 19, ""))
# 舔爪: 坐着抬前爪, 闭眼低头, 舌头一点粉. 抬起的那只从地上拿走, 地上只剩一只
_TORSO_SIT_ONE = TORSO_SIT[:6] + [
    "...KWWWWWWWWWWWKWWK",
    "...KWWWWWWWWWWWKWWK",
    "KKKKWWWWWWWWWWWKBBK",
    "KSWSKKKKKKKKKKKKKK"]
_PAW_UP = ["KBBBK", "KBBBK", "KWWWK", "KKKKK"]
_lick = _head("happy")
_pose("lick0", _lick, 7, 3, (_TORSO_SIT_ONE, 0, 14, "b"), over=((_PAW_UP, 12, 15, ""),))
_pose("lick1", _lick, 7, 3, (_TORSO_SIT_ONE, 0, 14, "b"), over=((["KPPK"], 13, 14, ""), (_PAW_UP, 12, 15, "")))
_pose("lick2", _lick, 7, 4, (_TORSO_SIT_ONE, 0, 14, "b"), over=((["KPPK"], 13, 15, ""), (_PAW_UP, 12, 16, "")))
_angry = _head("angry", flat=True, hiss=True)
_pose("puff0", _angry, 10, 6, (TAIL_PUFF, 0, 0, "t"), (TORSO_PUFF0, 2, 8, "b"), (LEGS["puff"], 0, 18, ""))
_pose("puff1", _angry, 10, 5, (TAIL_PUFF, 1, 1, "t"), (TORSO_PUFF1, 2, 8, "b"), (LEGS["puff"], 0, 18, ""))
_alert = _head("open", flat=True)
_pose("run0", _alert, 10, 6, (TAIL_LOW, 0, 14, "t"), (TORSO, 2, 12, "b"), (LEGS["runOut"], 0, 18, ""))
_pose("run1", _alert, 10, 6, (TAIL_LOW, 0, 14, "t"), (TORSO, 2, 13, "b"), (LEGS["stand"], 0, 19, ""))
_pose("run2", _alert, 10, 7, (TAIL_UP, 1, 6, "t"), (TORSO, 2, 14, "b"), (LEGS["runIn"], 0, 19, ""))

COATS = ("ginger", "cow")      # 配色在 theme.PET_COATS
_PATCH = {
    "cow": {"h": lambda c, r: (c <= 6 and r <= 5) or (c >= 11 and 4 <= r <= 7),
            "b": lambda c, r: c <= 8 or (c >= 11 and r <= 2),
            "t": lambda c, r: r <= 4},
}


def compose(name, coat="ginger"):
    """姿势 → 24 行 24 列字符网格. 底行描边去掉, 爪子直接贴字: 深描边压在深底上看着像悬空."""
    pose = POSES[name]
    grid = [["."] * 24 for _ in range(24)]
    patches = _PATCH.get(coat, {})

    def paint(rows, cx, cy, kind=""):
        patch = patches.get(kind)
        for r, row in enumerate(rows):
            for c, ch in enumerate(row):
                x, y = cx + c, cy + r
                if ch == "." or not (0 <= x < 24 and 0 <= y < 24):
                    continue
                grid[y][x] = "#" if ch in "WS" and patch and patch(c, r) else ch

    for rows, cx, cy, kind in pose["parts"]:
        paint(rows, cx, cy, kind)
    head, hx, hy = pose["head"], pose["hx"], pose["hy"]
    paint(EARS_FLAT + HEAD[4:] if head["flat"] else HEAD, hx, hy, "h")
    for ex, ey, m in EYES[head["eye"]]:
        paint(m, hx + ex, hy + ey)
    if head["hiss"]:
        paint(HISS[2], hx + HISS[0], hy + HISS[1])
    for rows, cx, cy, kind in pose["over"]:
        paint(rows, cx, cy, kind)
    grid[23] = ["." if ch == "K" else ch for ch in grid[23]]
    return grid


Z_PIX = ["#####", "...#.", "..#..", ".#...", "#####"]
HEART_PIX = [".#.#.", "#####", "#####", ".###.", "..#.."]
