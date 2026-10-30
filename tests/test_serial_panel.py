"""serial_panel 测试: 界面线程纪律 + 旧名字仍能从 serial_panel 导入. 纯逻辑见 test_serial_core.py."""

import serial_core as sc
import serial_panel as sp


def test_old_names_still_importable():
    # 别处老写法 sp.serial_conf_read() / from serial_panel import ... 不能坏
    for name in ("serial_conf_read", "data_dir", "broker_port", "parse_list", "byid_paths", "prefer_byid",
                 "alias_paths", "console_ports", "serve_target", "match_service", "kvm_devices",
                 "port_label", "paste_chunks", "serve_error", "key_bytes", "TermFeed", "parse_chat_line",
                 "select_port", "SERIAL_CONF", "BAUDS", "BYID_DIR", "ALIAS_GLOB", "HIDDEN_PORT",
                 "_run", "_conbroker_cmd"):
        assert getattr(sp, name) is getattr(sc, name), name


class _NoTk:
    """不起 Tk: 只验证后台线程不碰 after, 结果经队列在 _drain 里执行."""

    def __init__(self):
        import queue
        self.ui = queue.Queue()
        self.after_calls = []

    def after(self, *a):
        self.after_calls.append(a)


def test_async_result_runs_only_in_drain():
    import threading
    panel = _NoTk()
    done = []
    ev = threading.Event()
    sp.SerialPanel._async(panel, lambda: 42, lambda r: (done.append(r), ev.set()))
    for t in list(threading.enumerate()):
        if t.daemon and t is not threading.current_thread():
            t.join(timeout=1)
    assert done == [] and panel.after_calls == []
    sp.SerialPanel._run_ui(panel)
    assert done == [42]


# ------------------------------------------------------------ #15 巡检按钮

class _Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class _SurveyPanel(_NoTk):
    def __init__(self, dev, serves):
        super().__init__()
        self.port_var, self._serves, self._surveying = _Var(dev), serves, False
        self.cfg = {}
        self.written = []

    def _con_write(self, ops, tag=None):
        self.written.append("".join(v for op, v in ops if op == "text"))

    def _async(self, fn, done=None):
        return sp.SerialPanel._async(self, fn, done)


def _join_bg():
    import threading
    for t in list(threading.enumerate()):
        if t.daemon and t is not threading.current_thread():
            t.join(timeout=2)


def test_survey_without_service_only_prompts(monkeypatch):
    import survey_core

    def boom(*a, **k):
        raise AssertionError("不该跑")
    monkeypatch.setattr(survey_core, "run_survey", boom)
    p = _SurveyPanel("/dev/con-1", {})
    sp.SerialPanel.survey(p)
    _join_bg()
    assert "没有在跑的" in p.written[-1] and not p._surveying and p.ui.empty()


def test_survey_runs_in_background_and_reports_in_drain(monkeypatch):
    import survey_core
    seen, logged = {}, []

    def fake_run(port, vendor, **kw):
        seen.update(port=port, vendor=vendor, **kw)
        return {"ok": True, "summary": "SW-A 巡检完成, 已跑 8/8 条\n存档: /x/con-1_SW-A_20261030-1403.txt",
                "path": "/x/con-1_SW-A_20261030-1403.txt", "sections": [], "diff": None}
    monkeypatch.setattr(survey_core, "run_survey", fake_run)
    monkeypatch.setattr(survey_core, "record", lambda r: logged.append(r))
    p = _SurveyPanel("/dev/con-1", {"/dev/con-1": "9600"})
    sp.SerialPanel.survey(p)
    assert p._surveying
    sp.SerialPanel.survey(p)                     # 跑着时再点不起第二个
    _join_bg()
    n = len(p.written)
    assert p.after_calls == [] and seen["port"] == "/dev/con-1" and seen["vendor"] == "huawei"
    assert seen["head"] == "con-1" and seen["send"] is survey_core.conbroker_send
    assert len(logged) == 1
    sp.SerialPanel._run_ui(p)
    assert not p._surveying and len(p.written) == n + 1
    assert "巡检完成" in p.written[-1] and "/x/con-1_SW-A_20261030-1403.txt" in p.written[-1]


def test_survey_exception_still_reports_and_unlocks(monkeypatch):
    import survey_core

    def boom(*a, **k):
        raise RuntimeError("坏了")
    monkeypatch.setattr(survey_core, "run_survey", boom)
    monkeypatch.setattr(survey_core, "record", lambda r: None)
    p = _SurveyPanel("/dev/con-1", {"/dev/con-1": "9600"})
    sp.SerialPanel.survey(p)
    _join_bg()
    sp.SerialPanel._run_ui(p)
    assert not p._surveying and "坏了" in p.written[-1]


def test_survey_vendor_from_serial_conf(monkeypatch):
    import survey_core
    seen = {}
    monkeypatch.setattr(survey_core, "run_survey",
                        lambda port, vendor, **k: seen.update(vendor=vendor) or {"ok": True, "summary": "x"})
    monkeypatch.setattr(survey_core, "record", lambda r: None)
    p = _SurveyPanel("/dev/con-1", {"/dev/con-1": "9600"})
    p.cfg = {"survey_vendor": "cisco"}
    sp.SerialPanel.survey(p)
    _join_bg()
    assert seen["vendor"] == "cisco"
