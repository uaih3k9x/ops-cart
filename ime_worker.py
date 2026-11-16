#!/usr/bin/env python3
"""中文输入子进程 (#54): 在这里 dlopen librime, kiosk 主进程不碰它. stdin / stdout 一行一个 JSON.

kiosk 经 ime_proc 起它 (root 下用 systemd-run 套沙箱: 动态用户、没网络、没能力). librime 是 C++,
崩了只死这个进程, 主进程判"中文不可用"回英文.

请求 / 回复:
  {"op": "open"}                         -> {"ok": true} | {"ok": false, "why": "..."}
  {"op": "feed", "ch": "n"}              -> {"ok": true, "handled": bool, "snap": [...], "commit": str|null}
  {"op": "backspace" | "clear"}          -> 同上
  {"op": "select", "i": 0}               -> 同上
  {"op": "page", "backward": false}      -> 同上
  {"op": "close"}                        -> {"ok": true}, 然后退出
snap = [raw, preedit, candidates, page_no, last_page, highlighted] (ime_core.Snapshot 的顺序).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _dispatch(eng, req):
    op = req.get("op")
    if op == "feed":
        ch = req.get("ch")
        if not (isinstance(ch, str) and len(ch) == 1 and ("a" <= ch <= "z" or ch == "'")):
            raise ValueError("feed 只收一个小写字母或 '")
        handled = eng.feed(ch)
    elif op == "backspace":
        handled = eng.backspace()
    elif op == "clear":
        handled = eng.clear()
    elif op == "select":
        i = req.get("i")
        if not (isinstance(i, int) and 0 <= i < 9):
            raise ValueError("select 只收 0-8")
        handled = eng.select(i)
    elif op == "page":
        handled = eng.page(bool(req.get("backward")))
    else:
        raise ValueError(f"不认识的操作 {op!r}")
    return {"ok": True, "handled": handled is not False, "commit": eng.take_commit(), "snap": list(eng.snapshot())}


def serve(inp, out, opener=None):
    """读请求到 EOF 或 close. 单条出错回 {ok: false}, 不退出; librime 自己崩了那就是整个进程没了."""
    eng = None

    def reply(d):
        out.write(json.dumps(d, ensure_ascii=False) + "\n")
        out.flush()
    try:
        for line in inp:
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("请求要是 JSON 对象")
            except ValueError as e:
                reply({"ok": False, "why": f"请求不是 JSON: {e}"})
                continue
            op = req.get("op")
            if op == "close":
                reply({"ok": True})
                break
            if op == "open":
                if opener is None:
                    import ime_rime
                    opener = ime_rime.open_engine
                if eng is None:
                    eng, why = opener()
                    if eng is None:
                        reply({"ok": False, "why": why})
                        continue
                reply({"ok": True})
                continue
            if eng is None:
                reply({"ok": False, "why": "引擎没打开"})
                continue
            try:
                reply(_dispatch(eng, req))
            except Exception as e:
                reply({"ok": False, "why": f"{type(e).__name__}: {e}"})
    finally:
        if eng is not None:
            eng.close()


def main():
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
