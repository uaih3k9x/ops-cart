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


def test_every_animation_frame_is_a_real_pose():
    for anim in pc.ANIM.values():
        assert all(f in ps.POSES for f in anim["f"])


def test_allowed_only_when_locked_and_nothing_going_on():
    assert pc.allowed(_idle())
    for over in ({"locked": False}, {"boot": {"rows": []}}, {"stale": True}, {"serve": {"text": "x"}},
                 {"ups_alert": {"text": "x"}}, {"agent": {"text": "x"}}, {"heads": [{"active": True}]}):
        assert not pc.allowed(_idle(**over)), over


def test_perches_skip_glyphs_blocked_by_the_line_above():
    upper = _glyphs(top=800, n=2, x0=304)
    for g in upper:
        g["bottom"] = 840
    lower = [dict(g, li=1) for g in _glyphs(top=850, n=4)]
    lay = pc.layout(upper + lower)
    perched = {(g["li"], g["left"]) for g in lay["perches"]}
    assert all((0, g["left"]) in perched for g in upper)
    assert (1, 304) not in perched and (1, 208) in perched or (1, 496) in perched


def test_perches_below_the_ceiling_only():
    assert pc.layout(_glyphs(top=pc.CEILING + pc.CAT_H - 1))["perches"] == []


def test_no_perches_means_no_cat():
    pet = pc.Pet(random.Random(1))
    pet.sync(True, True, [])
    for _ in range(40):
        pet.tick(0.125)
    assert pet.draws() == [] and pet.interval() is None


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
            assert d[-1]["y"] == 840
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
    assert {"puff0", "knead0", "crouch", "land", "run0", "sit"} <= poses
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
    assert pet.draws()[-1]["y"] == 960 or pet.cat.air


def test_positions_snap_to_art_pixels():
    pet = pc.Pet(random.Random(7))
    pet.sync(True, True, _glyphs())
    for _ in range(3000):
        pet.tick(0.125)
        for d in pet.draws():
            assert d["x"] % pc.S == 0
