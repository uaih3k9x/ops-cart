#!/usr/bin/env python3
"""recipe: 屏幕自动化配方 (#17) -- 截图 (cart_snap) + OCR (cart_ocr) + 发键 (ch9329), 逻辑在 recipe_core.

  sudo python3 /opt/pxe/recipe.py boot-pxe --timeout 120        # 先跑起来, 再给被测机上电
  sudo python3 /opt/pxe/recipe.py boot-pxe --key f12            # 不认提示, 上电就狂按 F12
  sudo python3 /opt/pxe/recipe.py boot-pxe --dry-run            # 只截图识别, 一个键都不发
  sudo python3 /opt/pxe/recipe.py 我的配方.json --port /dev/serial/by-id/usb-1a86_...

截图和逐步日志 log.jsonl (截图路径 / OCR 命中的文字 / 发的键) 留在 /run/ops-cart/recipe-* 下,
结束时往 events.log 记一行. 键鼠每发一个键才打开、发完就关, 被人占着 (Ch9329Busy) 就等, 等不到就停.
退出码: 0 完成; 1 某步失败 (超时 / 拿不准 / 键鼠被占用); 2 用法错误或配方不合规.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import cart_ocr
import cartlog
import ch9329
import kvm_broker
import recipe_core

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = "/run/ops-cart"
SNAP_TIMEOUT = 40


def make_snap(out_dir, dev=None):
    """每次截一张新图 (编号递增, 留着对日志), 走 cart_snap 的命令行."""
    n = [0]

    def snap():
        out = os.path.join(out_dir, f"{n[0]:03d}.png")
        n[0] += 1
        cmd = [sys.executable, os.path.join(HERE, "cart_snap.py"), "-o", out] + (["-d", dev] if dev else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SNAP_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"cart_snap 退出码 {r.returncode}")
        return out
    return snap


def make_send(port, baud):
    def send(key):
        # 经键鼠代理算 agent (人刚动过就排队, 急停抛 KvmStopped -> 停); 代理不在直开.
        # 被占用 / 排队超时抛 Ch9329Busy, 由 Runner 等待重试
        dev = kvm_broker.open_ch(port, src="agent", who="recipe", baud=baud)
        try:
            dev.send_keys([key])
        finally:
            dev.close()
    return send


def make_log(f):
    def log(ev):
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        f.flush()
        e = ev["ev"]
        if e in ("hit", "key", "prompt", "menu", "skip", "busy", "fail", "done"):
            extra = {k: v for k, v in ev.items() if k not in ("ev", "t", "seen")}
            print(f"[{ev['t']:7.1f}s] {e} {extra}", file=sys.stderr)
    return log


def load(name, timeout, key):
    if os.path.isfile(name):
        with open(name, encoding="utf-8") as f:
            return recipe_core.load_json(f.read())
    return recipe_core.builtin(name, timeout=timeout, **({"key": key} if key else {}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recipe", help=f"内置配方名 ({', '.join(recipe_core.BUILTIN)}) 或 JSON 配方文件")
    ap.add_argument("--timeout", type=float, default=120, help="总超时秒数 (内置配方用)")
    ap.add_argument("--key", help="boot-pxe: 不认提示, 直接狂按这个键")
    ap.add_argument("--dry-run", action="store_true", help="只截图识别, 不发键")
    ap.add_argument("-d", "--dev", help="采集设备, 默认交给 cart_snap 选")
    ap.add_argument("--port", default="/dev/ttyACM0", help="CH9329 串口")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--lang", default="eng", help="OCR 语言, 同 cart_ocr")
    a = ap.parse_args(argv)
    try:
        recipe = load(a.recipe, a.timeout, a.key)
    except (recipe_core.RecipeError, OSError) as e:
        print(f"配方不能用: {e}", file=sys.stderr)
        return 2
    base = RUN_DIR if os.path.isdir(RUN_DIR) and os.access(RUN_DIR, os.W_OK) else None
    out_dir = tempfile.mkdtemp(prefix="recipe-", dir=base)
    print(f"截图和日志: {out_dir}", file=sys.stderr)
    with open(os.path.join(out_dir, "log.jsonl"), "a", encoding="utf-8") as f:
        runner = recipe_core.Runner(make_snap(out_dir, a.dev), lambda p: cart_ocr.ocr_image(p, lang=a.lang),
                                    make_send(a.port, a.baud), log=make_log(f), dry_run=a.dry_run)
        res = runner.run(recipe)
    cartlog.event("recipe", cartlog.fields(name=recipe.get("name"), ok=res["ok"], step=res["step"],
                                           reason=res["reason"] or None, dry_run=a.dry_run, dir=out_dir))
    if res["ok"]:
        print("完成", file=sys.stderr)
        return 0
    print(f"第 {res['step'] + 1} 步失败: {res['reason']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
