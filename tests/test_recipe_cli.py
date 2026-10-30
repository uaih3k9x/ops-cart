"""recipe.py 命令行: 只测接线 (配方加载、退出码、dry-run 不碰键鼠), 截图/OCR 换成假的, 不碰硬件."""

import json

import pytest

import recipe


@pytest.fixture
def fake_hw(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(recipe, "RUN_DIR", str(tmp_path))
    monkeypatch.setattr(recipe.cartlog, "event", lambda kind, msg, **kw: events.append((kind, msg)))
    monkeypatch.setattr(recipe, "make_snap", lambda out_dir, dev=None: lambda: str(tmp_path / "s.png"))
    monkeypatch.setattr(recipe.cart_ocr, "ocr_image",
                        lambda p, lang="eng": [{"text": "Please select boot device:", "x": 0, "y": 0, "h": 16}])

    def no_keyboard(*a, **kw):
        raise AssertionError("dry-run 不该打开键鼠")
    monkeypatch.setattr(recipe.ch9329, "Ch9329", no_keyboard)
    return tmp_path, events


def write(tmp_path, obj):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_unknown_recipe_exit_2(fake_hw):
    assert recipe.main(["no-such-recipe"]) == 2


def test_recipe_with_save_exit_2(fake_hw):
    tmp_path, _ = fake_hw
    path = write(tmp_path, {"name": "x", "timeout": 30, "steps": [{"do": "press", "key": "f10"}]})
    assert recipe.main([path]) == 2


def test_bad_key_override_exit_2(fake_hw):
    assert recipe.main(["boot-pxe", "--key", "f10"]) == 2


def test_dry_run_logs_but_never_opens_keyboard(fake_hw):
    tmp_path, events = fake_hw
    path = write(tmp_path, {"name": "x", "timeout": 30, "steps": [
        {"do": "wait_text", "patterns": ["select boot device"], "timeout": 5},
        {"do": "press", "key": "enter"}]})
    assert recipe.main([path, "--dry-run"]) == 0
    logs = list(tmp_path.glob("recipe-*/log.jsonl"))
    assert len(logs) == 1
    evs = [json.loads(l) for l in logs[0].read_text(encoding="utf-8").splitlines()]
    assert any(e["ev"] == "hit" and e["shot"].endswith("s.png") for e in evs)
    assert any(e["ev"] == "key" and e["key"] == "enter" and e.get("dry_run") for e in evs)
    assert events and events[0][0] == "recipe" and "ok=true" in events[0][1]


def test_failed_step_exit_1(fake_hw):
    tmp_path, _ = fake_hw
    path = write(tmp_path, {"name": "x", "timeout": 2, "steps": [
        {"do": "wait_text", "patterns": ["Network Boot"], "timeout": 1, "poll": 0.1}]})
    assert recipe.main([path, "--dry-run"]) == 1
