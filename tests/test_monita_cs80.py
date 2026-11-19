"""monita_cs80 测试: 设备发现 (sysfs) / 推帧 (脏区 + PRESENT) / 保活 / 重新 HELLO / 灯 / 段码屏 / 按键 / 不发 SLEEP.
全部用假设备 (tests/cs80_fakedev.py) 和假时钟, 不打真机."""

import io
import os
import struct

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

import cs80_link as cl  # noqa: E402
import cs80_proto as p  # noqa: E402
import monita_cs80 as mc  # noqa: E402
from cs80_fakedev import Clock, FakeDevice  # noqa: E402

WALL = 1795000000.0


def _disp(**kw):
    dev = FakeDevice(**kw)
    out = io.StringIO()
    d = mc.Display(cl.Link(dev, clock=dev.clock), clock=dev.clock, wall=lambda: WALL,
                   log=lambda s: out.write(s + "\n"))
    return d, dev, out


def _img(color=(10, 20, 30)):
    return Image.new("RGB", (800, 1280), color)


VIEW = {"net": "on", "checks": {"ok": 1, "warn": 0, "fail": 0}, "stale": False}


# ---- 设备发现: 只认 2c7c:0901 接口 07, 别的 tty 一个都不开

def _usb(tmp_path, name, vid, pid, intf, acm=False):
    """造一份 sysfs: class/tty/<name>/device -> devices/.../<接口>[/<name>]."""
    dev = tmp_path / "sys" / "devices" / ("usb-" + name)
    (dev / ("x:1.%s" % intf)).mkdir(parents=True)
    (dev / "idVendor").write_text(vid + "\n")
    (dev / "idProduct").write_text(pid + "\n")
    iface = dev / ("x:1.%s" % intf)
    (iface / "bInterfaceNumber").write_text(intf + "\n")
    target = iface if acm else iface / name
    target.mkdir(exist_ok=True)
    cls = tmp_path / "sys" / "class" / "tty" / name
    cls.mkdir(parents=True)
    os.symlink(str(target), str(cls / "device"))


def test_find_port_only_matches_cs80_interface_7(tmp_path):
    _usb(tmp_path, "ttyUSB0", "0403", "6001", "00")       # 串口头 (console), 绝不能碰
    _usb(tmp_path, "ttyUSB2", "2c7c", "0901", "04")       # 同一个模组的别的口
    _usb(tmp_path, "ttyUSB3", "2c7c", "0901", "07")
    (tmp_path / "sys" / "class" / "tty" / "console").mkdir()
    assert mc.find_port(str(tmp_path / "sys")) == "/dev/ttyUSB3"


def test_find_port_acm_layout(tmp_path):
    _usb(tmp_path, "ttyACM0", "2c7c", "0901", "07", acm=True)
    assert mc.find_port(str(tmp_path / "sys")) == "/dev/ttyACM0"


def test_open_display_without_match_raises_before_opening_anything(tmp_path):
    _usb(tmp_path, "ttyUSB0", "0403", "6001", "00")
    opened = []
    with pytest.raises(FileNotFoundError):
        mc.open_display(sys_root=str(tmp_path / "sys"), opener=lambda path: opened.append(path))
    assert opened == []


def test_open_display_explicit_port_skips_discovery(tmp_path):
    dev = FakeDevice()
    seen = []
    d = mc.open_display(port="/dev/pts/9", sys_root=str(tmp_path / "nothing"),
                        opener=lambda path: seen.append(path) or dev, clock=dev.clock, wall=lambda: WALL)
    assert seen == ["/dev/pts/9"] and d.wants_image and dev.types() == [p.HELLO, p.BACKLIGHT]


def test_open_display_closes_port_when_incompatible():
    dev = FakeDevice(width=480)
    with pytest.raises(ValueError):
        mc.open_display(port="/dev/x", opener=lambda path: dev, clock=dev.clock, wall=lambda: WALL)
    assert dev.closed


# ---- HELLO

def test_hello_carries_wall_time_and_zone():
    d, dev, _ = _disp()
    t, z = struct.unpack("<Ih", dev.last(p.HELLO))
    assert t == int(WALL) and z == p.tz_minutes(WALL)
    assert "CS80" in d.cfg_text


@pytest.mark.parametrize("kw", [dict(proto_ver=1), dict(height=800), dict(pixfmt=2), dict(max_payload=1000)])
def test_incompatible_device_refused(kw):
    with pytest.raises(ValueError):
        _disp(**kw)


# ---- 推帧

def test_first_send_is_full_frame_then_diff():
    d, dev, _ = _disp()
    a = _img()
    d.send(a)
    assert dev.types().count(p.RECT) == 80 and dev.types()[-1] == p.PRESENT
    assert bytes(dev.fb) == p.rgb565(a)
    n = len(dev.frames)
    b = a.copy()
    b.putpixel((5, 700), (255, 255, 255))
    d.send(b)
    assert dev.types()[n:] == [p.RECT, p.PRESENT]
    assert bytes(dev.fb) == p.rgb565(b)


def test_same_image_sends_nothing():
    d, dev, _ = _disp()
    a = _img()
    d.send(a)
    n = len(dev.frames)
    d.send(a)
    assert len(dev.frames) == n


def test_respects_max_payload():
    d, dev, _ = _disp(max_payload=16384)
    a = _img((200, 100, 50))
    d.send(a)
    assert all(len(pl) <= 16384 for t, _s, pl in dev.frames if t == p.RECT)
    assert bytes(dev.fb) == p.rgb565(a)


def test_device_errors_force_full_frame_next_time():
    d, dev, _ = _disp()
    d.send(_img())
    dev.reject_rects = 1
    b = _img()
    b.putpixel((0, 0), (255, 0, 0))
    d.send(b)                                     # 这一条 RECT 被拒, ACK 里 bad_param +1
    n = len(dev.frames)
    c = b.copy()
    c.putpixel((799, 1279), (0, 255, 0))
    d.send(c)
    assert dev.types()[n:].count(p.RECT) == 80
    assert bytes(dev.fb) == p.rgb565(c)


def test_error_baseline_resets_after_hello():
    """HELLO 把设备计数清零: 重连后第一次比较不能拿旧基线."""
    d, dev, _ = _disp()
    d.send(_img())
    dev.reject_rects = 1
    b = _img()
    b.putpixel((0, 0), (255, 0, 0))
    d.send(b)
    d.send(_img((1, 1, 1)))                        # 整帧, 基线 = 1
    dev.clock.advance(4.0)                        # 隔了 ≥ 3.5 s: 重新 HELLO, 设备计数清零
    d.tick(WALL, {}, VIEW)
    dev.reject_rects = 1                          # 重连后第一条被拒: 计数 1, 旧基线也是 1
    e = _img((1, 1, 1))
    e.putpixel((400, 400), (9, 9, 9))
    d.send(e)
    n = len(dev.frames)
    f = e.copy()
    f.putpixel((10, 10), (9, 9, 9))
    d.send(f)
    assert dev.types()[n:].count(p.RECT) == 80          # 基线归零了才看得出 1 > 0, 推整帧补上
    assert bytes(dev.fb) == p.rgb565(f)


def test_present_timeout_raises_and_keeps_committed_cache():
    d, dev, _ = _disp()
    a = _img()
    d.send(a)
    dev.silent.add(p.PRESENT)
    b = _img((90, 90, 90))
    with pytest.raises(OSError):
        d.send(b)
    assert d.committed is a


def test_send_after_long_gap_rehellos_and_pushes_full():
    d, dev, _ = _disp()
    a = _img()
    d.send(a)
    dev.clock.advance(3.6)
    n = len(dev.frames)
    b = a.copy()
    b.putpixel((1, 1), (255, 0, 0))
    d.send(b)
    assert dev.types()[n] == p.HELLO and dev.types()[n:].count(p.RECT) == 80


def test_backend_never_draws_on_input_image():
    d, _dev, _ = _disp()
    a = _img()
    before = a.tobytes()
    d.send(a)
    d.tick(WALL, {}, VIEW)
    assert a.tobytes() == before


# ---- 保活 / 段码屏 / 灯

def _run_ticks(d, dev, secs, step=0.2, view=VIEW, snap=None):
    for _ in range(int(secs / step)):
        dev.clock.advance(step)
        d.tick(WALL, snap or {}, view)


def test_tick_sends_seg_every_second_and_feeds_watchdog():
    d, dev, _ = _disp()
    d.send(_img())
    _run_ticks(d, dev, 6.0)
    assert 5 <= dev.types().count(p.SEG) <= 7
    assert p.HELLO not in dev.types()[1:]          # 一直在喂, 不用重新 HELLO
    assert dev.last(p.SEG) == p.seg_state({}, VIEW, WALL)


def test_without_seg_falls_back_to_ping_every_two_seconds():
    d, dev, _ = _disp(seg_bytes=71)
    d.send(_img())
    _run_ticks(d, dev, 6.0)
    assert p.SEG not in dev.types() and 2 <= dev.types().count(p.PING) <= 3
    assert p.HELLO not in dev.types()[1:]


def test_tick_after_gap_rehellos_resends_led_and_last_frame():
    d, dev, _ = _disp()
    a = _img()
    d.send(a)
    d.tick(WALL, {}, VIEW)
    dev.clock.advance(4.0)
    dev.fb[:] = bytes(len(dev.fb))                # 设备回了开机页
    n = len(dev.frames)
    d.tick(WALL, {}, VIEW)
    tail = dev.types()[n:]
    assert tail[0] == p.HELLO and tail.count(p.RECT) == 80 and p.LED in tail
    assert bytes(dev.fb) == p.rgb565(a)


def test_led_only_on_change():
    d, dev, _ = _disp()
    _run_ticks(d, dev, 1.0)
    assert dev.types().count(p.LED) == 1 and dev.last(p.LED) == p.led_payload("ok")
    bad = dict(VIEW, checks={"ok": 0, "warn": 0, "fail": 1})
    _run_ticks(d, dev, 1.0, view=bad)
    assert dev.types().count(p.LED) == 2 and dev.last(p.LED) == p.led_payload("alert")


def test_no_led_without_cap():
    d, dev, _ = _disp(caps=0x03)
    _run_ticks(d, dev, 1.0)
    assert p.LED not in dev.types()


def test_feature_rejected_is_disabled_not_disconnect():
    d, dev, out = _disp()
    dev.status[p.LED] = 3
    _run_ticks(d, dev, 1.0)
    _run_ticks(d, dev, 1.0, view=dict(VIEW, net="warn", checks={"warn": 1}))
    assert dev.types().count(p.LED) == 1
    assert out.getvalue().count("LED") == 1


def test_boot_view_ticks_without_crashing():
    d, dev, _ = _disp()
    boot = {"boot": True, "title": "OPS-CART", "lines": [], "stale": False, "clock": "09:00"}
    _run_ticks(d, dev, 1.0, view=boot, snap={"ups": {"percent": 50}})
    assert dev.last(p.LED) == p.led_payload("selftest")


# ---- 按键: 只开关背光, 不碰推车

def test_minus_and_plus_toggle_backlight_on_up_only():
    d, dev, _ = _disp()
    n = len(dev.frames)                           # HELLO 后那一条 (背光开) 不算
    dev.press(p.KEY_MINUS, p.EV_DOWN)
    d.tick(WALL, {}, VIEW)
    assert p.BACKLIGHT not in dev.types()[n:]
    dev.press(p.KEY_MINUS, p.EV_UP)
    d.tick(WALL, {}, VIEW)
    assert dev.last(p.BACKLIGHT) == b"\x00"
    dev.press(p.KEY_MINUS, p.EV_UP)               # 已经关了, 不重复发
    d.tick(WALL, {}, VIEW)
    assert dev.types()[n:].count(p.BACKLIGHT) == 1
    dev.press(p.KEY_PLUS, p.EV_UP)
    d.tick(WALL, {}, VIEW)
    assert dev.last(p.BACKLIGHT) == b"\x01"


def test_menu_and_query_keys_do_nothing():
    d, dev, _ = _disp()
    n = len(dev.frames)
    for k in (1, p.KEY_MENU):
        for ev in (p.EV_DOWN, p.EV_LONG, p.EV_UP):
            dev.press(k, ev)
    d.tick(WALL, {}, VIEW)
    assert p.BACKLIGHT not in dev.types()[n:]


def test_key_sequence_gap_only_logged():
    d, dev, out = _disp()
    dev.press(p.KEY_MENU, p.EV_DOWN, n=0)
    dev.press(p.KEY_MENU, p.EV_UP, n=1)
    d.tick(WALL, {}, VIEW)
    assert "断号" not in out.getvalue()
    dev.press(p.KEY_MINUS, p.EV_UP, n=3)
    d.tick(WALL, {}, VIEW)
    assert out.getvalue().count("断号") == 1 and dev.last(p.BACKLIGHT) == b"\x00"


def test_backlight_off_is_resent_after_rehello():
    d, dev, _ = _disp()
    dev.press(p.KEY_MINUS)
    d.tick(WALL, {}, VIEW)
    dev.clock.advance(4.0)
    n = len(dev.frames)
    d.tick(WALL, {}, VIEW)
    tail = dev.types()[n:]
    assert tail[0] == p.HELLO and p.BACKLIGHT in tail and dev.last(p.BACKLIGHT) == b"\x00"


# ---- 不发 SLEEP, 拔插

def test_never_sends_sleep_even_on_close_and_errors():
    d, dev, _ = _disp()
    d.send(_img())
    _run_ticks(d, dev, 2.0)
    dev.clock.advance(5.0)
    d.tick(WALL, {}, VIEW)
    d.close()
    assert p.SLEEP not in dev.types() and dev.closed


def test_unplug_raises_from_tick_and_send():
    d, dev, _ = _disp()
    dev.unplugged = True
    with pytest.raises(OSError):
        d.tick(WALL, {}, VIEW)
    with pytest.raises(OSError):
        d.send(_img())


def test_clock_helper():
    c = Clock(1.0)
    c.advance(0.5)
    assert c() == 1.5


# ---- 版面: CS80 原生竖屏 800x1280, 不重排不缩放不转向 (设计稿第 1 节); 深浅两种配色都原样落到设备上

@pytest.mark.parametrize("mode", ["dark", "light"])
def test_dash_render_lands_unrotated_in_both_themes(mode):
    import dash_core as dc
    import dash_render as r
    import theme as th
    try:
        m = th.use(mode)
        view = dict(dc.layout_view({"ts": 0, "rows": [], "net": "on"}), theme=m)
        img = r.render(view)
        bg = th.DASH_BG
    finally:
        th.use("dark")
    assert img.size == (p.PANEL_W, p.PANEL_H)
    d, dev, _ = _disp()
    d.send(img)
    assert bytes(dev.fb) == p.rgb565(img)
    rgb = tuple(int(bg[i:i + 2], 16) for i in (1, 3, 5)) if isinstance(bg, str) else bg
    want = ((rgb[0] >> 3) << 11) | ((rgb[1] >> 2) << 5) | (rgb[2] >> 3)
    corner = ((p.PANEL_H // 2) * p.PANEL_W + 2) * 2              # 左边距里, 只有底色
    assert struct.unpack_from("<H", dev.fb, corner)[0] == want


# ---- 评审修复: 快速重连后的背光 / 坏帧后 tick 自己补整帧

def test_fast_reconnect_resends_backlight_so_plus_key_works():
    """− 关了背光, 5 s 看门狗之前就重连 (新 Display): 固件 HELLO 不动背光, 主机必须按自己想要的重发."""
    d, dev, _ = _disp()
    dev.press(p.KEY_MINUS)
    d.tick(WALL, {}, VIEW)
    assert dev.backlight == 0
    d.close()
    d2 = mc.Display(cl.Link(dev, clock=dev.clock), clock=dev.clock, wall=lambda: WALL, log=lambda s: None)
    assert dev.backlight == 1                     # 新会话默认开, HELLO 后就发了
    dev.press(p.KEY_MINUS)
    d2.tick(WALL, {}, VIEW)
    assert dev.backlight == 0
    dev.press(p.KEY_PLUS)
    d2.tick(WALL, {}, VIEW)
    assert dev.backlight == 1


def test_tick_repushes_full_frame_after_device_error():
    """diff 的 RECT 被设备丢了 (计数涨): 不等下一次 send, 下一轮 tick 就推整帧补上."""
    d, dev, _ = _disp()
    d.send(_img())
    dev.reject_rects = 1
    b = _img()
    b.putpixel((0, 0), (255, 0, 0))
    d.send(b)
    assert bytes(dev.fb) != p.rgb565(b) and d.full
    n = len(dev.frames)
    d.tick(WALL, {}, VIEW)
    assert dev.types()[n:].count(p.RECT) == 80 and bytes(dev.fb) == p.rgb565(b) and not d.full


def test_tick_full_frame_retry_is_rate_limited():
    """设备每条 RECT 都拒: tick 不能每 0.2 s 推一张 2 MB 整帧, 最多 1 s 一次."""
    d, dev, _ = _disp()
    d.send(_img())
    dev.reject_rects = 10 ** 9
    b = _img((5, 5, 5))
    d.send(b)
    n = len(dev.frames)
    for _ in range(4):                            # 0.8 s 里 4 轮
        d.tick(WALL, {}, VIEW)
        dev.clock.advance(0.2)
    assert dev.types()[n:].count(p.RECT) == 80    # 只补了第一次
    dev.clock.advance(0.3)
    m = len(dev.frames)
    d.tick(WALL, {}, VIEW)
    assert dev.types()[m:].count(p.RECT) == 80


def test_silent_device_raises_builtin_timeout_error():
    """节点在但 HELLO 不应答: 必须抛内置 TimeoutError, dash_service 靠它判断要不要退避."""
    dev = FakeDevice()
    dev.silent.add(p.HELLO)
    with pytest.raises(TimeoutError):
        mc.open_display(port="/dev/x", opener=lambda path: dev, clock=dev.clock, wall=lambda: WALL)
    assert dev.closed
