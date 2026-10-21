#!/usr/bin/env python3
"""网页版 Kiosk: 标准库 http.server, 浏览器远程操作推车.

路由在 App.handle 里 (纯方法, 可离线测); Handler 只做 socket 转发.
来源过滤 + PIN 登录见 web_core. 用法: python3 webkiosk.py [--port 8088]
"""

import argparse
import base64
import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ch9329
import kiosk_core as kc
import serial_panel as sp
import web_core as wc

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


def _json(status, obj, extra=None):
    hdrs = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    hdrs.update(extra or {})
    return status, hdrs, json.dumps(obj, ensure_ascii=False).encode()


def _real_ch_open():
    """CH9329 按 by-id 找口; 打不开返回 None."""
    try:
        ch = ch9329.Ch9329(os.path.realpath(kc.ch9329_port()), log=lambda *a: None)
    except OSError:
        return None
    ch.send_report = lambda r: ch._send_raw(ch9329.build_frame(ch9329.CMD_SEND_DATA, r))
    return ch


def _real_serial_dir():
    return sp.data_dir(sp.serial_conf_read())


class App:
    def __init__(self, conf_path=wc.WEB_CONF, pin_path=None, status_src=None,
                 selftest=None, static_dir=STATIC_DIR):
        self.conf_path, self.pin_path, self.static_dir = conf_path, pin_path, str(static_dir)
        self.status_src = status_src or wc.default_status_sources()
        self.selftest = selftest or (lambda: kc.selftest_run(kc.selftest_plan_read(),
                                                             kc.selftest_context()))
        self.sessions = wc.Sessions()
        self._gate = None
        self.ch_open, self._ch, self._ch_lock = _real_ch_open, None, threading.Lock()
        self.kbd = wc.KeyboardState()
        self.video_root = "/dev"
        self.serial_dir = _real_serial_dir
        self.serial_socks = {}          # 会话令牌 -> conbroker socket

    # -------------------------------------------------------- 鉴权

    def gate(self):
        """PIN 可能被 kiosk-setpin 改过: 哈希变了就换一个新闸门 (冷却计数随之重置)."""
        stored = kc.pin_conf_read(self.pin_path)
        if self._gate is None or self._gate.stored != stored:
            self._gate = kc.PinGate(stored)
        return self._gate

    def source_ok(self, ip):
        return wc.client_allowed(ip, wc.web_conf_read(self.conf_path)["lan_open"],
                                 self.gate().required)

    def authed(self, headers):
        return self.sessions.check(wc.cookie_token(headers.get("Cookie")))

    # -------------------------------------------------------- 路由

    def handle(self, method, path, ip, headers, body):
        if not self.source_ok(ip):
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, "未开放局域网访问".encode()
        path = path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else (405, {}, b"")
        try:
            req = json.loads(body) if body else {}
        except ValueError:
            return _json(400, {"error": "bad json"})
        if (method, path) == ("GET", "/api/whoami"):
            return _json(200, {"authed": self.authed(headers), "kind": wc.client_kind(ip),
                               "pin_required": self.gate().required,
                               "lan_open": wc.web_conf_read(self.conf_path)["lan_open"]})
        if (method, path) == ("POST", "/api/login"):
            return self._login(req)
        if not self.authed(headers):
            return _json(401, {"error": "login required"})
        if (method, path) == ("POST", "/api/logout"):
            self.sessions.revoke(wc.cookie_token(headers.get("Cookie")))
            return _json(200, {"ok": True})
        if (method, path) == ("GET", "/api/status"):
            return _json(200, wc.status_payload(self.status_src))
        if (method, path) == ("GET", "/api/selftest"):
            return _json(200, {"rows": self.selftest()})
        if (method, path) == ("POST", "/api/hid"):
            return self._hid(req.get("events") or [])
        if (method, path) == ("POST", "/api/type"):
            return self._type(str(req.get("text", "")))
        if (method, path) == ("GET", "/api/videos"):
            devs = sorted(n for n in os.listdir(self.video_root) if wc.video_device(n, self.video_root))
            return _json(200, {"devices": devs})
        if (method, path) == ("GET", "/api/serial/ports"):
            return _json(200, {"ports": [p["name"] for p in wc.serial_ports(self.serial_dir())]})
        if (method, path) == ("POST", "/api/serial/send"):
            sock = self.serial_socks.get(wc.cookie_token(headers.get("Cookie")))
            if sock is None:
                return _json(409, {"error": "串口没连上"})
            try:
                sock.sendall(base64.b64decode(req.get("data", "")))
            except (OSError, ValueError) as e:
                return _json(409, {"error": str(e)})
            return _json(200, {"ok": True})
        if (method, path) == ("POST", "/api/lan"):
            if wc.client_kind(ip) != "trusted":      # 局域网这头不许自己改开关
                return _json(403, {"error": "只能从 tailnet 或本机切换"})
            wc.web_conf_set(self.conf_path, lan_open=bool(req.get("open")))
            return _json(200, {"lan_open": wc.web_conf_read(self.conf_path)["lan_open"]})
        return _json(404, {"error": "no such api"})

    # -------------------------------------------------------- 键鼠

    def _with_ch(self, fn):
        """拿 CH9329 发一串报告; 线被拔就丢句柄, 下次请求重开."""
        with self._ch_lock:
            if self._ch is None:
                self._ch = self.ch_open()
            if self._ch is None:
                return _json(503, {"error": "CH9329 未连接"})
            try:
                fn(self._ch)
            except (OSError, ValueError) as e:
                self._ch = None
                self.kbd.release_all()
                return _json(503, {"error": f"CH9329 发送失败: {e}"})
            return _json(200, {"ok": True})

    def _hid(self, events):
        def send(ch):
            for ev in events:
                t = ev.get("t")
                if t == "k":
                    rep = self.kbd.apply(str(ev.get("code")), bool(ev.get("down")))
                    if rep is not None:
                        ch.send_report(rep)
                elif t == "m":
                    b = wc.mouse_buttons(ev.get("b", 0))
                    for dx, dy, w in wc.mouse_chunks(ev.get("dx", 0), ev.get("dy", 0), ev.get("w", 0)):
                        ch.send_mouse(b, dx, dy, w)
                elif t == "release":           # 浏览器失焦 / 退出透传: 全部抬起
                    ch.send_report(self.kbd.release_all())
                    ch.send_mouse(0, 0, 0, 0)
        return self._with_ch(send)

    def _type(self, text):
        """粘贴即打字 (US 布局); 打不出的字符跳过."""
        def run(ch):
            for c in text:
                rep, unknown = ch9329.key_report(["enter"] if c == "\n" else [c])
                if unknown:
                    continue
                ch.send_report(rep)
                ch.send_report(bytes(8))
            self.kbd.release_all()
        return self._with_ch(run)

    # -------------------------------------------------------- 流 (Handler 调)

    def open_video(self, name, fps=15):
        dev = wc.video_device(name, self.video_root)
        if dev is None:
            return None
        return subprocess.Popen(wc.video_cmd(dev, fps=fps), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    def open_serial(self, tok, name):
        port = next((p["port"] for p in wc.serial_ports(self.serial_dir()) if p["name"] == name), None)
        if port is None:
            return None
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.settimeout(None)
        s.sendall(b"HELLO human\n")
        old = self.serial_socks.pop(tok, None)
        if old:
            old.close()
        self.serial_socks[tok] = s
        return s

    def _login(self, req):
        result = self.gate().attempt(str(req.get("pin", "")))
        if result != "ok":
            return _json(200, {"result": result})
        tok = self.sessions.issue()
        return _json(200, {"result": "ok"},
                     {"Set-Cookie": f"{wc.COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict"})

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(self.static_dir, name))
        if not full.startswith(os.path.realpath(self.static_dir) + os.sep) or not os.path.isfile(full):
            return 404, {"Content-Type": "text/plain"}, b"not found"
        ctype = _TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            return 200, {"Content-Type": ctype, "Cache-Control": "no-cache"}, f.read()


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "ops-cart"

    def _dispatch(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        status, hdrs, out = self.app.handle(method, self.path, self.client_address[0],
                                            self.headers, body)
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/api/video", "/api/serial/stream"):
            return self._stream(path, dict(p.partition("=")[::2] for p in query.split("&") if p))
        self._dispatch("GET")

    # 流式接口: 长连接, 不走 App.handle 的一次性返回

    def _stream(self, path, q):
        app = self.app
        if not app.source_ok(self.client_address[0]):
            return self.send_error(403)
        if not app.authed(self.headers):
            return self.send_error(401)
        if path == "/api/video":
            return self._video(q.get("dev", ""), int(q.get("fps") or 15))
        return self._serial(wc.cookie_token(self.headers.get("Cookie")), q.get("name", ""))

    def _video(self, dev, fps):
        proc = self.app.open_video(dev, fps=max(1, min(30, fps)))
        if proc is None:
            return self.send_error(404, "no such video device")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.wait()

    def _serial(self, tok, name):
        try:
            s = self.app.open_serial(tok, name)
        except OSError as e:
            return self.send_error(502, f"conbroker: {e}")
        if s is None:
            return self.send_error(404, "no running broker")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            s.sendall(b"\r")
            while True:
                data = s.recv(4096)
                if not data:
                    break
                self.wfile.write(b"data: " + base64.b64encode(data) + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            if self.app.serial_socks.get(tok) is s:
                del self.app.serial_socks[tok]
            s.close()

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="ops-cart 网页版 Kiosk")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    Handler.app = App()
    port = a.port or wc.web_conf_read()["port"]
    print(f"webkiosk on {a.bind}:{port}", flush=True)
    Server((a.bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
