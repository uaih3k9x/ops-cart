"""车机开关按钮跟随状态 (#83): 不起 Kiosk 窗口, 用假 self 调方法."""

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

import kiosk  # noqa: E402
import kiosk_ui_core as kui  # noqa: E402

K = kiosk.Kiosk


class _Lab:
    def __init__(self):
        self.cfg = {}

    def config(self, **kw):
        self.cfg.update(kw)


def _fake(rows=None, pane=None):
    f = SimpleNamespace(_check_rows=rows, _check_ts=123.0, _pxe_toggled_at=0.0, _checking=True,
                        banner=_Lab(), pane=kui.LeftPane(kui.PANES), mpv=None, closed=[], net=[])
    for attr, _ in kui.TOGGLES:
        setattr(f, attr, _Lab())
    if pane:
        f.pane.toggle(pane)
    f._sync_toggles = lambda: K._sync_toggles(f)
    f._render_net = lambda *a: f.net.append(a)
    f._close_pane = lambda: (f.closed.append(True), f.pane.close())
    return f


ROWS_ON = [{"label": "PXE SERVER", "state": "OK", "detail": "RUNNING"}]


@pytest.mark.parametrize("active, text", [(True, "PXE 装机服务:  关"), (False, "PXE 装机服务:  开")])
def test_pxe_toggle_done_success_updates_rows_and_button(active, text):
    f = _fake(rows=[{"label": "PXE SERVER", "state": "OK" if active else "--"}, {"label": "OCR", "state": "OK"}])
    K._pxe_toggle_done(f, (0, ""), active)
    assert f.btn_pxe.cfg["text"] == text
    assert f._check_ts == 0.0 and f._pxe_toggled_at > 0
    f.pane.toggle("serial")             # 之后开面板也不能用旧 rows 把按钮写回去
    K._sync_toggles(f)
    assert f.btn_pxe.cfg["text"] == text
    assert {"label": "OCR", "state": "OK"} in f._check_rows


@pytest.mark.parametrize("res", [(1, "failed"), RuntimeError("boom")])
def test_pxe_toggle_done_failure_keeps_button(res):
    f = _fake(rows=ROWS_ON)
    K._sync_toggles(f)
    K._pxe_toggle_done(f, res, True)
    assert f.btn_pxe.cfg["text"] == "PXE 装机服务:  开" and f._check_rows == ROWS_ON
    assert "失败" in f.banner.cfg["text"] and f._check_ts == 0.0


def test_checks_done_drops_result_started_before_toggle():
    f = _fake(rows=ROWS_ON)
    K._pxe_toggle_done(f, (0, ""), True)            # 停了
    old = ([], ROWS_ON)                              # 停之前开跑的自检: 还说在跑
    K._checks_done(f, f._pxe_toggled_at - 1, old)
    assert f.btn_pxe.cfg["text"] == "PXE 装机服务:  关" and not f._checking
    K._checks_done(f, f._pxe_toggled_at + 1, ([], [{"label": "PXE SERVER", "state": "--"}]))
    assert f._check_rows == [{"label": "PXE SERVER", "state": "--"}]


@pytest.mark.parametrize("pane, rc, closed", [("preview", 3, True), ("preview", None, False),
                                              ("serial", 3, False)])
def test_reap_preview_closes_only_when_mpv_died(pane, rc, closed):
    f = _fake(pane=pane)
    f.mpv = SimpleNamespace(poll=lambda: rc)
    K._reap_preview(f)
    assert bool(f.closed) is closed
    assert (f.banner.cfg.get("text") == "UVC 预览已退出 (rc=3)") is closed


def test_reap_preview_without_mpv_does_nothing():
    f = _fake(pane="preview")
    K._reap_preview(f)
    assert not f.closed
