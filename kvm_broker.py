#!/usr/bin/env python3
"""kvm_broker: 键鼠 (CH9329) 常驻代理 —— 像 conbroker 管串口那样统一持有 /dev/ttyACM*, 车机 / 网页口 / 命令行都经它发键 (#9).

看和写分开: 预览只占画面; 键鼠由这里一个进程打开, 谁要发键就连本地 Unix socket 说一声 (协议和仲裁规则见 kvm_broker_core).
  python3 kvm_broker.py [--sock /run/ops-kvm/kvm.sock] [--log /var/log/ops-cart/kvm.log]

客户端: open_ch(port, src, who) 优先连代理, 代理不在 (socket 不存在 / 没人听) 就退回直开 + flock, 和以前一样.
  BrokerCh 跟 ch9329.Ch9329 同一套方法 (send_report / send_mouse / send_mouse_abs / send_keys / type_text / info / close),
  调用方不用改; 出错也映射回原来的异常: 被占用 / 排队超时 = Ch9329Busy, 拔线 = Ch9329Gone, 急停 / 无租约 = KvmStopped.

线程: 每个连接一个线程 (提交请求后等结果); 每块键鼠一个发送线程 (只有它碰这块设备). 共享状态都在 self.cv 下.
"""

import argparse
import grp
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time

import ch9329
import kvm_broker_core as kbc

SOCK = "/run/ops-kvm/kvm.sock"
LOG = "/var/log/ops-cart/kvm.log"
GROUP = "dialout"           # socket 给哪个组: 能直接写 ttyACM 的人, 才能经代理发键
CLIENT_TIMEOUT_S = kbc.AGENT_WAIT_MAX_S + kbc.HOLD_MAX_MS / 1000.0 + 10

MSG = {"busy": "键鼠被别的进程直接占着", "gone": "键鼠断开 (拔线?)", "timeout": "人一直在操作, 排队超时",
       "estop": "急停中, agent 不能发键", "lease": "没有写租约", "human_only": "解除急停要人来",
       "bad": "请求不对"}


class KvmStopped(OSError):
    """急停中 / 租约无效 / 要人才能做的操作: agent 这边该停手, 不是设备坏了."""


def default_sock():
    return os.environ.get("OPS_KVM_SOCK") or SOCK


def _peer_uid(sock):
    """对端 uid (Linux SO_PEERCRED); 记日志用 —— who 是对方自报的, uid 才是真的. 取不到返回 None."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
        return uid
    except OSError:
        return None


class Broker:
    def __init__(self, sock_path=SOCK, log_path=LOG, opener=None, quiet_s=kbc.QUIET_S,
                 wait_max_s=kbc.AGENT_WAIT_MAX_S, realpath=os.path.realpath, group=GROUP,
                 clock=time.monotonic):
        self.opener = opener or (lambda dev: ch9329.Ch9329(dev, log=lambda *a: None))
        self.realpath, self.clock, self.port_ok = realpath, clock, kbc.port_ok
        self.arb = kbc.Arbiter(quiet_s, wait_max_s)
        self.bursts = kbc.HumanBursts(quiet_s)
        self.idle_wait = min(1.0, quiet_s / 2)
        self.cv = threading.Condition()
        self.devs, self.workers, self.events = {}, {}, {}
        self.running = True
        self.log_f = None
        if log_path:
            try:
                self.log_f = open(log_path, "a", buffering=1)
            except OSError as e:
                print(f"日志打不开, 只进 journald: {e}", file=sys.stderr)
        self.srv = self._listen(sock_path, group)
        self.log(kbc.record("start", sock=sock_path))

    # -------------------------------------------------------- socket

    def _listen(self, path, group):
        """已有代理在听就退出; 残留的 socket 文件删掉重建. 权限 0660 + 组 (默认 dialout)."""
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(path)
                raise SystemExit(f"已有键鼠代理在 {path}")
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(path)
            finally:
                probe.close()
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                uid = _peer_uid(self.request)
                while broker.running:
                    line = self.rfile.readline(kbc.LINE_MAX + 1)
                    if not line:
                        return
                    if len(line) > kbc.LINE_MAX:
                        resp = {"ok": False, "err": "bad", "msg": "请求太长"}
                    else:
                        resp = broker.handle(line, uid)
                    try:
                        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
                        self.wfile.flush()
                    except OSError:
                        return
                    if len(line) > kbc.LINE_MAX:
                        return

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        old = os.umask(0o117)               # bind 出来就是 0660, 不留 0777 的窗口
        try:
            srv = Server(path, Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o660)
        if group:
            try:
                os.chown(path, -1, grp.getgrnam(group).gr_gid)
            except (KeyError, OSError) as e:
                print(f"socket 改组 {group} 失败, 只有属主能连: {e}", file=sys.stderr)
        return srv

    def serve_forever(self):
        self.srv.serve_forever(poll_interval=0.2)

    def shutdown(self):
        self.running = False
        self.srv.shutdown()
        self.srv.server_close()
        with self.cv:
            self.cv.notify_all()
        self.log(kbc.record("stop"))

    # -------------------------------------------------------- 日志

    def log(self, rec):
        line = kbc.log_line(rec)
        if self.log_f is not None:
            try:
                self.log_f.write(line + "\n")
                return
            except OSError:
                pass
        print(line, flush=True)

    def _flush_bursts(self, now):
        for dev, who, n in self.bursts.ended(now):
            self.log(kbc.record("human_end", src="human", who=who, dev=dev.rsplit("/", 1)[-1], n=n))

    # -------------------------------------------------------- 请求

    def handle(self, line, uid=None):
        """一行请求 -> 应答 dict. 发键请求会一直等到发完 / 被丢."""
        try:
            r = kbc.parse_request(line)
        except kbc.BadRequest as e:
            return {"ok": False, "err": "bad", "msg": str(e)}
        if r["op"] in kbc.CONTROL_OPS:
            return self._control(r, uid)
        dev = self.realpath(r["port"])
        if not self.port_ok(dev):
            return {"ok": False, "err": "bad", "msg": f"不是键鼠串口: {r['port']}"}
        try:
            job = kbc.Job(r, kbc.plan(r), self.clock(), dev=dev)
        except kbc.BadRequest as e:
            return {"ok": False, "err": "bad", "msg": str(e)}
        ev = threading.Event()
        with self.cv:
            now = self.clock()
            try:
                self.arb.submit(job, now)
            except kbc.Refused as e:
                job.state, job.why = "dropped", e.code
                self.log(kbc.record("refused", job, uid=uid))
                return {"ok": False, "err": e.code, "msg": str(e)}
            self.events[job.id] = ev
            if job.src == "human" and job.active and self.bursts.note(dev, job.who, now):
                self.log(kbc.record("human_start", src="human", who=job.who, dev=dev.rsplit("/", 1)[-1]))
            self._worker(dev)
            self.cv.notify_all()
        if not ev.wait(CLIENT_TIMEOUT_S):
            with self.cv:
                if job.state != "done":
                    self.arb.fail(job, "timeout")
                self.events.pop(job.id, None)
        if job.src == "agent" and not job.passive:
            self.log(kbc.record("sent" if job.state == "done" else "dropped", job, uid=uid))
        elif job.state == "dropped":
            self.log(kbc.record("dropped", job, uid=uid))
        if job.state == "done":
            return {"ok": True, "reply": job.reply} if job.reply is not None else {"ok": True}
        return {"ok": False, "err": job.why, "msg": MSG.get(job.why, job.why), "sent": job.i}

    def _control(self, r, uid):
        dev = self.realpath(r["port"]) if r.get("port") else None
        with self.cv:
            if r["op"] == "status":
                return {"ok": True, "status": self.arb.status(self.clock())}
            if r["op"] == "resume" and r["src"] != "human":
                return {"ok": False, "err": "human_only", "msg": MSG["human_only"]}
            if r["op"] == "estop":
                dropped = self.arb.estop(dev)
                for j in dropped:
                    self._finish(j)
                extra = {"dropped": len(dropped)}
            else:
                self.arb.resume(dev)
                extra = {}
            self.cv.notify_all()
        self.log(kbc.record(r["op"], src=r["src"], who=r["who"], dev=(dev or "*").rsplit("/", 1)[-1],
                            uid=uid, **extra))
        return dict(ok=True, **extra)

    def _finish(self, job):
        ev = self.events.pop(job.id, None)
        if ev is not None:
            ev.set()

    # -------------------------------------------------------- 发送线程

    def _worker(self, dev):
        """每块键鼠一个发送线程 (调用方持 cv)."""
        if dev not in self.workers:
            t = threading.Thread(target=self._loop, args=(dev,), daemon=True, name=f"kvm-{dev}")
            self.workers[dev] = t
            t.start()

    def _next(self, dev):
        with self.cv:
            while self.running:
                now = self.clock()
                for j in self.arb.expire(now):
                    self._finish(j)
                self._flush_bursts(now)
                got = self.arb.take(dev, now)
                if got:
                    return got
                hint = self.arb.wait_hint(dev, now)
                self.cv.wait(self.idle_wait if hint is None else min(max(hint, 0.005), self.idle_wait))
        return None

    def _loop(self, dev):
        while True:
            got = self._next(dev)
            if got is None:
                return
            job, unit = got
            try:
                reply = self._run(dev, unit)
            except ch9329.Ch9329Busy:
                why = "busy"
            except OSError:
                self._drop_dev(dev)
                why = "gone"
            except ValueError:
                why = "bad"
            else:
                why = None
            with self.cv:
                if why:
                    self.arb.fail(job, why)
                    self._finish(job)
                elif self.arb.done(job, self.clock(), reply):
                    self._finish(job)
                self.cv.notify_all()

    def _drop_dev(self, dev):
        d = self.devs.pop(dev, None)
        if d is not None:
            try:
                d.close()
            except OSError:
                pass

    def _run(self, dev, unit):
        """在这块键鼠的发送线程里发一步. 设备第一次用才开, 开了就一直拿着 (统一持有), 拔线才关."""
        d = self.devs.get(dev)
        if d is None:
            d = self.devs[dev] = self.opener(dev)
        reply = None
        for step in unit:
            kind = step[0]
            if kind == "info":
                reply = d.info()
            elif kind == "report":
                d.send_report(step[1])
            elif kind == "wait":
                time.sleep(step[1])
            elif kind == "mouse":
                d.send_mouse(*step[1:])
            elif kind == "mouse_abs":
                d.send_mouse_abs(*step[1:])
        return reply


# ------------------------------------------------------------ 客户端


class BrokerCh:
    """经代理发键, 方法同 ch9329.Ch9329. 连上就发一次 open: 被直接占着 / 拔了 / 口不对当场报, 跟直开一样."""

    def __init__(self, port, src="agent", who="cli", sock_path=None, timeout=CLIENT_TIMEOUT_S):
        self.port, self.src, self.who = port, src, who
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(sock_path or default_sock())
            self.f = self.sock.makefile("rwb")
            self._call("open")
        except BaseException:
            self.sock.close()
            raise

    def _call(self, op, **kw):
        req = {"op": op, "src": self.src, "who": self.who}
        if self.port is not None:
            req["port"] = self.port
        req.update(kw)
        try:
            self.f.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
            self.f.flush()
            line = self.f.readline()
        except (OSError, ValueError) as e:
            raise ch9329.Ch9329Gone(f"键鼠代理连接出错: {e}") from None
        if not line:
            raise ch9329.Ch9329Gone("键鼠代理断开")
        resp = json.loads(line)
        if resp.get("ok"):
            return resp
        err, msg = resp.get("err"), resp.get("msg") or resp.get("err")
        if err in ("busy", "timeout"):
            raise ch9329.Ch9329Busy(f"键鼠被占用: {msg}")
        if err == "gone":
            raise ch9329.Ch9329Gone(msg)
        if err in ("estop", "lease", "human_only"):
            raise KvmStopped(msg)
        raise ValueError(msg)

    def send_report(self, report):
        self._call("report", report=bytes(report).hex())

    def send_mouse(self, buttons=0, dx=0, dy=0, wheel=0):
        self._call("mouse", b=int(buttons), dx=int(dx), dy=int(dy), w=int(wheel))

    def send_mouse_abs(self, buttons=0, x=0, y=0, wheel=0):
        self._call("mouse_abs", b=int(buttons), x=int(x), y=int(y), w=int(wheel))

    def send_keys(self, key_names, press_ms=kbc.PRESS_MS):
        self._call("keys", keys=list(key_names), press_ms=int(press_ms))

    def type_text(self, text, press_ms=kbc.PRESS_MS, secret=False):
        bad = ch9329.untypable(text)
        if bad:                              # 同直开: 先整串校验, 有打不出的就一个都不发
            raise ValueError(f"无法输入的字符: {''.join(bad)!r}")
        if text:
            self._call("type", text=text, press_ms=int(press_ms), secret=bool(secret))

    def info(self):
        return self._call("info").get("reply") or ""

    def estop(self, all_devs=False):
        return self._control("estop", all_devs)

    def resume(self, all_devs=False):
        return self._control("resume", all_devs)

    def status(self):
        return self._control("status", True)["status"]

    def _control(self, op, all_devs):
        port, self.port = self.port, None if all_devs else self.port
        try:
            return self._call(op)
        finally:
            self.port = port

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def open_ch(port, src="agent", who="cli", baud=115200, sock_path=None):
    """优先经代理; 代理不在 (没 socket / 没人听) 退回直开 + flock (旧用法不变). 代理在但报错就照报, 不再直开."""
    try:
        return BrokerCh(port, src=src, who=who, sock_path=sock_path)
    except (FileNotFoundError, ConnectionRefusedError):
        return ch9329.Ch9329(port, baud, log=lambda *a: None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="键鼠 (CH9329) 常驻代理")
    ap.add_argument("--sock", default=default_sock())
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--group", default=GROUP)
    ap.add_argument("--quiet", type=float, default=kbc.QUIET_S, help="人停手多少秒 agent 才能发")
    args = ap.parse_args(argv)
    b = Broker(args.sock, args.log, quiet_s=args.quiet, group=args.group or None)
    import signal
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=b.shutdown).start())
    try:
        b.serve_forever()
    except KeyboardInterrupt:
        b.shutdown()


if __name__ == "__main__":
    main()
