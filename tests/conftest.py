"""macOS 上 tk.Tk() 总能建出窗口, 会把焦点抢到 Python 上. 默认让它抛 TclError,
各测试照旧按"没有显示"跳过; 想在 Mac 上真跑 Tk 测试就设 OPS_TK_TESTS=1."""
import os
import sys

if sys.platform == "darwin" and os.environ.get("OPS_TK_TESTS") != "1":
    try:
        import tkinter
    except ImportError:
        tkinter = None
    if tkinter is not None:
        class _NoTk(tkinter.Tk):            # 是类: 有代码继承 tk.Tk
            def __init__(self, *a, **k):
                raise tkinter.TclError("macOS: 不开 Tk 窗口 (OPS_TK_TESTS=1 才开)")
        tkinter.Tk = _NoTk


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _bigfish_estop_tmp(tmp_path_factory, monkeypatch):
    """大肥鱼急停信号 (#53) 默认写 /run/bigfish-ctl; 测试一律改到临时目录, 走急停的用例不碰真路径."""
    import bigfish_core
    d = tmp_path_factory.mktemp("bigfish-ctl")
    monkeypatch.setattr(bigfish_core, "ESTOP_DIR", str(d))
    monkeypatch.setattr(bigfish_core, "ESTOP_PATH", str(d / "estop.json"))
