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
