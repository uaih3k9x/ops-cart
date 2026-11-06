#!/usr/bin/env python3
"""kiosk 这边的中文引擎 (#54): 起 ime_worker.py 子进程, 行 JSON 通信. 主进程不 dlopen librime.

librime 是 C++, 在 kiosk (root) 进程里崩了会带崩车机, 还可能把 systemd 的重启次数用完. 放子进程:
  - root 下用 systemd-run --pipe 套沙箱 (动态用户、没网络、没能力、只读系统、内存上限), 没有 systemd-run 就不起;
  - 非 root (开发机) 直接用当前 python 起, 只有崩溃隔离, 没有降权断网;
  - 子进程退出 / 超时 / 回了看不懂的东西: 杀掉收尸, 抛 OSError 给 ime_core, 它判"不可用"回英文.
"""

import json
import os
import select
import shutil
import subprocess
import sys
import time

import ime_core

OPEN_TIMEOUT = 600          # 首次部署 (编译词库) 可能好几分钟; 后台线程里等
KEY_TIMEOUT = 3.0           # 每个键; 超了就当坏了 (界面线程在等)
LINE_MAX = 1 << 20
STATE_DIR = "/var/lib/ops-cart-ime"
_HERE = os.path.dirname(os.path.abspath(__file__))
_PASS_ENV = ("OPS_CART_LIBRIME", "OPS_CART_RIME_SHARED", "OPS_CART_RIME_SCHEMA")

_SANDBOX = ("DynamicUser=yes", "StateDirectory=ops-cart-ime", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
            "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes",
            "PrivateTmp=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
            "ProtectControlGroups=yes", "RestrictNamespaces=yes", "LockPersonality=yes",
            "MemoryMax=512M", "CPUWeight=50")


def worker_cmd():
    """起子进程的命令; root 又没有 systemd-run (降不了权) -> None."""
    worker = os.path.join(_HERE, "ime_worker.py")
    if os.geteuid() != 0:
        return [sys.executable, worker]
    run = shutil.which("systemd-run")
    if not run:
        return None
    cmd = [run, "--pipe", "--quiet", "--collect"]
    for p in _SANDBOX:
        cmd += ["-p", p]
    cmd.append(f"--setenv=OPS_CART_RIME_USER={STATE_DIR}")
    cmd += [f"--setenv={k}={os.environ[k]}" for k in _PASS_ENV if os.environ.get(k)]
    return cmd + [sys.executable, worker]


class ProcEngine:
    """ime_core 要的引擎接口, 每个调用一来一回."""

    def __init__(self, proc, key_timeout=KEY_TIMEOUT):
        self.proc, self.key_timeout = proc, key_timeout
        self._buf = b""
        self._snap, self._commit = ime_core.Snapshot("", "", [], 0, True, 0), None

    # ---------------------------------------------------------------- 管道

    def _fail(self, why):
        self.kill()
        raise OSError(f"中文输入子进程{why}")

    def _readline(self, timeout):
        end = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            left = end - time.monotonic()
            if left <= 0:
                self._fail(f"{timeout:g} 秒没回 (超时)")
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                code = self.proc.poll()
                self._fail(f"退出了 (退出码 {code if code is not None else '未知'})")
            self._buf += chunk
            if len(self._buf) > LINE_MAX:
                self._fail("回复太长")
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def call(self, req, timeout=None):
        if self.proc.poll() is not None:
            self._fail(f"已经退出 (退出码 {self.proc.returncode})")
        try:
            self.proc.stdin.write((json.dumps(req) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            self._fail("退出了 (写不进去)")
        line = self._readline(self.key_timeout if timeout is None else timeout)
        try:
            resp = json.loads(line)
            if not isinstance(resp, dict):
                raise ValueError
        except ValueError:
            self._fail("回复看不懂")
        if not resp.get("ok"):
            raise OSError(resp.get("why") or "中文输入子进程报错")
        return resp

    def _op(self, **req):
        resp = self.call(req)
        try:
            raw, pre, cands, page_no, last, hi = resp["snap"]
            self._snap = ime_core.Snapshot(str(raw), str(pre), [str(c) for c in cands][:9], int(page_no),
                                           bool(last), int(hi))
        except (KeyError, TypeError, ValueError):
            self._fail("回复看不懂")
        c = resp.get("commit")
        self._commit = c if isinstance(c, str) and c else None
        return bool(resp.get("handled"))

    # ---------------------------------------------------------------- 引擎接口

    def feed(self, ch):
        return self._op(op="feed", ch=ch)

    def backspace(self):
        return self._op(op="backspace")

    def clear(self):
        return self._op(op="clear")

    def select(self, i):
        return self._op(op="select", i=i)

    def page(self, backward):
        return self._op(op="page", backward=bool(backward))

    def snapshot(self):
        return self._snap

    def take_commit(self):
        c, self._commit = self._commit, None
        return c

    def kill(self):
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def close(self):
        if self.proc.poll() is None:
            try:
                self.call({"op": "close"}, timeout=1)
            except OSError:
                pass
        self.kill()


def open_engine(cmd=None, open_timeout=OPEN_TIMEOUT, key_timeout=KEY_TIMEOUT):
    """起子进程并打开引擎 -> (ProcEngine, "") 或 (None, "中文输入不可用: 原因"). 不抛; 可能慢, 放后台线程."""
    cmd = cmd or worker_cmd()
    if cmd is None:
        return None, "中文输入不可用: root 下没有 systemd-run, 不降权就不起 librime 子进程"
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd="/")
    except OSError as e:
        return None, f"中文输入不可用: 起不了子进程: {e}"
    eng = ProcEngine(proc, key_timeout)
    try:
        eng.call({"op": "open"}, timeout=open_timeout)
    except OSError as e:
        eng.kill()
        why = str(e)
        return None, why if why.startswith("中文输入不可用") else f"中文输入不可用: {why}"
    return eng, ""
