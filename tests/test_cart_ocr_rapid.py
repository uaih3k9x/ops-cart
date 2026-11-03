"""screen_ocr 换 RapidOCR (#46): 子进程跑独立 venv, 没装 / 出错退回 Tesseract. 不依赖真的 rapidocr."""

import json
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

import cart_ocr as co

ROOT = pathlib.Path(__file__).resolve().parent.parent


def row(text="shutdown /r /t 5", conf=92.0):
    return {"text": text, "x": 1, "y": 2, "w": 3, "h": 4, "conf": conf}


def ok_run(rows, log="[INFO] noise\n"):
    return lambda *a, **k: SimpleNamespace(returncode=0, stdout=log + json.dumps(rows) + "\n", stderr="")


def test_parse_keeps_last_json_line():
    assert co.parse_rapid("[INFO] x\n" + json.dumps([row()]) + "\n") == [row()]


@pytest.mark.parametrize("bad", ["", "not json", "{}", '[{"text": 1}]', '[{"text": "a"}]', "[1]"])
def test_parse_rejects_bad_output(bad):
    with pytest.raises(ValueError):
        co.parse_rapid(bad)


def test_noise_filter_drops_low_conf_and_lone_symbols():
    rows = [row("MATLAB", 99.0), row("口", 56.0), row("+", 73.0), row("器", 100.0), row("X", 80.0), row("?", 95.0)]
    assert [r["text"] for r in co.drop_noise(rows)] == ["MATLAB", "器", "X", "?"]


def test_ocr_rapid_runs_venv_isolated_with_timeout():
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return SimpleNamespace(returncode=0, stdout=json.dumps([row()]), stderr="")
    assert co.ocr_rapid("/tmp/a.png", run=run, py="/v/bin/python")[0]["text"] == row()["text"]
    assert seen["cmd"][:2] == ["/v/bin/python", "-I"] and seen["cmd"][-1] == "/tmp/a.png"
    assert seen["cmd"][2].endswith("ocr_rapid_worker.py") and seen["kw"]["timeout"] == co.RAPID_TIMEOUT


def test_best_prefers_rapid():
    assert co.ocr_best("x", "eng", rapid=lambda p: [row()], tess=lambda p, lang: 1 / 0) == ([row()], "rapidocr")


@pytest.mark.parametrize("exc", [FileNotFoundError("no venv"), ValueError("bad json"),
                                 subprocess.TimeoutExpired("py", 30), subprocess.CalledProcessError(1, "py"),
                                 OSError("boom")])
def test_best_falls_back_and_says_why(exc):
    def rapid(p):
        raise exc
    lines, engine = co.ocr_best("x", "eng", rapid=rapid, tess=lambda p, lang: [row("tess")])
    assert lines[0]["text"] == "tess" and engine.startswith("tesseract") and "RapidOCR" in engine


def test_best_empty_screen_is_a_result_not_a_failure():
    assert co.ocr_best("x", "eng", rapid=lambda p: [], tess=lambda p, lang: 1 / 0) == ([], "rapidocr")


def test_recipes_stay_on_tesseract():
    """配方靠 Tesseract 的坐标定位, 这次不动."""
    src = (ROOT / "recipe.py").read_text(encoding="utf-8")
    assert "cart_ocr.ocr_image" in src and "ocr_best" not in src


def test_worker_outputs_contract_shape():
    src = (ROOT / "ocr_rapid_worker.py").read_text(encoding="utf-8")
    assert "intra_op_num_threads" in src and "import cart_" not in src    # 跑在 -I 的 venv 里, 不能 import 仓库模块


def test_worker_box_to_row():
    import importlib.util
    spec = importlib.util.spec_from_file_location("w", ROOT / "ocr_rapid_worker.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    r = w.to_row([[10.2, 20.0], [30.0, 20.0], [30.0, 40.9], [10.2, 40.9]], "hi", 0.915)
    assert r == {"text": "hi", "x": 10, "y": 20, "w": 20, "h": 20, "conf": 91.5}


def test_requirements_are_pinned():
    lines = [ln for ln in (ROOT / "deploy" / "ocr-requirements.txt").read_text().splitlines() if ln and ln[0] != "#"]
    assert lines and all("==" in ln for ln in lines) and any(ln.startswith("rapidocr-onnxruntime==") for ln in lines)
