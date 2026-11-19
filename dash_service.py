#!/usr/bin/env python3
"""dash_service: cart-dash 常驻进程 —— 采集快照 + 画副屏 + 推 USB.

职责边界（issue #6）:
  - 每 200 ms 采集一次, 原子写 /run/ops-cart/state.json (带 ts/seq)
  - 版面有变化 / 时钟跳分钟 才重画, 画完推一帧到 USB 副屏
  - 屏幕拔掉不退出: 继续写快照, 每 3 s 试着重连
  - LAN1 / LAN2 的 LLDP 监听线程归这里管 (#14): 起不来/没 root/网口没了都只是"不可用", 不退出
  - 车机 kiosk 没就绪 (没写 kiosk.json / 写它的进程没了) 画开机自检, 不画旧的锁屏/工作页 (#26)
  - 深浅 (#32): 每帧按快照里的 theme.mode 切配色, 模式一变下一帧整帧换
  - 桌宠 (#35): 锁屏且没事时副屏有只猫, 动的时候在采集间隙按 8 fps 推整帧; 猫出错就摘掉, 不影响状态板
  - 两种副屏二选一 (docs/monita-dash.md): udisp (默认, JPEG 横屏) 或 Monita-CS80 (--display cs80):
    CS80 后端声明 wants_image, 收竖屏原图自己按脏区推; 每轮采集调一次它的 tick() 做保活 / 段码屏 / 灯 / 按键

用法:
  python3 dash_service.py                                  # 常驻
  python3 dash_service.py --once --preview /tmp/d.jpg      # 跑一轮, 导出画面(不推屏)
  python3 dash_service.py --fps 5                          # 推送上限 (猫不走这个, 见 pet_core)
  python3 dash_service.py --no-pet                         # 不要猫
  python3 dash_service.py --display cs80                   # 用 Monita-CS80 当副屏 (sysfs 找 2c7c:0901 接口 07)
  python3 dash_service.py --display cs80 --cs80-port /dev/ttys012          # 指定节点 (fwsim 的 PTY / udev 链接)
  python3 dash_service.py --once --display cs80 --preview /tmp/d.png       # 导出 CS80 要推的竖屏图
"""

import argparse
import json
import sys
import time

import dash_core as dc
import lamps_core
import lldp_core
import theme as th

SAMPLE_S = 0.2          # 快照节奏
RECONNECT_S = 3.0       # 拔线后重连间隔
SLOW_RETRY_MAX_S = 30.0 # 节点在但 HELLO 超时 (每次卡采集线程 1 s): 间隔翻倍退避到这么久
DEFAULT_FPS = 2.0       # 副屏推送上限: 仪表板不是视频


def _view_key(view):
    """版面是否变了: 只比对会画出来的字段, 时钟和 stale 单独算."""
    return json.dumps({k: v for k, v in view.items() if k not in ("clock", "stale")},
                      sort_keys=True, ensure_ascii=False)


def _why(e):
    return "%s: %s" % (type(e).__name__, e)


def _connect(factory, out, last=None):
    """返回 (显示对象, None) 或 (None, 异常). last: 上一次失败的 _why; 原因没变就不再打日志, 不刷 journal
    (没插 → 插上个不兼容的, 原因变了照打一行)."""
    try:
        disp = factory().open_display()
        print("副屏已连接 %r" % (getattr(disp, "cfg_text", "") or "").strip("\x00"), file=out)
        return disp, None
    except Exception as e:                     # 没插 / 权限 / libusb 忙 / 不兼容 / HELLO 超时
        if _why(e) != last:
            print("副屏没插/打不开: %s" % _why(e), file=out)
        return None, e


def _retry_gap(e, gap):
    """下一次重连等多久. TimeoutError = 节点在但不应答 (旧固件 / 口被占), 翻倍退避; 别的 (没插等) 照旧 3 s."""
    if isinstance(e, TimeoutError):
        return min(SLOW_RETRY_MAX_S, gap * 2) if gap else RECONNECT_S
    return RECONNECT_S


def pick_display(display, cs80_port=None, out=sys.stdout):
    """--display 选出来的副屏模块: 有 open_display() 就行. 进程起来就定了, 重连只重连同一种屏."""
    if display == "cs80":
        class _Cs80:
            @staticmethod
            def open_display():
                import monita_cs80
                return monita_cs80.open_display(port=cs80_port, log=lambda s: print(s, file=out))
        return _Cs80
    return lambda: __import__("udisp_usb")


def _start_lldp(factory, out):
    """起 LLDP 监听线程; 起不来就算了 (快照里 lldp 为空), 不影响别的采集."""
    try:
        return factory(dc.LLDP_IFACES).start()
    except Exception as e:
        print("LLDP 监听起不来: %s: %s" % (type(e).__name__, e), file=out)
        return None


def _make_pet():
    """真机才有猫; 模块坏了也不能拖累状态板."""
    import pet_core
    import pet_render
    pet_render.use_skin(pet_render.load_skin())
    return pet_core.Pet(), pet_render.Stage()


def run(once=False, preview=None, fps=DEFAULT_FPS, state_path=dc.STATE_PATH,
        sample_s=SAMPLE_S, out=sys.stdout, ctx=None, display_factory=None,
        render=None, lldp_factory=None, pet=None, stage=None, want_pet=False, display="udisp", cs80_port=None):
    """主循环. 依赖都可注入, 测试里用假的. 返回推了几帧.
    ctx 不注入时 (真机) 才起 LLDP 监听, 退出时关掉."""
    watcher = None
    if want_pet and pet is None and not once and not preview:
        try:
            pet, stage = _make_pet()
        except Exception as e:
            print("桌宠起不来: %s: %s" % (type(e).__name__, e), file=out)
    if ctx is None:
        watcher = _start_lldp(lldp_factory or lldp_core.Watcher, out)
        ctx = dc.default_ctx(lldp=watcher.snapshot if watcher else None)
    factory = display_factory or pick_display(display, cs80_port, out)
    try:
        return _loop(once, preview, fps, state_path, sample_s, out, ctx, factory, render, pet, stage,
                     images=display == "cs80")
    finally:
        if watcher is not None:
            watcher.close()


def _loop(once, preview, fps, state_path, sample_s, out, ctx, factory, render, pet=None, stage=None, images=False):
    """采集 → 落盘 → 版面变了才推屏. 拔屏重连. 有猫时采集间隙按猫的节奏再推几帧.
    images: 出竖屏 PIL 图而不是 JPEG (CS80); 连上的显示对象自己声明 wants_image 也算."""
    box = {"pet": pet}                      # 猫出错就摘掉, 闭包里看得见

    def paint(v):
        img = images or getattr(disp, "wants_image", False)
        if box["pet"] is None or stage is None or render_given:
            return plain(v, img)
        try:
            return stage.image(v, box["pet"]) if img else stage.frame(v, box["pet"])
        except Exception as e:
            _drop_pet(box, out, e)
            return plain(v, img)

    def plain(v, img):
        if render_given:
            return render(v)
        import dash_render
        return dash_render.render(v) if img else dash_render.make_frame(v)

    def drop(why, e, at):
        print("%s, 稍后重连: %s" % (why, _why(e)), file=out)
        try:
            disp.close()
        except Exception:
            pass
        return None, at + RECONNECT_S

    render_given = render is not None

    seq = pushed = 0
    last_key = last_clock = last_view = None
    retry = False                           # 刚连上: 不管版面变没变都推一帧
    fail_why = None                         # 上一次打不开的原因: 没变就不再打日志
    fail_gap = 0.0                          # 上一次的重连间隔 (HELLO 超时时翻倍)
    last_push = 0.0
    disp = None
    next_try = 0.0
    max_gap = 1.0 / max(fps, 0.1)
    moving = lamps_core.Activity()          # 计数器变了 → 那盏灯点亮 HOLD_S 秒
    boot = dc.BootTracker()                 # 车机没就绪就画开机自检 (#26)

    while True:
        seq += 1
        snap = dc.collect(dict(ctx, seq=seq))
        dc.write(state_path, snap)
        now = time.time()
        # 深浅 (#32): 先切配色再排版 (灯色在版面模型里), 模式记进版面: 变了 key 就变, 下一帧整帧换
        mode = th.use((snap.get("theme") or {}).get("mode"))
        view = dc.layout_view(snap, active=moving.update(snap.get("lamps") or {}, now))
        view = dict(boot.view(snap, now) or view, theme=mode)
        key = _view_key(view)
        _pet_sync(box, view, out)

        if not preview and disp is None and now >= next_try:      # 重连按时间走, 不等版面变 (设计稿第 8 节)
            disp, err = _connect(factory, out, fail_why)
            if disp is None:
                fail_gap = _retry_gap(err, fail_gap)
                next_try, fail_why = now + fail_gap, _why(err)
            else:
                retry, fail_why, fail_gap = True, None, 0.0

        want = key != last_key or view.get("clock") != last_clock or view.get("stale") or retry
        if want and now - last_push >= max_gap:
            last_key, last_clock, last_push, last_view = key, view.get("clock"), now, view
            if preview:
                frame = paint(view)
                if hasattr(frame, "save"):
                    frame.save(preview, format="PNG")
                else:
                    with open(preview, "wb") as f:
                        f.write(frame)
                print("preview -> %s" % preview, file=out)
                retry = False
            elif disp is not None:
                retry = False
                try:
                    disp.send(paint(view))
                    pushed += 1
                except Exception as e:
                    disp, next_try = drop("推送失败", e, now)
        if disp is not None and hasattr(disp, "tick"):          # CS80: 保活 / 段码屏 / 灯 / 按键, 每轮都要
            try:
                disp.tick(now, snap, view)
            except Exception as e:
                disp, next_try = drop("副屏出错", e, now)

        if once:
            break
        if box["pet"] is None or disp is None or last_view is None:
            time.sleep(sample_s)
            continue
        try:
            pushed += _pet_frames(box["pet"], sample_s, lambda: disp.send(paint(last_view)))
        except Exception as e:              # 这里多半是 USB; 猫自己的错 paint 已经接住了
            disp, next_try = drop("推送失败", e, time.time())

    return pushed


def _drop_pet(box, out, e):
    """猫出错: 摘掉, 状态板照常. 宁可没猫也不能让它带崩采集."""
    print("桌宠出错, 摘掉: %s: %s" % (type(e).__name__, e), file=out)
    box["pet"] = None


def _pet_sync(box, view, out):
    """每轮采集告诉猫: 还在不在锁屏、能不能待、字在哪."""
    pet = box["pet"]
    if pet is None:
        return
    try:
        import dash_render
        import pet_core
        locked = bool(view.get("locked")) and not view.get("boot")
        if box.get("notice") != view.get("notice") or "glyphs" not in box:     # 量字不贵, 但一轮采集没必要量一次
            box["notice"], box["glyphs"] = view.get("notice"), dash_render.glyph_boxes(view) if locked else []
        pet.sync(locked, pet_core.allowed(view), box["glyphs"] if locked else [], pet_core.red_lamp(view))
    except Exception as e:
        _drop_pet(box, out, e)


def _pet_frames(pet, span, push):
    """这一轮采集间隙里按猫的节奏推帧, 返回推了几帧. 猫刚走再补一帧把它擦掉."""
    end = time.time() + span
    pushed = 0
    last = time.time()
    while True:
        gap = pet.interval()
        if gap is None:
            if pet.drawn:
                pet.drawn = False
                push()
                pushed += 1
            time.sleep(max(0.0, end - time.time()))
            return pushed
        if pet.next_at is None:
            pet.next_at = last
        now = time.time()
        if now >= pet.next_at:
            pet.tick(now - pet.last_tick if pet.last_tick else gap)
            pet.last_tick = now
            pet.next_at = now + (pet.interval() or gap)
            pet.drawn = True
            push()
            pushed += 1
        now = time.time()
        if now >= end:
            return pushed
        time.sleep(max(0.0, min(end, pet.next_at) - now))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="cart-dash: 采集 + 副屏")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--preview", help="把这一帧写到文件而不是推屏")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--no-pet", action="store_true", help="不要副屏桌宠")
    ap.add_argument("--state", default=dc.STATE_PATH)
    ap.add_argument("--sample", type=float, default=SAMPLE_S, help="采集间隔秒")
    ap.add_argument("--display", choices=("udisp", "cs80"), default="udisp",
                    help="副屏: udisp (默认, xfz1986 USB 屏) 或 cs80 (Monita-CS80)")
    ap.add_argument("--cs80-port", help="CS80 串口节点 (不给就按 sysfs 找 2c7c:0901 接口 07)")
    a = ap.parse_args(argv)
    if a.cs80_port and a.display != "cs80":
        ap.error("--cs80-port 只配 --display cs80 用")
    return a


def main():
    a = parse_args()
    run(once=a.once, preview=a.preview, fps=a.fps, state_path=a.state, sample_s=a.sample, want_pet=not a.no_pet,
        display=a.display, cs80_port=a.cs80_port)


if __name__ == "__main__":
    main()
