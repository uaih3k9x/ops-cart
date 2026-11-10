#!/usr/bin/env python3
"""kiosk 这边的中文引擎 (#54): 起 ime_worker.py 子进程, 行 JSON 通信. 主进程不 dlopen librime.

librime 是 C++, 在 kiosk (root) 进程里崩了会带崩车机, 还可能把 systemd 的重启次数用完. 放子进程:
  - root 下用 systemd-run 套沙箱 (动态用户、没网络、没能力、只读系统、内存上限, 对齐 cart-ocr.service),
    没有 systemd-run 就不起; 单元名 ops-cart-ime-<pid>-<n>, kill() 连单元一起杀; RuntimeMaxSec 兜底,
    在 kiosk.service 里跑时 BindsTo 它 (同生同死);
    stdin/stdout 走 FIFO, 由 systemd 按路径打开. 不用 --pipe: 它经 D-Bus 传 fd, kiosk 的 SELinux 域
    (unconfined_service_t) 建的管道 dbus-broker 写不了, 启动直接失败;
  - 非 root (开发机) 直接用当前 python 起, 只有崩溃隔离, 没有降权断网;
  - 子进程退出 / 超时 / 回了看不懂的东西: 杀掉收尸, 抛 OSError 给 ime_core, 它判"不可用"回英文.
"""

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import ime_core

OPEN_TIMEOUT = 600          # 首次部署 (编译词库) 可能好几分钟; 后台线程里等
KEY_TIMEOUT = 3.0           # 每个键; 超了就当坏了 (界面线程在等)
LINE_MAX = 64 * 1024
COMMIT_MAX, CAND_MAX, TEXT_MAX, PAGE_MAX = 64, 32, 64, 999      # 子进程回复清洗的上限
UNIT_PREFIX = "ops-cart-ime"
KILL_TIMEOUT = 3
STATE_DIR = "/var/lib/ops-cart-ime"
# 别改成 /run/ops-cart: FIFO 的标签继承目录, 由 systemd 以 init_t 打开. /run/ops-cart 是 var_run_t, 单元起不来
# (208/STDIN, denied { read } scontext=init_t tcontext=var_run_t tclass=fifo_file); /run/systemd 是 init_var_run_t
FIFO_ROOT = "/run/systemd"
_HERE = os.path.dirname(os.path.abspath(__file__))
_PASS_ENV = ("OPS_CART_LIBRIME", "OPS_CART_RIME_SHARED", "OPS_CART_RIME_SCHEMA")

_SANDBOX = ("DynamicUser=yes", "StateDirectory=ops-cart-ime", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
            "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes",
            "PrivateTmp=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
            "ProtectControlGroups=yes", "RestrictNamespaces=yes", "LockPersonality=yes",
            "ProtectKernelLogs=yes", "RestrictRealtime=yes", "ProtectProc=invisible", "ProtectClock=yes",
            "ProtectHostname=yes", "SystemCallFilter=@system-service", "MemoryDenyWriteExecute=yes", "UMask=0077",
            "InaccessiblePaths=-/etc/ops-cart -/var/lib/ops-cart -/var/log/ops-cart -/run/ops-cart -/root "
            "-/etc/shadow -/etc/gshadow -/opt/pxe/http -/opt/pxe/kiosk.conf",
            "MemoryMax=512M", "MemorySwapMax=0", "TasksMax=32", "CPUWeight=50", "CPUQuota=50%",
            "RuntimeMaxSec=900")
_unit_seq = [0]
_CTRL = {c: None for c in list(range(0x20)) + [0x7f]}
_SURROGATE = re.compile("[\ud800-\udfff]")       # json 能解出孤立代理字符, 插进 Tk 会出错


def _own_cgroup():
    try:
        with open("/proc/self/cgroup") as f:
            return f.read()
    except OSError:
        return ""


def _spawn(f):
    threading.Thread(target=f, daemon=True, name="ime-kill").start()


def _kill_unit(unit):
    """systemctl kill --signal=KILL <unit>, 放后台线程 (带超时), 不阻塞 Tk 主线程; 失败不管 (--collect 会收掉)."""
    exe = shutil.which("systemctl") or "systemctl"

    def run():
        try:
            subprocess.run([exe, "kill", "--signal=KILL", unit], timeout=KILL_TIMEOUT,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:       # noqa: BLE001
            pass
    _spawn(run)


def _clean(s, n):
    return _SURROGATE.sub("", str(s).translate(_CTRL))[:n]


def worker_cmd():
    """起子进程的命令; root 又没有 systemd-run (降不了权) -> None."""
    worker = os.path.join(_HERE, "ime_worker.py")
    if os.geteuid() != 0:
        return [sys.executable, "-I", worker]
    run = shutil.which("systemd-run")
    if not run:
        return None
    _unit_seq[0] += 1
    # 单元名确定: kill() 能 systemctl kill 它 (worker 在这个单元里).
    # Type=exec: systemd-run 等到 worker exec 成功才返回, 返回 0 时 systemd 已经打开了 FIFO 两端
    cmd = [run, "--quiet", "--collect", "--service-type=exec", f"--unit={UNIT_PREFIX}-{os.getpid()}-{_unit_seq[0]}"]
    props = list(_SANDBOX) + ["StandardError=journal"]
    if "/kiosk.service" in _own_cgroup():   # 同生同死; 不在 kiosk.service 里跑就不加 (否则会把它拉起来)
        props += ["BindsTo=kiosk.service", "After=kiosk.service"]
    for p in props:
        cmd += ["-p", p]
    cmd.append(f"--setenv=OPS_CART_RIME_USER={STATE_DIR}")
    cmd += [f"--setenv={k}={os.environ[k]}" for k in _PASS_ENV if os.environ.get(k)]
    return cmd + [sys.executable, "-I", worker]


class _FifoProc:
    """FIFO 接着的 worker, 接口同 ProcEngine 用到的那部分 Popen. systemd-run 早退出了, 存活只看 stdout 的 EOF."""

    EXIT_UNKNOWN = "未知"       # 退出码在单元里 (日志查), 这边拿不到

    def __init__(self, stdin_fd, stdout_fd):
        self.stdin = os.fdopen(stdin_fd, "wb")                  # O_NONBLOCK: 写满了抛错, 不卡住
        self.stdout = os.fdopen(stdout_fd, "rb", buffering=0)
        self.returncode = None

    def poll(self):
        if self.returncode is None and select.select([self.stdout], [], [], 0)[0]:
            # 一问一答, 闲着时可读只有两种: EOF, 或 worker 不按协议乱说话. 都当它没了
            if self.stdout.read(4096) is not None:
                self.returncode = self.EXIT_UNKNOWN
        return self.returncode

    def kill(self):             # 真正的杀在 ProcEngine.kill 的 _kill_unit; 关 FIFO 后 worker 也会读到 EOF
        self.returncode = self.EXIT_UNKNOWN

    def wait(self, timeout=None):
        return self.returncode


def _start_in_unit(cmd, timeout):
    """建 FIFO, systemd-run 起 worker -> _FifoProc. 失败抛 OSError / SubprocessError. 所有 open 都不阻塞.

    FIFO 单向打开要等另一端, 所以先 O_RDWR 占住两端 (Linux 上不阻塞), systemd 的单向打开就不会卡;
    systemd-run 返回 0 后 worker 持有自己那端, 再换成单向并放掉 O_RDWR: 不然自己占着对端, worker 死了也读不到 EOF.
    两端都开着 fd 之后路径就没用了, 目录当场删; mkdtemp 名字随机, kiosk 崩溃留下的旧目录挡不住下次.
    """
    d = tempfile.mkdtemp(prefix="ime-", dir=FIFO_ROOT)          # 0700
    held, ends = [], []
    try:
        inp, out = os.path.join(d, "in"), os.path.join(d, "out")
        for p in (inp, out):
            os.mkfifo(p, 0o600)
            held.append(os.open(p, os.O_RDWR | os.O_NONBLOCK))
        r = subprocess.run(cmd[:1] + ["-p", f"StandardInput=file:{inp}", "-p", f"StandardOutput=file:{out}"] + cmd[1:],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout, cwd="/")
        if r.returncode != 0:
            raise OSError(f"systemd-run 失败 ({r.returncode}): {_clean(r.stderr.decode(errors='replace'), 200)}")
        ends.append(os.open(inp, os.O_WRONLY | os.O_NONBLOCK))
        ends.append(os.open(out, os.O_RDONLY | os.O_NONBLOCK))
        return _FifoProc(*ends)
    except BaseException:
        for fd in ends:
            os.close(fd)
        raise
    finally:
        for fd in held:
            os.close(fd)
        shutil.rmtree(d, ignore_errors=True)


class ProcEngine:
    """ime_core 要的引擎接口, 每个调用一来一回."""

    def __init__(self, proc, key_timeout=KEY_TIMEOUT, unit=None):
        self.proc, self.key_timeout, self.unit = proc, key_timeout, unit
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
        except Exception:       # noqa: BLE001 -- ValueError / RecursionError (层层嵌套) 一样当坏了
            self._fail("回复看不懂")
        if not resp.get("ok"):
            raise OSError(resp.get("why") or "中文输入子进程报错")
        return resp

    def _op(self, **req):
        resp = self.call(req)
        try:        # 子进程回的东西当不可信数据: 去控制字符、限长、数字夹到范围里
            raw, pre, cands, page_no, last, hi = resp["snap"]
            cands = [_clean(c, CAND_MAX) for c in list(cands)[:9]]
            self._snap = ime_core.Snapshot(_clean(raw, TEXT_MAX), _clean(pre, TEXT_MAX), cands,
                                           min(max(int(page_no), 0), PAGE_MAX), bool(last),
                                           min(max(int(hi), 0), max(len(cands) - 1, 0)))
            c = resp.get("commit")
            c = _clean(c, COMMIT_MAX) if isinstance(c, str) else None
        except Exception:       # noqa: BLE001 -- 含 OverflowError (inf)、ValueError (nan)
            self._fail("回复看不懂")
        self._commit = c or None
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

    def alive(self):
        return self.proc.poll() is None

    def take_commit(self):
        c, self._commit = self._commit, None
        return c

    def kill(self):
        if self.unit:               # root 下: 杀 worker 所在的单元 (只一次; 客户端死了单元也可能还在)
            unit, self.unit = self.unit, None
            _kill_unit(unit)
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
    in_unit = cmd is None and os.geteuid() == 0
    cmd = cmd or worker_cmd()
    if cmd is None:
        return None, "中文输入不可用: root 下没有 systemd-run, 不降权就不起 librime 子进程"
    unit = next((a.split("=", 1)[1] for a in cmd if a.startswith("--unit=")), None)
    try:
        if in_unit:
            proc = _start_in_unit(cmd, open_timeout)
        else:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd="/")
    except subprocess.TimeoutExpired:
        _kill_unit(unit)        # 客户端超时了, 作业可能还在排队
        return None, f"中文输入不可用: systemd-run {open_timeout:g} 秒没回 (超时)"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"中文输入不可用: 起不了子进程: {e}"
    eng = ProcEngine(proc, key_timeout, unit=unit)
    try:
        eng.call({"op": "open"}, timeout=open_timeout)
    except Exception as e:      # noqa: BLE001 -- OSError 之外 (RecursionError 等) 也杀掉, 不留子进程
        eng.kill()
        why = str(e) if isinstance(e, OSError) else f"{type(e).__name__}: {e}"
        return None, why if why.startswith("中文输入不可用") else f"中文输入不可用: {why}"
    return eng, ""
