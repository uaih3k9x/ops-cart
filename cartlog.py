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


def fields(**kv):
    """结构化事件的正文: 'k=v k=v'; 带空白的值加引号, None 不写, 布尔写成 true/false.
    调用方自己决定写什么 —— PIN、令牌、打字内容这类不该进日志的, 别传进来."""
    out = []
    for k, v in kv.items():
        if v is None:
            continue
        v = ("true" if v else "false") if isinstance(v, bool) else _one_line(v).replace('"', "'")
        out.append(f'{k}="{v}"' if (" " in v or not v) else f"{k}={v}")
    return " ".join(out)


def event(kind, msg, path=EVENTS, now=None):
    """'2026-10-22 14:03:00 [kind] msg'"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    return _append(path, f"{ts} [{kind}] {_one_line(msg)}")


def summary(msg, who="kiosk", path=CART_LOG, now=None):
    """往 CART-LOG.md 末尾的日志表追加一行: | 日期 | 谁 | 时:分 摘要 |"""
    t = time.localtime(now if now is not None else time.time())
    text = _one_line(msg).replace("|", "\\|")
    return _append(path, f"| {time.strftime('%Y-%m-%d', t)} | {who} | {time.strftime('%H:%M', t)} {text} |")


# ---------------------------------------------------------------- CART-LOG 滚动
# 手册部分 (待办、铁律…) 不动, 只把 "## 日志" 表里的老条目按月挪进 /opt/pxe/cart-log/.
# cart_dream 按字节位置记"读到哪了": 只挪它已经读过的行, 再把位置换算到新文件, 没读过的原样留着.
# 和 summary() 的追加不加锁: 滚动是人手动跑的, 别挑 kiosk 正在写的时候.

import json
import re

ARCHIVE_DIR = "/opt/pxe/cart-log"
DREAM_STATE = "/var/lib/ops-cart/dream.json"
ARCHIVE_NOTE = "> 更早的条目按月归档在 `/opt/pxe/cart-log/`。"
_ROW = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \|")
_TABLE_HEAD = "| 日期 | 谁 | 做了什么 | 回滚 |\n| --- | --- | --- | --- |\n"


def roll(data, keep_since, offset):
    """CART-LOG 字节 -> (新字节, 挪走的行, 新的读取位置). keep_since 之前且已被读过的行才挪."""
    lines = data.splitlines(keepends=True)
    pos, starts = 0, []
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    try:
        sec = next(i for i, ln in enumerate(lines) if ln.rstrip() == "## 日志".encode())
        sep = next(i for i in range(sec, len(lines)) if lines[i].startswith(b"| ---"))
    except StopIteration:
        raise ValueError("CART-LOG 里没找到 '## 日志' 表") from None
    keep, moved = [], []
    for i in range(sep + 1, len(lines)):
        m = _ROW.match(lines[i].decode("utf-8", "replace"))
        if m and m.group(1) < keep_since and starts[i] + len(lines[i]) <= offset:
            moved.append(lines[i].decode("utf-8").rstrip("\n"))
        else:
            keep.append(lines[i])
    if not moved:
        return data, [], offset
    head = lines[:sep + 1]
    if not any(ln.decode("utf-8", "replace").strip() == ARCHIVE_NOTE for ln in head):
        head = head[:sec + 1] + [b"\n", ARCHIVE_NOTE.encode() + b"\n"] + head[sec + 1:]
    new = b"".join(head + keep)
    tail = data[offset:]
    assert new.endswith(tail), "没读过的部分被改动了"
    return new, moved, len(new) - len(tail)


def archive_text(old, rows):
    """往一个月的归档里追加行; 新文件先写标题和表头."""
    if not old:
        month = rows[0][2:9] if rows else ""
        old = f"# CART-LOG 归档 {month}\n\n从 /opt/pxe/CART-LOG.md 的日志表滚动出来, 只追加不改.\n\n{_TABLE_HEAD}"
    return old + "".join(r + "\n" for r in rows)


def roll_files(keep_since, log=CART_LOG, state=DREAM_STATE, archive_dir=ARCHIVE_DIR):
    """真滚: 先写归档, 再换 CART-LOG, 最后改做梦的读取位置. 做梦还没读过的一行都不挪."""
    import atomicfile
    with open(log, "rb") as f:
        data = f.read()
    try:
        with open(state) as f:
            st = json.load(f)
        offset = int(st["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return "做梦的读取位置读不到, 不滚 (没整理过的条目不能挪走)"
    new, moved, off = roll(data, keep_since, offset)
    if not moved:
        return f"{keep_since} 之前没有已整理过的条目, 不用滚"
    by_month = {}
    for r in moved:
        by_month.setdefault(_ROW.match(r).group(1)[:7], []).append(r)
    names = []
    for month, rows in sorted(by_month.items()):
        path = os.path.join(archive_dir, f"CART-LOG-{month}.md")
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
        atomicfile.write_text(path, archive_text(old, rows))
        names.append(path)
    atomicfile.write_text(log, new.decode("utf-8"))
    st["offset"] = off
    atomicfile.write_json(state, st)
    return f"归档 {len(moved)} 条 → {', '.join(names)}; 做梦读取位置 {offset} → {off}"


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="CART-LOG 滚动: 老条目按月归档")
    ap.add_argument("cmd", choices=["roll"])
    ap.add_argument("--keep-days", type=int, default=7, help="保留最近几天 (默认 7)")
    a = ap.parse_args()
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - a.keep_days * 86400))
    print(roll_files(since))
    sys.exit(0)
