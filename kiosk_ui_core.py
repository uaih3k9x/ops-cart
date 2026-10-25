"""车机 Tk 界面的纯逻辑: 不 import tkinter, 用假 root 就能测.

- UiBridge: 后台线程只往队列放回调, 主线程 after(50) 轮询执行 (Tk 不是线程安全的)
- pxe_tick_decide: 装机模式定时器每轮该做什么
- boot_rows_safe / Once: 开机自检兜底与幂等
- LeftPane / PANES / MENU: 左侧面板状态机和右侧菜单
- rel_split / rel_motion / abs_should_send / ChWorker: 键鼠透传
- kp_digits_as_digits: 小键盘数字不看 NumLock
- ks_save / read_text: Kickstart 保存与恢复
"""

import queue
import sys
import threading

import kiosk_core as kc


# ---------------------------------------------------------------- UiBridge

class UiBridge:
    """后台 -> 主线程的唯一通道. post() 任何线程都能调; drain() 只在主线程 (after 回调) 里跑.

    run_bg(work, done, key): work 在线程里跑, 结果 (或异常对象) 交给 done 在主线程执行;
    给了 key 时同一 key 同时只跑一个, 返回 False 表示上一次还没回来 (界面据此提示"处理中")."""

    def __init__(self, root, interval=50, spawn=None, on_error=None):
        self.root, self.interval = root, interval
        self.q = queue.Queue()
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._on_error = on_error or (lambda ex: print(f"UiBridge 回调出错: {ex!r}", file=sys.stderr))
        self._busy = set()
        self._job = None
        self.stopped = False

    def post(self, fn, *args):
        self.q.put((fn, args))

    def busy(self, key):
        return key in self._busy

    def run_bg(self, work, done=None, key=None):
        if key is not None:
            if key in self._busy:
                return False
            self._busy.add(key)

        # work/done 常常捎带着 Tk 对象 (面板的 self). 最后一个引用必须在主线程放掉,
        # 否则 StringVar 之类在工作线程里析构会去调 Tcl ("main thread is not in main loop").
        box = [work, done]

        def finish(res):
            fn = box[1]
            box.clear()
            self._busy.discard(key)
            if fn:
                fn(res)

        def wrap():
            try:
                res = box[0]()
            except Exception as ex:          # noqa: BLE001  交给 done 显示, 不让线程静默死掉
                res = ex
            self.post(finish, res)
        self._spawn(wrap)
        return True

    def drain(self, limit=200):
        for _ in range(limit):
            try:
                fn, args = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception as ex:          # noqa: BLE001  一个回调炸了不能拖垮轮询
                self._on_error(ex)

    def start(self):
        self.stopped = False
        self._schedule()

    def _schedule(self):
        try:
            self._job = self.root.after(self.interval, self._tick)
        except Exception:                    # noqa: BLE001  窗口已销毁 (TclError)
            self.stopped = True

    def _tick(self):
        try:
            self.drain()
        finally:
            if not self.stopped:
                self._schedule()

    def stop(self):
        self.stopped = True
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:                # noqa: BLE001
                pass
            self._job = None


# ---------------------------------------------------------------- 装机模式定时器

def pxe_tick_decide(st, log_text, now, consumed=kc.pxe_consumed):
    """st = kc.pxe_mode_read(). -> {disarm, banner, view}; view 给按钮显示:
    {"mode": "install", mac, left} 或 {"mode": "rescue"}. 到期 / 选定机器取走菜单 -> 上锁."""
    if st.get("mode") == "expired":
        return {"disarm": True, "banner": "装机模式 30 分钟到期, 已自动上锁", "view": {"mode": "rescue"}}
    if st.get("mode") == "install":
        if consumed(log_text, st["mac"], st["since"]):
            return {"disarm": True, "banner": f"{st['mac']} 已取走装机菜单, 装机模式已自动上锁",
                    "view": {"mode": "rescue"}}
        left = max(0, int(st["until"] - now))
        return {"disarm": False, "banner": None, "view": {"mode": "install", "mac": st["mac"], "left": left}}
    return {"disarm": False, "banner": None, "view": {"mode": "rescue"}}


def pxe_button_texts(view):
    """-> (装机页按钮, 主页按钮) 的文字."""
    if view["mode"] != "install":
        return "装机模式:  救援 (安全)", "装机"
    left = view["left"]
    mmss = f"{left // 60:02d}:{left % 60:02d}"
    return f"警告: 装机已解锁 {view['mac'][-8:]}  {mmss}", f"警告: 装机已解锁  {mmss}"


# ---------------------------------------------------------------- 开机自检兜底

def fail_rows(ex):
    return [{"label": "SELFTEST", "state": "FAIL", "detail": type(ex).__name__, "check": None}]


def boot_rows_safe(run):
    """run() 产出自检行; 任何异常都变成一行 SELFTEST FAIL, 不让开机页卡住."""
    try:
        return run()
    except Exception as ex:                  # noqa: BLE001
        return fail_rows(ex)


class Once:
    """只跑第一次: 自检正常结束和 30 秒看门狗谁先到谁算."""

    def __init__(self, fn):
        self.fn, self.done = fn, False

    def __call__(self):
        if self.done:
            return False
        self.done = True
        self.fn()
        return True


# ---------------------------------------------------------------- 左侧面板

# 名字 -> 打开时的横幅
PANES = {
    "preview": "",
    "serial": "串口会话 · conbroker (display 类只读命令可直接发, 改配置先问管理员)",
    "ks": "Kickstart 编辑 · 保存自动备份, 修改立即对下次装机生效",
    "wifi": "无线网络 · 优先级从上往下自动连, 再点一次按钮返回",
    "netprof": "网口配置文件 · 切换 / 回退 / 新建, 再点一次按钮返回",
    "wol": "网络唤醒 · 双击清单里的机器直接唤醒, 再点一次按钮返回",
}


class LeftPane:
    """左侧同时只显示一个面板 (都不开 = 状态板). 只记 current, 返回要关的和要开的, 由界面执行."""

    def __init__(self, names):
        self.names, self.current = set(names), None

    def toggle(self, name):
        if name not in self.names:
            raise KeyError(name)
        if self.current == name:
            self.current = None
            return name, None
        close, self.current = self.current, name
        return close, name

    def close(self):
        cur, self.current = self.current, None
        return cur


# ---------------------------------------------------------------- 右侧菜单
# do = Kiosk 方法名, arg = 传给它的参数; page = 进二级页; attr = 存成 self.<attr> 以便改文字;
# style = warn / danger. 子页由 kiosk 自动加「返回」. 常用的 (预览/串口/锁屏) 留在主页一键直达.

def _act(text, do, arg=None, attr=None, style=None):
    it = {"text": text, "do": do}
    for k, v in (("arg", arg), ("attr", attr), ("style", style)):
        if v is not None:
            it[k] = v
    return it


def _pane(text, name, attr=None):
    return _act(text, "toggle_pane", arg=name, attr=attr)


def _page(text, name, attr=None):
    it = {"text": text, "page": name}
    if attr:
        it["attr"] = attr
    return it


MENU = {
    "main": [
        _pane("UVC 预览:  开 / 关", "preview", attr="btn_video"),
        _page("画面设置", "video"),
        _pane("串口会话:  开 / 关", "serial", attr="btn_serial"),
        _page("装机", "install", attr="btn_install_page"),
        _page("网络", "net"),
        _act("锁屏", "lock_now"),
        _page("电源", "power"),
    ],
    "video": [
        _act("画面源", "switch_source", attr="btn_src"),
        _act("鼠标:  绝对 (点哪是哪)", "toggle_mouse_mode", attr="btn_mouse"),
        _act("键鼠绑定:  不绑定", "cycle_kvm", attr="btn_kvm"),
    ],
    "install": [
        _act("PXE 装机服务:  开 / 关", "toggle_pxe", attr="btn_pxe"),
        _act("装机模式:  救援 (安全)", "toggle_install_mode", attr="btn_install"),
        _pane("Kickstart 编辑", "ks", attr="btn_ks"),
    ],
    "net": [
        _pane("无线网络", "wifi", attr="btn_wifi"),
        _pane("网口配置文件", "netprof"),
        _pane("网络唤醒 (WOL)", "wol"),
        _act("LAN2 管理口配置", "cfg_lan2", attr="btn_lan2"),
    ],
    "power": [
        _act("重启推车", "reboot", style="warn"),
        _act("关机", "poweroff", style="danger"),
    ],
}


# ---------------------------------------------------------------- 键鼠透传

REL_MAX = 127


def _clamp(v, lim=REL_MAX):
    return max(-lim, min(lim, v))


def rel_split(dx, dy, wheel, lim=REL_MAX):
    """攒的相对位移 -> (这一帧发的, 留到下一帧的). 一帧最多 ±127, 多出来的不丢."""
    send = (_clamp(dx, lim), _clamp(dy, lim), _clamp(wheel, lim))
    return send, (dx - send[0], dy - send[1], wheel - send[2])


def rel_motion(pos, center):
    """相对模式: 指针每次被 warp 回中心; 落在中心上的那个事件就是 warp 自己, 不算位移."""
    if pos == center:
        return None
    return pos[0] - center[0], pos[1] - center[1]


def abs_should_send(kind, pt, have_last):
    """绝对模式要不要发帧. pt = abs_point 结果 (None = 黑边上).
    黑边上按下 / 滚轮 / 移动不发 (否则点到上一个位置); 松开照发 (用上次的坐标), 免得按键卡住."""
    if pt is not None:
        return True
    return kind == "release" and have_last


class ChWorker:
    """CH9329 发送放到专门线程 + 有界队列: 主线程只入队, 等 ACK 不卡界面.
    出错 (拔线, OSError) 就关句柄、标 dead, on_gone(ex) 只调一次 (在工作线程里, 调用方自己转回主线程)."""

    def __init__(self, dev, on_gone, maxsize=64):
        self.dev, self.on_gone = dev, on_gone
        self.q = queue.Queue(maxsize=maxsize)
        self.dead = False
        self._closed = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def send(self, fn, *args, droppable=False):
        """入队; 满了时可丢的 (位移) 直接丢, 不可丢的 (按键/点击) 最多等 0.5 秒. 返回是否入队."""
        if self.dead or self._closed:
            return False
        try:
            if droppable:
                self.q.put_nowait((fn, args))
            else:
                self.q.put((fn, args), timeout=0.5)
        except queue.Full:
            return False
        return True

    def idle(self):
        return self.q.unfinished_tasks == 0

    def join_queue(self, timeout):
        import time
        end = time.monotonic() + timeout
        while not self.idle() and time.monotonic() < end:
            time.sleep(0.005)

    def _close_dev(self):
        try:
            self.dev.close()
        except OSError:
            pass

    def _loop(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    self._close_dev()
                    return
                if self.dead:
                    continue
                fn, args = item
                try:
                    getattr(self.dev, fn)(*args)
                except OSError as ex:
                    self.dead = True
                    self._close_dev()
                    self.on_gone(ex)
            finally:
                self.q.task_done()

    def close(self, wait=False):
        if self._closed:
            return
        self._closed = True
        try:
            self.q.put(None, timeout=2)
        except queue.Full:
            self.dead = True
            self._close_dev()
        if wait:
            self._t.join(2)

    def join(self, timeout):
        """等队列里已有的发完 (测试和退出时用)."""
        self.join_queue(timeout)


# ---------------------------------------------------------------- 小键盘数字

# X keycode (= evdev + 8) -> 字符. NumLock 关着时这些键报成 KP_Home/KP_Up..., Entry 不插字;
# 按物理键码插数字, NumLock 开关都一样. NumLock 开着时报 KP_7 等, Entry 自己会插, 不经过这里.
KP_DIGITS = {79: "7", 80: "8", 81: "9", 83: "4", 84: "5", 85: "6",
             87: "1", 88: "2", 89: "3", 90: "0", 91: "."}
_KP_NAV = ("KP_Home", "KP_Up", "KP_Prior", "KP_Left", "KP_Begin", "KP_Right",
           "KP_End", "KP_Down", "KP_Next", "KP_Insert", "KP_Delete")


def kp_digits_as_digits(root):
    def ins(e):
        ch = KP_DIGITS.get(e.keycode)
        if ch is None:
            return None
        e.widget.insert("insert", ch)
        return "break"
    for ks in _KP_NAV:
        root.bind_class("Entry", f"<KeyPress-{ks}>", ins)


# ---------------------------------------------------------------- Kickstart 保存 / 恢复

def ks_save(text, confirm, write=kc.ks_write, validate=kc.ks_validate):
    """校验不通过时 confirm(警告列表) 二次确认才写. -> (成功, 提示)."""
    warns = validate(text)
    if warns and not confirm(warns):
        return False, "已取消"
    try:
        write(text)
    except OSError as ex:
        return False, f"写入失败: {ex}"
    return True, ""


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 文案检查

_SYMBOL_RANGES = ((0x2190, 0x2BFF),         # 箭头 / 技术符号 / 制表 / 方块 / 几何 / 杂项符号 / 丁贝
                  (0x1F000, 0x1FAFF),       # emoji
                  (0x2022, 0x2022),         # 圆点
                  (0xFE0F, 0xFE0F))         # emoji 变体选择符


def is_symbol_char(s):
    """文本里有没有 emoji / 箭头 / 方块这类符号 (界面文案只用文字; 省略号、间隔号、破折号不算)."""
    return any(lo <= ord(c) <= hi for c in s for lo, hi in _SYMBOL_RANGES)
