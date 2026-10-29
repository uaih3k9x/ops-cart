#!/usr/bin/env python3
"""dash_service: cart-dash 常驻进程 —— 采集快照 + 画副屏 + 推 USB.

职责边界（issue #6）:
  - 每 200 ms 采集一次, 原子写 /run/ops-cart/state.json (带 ts/seq)
  - 版面有变化 / 时钟跳分钟 才重画, 画完推一帧到 USB 副屏
  - 屏幕拔掉不退出: 继续写快照, 每 3 s 试着重连

用法:
  python3 dash_service.py                                  # 常驻
  python3 dash_service.py --once --preview /tmp/d.jpg      # 跑一轮, 导出画面(不推屏)
  python3 dash_service.py --fps 5                          # 推送上限
"""

import argparse
import json
import sys
import time

import dash_core as dc

SAMPLE_S = 0.2          # 快照节奏
RECONNECT_S = 3.0       # 拔线后重连间隔
DEFAULT_FPS = 2.0       # 副屏推送上限: 仪表板不是视频


def _view_key(view):
    """版面是否变了: 只比对会画出来的字段, 时钟和 stale 单独算."""
    return json.dumps({k: v for k, v in view.items() if k not in ("clock", "stale")},
                      sort_keys=True, ensure_ascii=False)


def _connect(factory, out):
    try:
        disp = factory().open_display()
        print("副屏已连接 %r" % (getattr(disp, "cfg_text", "") or "").strip("\x00"), file=out)
        return disp
    except Exception as e:                     # 没插 / 权限 / libusb 忙
        print("副屏没插/打不开: %s" % type(e).__name__, file=out)
        return None


def run(once=False, preview=None, fps=DEFAULT_FPS, state_path=dc.STATE_PATH,
        sample_s=SAMPLE_S, out=sys.stdout, ctx=None, display_factory=None,
        render=None):
    """主循环. 依赖都可注入, 测试里用假的. 返回推了几帧."""
    ctx = ctx or dc.default_ctx()
    render = render or (lambda v: __import__("dash_render").make_frame(v))
    factory = display_factory or (lambda: __import__("udisp_usb"))

    seq = pushed = 0
    last_key = last_clock = None
    last_push = 0.0
    disp = None
    next_try = 0.0
    max_gap = 1.0 / max(fps, 0.1)

    while True:
        seq += 1
        snap = dc.collect(dict(ctx, seq=seq))
        dc.write(state_path, snap)
        view = dc.layout_view(snap)
        key = _view_key(view)
        now = time.time()

        want = (key != last_key or view.get("clock") != last_clock or view.get("stale"))
        if want and now - last_push >= max_gap:
            last_key, last_clock, last_push = key, view.get("clock"), now
            if preview:
                with open(preview, "wb") as f:
                    f.write(render(view))
                print("preview -> %s" % preview, file=out)
            else:
                if disp is None and now >= next_try:
                    disp = _connect(factory, out)
                    if disp is None:
                        next_try = now + RECONNECT_S
                if disp is not None:
                    try:
                        disp.send(render(view))
                        pushed += 1
                    except Exception as e:
                        print("推送失败, 稍后重连: %s" % type(e).__name__, file=out)
                        try:
                            disp.close()
                        finally:
                            disp, next_try = None, now + RECONNECT_S

        if once:
            break
        time.sleep(sample_s)

    return pushed


def main():
    ap = argparse.ArgumentParser(description="cart-dash: 采集 + 副屏")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--preview", help="把这一帧写到文件而不是推屏")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--state", default=dc.STATE_PATH)
    ap.add_argument("--sample", type=float, default=SAMPLE_S, help="采集间隔秒")
    a = ap.parse_args()
    run(once=a.once, preview=a.preview, fps=a.fps, state_path=a.state, sample_s=a.sample)


if __name__ == "__main__":
    main()
