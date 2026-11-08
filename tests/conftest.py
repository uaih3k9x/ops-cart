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
