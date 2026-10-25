#!/usr/bin/env python3
"""网页版 Kiosk: 标准库 http.server, 浏览器远程操作推车 (只做远程管理, 车机屏幕永远是 Tk).

路由在 App.handle 里 (纯方法, 可离线测); Handler 只做 socket 转发.
来源过滤 + PIN 登录见 web_core. 用法: python3 webkiosk.py [--port 8088] [--bind 地址]

安全约定:
- 装机口 (LAN1) 网段来的连接一律拒绝, 开了局域网开关也一样
- 会话绑定签发时的 PIN; PIN 一改, 退出登录 / 关局域网开关, 对应的会话和长连接一起断
- 键鼠只发给当前画面源绑定的那块 CH9329 (kvm_core), 没绑就只能看
- Host 白名单 + POST 同源, 都在读请求体之前查
- 审计: 登录 / 退出 / 开关 / 打字 (只记长度) / 画面和串口的开关 (只记字节数) 写 cartlog, PIN 不进日志
"""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cartlog
import ch9329
import kiosk_core as kc
import kvm_core as kvm
import serial_panel as sp
import web_core as wc

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}

BODY_MAX = 64 * 1024        # 请求体上限; 粘贴打字也够用
TYPE_MAX = 4096             # /api/type 一次最多几个字符 (115200 波特下约 10 秒)
TYPE_CHUNK = 32             # 打字分块发, 块间放开键鼠锁, 别的键鼠请求插得进来
STREAM_RECHECK_S = 2.0      # 长连接多久复查一次来源和会话
LAN_MAX_LOCK_S = 3600       # 局域网输错冷却: 60 秒起翻倍, 封顶 1 小时
GATES_MAX = 1024            # 按来源 IP 记的输错闸门上限


def _json(status, obj, extra=None):
    hdrs = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    hdrs.update(extra or {})
    return status, hdrs, json.dumps(obj, ensure_ascii=False).encode()


def _text(status, msg):
    return status, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}, msg.encode()


def _real_ch_open(path):
    """按绑定解析出的 by-id 路径打开 CH9329; 打不开返回 None."""
    try:
        return ch9329.Ch9329(os.path.realpath(path), log=lambda *a: None)
    except OSError:                     # 含被占用 (Ch9329Busy) 和打不开
        return None


def _real_serial_dir():
    return sp.data_dir(sp.serial_conf_read())


class App:
    def __init__(self, conf_path=wc.WEB_CONF, pin_path=None, status_src=None,
                 selftest=None, static_dir=STATIC_DIR, pxe_env=kc.PXE_ENV):
        self.conf_path, self.static_dir, self.pxe_env = conf_path, str(static_dir), pxe_env
        self.pin_path = pin_path or kc.KIOSK_CONF
        self.status_src = status_src or wc.default_status_sources()
        self.selftest = selftest or (lambda: kc.selftest_run(kc.selftest_plan_read(),
                                                             kc.selftest_context()))
        self.sessions = wc.Sessions()
        self.audit = lambda kind, msg: cartlog.event(kind, msg)
        self._state_lock = threading.Lock()
        self._pin_tag, self._gates = None, {}
        # 键鼠
        self.ch_open, self._ch, self._ch_path, self._ch_lock = _real_ch_open, None, None, threading.Lock()
        self.kbd = wc.KeyboardState()
        self.video_nodes, self.kvm_load, self.kvm_ports = kc.video_nodes, kvm.load, kvm.ports
        # 画面 / 串口 / 长连接
        self.popen = subprocess.Popen
        self._videos, self._video_lock = {}, threading.Lock()
        self.serial_dir = _real_serial_dir
        self.serial_socks = {}          # (会话令牌, 连接序号) -> conbroker socket; 每个标签页各一条
        self._send_locks = {}           # 同上 -> 发送锁 (同一条连接的按键按顺序进串口)
        self._serial_tx = {}            # 同上 -> 发出去的字节数 (审计用)
        self._streams = {}              # 会话令牌 -> {关闭函数}

    # -------------------------------------------------------- 鉴权

    def pin(self):
        """-> (状态, 哈希). PIN 被 kiosk-setpin 改过 (含读不到): 会话全作废, 闸门重建, 长连接断."""
        state, stored = wc.pin_state(self.pin_path)
        with self._state_lock:
            changed = self._pin_tag is not None and self._pin_tag != (state, stored)
            if self._pin_tag != (state, stored):
                self._pin_tag, self._gates = (state, stored), {}
        if changed:
            self._close_streams(self.sessions.revoke_all())
        return state, stored

    def gate_for(self, ip):
        """输错冷却按来源 IP 分开: 一台机器试错锁不到别人, 更锁不到 tailnet; 局域网冷却指数退避."""
        _state, stored = self.pin()
        kind = wc.client_kind(ip)
        with self._state_lock:
            g = self._gates.get(ip)
            if g is None:
                if len(self._gates) >= GATES_MAX:       # 防内存涨: 满了从头记
                    self._gates = {}
                g = self._gates[ip] = kc.PinGate(
                    stored, lock_s=60, max_lock_s=LAN_MAX_LOCK_S if kind == "lan" else None)
        return g

    def request_reason(self, method, headers):
        hosts = list(wc.web_conf_read(self.conf_path).get("hosts") or []) + [socket.gethostname()]
        return wc.request_reason(method, headers, hosts)

    def deny_nets(self, conf=None):
        """装机口网段 (pxe.env) + web.conf 里额外写的; 这些来源一律不给管理口."""
        conf = conf or wc.web_conf_read(self.conf_path)
        env = kc.pxe_env_read(self.pxe_env)
        extra = conf.get("deny_nets")
        return [f"{env['PXE_IP']}/{env['PXE_PREFIX']}"] + (list(extra) if isinstance(extra, list) else [])

    def source_reason(self, ip, local_ip=None):
        conf = wc.web_conf_read(self.conf_path)
        return wc.source_reason(ip, bool(conf.get("lan_open")), self.pin()[0] == "set",
                                self.deny_nets(conf), local_ip)

    def _session_ok(self, tok):
        state, stored = self.pin()
        return state != "unreadable" and self.sessions.check(tok, pin_tag=stored)

    def authed(self, headers):
        return self._session_ok(wc.cookie_token(headers.get("Cookie")))

    def stream_ok(self, ip, tok, local_ip=None):
        return self.source_reason(ip, local_ip) is None and self._session_ok(tok)

    def log(self, act, ip, tok=None, **kv):
        """审计一行; 日志坏了不影响服务."""
        try:
            self.audit("web", cartlog.fields(act=act, src=ip, kind=wc.client_kind(ip),
                                             sid=wc.session_tag(tok), **kv))
        except Exception:
            pass

    # -------------------------------------------------------- 长连接登记

    def stream_register(self, tok, closer):
        with self._state_lock:
            self._streams.setdefault(tok, set()).add(closer)

        def unregister():
            with self._state_lock:
                self._streams.get(tok, set()).discard(closer)
        return unregister

    def _close_streams(self, toks):
        for tok in toks:
            with self._state_lock:
                closers = self._streams.pop(tok, set())
            for c in closers:
                try:
                    c()
                except OSError:
                    pass

    # -------------------------------------------------------- 路由

    def handle(self, method, path, ip, headers, body, local_ip=None):
        reason = self.source_reason(ip, local_ip) or self.request_reason(method, headers)
        if reason:
            return _text(403, reason)
        path = path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else (405, {}, b"")
        try:
            req = json.loads(body) if body else {}
        except ValueError:
            return _json(400, {"error": "bad json"})
        if not isinstance(req, dict):
            return _json(400, {"error": "请求体必须是 JSON 对象"})
        tok = wc.cookie_token(headers.get("Cookie"))
        if (method, path) == ("GET", "/api/whoami"):
            state, _ = self.pin()
            return _json(200, {"authed": self.authed(headers), "kind": wc.client_kind(ip),
                               "pin_required": state != "unset", "pin_state": state,
                               "lan_open": bool(wc.web_conf_read(self.conf_path).get("lan_open"))})
        if (method, path) == ("POST", "/api/login"):
            return self._login(req, ip)
        if not self.authed(headers):
            return _json(401, {"error": "login required"})
        if (method, path) == ("POST", "/api/logout"):
            self.sessions.revoke(tok)
            self._close_streams([tok])
            self.log("logout", ip, tok)
            return _json(200, {"ok": True})
        if (method, path) == ("GET", "/api/status"):
            return _json(200, wc.status_payload(self.status_src))
        if (method, path) == ("GET", "/api/selftest"):
            return _json(200, {"rows": self.selftest()})
        if (method, path) == ("POST", "/api/hid"):
            return self._hid(req)
        if (method, path) == ("POST", "/api/type"):
            return self._type(req, ip, tok)
        if (method, path) == ("GET", "/api/videos"):
            return _json(200, {"devices": self._video_list()})
        if (method, path) == ("GET", "/api/serial/ports"):
            return _json(200, {"ports": [p["name"] for p in wc.serial_ports(self.serial_dir())]})
        if (method, path) == ("POST", "/api/serial/send"):
            return self._serial_send(tok, req.get("cid"), req.get("data"))
        if (method, path) == ("POST", "/api/lan"):
            return self._lan(ip, tok, req.get("open"))
        return _json(404, {"error": "no such api"})

    def _login(self, req, ip):
        pin = req.get("pin", "")
        if not isinstance(pin, str):
            return _json(400, {"error": "pin 必须是字符串"})
        state, stored = self.pin()
        if state == "unreadable":           # 读不到不等于没设: 谁都不放
            self.log("login", ip, result="unavailable")
            return _json(200, {"result": "unavailable"})
        result = self.gate_for(ip).attempt(pin)
        if result != "ok":
            self.log("login", ip, result=result)
            return _json(200, {"result": result})
        tok = self.sessions.issue(wc.client_kind(ip), pin_tag=stored)
        self.log("login", ip, tok, result="ok")
        return _json(200, {"result": "ok"},
                     {"Set-Cookie": f"{wc.COOKIE}={tok}; Path=/; HttpOnly; SameSite=Strict"})

    def _lan(self, ip, tok, want):
        if wc.client_kind(ip) != "trusted":      # 局域网这头不许自己改开关
            return _json(403, {"error": "只能从 tailnet 或本机切换"})
        if not isinstance(want, bool):
            return _json(400, {"error": "open 必须是 true / false"})
        wc.web_conf_set(self.conf_path, lan_open=want)
        self.log("lan", ip, tok, open=want)
        if not want:                             # 关掉 = 局域网的会话和长连接立刻作废
            self._close_streams(self.sessions.revoke_kind("lan"))
        return _json(200, {"lan_open": bool(wc.web_conf_read(self.conf_path).get("lan_open"))})

    # -------------------------------------------------------- 键鼠 (按画面源绑定)

    def _src_node(self, src):
        if not isinstance(src, str):
            return None
        return next((n for n in self.video_nodes() if os.path.basename(n["dev"]) == src), None)

    def _kvm_port(self, src):
        """-> (串口路径, None) 或 (None, 给人看的原因). 绝不退回"随便一块"."""
        node = self._src_node(src)
        if node is None:
            return None, "先选一个画面源"
        path, _serial, state = kvm.resolve(self.kvm_load(), node["key"], self.kvm_ports())
        if state == "unbound":
            return None, "这个画面源没绑键鼠 (车机「画面设置」里绑), 只能看"
        if state == "offline":
            return None, "绑定的键鼠不在线"
        return path, None

    def _drop_ch(self):
        ch, self._ch, self._ch_path = self._ch, None, None
        try:
            ch.close()
        except (AttributeError, OSError):
            pass

    def _with_ch(self, path, fn):
        """拿绑定的 CH9329 发一串报告 -> None (成功) 或错误响应.
        换了画面源就换口; 线被拔就丢句柄, 下次请求重开."""
        with self._ch_lock:
            if self._ch is not None and self._ch_path != path:
                self._drop_ch()
                self.kbd.release_all()
            if self._ch is None:
                self._ch, self._ch_path = self.ch_open(path), path
            if self._ch is None:
                return _json(503, {"error": "CH9329 打不开 (没插好或被车机占着)"})
            try:
                fn(self._ch)
            except (OSError, ValueError) as e:
                self._drop_ch()
                self.kbd.release_all()
                return _json(503, {"error": f"CH9329 发送失败: {e}"})
            return None

    def _hid(self, req):
        try:
            events = wc.parse_hid_events(req)
        except ValueError as e:
            return _json(400, {"error": str(e)})
        path, why = self._kvm_port(req.get("src"))
        if path is None:
            return _json(409, {"error": why})

        def send(ch):
            for ev in events:
                if ev["t"] == "k":
                    rep = self.kbd.apply(ev["code"], ev["down"])
                    if rep is not None:
                        ch.send_report(rep)
                elif ev["t"] == "m":
                    for dx, dy, w in wc.mouse_chunks(ev["dx"], ev["dy"], ev["w"]):
                        ch.send_mouse(ev["b"], dx, dy, w)
                elif ev["t"] == "a":
                    if ev["b"] and not ev["inside"]:   # 黑边上按下 / 拖着: 不发, 免得点到画面边上的东西
                        continue
                    ch.send_mouse_abs(ev["b"], ev["x"], ev["y"], ev["w"])
                else:                          # 浏览器失焦 / 退出透传: 全部抬起
                    ch.send_report(self.kbd.release_all())
                    ch.send_mouse(0, 0, 0, 0)
        return self._with_ch(path, send) or _json(200, {"ok": True})

    def _type(self, req, ip=None, tok=None):
        """粘贴即打字 (US 布局). 打不出的字符跳过并如实报数; 分块发, 最后真发一次全部松开."""
        text = req.get("text", "")
        if not isinstance(text, str):
            return _json(400, {"error": "text 必须是字符串"})
        if len(text) > TYPE_MAX:
            return _json(413, {"error": f"一次最多 {TYPE_MAX} 个字符"})
        path, why = self._kvm_port(req.get("src"))
        if path is None:
            return _json(409, {"error": why})
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        unknown = ch9329.untypable(text)
        keys = [c for c in text if c not in unknown]
        self.log("type", ip, tok, n=len(text), skipped=len(text) - len(keys))

        def run(chunk):
            def send(ch):
                for c in chunk:
                    rep, _ = ch9329.key_report(["enter"] if c == "\n" else [c])
                    ch.send_report(rep)
                    ch.send_report(bytes(8))
            return send
        for i in range(0, len(keys), TYPE_CHUNK):
            err = self._with_ch(path, run(keys[i:i + TYPE_CHUNK]))
            if err:
                return err
        err = self._with_ch(path, lambda ch: ch.send_report(self.kbd.release_all()))
        if err:
            return err
        return _json(200, {"ok": True, "sent": len(keys), "skipped": len(text) - len(keys),
                           "unknown": unknown[:32]})

    # -------------------------------------------------------- 画面

    def _video_list(self):
        conf, online = self.kvm_load(), self.kvm_ports()
        out = []
        for n in self.video_nodes():
            _path, serial, state = kvm.resolve(conf, n["key"], online)
            out.append({"dev": os.path.basename(n["dev"]), "name": n.get("friendly") or n["name"],
                        "kvm": state, "kvm_label": kvm.label(conf, serial)})
        return out

    def video_dev(self, name):
        """只认 video_nodes 给出的采集节点 (UVC 元数据节点、乱填的名字都不认)."""
        node = self._src_node(name)
        return node["dev"] if node else None

    def open_video(self, name, fps=15):
        """同一采集设备只留一个 ffmpeg: 新开的顶掉旧的, 免得占着设备越开越多."""
        dev = self.video_dev(name)
        if dev is None:
            return None
        with self._video_lock:
            old = self._videos.pop(dev, None)
            if old is not None:
                old.kill()
                old.wait()
            proc = self.popen(wc.video_cmd(dev, fps=fps), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, bufsize=0)
            self._videos[dev] = proc
        return proc

    def video_done(self, name, proc):
        dev = self.video_dev(name)
        with self._video_lock:
            if dev and self._videos.get(dev) is proc:
                del self._videos[dev]

    # -------------------------------------------------------- 串口

    def open_serial(self, tok, name, cid):
        """接到 conbroker 的 human 通道. 只打招呼, 不替人按回车 (唤醒由页面上的按钮来).
        同一标签页重连顶掉自己的旧连接, 别的标签页不受影响."""
        port = next((p["port"] for p in wc.serial_ports(self.serial_dir()) if p["name"] == name), None)
        if port is None:
            return None
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"HELLO human\n")
        with self._state_lock:
            old = self.serial_socks.pop((tok, cid), None)
            self.serial_socks[(tok, cid)] = s
            self._serial_tx[(tok, cid)] = 0
        if old:
            old.close()
        return s

    def serial_history(self, name):
        return wc.serial_history(self.serial_dir(), name)

    def serial_close(self, tok, cid, s):
        """-> 这条连接发出去的字节数."""
        with self._state_lock:
            if self.serial_socks.get((tok, cid)) is s:
                del self.serial_socks[(tok, cid)]
                self._send_locks.pop((tok, cid), None)
            sent = self._serial_tx.pop((tok, cid), 0)
        s.close()
        return sent

    def _serial_send(self, tok, cid, data):
        if not wc.valid_cid(cid):
            return _json(400, {"error": "cid 不对"})
        if not isinstance(data, str):
            return _json(400, {"error": "data 必须是 base64 字符串"})
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            return _json(400, {"error": "data 不是合法的 base64"})
        key = (tok, cid)
        with self._state_lock:
            sock = self.serial_socks.get(key)
            lock = self._send_locks.setdefault(key, threading.Lock()) if sock else None
        if sock is None:
            return _json(409, {"error": "串口没连上"})
        with lock:
            try:
                sock.sendall(raw)
            except OSError as e:
                return _json(409, {"error": str(e)})
        with self._state_lock:
            if key in self._serial_tx:
                self._serial_tx[key] += len(raw)
        return _json(200, {"ok": True})

    # -------------------------------------------------------- 静态文件

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
    protocol_version = "HTTP/1.1"       # 键鼠批次 / 串口按键复用同一条连接
    timeout = 30                         # 发一半不发完的请求, 30 秒后放手

    def _local_ip(self):
        try:
            return self.connection.getsockname()[0]
        except OSError:
            return None

    def _send(self, status, hdrs, out):
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(out)

    def _body(self):
        """-> (body, None) 或 (None, 错误响应). 来源检查之后才读."""
        raw = self.headers.get("Content-Length")
        try:
            n = int(raw) if raw else 0
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return None, _text(400, "bad Content-Length")
        if n > BODY_MAX:
            self.close_connection = True
            return None, _text(413, "request body too large")
        return (self.rfile.read(n) if n else b""), None

    def _dispatch(self, method):
        app, ip, local = self.app, self.client_address[0], self._local_ip()
        try:
            reason = app.source_reason(ip, local) or app.request_reason(method, self.headers)
            if reason:                       # 拒绝的来源 / 跨站: 请求体一个字节都不读
                self.close_connection = True
                return self._send(*_text(403, reason))
            body, err = self._body()
            if err:
                return self._send(*err)
            resp = app.handle(method, self.path, ip, self.headers, body, local_ip=local)
        except Exception as e:               # 路由里漏网的异常: 回 500, 别让连接直接断
            print(f"webkiosk: {method} {self.path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            self.close_connection = True
            resp = _json(500, {"error": "internal error"})
        self._send(*resp)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/api/video", "/api/serial/stream"):
            q = dict(p.partition("=")[::2] for p in query.split("&") if p)
            return self._stream(path, q)
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # 流式接口: 长连接, 不走 App.handle 的一次性返回; 每隔几秒复查一次鉴权

    def _stream(self, path, q):
        app, ip, local = self.app, self.client_address[0], self._local_ip()
        self.close_connection = True
        reason = app.source_reason(ip, local) or app.request_reason("GET", self.headers)
        if reason:
            return self._send(*_text(403, reason))
        tok = wc.cookie_token(self.headers.get("Cookie"))
        if not app.authed(self.headers):
            return self._send(*_json(401, {"error": "login required"}))
        still_ok = lambda: app.stream_ok(ip, tok, local)        # noqa: E731
        if path == "/api/video":
            try:
                fps = max(1, min(30, int(q.get("fps") or 15)))
            except ValueError:
                return self._send(*_text(400, "bad fps"))
            return self._video(ip, tok, q.get("dev", ""), fps, still_ok)
        if not wc.valid_cid(q.get("cid")):
            return self._send(*_text(400, "bad cid"))
        return self._serial(ip, tok, q.get("name", ""), q["cid"], still_ok)

    def _stream_head(self, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _video(self, ip, tok, dev, fps, still_ok):
        app = self.app
        proc = app.open_video(dev, fps=fps)
        if proc is None:
            return self._send(*_text(404, "no such video device"))
        app.log("video-open", ip, tok, dev=dev, fps=fps)
        unregister = app.stream_register(tok, proc.kill)
        try:
            self._stream_head("multipart/x-mixed-replace; boundary=ffmpeg")
            wc.relay(lambda: proc.stdout.read(65536), self.wfile.write, still_ok,
                     every=STREAM_RECHECK_S)
        except OSError:
            pass
        finally:
            unregister()
            proc.kill()
            proc.wait()
            app.video_done(dev, proc)
            app.log("video-close", ip, tok, dev=dev)

    def _serial(self, ip, tok, name, cid, still_ok):
        app = self.app
        try:
            s = app.open_serial(tok, name, cid)
        except OSError as e:
            return self._send(*_text(502, f"conbroker: {e}"))
        if s is None:
            return self._send(*_text(404, "no running broker"))
        s.settimeout(STREAM_RECHECK_S)

        def read():
            try:
                return s.recv(4096)
            except socket.timeout:
                return None

        rx = [0]

        def write(data):
            rx[0] += len(data)
            self.wfile.write(b"data: " + base64.b64encode(data) + b"\n\n")
            self.wfile.flush()

        def closer():
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        unregister = app.stream_register(tok, closer)
        app.log("serial-open", ip, tok, name=name, cid=cid)
        try:
            self._stream_head("text/event-stream")
            history = app.serial_history(name)
            if history:                          # 回放最近的输出: 进来就看得到停在哪个提示符上
                self.wfile.write(b"data: " + base64.b64encode(history) + b"\n\n")
                self.wfile.flush()
            wc.relay(read, write, still_ok, every=STREAM_RECHECK_S)
        except OSError:
            pass
        finally:
            unregister()
            sent = app.serial_close(tok, cid, s)
            app.log("serial-close", ip, tok, name=name, cid=cid, rx=rx[0], tx=sent)

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="ops-cart 网页版 Kiosk")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="监听地址; 装机口网段的来源无论绑在哪都会被拒绝")
    a = ap.parse_args()
    Handler.app = App()
    port = a.port or wc.web_conf_read()["port"]
    print(f"webkiosk on {a.bind}:{port}", flush=True)
    Server((a.bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
