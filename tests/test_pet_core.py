"""副屏桌宠 (#35): 精灵数据 + 行为模拟. 纯逻辑, 不碰屏幕."""
import random

import pet_core as pc
import pet_sprites as ps
import theme as th


def _glyphs(top=840, n=4, w=96, x0=208):
    """一行字: 每个字 w 宽, 紧挨着."""
    return [{"ch": "字", "li": 0, "left": x0 + i * w, "right": x0 + (i + 1) * w, "top": top, "bottom": top + w}
            for i in range(n)]


def _idle(**over):
    view = {"locked": True, "stale": False, "serve": None, "ups_alert": None, "agent": None,
            "heads": [{"active": False}], "notice": "x"}
    view.update(over)
    return view


def test_every_pose_composes_to_24_by_24_for_both_coats():
    for name in ps.POSES:
        for coat in ps.COATS:
            grid = ps.compose(name, coat)
            assert len(grid) == 24 and all(len(r) == 24 for r in grid)
            assert all(ch == "." or ch in th.PET_COATS[coat] for r in grid for ch in r), name


def test_no_pose_is_clipped_by_the_sprite_box():
    """脸和身子都不能被 24x24 切掉一块 (踩奶时脸缺过一列)."""
    for name, pose in ps.POSES.items():
        head = ps.EARS_FLAT + ps.HEAD[4:] if pose["head"]["flat"] else ps.HEAD
        parts = list(pose["parts"]) + [(head, pose["hx"], pose["hy"], "h")] + list(pose["over"])
        for rows, cx, cy, _ in parts:
            for r, row in enumerate(rows):
                for c, ch in enumerate(row):
                    assert ch == "." or (0 <= cx + c < 24 and 0 <= cy + r < 24), (name, cx + c, cy + r)


def test_every_animation_frame_is_a_real_pose():
    for anim in pc.ANIM.values():
        assert all(f in ps.POSES for f in anim["f"])


def test_allowed_only_when_locked_and_nothing_going_on():
    assert pc.allowed(_idle())
    for over in ({"locked": False}, {"boot": {"rows": []}}, {"stale": True}, {"serve": {"text": "x"}},
                 {"ups_alert": {"text": "x"}}, {"agent": {"text": "x"}}, {"heads": [{"active": True}]}):
        assert not pc.allowed(_idle(**over)), over


def test_cat_can_stand_on_second_line_and_cover_the_first():
    upper = _glyphs(top=800, n=2, x0=304)
    for g in upper:
        g["bottom"] = 840
    lower = [dict(g, li=1) for g in _glyphs(top=850, n=4)]
    lay = pc.layout(upper + lower)
    assert len(lay["perches"]) == 6
    assert sum(1 for r in lay["runs"] if r["li"] == 1) == 1


def test_perches_below_the_ceiling_only():
    assert pc.layout(_glyphs(top=pc.CEILING + pc.CAT_H - 1))["perches"] == []


def test_platforms_are_perches_even_without_text():
    pet = pc.Pet(random.Random(1))
    pet.sync(True, True, [])
    for _ in range(40):
        pet.tick(0.125)
    assert pet.draws()


def test_red_lamp_takes_the_divider_away():
    assert pc.DIVIDER_Y in {p["top"] for p in pc.platforms(False)}
    assert {p["top"] for p in pc.platforms(True)} == {pc.FLOOR_Y}


def test_red_lamp_means_lamp_fail_only_not_routine_yellow():
    assert not pc.red_lamp({"lower": [{"health": "WARN"}, {"health": None}], "heads": [{"health": "OK"}]})
    assert pc.red_lamp({"lower": [{"health": "FAIL"}], "heads": []})
    assert pc.red_lamp({"lower": [], "heads": [{"health": "FAIL"}]})


def test_cat_on_divider_gets_off_when_a_lamp_goes_red():
    pet = pc.Pet(random.Random(1))
    glyphs = _glyphs()
    pet.sync(True, True, glyphs)
    div = next(g for g in pet.lay["perches"] if g["top"] == pc.DIVIDER_Y)
    pet.cat.visible, pet.cat.script = True, None
    pet.cat.place(div)
    pet.sync(True, True, glyphs, red=True)
    _settle(pet, 5)
    assert pet.cat.y != pc.DIVIDER_Y
    assert all(g["top"] != pc.DIVIDER_Y for g in pet.lay["perches"])
    _settle(pet, 1200)
    assert pet.cat.y != pc.DIVIDER_Y


def test_cat_visits_every_level():
    pet = pc.Pet(random.Random(8))
    pet.sync(True, True, _glyphs())
    tops = set()
    for _ in range(int(3 * 3600 / 0.125)):
        pet.tick(0.125)
        if pet.cat.visible and not pet.cat.air and pet.cat.run:
            tops.add(pet.cat.y)
    assert {840, pc.DIVIDER_Y, pc.FLOOR_Y} <= tops


def test_platforms_are_not_obstacles_for_text():
    """贴着分隔线的字照样能站, 线不算挡头."""
    lay = pc.layout(_glyphs(top=pc.DIVIDER_Y + 20), pc.platforms(False))
    assert any(g["top"] == pc.DIVIDER_Y + 20 for g in lay["perches"])


def test_text_never_merges_with_platform_runs():
    lay = pc.layout(_glyphs(top=pc.FLOOR_Y), pc.platforms(False))
    assert len(lay["runs"]) == 3


def _settle(pet, secs=400, dt=0.125):
    for _ in range(int(secs / dt)):
        pet.tick(dt)


def test_cat_arrives_and_stands_on_a_glyph_top():
    pet = pc.Pet(random.Random(2))
    glyphs = _glyphs()
    pet.sync(True, True, glyphs)
    seen_on = 0
    for _ in range(4000):
        pet.tick(0.125)
        d = pet.draws()
        assert d
        if not pet.cat.air and pet.cat.visible and pet.cat.run:
            assert d[-1]["y"] in (840, pc.DIVIDER_Y, pc.FLOOR_Y)
            seen_on += 1
    assert seen_on > 1000


def test_long_run_mostly_sleeps_and_never_raises():
    for seed in range(4):
        pet = pc.Pet(random.Random(seed))
        pet.sync(True, True, _glyphs())
        asleep = total = 0
        poses = set()
        for _ in range(int(3600 / 0.125)):
            pet.tick(0.125)
            for d in pet.draws():
                assert d["pose"] in ps.POSES
                poses.add(d["pose"])
            asleep += pet.cat.pose == "sleep"
            total += 1
        assert asleep / total > 0.6
        assert {"sleep0", "stand", "walk0"} <= poses


def test_all_behaviours_eventually_happen():
    pet = pc.Pet(random.Random(5))
    pet.sync(True, True, _glyphs())
    poses = set()
    says = set()
    for _ in range(int(4 * 3600 / 0.125)):
        pet.tick(0.125)
        for d in pet.draws():
            poses.add(d["pose"])
            says.add(d["say"])
    assert {"puff0", "knead0", "crouch", "land", "sit"} <= poses
    assert "哈!" in says


def test_interval_is_slow_while_asleep_and_8fps_while_moving():
    pet = pc.Pet(random.Random(3))
    pet.sync(True, True, _glyphs())
    assert pet.interval() == pc.MOVE_S
    for _ in range(4000):
        pet.tick(0.125)
        if pet.cat.pose == "sleep" and not pet.other.visible:
            assert pet.interval() == pc.SLEEP_S
            return
    raise AssertionError("一直没睡")


def test_busy_makes_cat_run_away_then_it_comes_back():
    pet = pc.Pet(random.Random(4))
    glyphs = _glyphs()
    pet.sync(True, True, glyphs)
    _settle(pet, 30)
    pet.sync(True, False, glyphs)
    _settle(pet, 30)
    assert pet.draws() == [] and pet.interval() is None
    pet.sync(True, True, glyphs)
    _settle(pet, 30)
    assert pet.draws()


def test_unlock_hides_cat_at_once():
    pet = pc.Pet(random.Random(4))
    glyphs = _glyphs()
    pet.sync(True, True, glyphs)
    _settle(pet, 5)
    pet.sync(False, False, [])
    assert pet.draws() == [] and pet.interval() is None


def test_layout_change_lands_cat_on_new_text():
    pet = pc.Pet(random.Random(6))
    pet.sync(True, True, _glyphs(top=840))
    _settle(pet, 5)
    pet.sync(True, True, _glyphs(top=960, n=2, x0=300))
    _settle(pet, 8)
    assert pet.draws()[-1]["y"] in (960, pc.DIVIDER_Y, pc.FLOOR_Y) or pet.cat.air
    assert all(g["top"] != 840 for g in pet.lay["perches"])


def test_positions_snap_to_art_pixels():
    pet = pc.Pet(random.Random(7))
    pet.sync(True, True, _glyphs())
    for _ in range(3000):
        pet.tick(0.125)
        for d in pet.draws():
            assert d["x"] % pc.S == 0


def test_cat_walks_the_second_line_under_the_first():
    upper = _glyphs(top=800, n=2, x0=304)
    lower = [dict(g, li=1) for g in _glyphs(top=850, n=4)]
    pet = pc.Pet(random.Random(9))
    pet.sync(True, True, upper + lower)
    tops = set()
    for _ in range(int(3 * 3600 / 0.125)):
        pet.tick(0.125)
        if pet.cat.visible and not pet.cat.air and pet.cat.run:
            tops.add(pet.cat.y)
    assert 850 in tops


def test_lick_shows_paw_over_the_face():
    assert {"lick0", "lick1", "lick2"} <= set(ps.POSES)
    assert ps.compose("lick0") != ps.compose("sit")
    assert any("P" in "".join(r) for r in ps.compose("lick1"))


def test_lick_has_one_front_paw_on_the_ground():
    """抬起一只, 地上就只剩一只; 不然看着像两条腿站着又多长一只手."""
    def feet(name):
        row = "".join(ps.compose(name)[22])
        return len([x for x in row.replace("K", ".").split(".") if "B" in x])
    assert feet("sit") == 2
    for name in ("lick0", "lick1", "lick2"):
        assert feet(name) == 1


def test_cat_licks_its_paw_sometimes():
    pet = pc.Pet(random.Random(5))
    pet.sync(True, True, _glyphs())
    seen = set()
    for _ in range(int(2 * 3600 / 0.125)):
        pet.tick(0.125)
        seen.update(d["pose"] for d in pet.draws())
    assert {"lick0", "lick1", "lick2"} <= seen


def test_eye_override_changes_face_but_not_body():
    a, b = ps.compose("stand"), ps.compose("stand", eye="half")
    assert a != b
    assert a[20:] == b[20:]     # 身子和腿不动


def test_hiss_poses_keep_their_angry_eyes():
    assert ps.compose("puff0", eye="happy") == ps.compose("puff0")


def test_every_expression_composes_with_every_pose():
    for name in ps.POSES:
        for eye in ps.EXPRESSIONS:
            assert len(ps.compose(name, eye=eye)) == 24


def test_cat_blinks_half_then_closed_then_open():
    pet = pc.Pet(random.Random(3))
    pet.cat.visible = True
    pet.cat.set_pose("stand")
    seen = []
    for _ in range(int(60 / 0.125)):
        pet._blink(0.125)
        seen.append(pet.cat.shown_eye())
    assert 8 <= seen.count("closed") <= 40          # 大约几秒一次
    assert seen.count("half") == seen.count("closed")
    for i, e in enumerate(seen):
        if e == "closed":
            assert seen[i - 1] == "half" and seen[i + 1] is None


def test_half_lidded_cat_only_closes_when_blinking():
    pet = pc.Pet(random.Random(3))
    pet.cat.set_pose("stand")
    pet.cat.eye = "half"
    seen = []
    for _ in range(400):
        pet._blink(0.125)
        seen.append(pet.cat.shown_eye())
    assert "closed" in seen and set(seen) == {"half", "closed"}


def test_no_blink_while_asleep_or_eyes_closed():
    pet = pc.Pet(random.Random(3))
    pet.cat.set_pose("sleep")
    pet.cat.blink_left = 0
    for _ in range(200):
        pet._blink(0.125)
        assert pet.cat.shown_eye() is None
    pet.cat.set_pose("stand")
    pet.cat.eye = "closed"
    for _ in range(200):
        pet._blink(0.125)
        assert pet.cat.shown_eye() == "closed"


def test_draws_report_eye():
    pet = pc.Pet(random.Random(1))
    pet.sync(True, True, _glyphs())
    pet.tick(0.125)
    assert "eye" in pet.draws()[0]


def test_cat_shows_varied_expressions():
    pet = pc.Pet(random.Random(5))
    pet.sync(True, True, _glyphs())
    seen = set()
    for _ in range(int(2 * 3600 / 0.125)):
        pet.tick(0.125)
        seen.update(d["eye"] for d in pet.draws())
    assert {"half", "closed"} <= seen


def _with_visit(coat, seed=11, fight=None, glyphs=None):
    pet = pc.Pet(random.Random(seed), strays=("cow", "stray", "tabby"))
    pet.sync(True, True, glyphs or _glyphs())
    _settle(pet, 8)
    pet.visit(coat, fight=fight)
    return pet


def _watch(pet, secs=70):
    poses, says, bumps, smoke, other_seen = set(), set(), False, False, False
    for _ in range(int(secs / 0.125)):
        pet.tick(0.125)
        for d in pet.draws():
            poses.add(d["pose"])
            says.add(d["say"])
            bumps = bumps or d["bump"]
        other_seen = other_seen or pet.other.visible
        smoke = smoke or pet.effects() is not None
    return poses, says, bumps, smoke, other_seen


def test_stray_visits_at_random_and_leaves():
    pet = pc.Pet(random.Random(2), strays=("cow",))
    pet.sync(True, True, _glyphs())
    seen = 0
    was = False
    for _ in range(int(3600 / 0.125)):
        pet.tick(0.125)
        if pet.other.visible and not was:
            seen += 1
        was = pet.other.visible
    assert 3 <= seen <= 20


def test_timid_stray_runs_after_one_hiss_then_cat_licks():
    pet = _with_visit("cow")
    poses, says, _, smoke, seen = _watch(pet)
    assert seen and "puff0" in poses and "哈!" in says and not smoke
    assert "lick0" in poses
    assert not pet.other.visible


def test_bold_stray_hisses_back():
    pet = _with_visit("stray", fight=False)
    says_by = set()
    for _ in range(int(70 / 0.125)):
        pet.tick(0.125)
        if pet.other.say:
            says_by.add(pet.other.coat)
    assert "stray" in says_by
    assert not pet.other.visible


def test_fight_hides_both_in_smoke_and_leaves_a_bump():
    pet = _with_visit("stray", fight=True)
    hid = False
    for _ in range(int(70 / 0.125)):
        pet.tick(0.125)
        if pet.effects() is not None:
            hid = hid or pet.draws() == []
    assert hid
    assert pet.effects() is None and not pet.other.visible


def test_fight_can_also_not_happen_for_timid():
    pet = _with_visit("cow", fight=True)
    _, _, _, smoke, _ = _watch(pet)
    assert not smoke


def test_tabby_keeps_distance_and_leaves_alone():
    pet = _with_visit("tabby")
    poses, says, _, smoke, seen = _watch(pet, 90)
    assert seen and not smoke
    assert "哈!" not in says
    assert not pet.other.visible


def test_cat_watches_stray_while_it_is_around():
    pet = _with_visit("tabby")
    awake = 0
    for _ in range(int(25 / 0.125)):
        pet.tick(0.125)
        if pet.other.visible and not pet.other.air:
            awake += pet.cat.pose != "sleep"
    assert awake > 20


def test_all_strays_can_visit_by_default():
    assert set(pc.Pet(random.Random(1)).strays) == {"cow", "stray", "tabby"}


def test_stray_orange_looks_different_from_main():
    assert th.PET_COATS["stray"]["W"] != th.PET_COATS["ginger"]["W"]


def test_busy_clears_smoke_and_both_cats_leave():
    pet = _with_visit("stray", fight=True)
    for _ in range(int(70 / 0.125)):
        pet.tick(0.125)
        if pet.effects() is not None:
            break
    assert pet.effects() is not None
    pet.sync(True, False, _glyphs())
    _settle(pet, 15)
    assert pet.effects() is None and pet.draws() == []


def test_unlock_vanishes_stray_and_smoke():
    pet = _with_visit("stray", fight=True)
    _settle(pet, 2)
    pet.sync(False, False, [])
    assert pet.draws() == [] and pet.effects() is None and pet.interval() is None


def test_visits_keep_working_across_many_hours():
    for seed in range(3):
        pet = pc.Pet(random.Random(seed), strays=("cow", "stray", "tabby"))
        pet.sync(True, True, _glyphs())
        asleep = total = fights = 0
        was = False
        for _ in range(int(6 * 3600 / 0.125)):
            pet.tick(0.125)
            asleep += pet.cat.pose == "sleep"
            total += 1
            now = pet.effects() is not None
            fights += now and not was
            was = now
        assert asleep / total > 0.5
        assert fights >= 1


def test_tabby_lands_outside_the_territory():
    for seed in range(12):
        pet = _with_visit("tabby", seed=seed)
        far = True
        for _ in range(int(40 / 0.125)):
            pet.tick(0.125)
            if pet.other.visible and not pet.other.air:
                far = far and not pet._near(pet.cat, pet.other)
        assert far, seed


def _arc_path(x0, y0, tx, ty, **kw):
    c = pc.Cat(pc.COAT)
    c.x, c.y = x0, y0
    gen = pc._arc(c, tx, ty, **kw)
    ys, secs = [], 0.0
    next(gen)
    try:
        while True:
            ys.append(c.y)
            gen.send(0.125)
            secs += 0.125
    except StopIteration:
        pass
    return ys, secs


def test_jump_up_barely_clears_the_target_like_a_cat():
    ys, secs = _arc_path(400, 1130, 450, 836)   # 从地上跳到字上
    assert min(ys) >= 836 - pc.CLEARANCE - 8
    assert 0.4 <= secs <= 0.95                  # 猫跳得快, 不飘


def test_short_hop_stays_low():
    ys, secs = _arc_path(300, 836, 420, 836)
    assert min(ys) >= 836 - pc.CLEARANCE - 8
    assert secs <= 0.5


def test_jump_down_does_not_go_up_much():
    ys, _ = _arc_path(300, 680, 420, 1130)
    assert min(ys) >= 680 - pc.CLEARANCE - 8
