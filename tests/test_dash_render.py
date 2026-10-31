"""dash_render 测试: 出图朝向 / JPEG 预算降质. 需要 Pillow, 没装就跳过."""

import io

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

import dash_core as dc  # noqa: E402
import dash_render as r  # noqa: E402


def _view():
    return dc.layout_view({"ts": 0, "rows": [], "todo": ["换 con-2 的线", "补 CART-LOG"]})


def test_make_frame_is_landscape_for_the_panel():
    # 竖屏画, 发出去必须是配置串报告的横屏 1280x800
    img = Image.open(io.BytesIO(r.make_frame(_view())))
    assert img.size == (r.PANEL_H, r.PANEL_W)


def test_make_frame_lowers_quality_until_within_budget(monkeypatch):
    seen = []

    def fake_encode(img, quality=r.JPEG_QUALITY):
        seen.append(quality)
        return b"x" * (quality * 1000)       # 质量越低越小
    monkeypatch.setattr(r, "encode", fake_encode)
    out = r.make_frame(_view(), budget=50_000)
    assert len(out) <= 50_000
    assert seen == sorted(seen, reverse=True)          # 逐档往下降
    assert len(seen) == len(set(seen))                 # 每档只编码一次, 不旋转后再压一遍


# ---- 版面 v2

def _full_snap(**over):
    import time as _t
    s = {"ts": _t.time(), "net": "warn", "lan2": "192.0.2.5", "uptime": "up 1 day, 3 hours",
         "rows": [{"label": "WIFI", "state": "WARN", "detail": "lab 78% IN USE"},
                  {"label": "PXE SERVER", "state": "FAIL", "detail": "DEAD"}],
         "slots": [{"dev": "/dev/con-%d" % i, "present": i != 3, "baud": 9600 if i == 1 else None,
                    "count": None} for i in range(1, 5)],
         "temps": [["CPU", 74.0], ["显卡", 38.0], ["主板", 27.8]], "fans": [["fan2", 1300], ["fan4", 0]],
         "lamps": {}, "notice": "2026级 IMIS 请前往 B101 上课", "todo": ["换 con-2 的线"],
         "dream": "昨夜整理 10-30 · Wi-Fi 优先级调整完成但 Lan2 临时路由仍未清理"}
    s.update(over)
    return s


@pytest.mark.parametrize("locked", [False, True])
def test_render_v2_both_modes(locked):
    v = dc.layout_view(_full_snap(), locked=locked, active={"con-1", "pxe"})
    assert r.render(v).size == (r.PANEL_W, r.PANEL_H)


def test_render_lldp_peer_lines():
    nb = {"sys_name": "SW-A-VERY-LONG-NAME-" * 4, "port_id": "GigabitEthernet0/0/1", "port_subtype": 5,
          "vlan": 10, "ttl": 120}
    lldp = {"LAN1": {"state": "fresh", "age": 3, "neighbor": nb, "error": ""},
            "LAN2": {"state": "stale", "age": 900, "neighbor": nb, "error": ""}}
    v = dc.layout_view(_full_snap(lldp=lldp))
    assert len(v["peers"]) == 2
    assert r.render(v).size == (r.PANEL_W, r.PANEL_H)


def test_render_v2_stale_and_empty_snapshot():
    assert r.render(dc.layout_view({}, locked=False)).size == (r.PANEL_W, r.PANEL_H)
    old = _full_snap(ts=1.0)
    assert dc.layout_view(old)["stale"] and r.render(dc.layout_view(old)).size == (r.PANEL_W, r.PANEL_H)


class _MonoFont:
    def getlength(self, s):
        return 10 * len(s)


def test_wrap_by_pixel_width_with_ellipsis():
    assert r.wrap("abcdefghij", _MonoFont(), 50, 3) == ["abcde", "fghij"]
    assert r.wrap("abcdefghijklmnop", _MonoFont(), 50, 2) == ["abcde", "fghi…"]
    assert r.wrap("", _MonoFont(), 50, 2) == []


def test_notice_paragraphs_split_on_slash_and_newline():
    assert r.paragraphs("第6-10周 / ■组织管理\n请前往B101") == ["第6-10周", "■组织管理", "请前往B101"]
    assert r.paragraphs("") == []


def test_wrap_keeps_ascii_runs_together():
    # "6-10" "B101" 这类串整块换行, 不从中间劈开; 单个串比整行还宽才逐字拆
    assert r.wrap("第6-10周", _MonoFont(), 40, 5) == ["第", "6-10", "周"]
    assert r.wrap("abcdefgh", _MonoFont(), 40, 5) == ["abcd", "efgh"]


def test_wrap_never_starts_a_line_with_closing_punctuation():
    # 避头: "，" 放不下也挂在上一行末尾, 不另起一行
    assert r.wrap("第6-10，12周", _MonoFont(), 40, 5) == ["第", "6-10，", "12周"]


# ---- #11: 自己发地址时副屏头部一整条红 (到时停发琥珀), 解锁/锁屏都画

@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("state,color", [("serving", r.BAD), ("expired", r.WARN)])
def test_render_serve_banner(locked, state, color):
    from PIL import ImageColor
    snap = _full_snap(pxe={"state": state, "until": 10 ** 12 if state == "serving" else 1, "window": 1800})
    img = r.render(dc.layout_view(snap, locked=locked))
    assert img.getpixel((4, 230)) == ImageColor.getrgb(color)          # 条的左边缘
    plain = r.render(dc.layout_view(_full_snap(), locked=locked))
    assert plain.getpixel((4, 230)) == ImageColor.getrgb(r.BG)


# ---- 转电池那行不能压到大时钟 (10/31 放电演练实测压住了)

@pytest.mark.parametrize("clock", ["23:58", "00:00", "--:--"])
def test_ups_line_on_battery_never_overlaps_clock(clock):
    ups = dc.layout_view({"ts": 0, "rows": [], "ups": {"state": "discharging", "percent": 100,
                                                       "status": "OB DISCHRG", "runtime": 5940}})["ups"]
    text, size = r.ups_fit(ups, clock)
    right = r.PANEL_W - r.M
    left = right - r._font(size).getlength(text)
    assert left >= r.clock_right(clock) + r.UPS_GAP, (text, size)
    assert size >= r.UPS_MIN_PX


# ---- 两段式低电量关机 (#4): 预告琥珀横条, 倒计时红横条, 都只占头部那一条, 不铺满

OB22 = {"state": "discharging", "percent": 22, "status": "OB DISCHRG", "runtime": 400}


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("ups,kiosk,color", [
    (OB22, {}, r.WARN),
    (dict(OB22, percent=15), {"ups_since": 10 ** 12}, r.BAD),
])
def test_render_ups_alert_band(locked, ups, kiosk, color):
    from PIL import ImageColor
    img = r.render(dc.layout_view(_full_snap(ups=ups, kiosk=kiosk), locked=locked))
    assert img.getpixel((4, 230)) == ImageColor.getrgb(color)
    assert img.getpixel((4, 700)) != ImageColor.getrgb(color)      # 只是一条, 下面照常


def test_render_ups_countdown_beats_serve_but_warn_yields_to_it():
    from PIL import ImageColor
    serve_expired = {"state": "expired", "until": 1, "window": 1800}
    low = dict(OB22, percent=15)
    img = r.render(dc.layout_view(_full_snap(ups=low, pxe=serve_expired)))
    assert img.getpixel((4, 230)) == ImageColor.getrgb(r.BAD)       # 倒计时压过"到时停发"
    serving = {"state": "serving", "until": 10 ** 12, "window": 1800}
    img = r.render(dc.layout_view(_full_snap(ups=OB22, pxe=serving)))
    assert img.getpixel((4, 230)) == ImageColor.getrgb(r.BAD)       # 预告让位给发地址那条红


@pytest.mark.parametrize("stage,left", [("warn", None), ("countdown", 30), ("poweroff", 0)])
def test_ups_alert_band_text_fits_panel(stage, left):
    import ups_core
    alert = ups_core.alert(dict(OB22, percent=100, runtime=5999), stage, left)
    text, px = r.band_fit(alert)
    assert r._font(px).getlength(text) <= r.PANEL_W - 2 * r.M
    assert text == alert["text"]                                     # 全文放得下, 不该退成短写法


def test_ups_line_keeps_full_text_when_it_fits():
    ups = {"text": "电池 100% · 负载 18%", "level": "ok"}
    assert r.ups_fit(ups, "23:58") == ("电池 100% · 负载 18%", r.UPS_PX)


# ---- #26: 开机自检版面

def _boot_snap(**over):
    import time as _t
    snap = {"ts": _t.time(), "version": "Ver 1.08.03   Build 0042 (abc1234)",
            "rows": [{"label": "PXE SERVER", "state": "FAIL", "detail": "NOT RUNNING", "check": "service"},
                     {"label": "LAN2", "state": "OK", "detail": "192.0.2.5", "check": "lan"},
                     {"label": "TAILSCALE", "state": "WARN", "detail": "OFFLINE", "check": "tailscale"},
                     {"label": "CH9329", "state": "OK", "detail": "1 ONLINE", "check": "ch9329"}],
            "ups": {"state": "fully-charged", "percent": 100, "status": "OL"},
            "slots": [{"dev": "/dev/con-%d" % i, "present": i < 3, "baud": None, "count": None}
                      for i in range(1, 5)],
            "svcs": {"chronyd": [True, True], "kiosk": [True, True]}, "kiosk": {}}
    snap.update(over)
    return snap


def _boot_view(late=30, **over):
    """跑完逐行动画后的那一帧."""
    snap = _boot_snap(**over)
    t = dc.BootTracker(step=0.5, late=late)
    for i in range(20):
        v = t.view(snap, snap["ts"] + 0.5 * i)
    return v


def _has(img, box, color):
    from PIL import ImageColor
    want = ImageColor.getrgb(color)
    return want in set(img.crop(box).getdata())


def test_render_boot_screen_is_portrait_and_sends_landscape():
    v = _boot_view()
    assert v["boot"] and r.render(v).size == (r.PANEL_W, r.PANEL_H)
    assert Image.open(io.BytesIO(r.make_frame(v))).size == (r.PANEL_H, r.PANEL_W)


def test_render_boot_status_colors_follow_state():
    v = _boot_view()
    img = r.render(v)
    states = [ln["state"] for ln in v["lines"]]
    assert {"ok", "fail", "wait"} <= set(states)
    for i, st in enumerate(states):
        box = r.boot_status_box(i)
        want = {"ok": r.ACCENT, "fail": r.BAD, "warn": r.WARN, "late": r.WARN}.get(st)
        if want:
            assert _has(img, box, want), (i, st)
        else:                                       # 等待 / 没装: 灰, 不带红绿
            assert not _has(img, box, r.BAD) and not _has(img, box, r.ACCENT), (i, st)


def test_render_boot_late_lines_are_amber():
    v = _boot_view(late=1, rows=[], ups=None, slots=[], svcs={})
    assert all(ln["state"] == "late" for ln in v["lines"])
    img = r.render(v)
    assert all(_has(img, r.boot_status_box(i), r.WARN) for i in range(len(v["lines"])))


def test_render_boot_stale_banner():
    from PIL import ImageColor
    img = r.render(dict(_boot_view(), stale=True))
    assert img.getpixel((4, r.PANEL_H - 30)) == ImageColor.getrgb(r.BAD)


# ---- #23 Agent 写串口: 操作头方块变色 + "操作头"那行右边写挂起数 / 急停

def _agent_snap(**over):
    st = [{"estop": False, "pending": 0, "writer": False, "sending": False} for _ in range(4)]
    for n, d in over.items():
        st[int(n[1:]) - 1].update(d)
    return _full_snap(agent=st)


@pytest.mark.parametrize("locked", [False, True])
def test_render_agent_pending_and_estop_tiles(locked):
    from PIL import ImageColor
    img = r.render(dc.layout_view(_agent_snap(s1={"pending": 1, "writer": True}, s2={"estop": True}),
                                  locked=locked))
    tw = (r.PANEL_W - 2 * r.M - 20 * 3) // 4
    mid = lambda i: (r.M + i * (tw + 20) + 8, 320 + 150)         # noqa: E731  方块左下角, 避开字
    assert img.getpixel(mid(0)) == ImageColor.getrgb(confirm_core_color("PENDING"))
    assert img.getpixel(mid(1)) == ImageColor.getrgb(confirm_core_color("ESTOP"))
    plain = r.render(dc.layout_view(_full_snap(), locked=locked))
    assert plain.getpixel(mid(0)) != img.getpixel(mid(0))


def test_render_agent_summary_line_only_when_needed():
    from PIL import ImageColor
    box = (r.PANEL_W - r.M - 300, 280, r.PANEL_W - r.M, 310)

    def has(img, color):
        c = ImageColor.getrgb(color)
        return any(img.getpixel((x, y)) == c for x in range(box[0], box[2]) for y in range(box[1], box[3]))
    assert has(r.render(dc.layout_view(_agent_snap(s4={"estop": True}))), r.BAD)
    assert not has(r.render(dc.layout_view(_full_snap())), r.BAD)


def confirm_core_color(name):
    import confirm_core
    return getattr(confirm_core, name + "_COLOR")
