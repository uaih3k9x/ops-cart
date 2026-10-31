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
    assert plain.getpixel((4, 230)) == r.BG
