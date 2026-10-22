"""atomicfile 测试: 断电时要么旧要么新, 不出现半个文件."""

import json
import os
import stat

import pytest

import atomicfile as af


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_write_text_creates_with_mode_and_no_tmp_left(tmp_path):
    p = tmp_path / "sub" / "a.conf"
    af.write_text(str(p), "你好\n", mode=0o600)
    assert p.read_text() == "你好\n" and _mode(p) == 0o600
    assert os.listdir(p.parent) == ["a.conf"]


def test_write_keeps_existing_mode_by_default(tmp_path):
    p = tmp_path / "unit.service"
    p.write_text("old")
    p.chmod(0o640)
    af.write_text(str(p), "new")
    assert p.read_text() == "new" and _mode(p) == 0o640


def test_new_file_default_mode_644(tmp_path):
    p = tmp_path / "x"
    af.write_text(str(p), "1")
    assert _mode(p) == 0o644


def test_write_json(tmp_path):
    p = tmp_path / "c.json"
    af.write_json(str(p), {"pin": "密"}, mode=0o600, indent=2)
    assert json.loads(p.read_text()) == {"pin": "密"} and _mode(p) == 0o600
    assert "\n" in p.read_text()                  # indent 透传给 json.dump


def test_failed_write_leaves_old_file_and_no_tmp(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"ok": 1}')
    with pytest.raises(TypeError):
        af.write_json(str(p), {"bad": object()})
    assert p.read_text() == '{"ok": 1}'
    assert os.listdir(tmp_path) == ["c.json"]
