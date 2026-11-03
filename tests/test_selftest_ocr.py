"""开机自检查 OCR 常驻服务 (#47): 挂了 WARN 不算 FAIL, 有 Tesseract 兜底; 没装不算问题."""

import json
import pathlib

import kiosk_core as kc

ROOT = pathlib.Path(__file__).resolve().parent.parent
ITEM = {"check": "service", "label": "OCR", "unit": "cart-ocr"}


def test_status_query_includes_ocr(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return 0, "Id=cart-ocr.service\nActiveState=active\nUnitFileState=enabled\n"
    monkeypatch.setattr(kc, "run", run)
    assert kc.services_status()["cart-ocr"] == (True, True) and "cart-ocr" in seen["cmd"]


def test_plans_list_ocr():
    assert ITEM in kc.DEFAULT_PLAN
    assert ITEM in json.loads((ROOT / "examples" / "selftest.example.json").read_text(encoding="utf-8"))


def rows(enabled, active):
    return kc.selftest_run([ITEM], {"svcs": {"cart-ocr": (enabled, active)}})


def test_running_is_ok_and_keeps_all_online():
    r = rows(True, True)
    assert r[0]["state"] == "OK" and kc.ALL_ONLINE[0] in kc.selftest_verdict(r)[0]


def test_enabled_but_dead_warns_without_failing():
    r = rows(True, False)
    assert r[0]["state"] == "WARN" and kc.selftest_verdict(r) == ("SYSTEM READY", "OK")


def test_not_installed_is_not_a_problem():
    r = kc.selftest_run([ITEM], {"svcs": {}})
    assert r[0]["state"] == "--" and kc.ALL_ONLINE[0] in kc.selftest_verdict(r)[0]
