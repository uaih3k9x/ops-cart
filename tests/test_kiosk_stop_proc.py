"""关预览: mpv 卡住不理 SIGTERM 时要补 SIGKILL 并回收, 不卡调用方 (#39)."""

import signal
import subprocess
import sys
import time

import kiosk_core as kc

STUBBORN = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);time.sleep(60)"


def test_stop_proc_kills_process_ignoring_sigterm_without_blocking():
    p = subprocess.Popen([sys.executable, "-c", STUBBORN], stdout=subprocess.PIPE)
    p.stdout.readline()
    t0 = time.monotonic()
    th = kc.stop_proc(p, wait_s=0.2)
    assert time.monotonic() - t0 < 0.15                  # 不等, 主线程要接着画界面
    th.join(5)
    assert p.returncode == -signal.SIGKILL


def test_stop_proc_polite_exit_is_reaped_without_kill():
    p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    kc.stop_proc(p, wait_s=5).join(6)
    assert p.returncode == -signal.SIGTERM


def test_stop_proc_already_gone_does_not_raise():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    kc.stop_proc(p, wait_s=0.1).join(2)
