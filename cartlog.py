"""推车日志分层: 机器事件进 /var/log/ops-cart/ (logrotate 轮转), CART-LOG.md 只收一行摘要.

CART-LOG.md 是人读的维护手册, 机器别往里倒细节; 细节写 events.log 或报告文件, 摘要里给路径.
写失败一律吞掉 (返回 False), 日志不该把 Kiosk 弄挂.
"""

import os
import time

LOG_DIR = "/var/log/ops-cart"
EVENTS = f"{LOG_DIR}/events.log"
REPORTS = f"{LOG_DIR}/reports"
CART_LOG = "/opt/pxe/CART-LOG.md"


def _append(path, line):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except OSError:
        return False


def _one_line(msg):
    return " ".join(str(msg).split())


def event(kind, msg, path=EVENTS, now=None):
    """'2026-10-22 14:03:00 [kind] msg'"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    return _append(path, f"{ts} [{kind}] {_one_line(msg)}")


def summary(msg, who="kiosk", path=CART_LOG, now=None):
    """往 CART-LOG.md 末尾的日志表追加一行: | 日期 | 谁 | 时:分 摘要 |"""
    t = time.localtime(now if now is not None else time.time())
    text = _one_line(msg).replace("|", "\\|")
    return _append(path, f"| {time.strftime('%Y-%m-%d', t)} | {who} | {time.strftime('%H:%M', t)} {text} |")
