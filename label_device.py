"""车机打标签的打印机动作 (#104): 写有超时, 查状态不排队, 打之前看状态. 都在后台线程调, 不碰 Tk.

状态字节解析、设备打开、进程间打印机锁沿用 mcp_label (和 MCP 打印互斥); 差别是这里用非阻塞 fd,
打印机卡住时写 / 查状态按期限报错, 不把后台线程永远挂住.
"""

import os
import select
import time

import cartlog
import label_core as lc
import label_render as lr
import mcp_core as mc
import mcp_label as ml

WRITE_TIMEOUT_S = 20.0
STATUS_TIMEOUT_S = ml.STATUS_TIMEOUT
_CHUNK = 4096


def _pump(fd, data, deadline, dev):
    """非阻塞写完 data, 并等最后一块传完才返回; 到期抛超时 (打印机可能已经收了半个任务, 话里说清楚).

    usblp 的非阻塞 write 提交 URB 就返回, close 会取消还在途的 URB, 任务尾巴就丢了;
    所以发完要再等一次可写 (usblp 的 wcomplete). 可写只说明传输结束, 不代表打印机出了纸."""
    view, total = memoryview(data), len(data)
    while view:
        left = deadline - time.monotonic()
        if left <= 0:
            raise mc.ToolError(f"写打印机超时: 只发出 {total - len(view)}/{total} 字节 ({dev}); 看看打印机, 必要时重新上电")
        if not select.select([], [fd], [], left)[1]:
            continue
        try:
            view = view[os.write(fd, view[:_CHUNK]):]
        except BlockingIOError:
            time.sleep(0.01)
        except OSError as e:
            raise mc.ToolError(f"写 {dev} 失败: {e}") from None
    if not select.select([], [fd], [], max(deadline - time.monotonic(), 0))[1]:
        raise mc.ToolError(f"写打印机超时: {total} 字节已提交, 最后一块没传完 ({dev}); 看看打印机, 必要时重新上电")


def write_job(data, dev=ml.PRINTER, timeout=WRITE_TIMEOUT_S):
    fd = ml._open(dev, os.O_WRONLY | os.O_NONBLOCK)
    try:
        _pump(fd, data, time.monotonic() + timeout, dev)
    finally:
        os.close(fd)


def read_status(dev=ml.PRINTER, timeout=STATUS_TIMEOUT_S):
    """发 ESC !? 读一个状态字节 (同 mcp_label.real_status, 换成带期限的写)."""
    fd = ml._open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        _pump(fd, b"\x1b!?", time.monotonic() + timeout, dev)
        got = b""
        if select.select([fd], [], [], timeout)[0]:
            try:
                got = os.read(fd, 1)
            except BlockingIOError:
                pass
            except OSError as e:
                raise mc.ToolError(f"读 {dev} 状态失败: {e}") from None
    finally:
        os.close(fd)
    if not got:
        raise mc.ToolError(f"打印机没回状态字节 ({dev}, {timeout:g} 秒); 没开机或不支持双向?")
    return got[0]


def query_status(read=read_status, lock=ml.printer_lock):
    """面板定时看状态: 不排队抢锁 (#60: 循环查状态不能饿死打印)."""
    with lock(wait_s=0):
        return ml.decode_status(read())


def send(job, write=write_job, read=read_status, lock=ml.printer_lock):
    """拿锁、看状态、写. 打印机没准备好就不写, 话里说缺什么."""
    with lock():
        st = ml.decode_status(read())
        if not st["ok"]:
            raise lc.LabelError(f"没打: {lc.status_view(st)[0]}")
        write(job)


def _note(msg):
    cartlog.event("label", msg)


def print_label(lines, fold_mm, copies=1, render=lr.render_plan, send_fn=send, note=_note):
    """渲染 -> 查版面 -> 发打印机. 返回 Plan. 出旗子 / 出纸不打."""
    plan = render(lines, fold_mm)
    bad = lc.blocking(plan)
    if bad:
        raise lc.LabelError("不打: " + "; ".join(bad))
    send_fn(lc.tspl_job(plan, copies))
    try:
        note(f"车机打标签 {copies} 张, 字号 {plan.px} 点, 折痕 {plan.fold_mm:g} mm")
    except Exception:                   # noqa: BLE001  记不下也不能算打印失败
        pass
    return plan


def feed(send_fn=send):
    send_fn(lc.feed_job())


def ruler(send_fn=send):
    send_fn(lc.ruler_job())
