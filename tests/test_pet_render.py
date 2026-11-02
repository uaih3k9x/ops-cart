"""副屏桌宠 (#35): 画到整帧上. 要 Pillow."""
import io
import random

import pytest

pytest.importorskip("PIL")

from PIL import Image

import dash_render as r
import pet_core as pc
import pet_render as pr


def _view(notice="zzzzz\n正在做梦", **over):
    v = {"locked": True, "clock": "12:00", "date": "11月2日 周一", "net": "on", "notice": notice,
         "heads": [], "lower": [], "stale": False}
    v.update(over)
    return v


def test_glyph_boxes_match_the_ink_drawn():
    view = _view("正在做梦")
    img = r.render(view).convert("L")
    bg = img.getpixel((5, 800))
    boxes = r.glyph_boxes(view)
    assert [b["ch"] for b in boxes] == list("正在做梦")
    for b in boxes:
        col = img.crop((int(b["left"]), b["top"] - 3, int(b["right"]), b["top"] + 3))
        rows = [any(col.getpixel((x, y)) != bg for x in range(col.width)) for y in range(col.height)]
        assert rows[3] and not any(rows[:2]), b


def test_stage_draws_cat_only_inside_pet_area():
    view = _view()
    pet = pc.Pet(random.Random(1))
    pet.sync(True, True, r.glyph_boxes(view))
    for _ in range(100):
        pet.tick(0.125)
    stage = pr.Stage()
    frame = Image.open(io.BytesIO(stage.frame(view, pet))).transpose(Image.ROTATE_90)
    plain = r.render(view).convert("RGB")
    assert frame.size == plain.size
    top = (0, 0, r.PANEL_W, pr.AREA_TOP - 4)
    diff = sum(abs(a - b) for pa, pb in zip(frame.crop(top).getdata(), plain.crop(top).getdata())
               for a, b in zip(pa, pb)) / (r.PANEL_W * (pr.AREA_TOP - 4))
    assert diff < 6       # JPEG 有噪声, 但上半屏不该多出一只猫


def test_stage_without_cat_is_just_make_frame():
    view = _view()
    assert pr.Stage().frame(view, None) == r.make_frame(view)


def test_stage_frame_stays_within_budget():
    view = _view()
    pet = pc.Pet(random.Random(1))
    pet.sync(True, True, r.glyph_boxes(view))
    stage = pr.Stage()
    for _ in range(200):
        pet.tick(0.125)
        assert len(stage.frame(view, pet)) <= r.JPEG_BUDGET


def test_stage_reuses_base_until_view_changes(monkeypatch):
    calls = []
    real = r.render
    monkeypatch.setattr(r, "render", lambda v: (calls.append(1), real(v))[1])
    stage = pr.Stage()
    view = _view()
    stage.frame(view, None)
    stage.frame(view, None)
    assert len(calls) == 1
    stage.frame(dict(view, clock="12:01"), None)
    assert len(calls) == 2


def test_every_pose_and_coat_has_an_image_both_ways():
    import pet_sprites as ps
    for name in ps.POSES:
        for coat in ps.COATS:
            for d in (1, -1):
                assert pr.sprite(name, coat, d).size == (24 * pc.S, 24 * pc.S)


def test_divider_cat_stays_inside_area_and_covers_little_of_the_lamps():
    assert pr.AREA_TOP < pc.DIVIDER_Y - 23 * pc.S
    assert pr.AREA_BOTTOM > pc.FLOOR_Y + pc.S
