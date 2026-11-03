"""MCP 工具: 看和动被测机 (#41). 键鼠 kvm_* / OCR screen_ocr / 网络唤醒 wol_send.

键鼠: 一把写租约 (/run/ops-cart/kvm-lease.json, root 0600), 有期限要续, 同时一个写入者; 全部急停 (kvm_broker 读的公开标记)
时收回租约, 解除后要重新申请. 发键只经键鼠代理、来源固定 agent, 代理不在就报错, 不退回直开串口 (直开绕过人优先和急停).
只给整键和整行, 不给原始报告 / 鼠标 (同 kvm_broker_core.AGENT_OPS). 没有闸门: 敲到被测机上的内容推车看不懂, 靠人盯和急停.
敲的内容进审计日志 (mcp-audit.log), 别敲密码.
这里没有一个工具能解锁 PXE, 也不碰串口写路径.
"""

import contextlib
import fcntl
import hmac
import json
import os
import secrets
import subprocess
import time

import atomicfile
import cart_ocr
import cart_snap
import kvm_broker
import kiosk_core as kc
import kvm_core
import mcp_core as mc
import mcp_serial as ms
import wol_core

LEASE_PATH = "/run/ops-cart/kvm-lease.json"
TTL_S, TTL_MIN_S, TTL_MAX_S = 60, 10, 300
LAN1 = "eno1"                   # 魔术包只从装机口发: LAN2 / 无线上都是别人的机器
TEXT_MAX = 1000
OCR_MIN_LINE = 40.0             # 低于这个置信度的行当噪声扔掉
OCR_MIN_MEAN = 60.0             # 平均低于这个: 明说读不出, 叫 Agent 看图
OCR_HINT = "OCR 读不出 (彩色桌面 / 图形界面 / 没信号都这样), 用 screenshot 看图"


class LeaseStore:
    """一把键鼠写租约. run(fn): 加锁 -> 读 -> fn(doc, now) -> 存. 文件读不懂 = 没有租约. 全部急停时收回."""

    def __init__(self, path=LEASE_PATH, clock=time.time, stopped=kvm_broker.serial_all_stopped):
        self.path, self.clock, self.stopped = path, clock, stopped

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, now):
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict) or not isinstance(doc.get("until"), (int, float)) or doc["until"] <= now:
            return {}
        return doc

    def _write(self, doc):
        atomicfile.write_text(self.path, json.dumps(doc), mode=0o600)

    def _live(self, now):
        """当前有效的租约; 全部急停就地收回 (解除后旧令牌不复活)."""
        doc = self._read(now)
        if doc and self.stopped():
            self._write({})
            return {}
        return doc

    def _need(self, token, now):
        if self.stopped():
            self._write({})
            raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除, 之后重新申请租约")
        doc = self._read(now)
        if not doc or not hmac.compare_digest(str(doc.get("token", "")), str(token)):
            raise mc.ToolError("没有有效的键鼠写租约 (过期了? 急停收回了?), 先 kvm_lease_acquire")
        return doc

    def acquire(self, who, ttl):
        ttl = int(max(TTL_MIN_S, min(TTL_MAX_S, ttl)))
        with self._locked():
            now = self.clock()
            if self.stopped():
                self._write({})
                raise mc.ToolError("全部急停中, Agent 不能动键鼠; 要人在推车上解除")
            doc = self._read(now)
            if doc:                     # 认令牌不认名字: stdio 下两个 Agent 可能是同一个用户
                raise mc.ToolError(f"键鼠写租约在 {doc.get('who')} 手里, 还剩 {int(doc['until'] - now)} 秒")
            doc = {"token": secrets.token_urlsafe(16), "who": who, "until": now + ttl, "ttl": ttl}
            self._write(doc)
            return {"lease": doc["token"], "ttl_s": ttl, "hint": "用完 kvm_lease_release; 令牌别给别人."}

    def renew(self, token):
        with self._locked():
            now = self.clock()
            doc = self._need(token, now)
            doc["until"] = now + doc.get("ttl", TTL_S)
            self._write(doc)
            return {"expires_in": int(doc["until"] - now)}

    def release(self, token):
        with self._locked():
            self._need(token, self.clock())
            self._write({})
            return {"released": True}

    def check(self, token):
        with self._locked():
            self._need(token, self.clock())

    def view(self):
        with self._locked():
            now = self.clock()
            doc = self._live(now)
            return {"writer": doc.get("who"), "expires_in": int(doc["until"] - now) if doc else None,
                    "all_stopped": bool(self.stopped())}


def ocr_view(lines):
    """cart_ocr 的行 -> {readable, mean_conf, text, lines}. 按字数加权; 噪声行扔掉, 读不出就明说."""
    good = [ln for ln in lines if ln["text"].strip() and ln["conf"] >= OCR_MIN_LINE]
    chars = sum(len(ln["text"]) for ln in good)
    mean = sum(ln["conf"] * len(ln["text"]) for ln in good) / chars if chars else 0.0
    out = {"readable": bool(good) and mean >= OCR_MIN_MEAN, "mean_conf": round(mean, 1),
           "text": "\n".join(ln["text"] for ln in good),
           "lines": [{"text": ln["text"], "x": ln["x"], "y": ln["y"]} for ln in good]}
    if not out["readable"]:
        out["hint"] = OCR_HINT
    return out


def build_tools(store, ports, opener, ocr, wol, broker_up, who=ms.current_who):
    """ports() -> {序列号: 路径}; opener(port, who) -> 代理连接 (send_keys / type_text / close);
    ocr(dev|None, lang) -> (行, 哪块卡的说明); wol(mac, iface); broker_up(port) -> bool. 测试里全换成假的."""

    def _port(serial):
        online = ports()
        if serial:
            if serial not in online:
                raise mc.ToolError(f"没有序列号 {serial} 的键鼠; 在线的: {', '.join(online) or '没有'}")
            return online[serial]
        if not online:
            raise mc.ToolError("没有在线的键鼠 (CH9329 没插?)")
        if len(online) > 1:
            raise mc.ToolError(f"有几块键鼠, 用 serial 指定: {', '.join(online)}")
        return next(iter(online.values()))

    def _send(a, fn):
        store.check(a["lease"])
        port = _port(a.get("serial"))
        try:
            ch = opener(port, who())
        except (FileNotFoundError, ConnectionRefusedError):
            raise mc.ToolError("键鼠代理 (kvm-broker) 没在跑; 不直开串口, 请人在推车上开服务") from None
        except (OSError, ValueError) as e:
            raise mc.ToolError(f"键鼠打不开: {e}") from None
        try:
            fn(ch)
        except (OSError, ValueError) as e:
            raise mc.ToolError(f"发键失败: {e}") from None
        finally:
            ch.close()
        return [mc.json_content({"sent": True, "hint": "发完等几秒, 用 screen_ocr 看结果"})]

    def kvm_status(_a):
        online = ports()
        up = any(broker_up(p) for p in online.values()) if online else None
        return [mc.json_content(dict(store.view(), keyboards=sorted(online), broker=up))]

    def kvm_lease_acquire(a):
        return [mc.json_content(store.acquire(who(), a["ttl_s"]))]

    def kvm_lease_renew(a):
        return [mc.json_content(store.renew(a["lease"]))]

    def kvm_lease_release(a):
        return [mc.json_content(store.release(a["lease"]))]

    def kvm_key(a):
        keys = [k for k in a["keys"].split("+") if k]
        return _send(a, lambda ch: ch.send_keys(keys))

    def kvm_type(a):
        def go(ch):
            ch.type_text(a["text"])
            if a.get("enter"):
                ch.send_keys(["enter"])
        return _send(a, go)

    def screen_ocr(a):
        dev = a.get("device")
        if dev and not dev.startswith("/dev/"):
            dev = "/dev/" + dev
        lines, label = ocr(dev, a["lang"])
        return [mc.json_content(dict(ocr_view(lines), screen=label))]

    def wol_send(a):
        try:
            mac = wol_core.parse_mac(a["mac"])
            wol(mac, LAN1)
        except (ValueError, OSError) as e:
            raise mc.ToolError(f"魔术包发不出: {e}") from None
        return [mc.json_content({"mac": mac, "iface": LAN1, "note": "发了, 不保证醒: 要机器开了网络唤醒且关机后"
                                 "网口还亮. 看 pxe_clients 或 screen_ocr 确认"})]

    LEASE = {"type": "string", "required": True, "maxLength": 64, "description": "kvm_lease_acquire 给的令牌"}
    SERIAL = {"type": "string", "maxLength": 32, "description": "键鼠序列号, 只有一块时不用给"}
    T = ms.Tool
    return [
        T("kvm_status", "键鼠状态 (只读): 在线的键鼠序列号、代理在不在、写入者、全部急停.", {}, kvm_status, read_only=True),
        T("kvm_lease_acquire", "申请键鼠写租约 (同时一个写入者), 返回令牌. 全部急停时不给.",
          {"ttl_s": {"type": "integer", "minimum": TTL_MIN_S, "maximum": TTL_MAX_S, "default": TTL_S,
                     "description": "期限秒"}}, kvm_lease_acquire),
        T("kvm_lease_renew", "续键鼠写租约. 过期了要重新申请.", {"lease": LEASE}, kvm_lease_renew),
        T("kvm_lease_release", "交还键鼠写租约.", {"lease": LEASE}, kvm_lease_release),
        T("kvm_key", "往被测机按键或组合键, 如 enter / esc / f12 / ctrl+alt+delete. 人最近 3 秒动过键鼠会排队. "
          "按之前先 screen_ocr 确认焦点对.",
          {"keys": {"type": "string", "required": True, "maxLength": 64, "pattern": r"[A-Za-z0-9_+-]+",
                    "description": "键名, 组合键用 + 连"}, "lease": LEASE, "serial": SERIAL},
          kvm_key, destructive=True),
        T("kvm_type", "往被测机打一段 ASCII (被测机输入法要在英文). 内容进审计日志, 别敲密码. "
          "敲之前先 screen_ocr 确认焦点对.",
          {"text": {"type": "string", "required": True, "maxLength": TEXT_MAX, "description": "要打的字"},
           "enter": {"type": "boolean", "description": "打完按回车"}, "lease": LEASE, "serial": SERIAL},
          kvm_type, destructive=True),
        T("screen_ocr", "读被测机屏幕上的字 (OCR, 带坐标), 比 screenshot 省得多, 看画面先用这个. readable=false 时再用 "
          "screenshot. BIOS / 命令行读得准, 彩色桌面读不出.",
          {"device": {"type": "string", "pattern": r"(/dev/)?video[0-9]{1,2}", "maxLength": 16,
                      "description": "采集设备, 默认正在预览的那块, 没预览就有信号的采集卡"},
           "lang": {"type": "string", "enum": ["eng", "eng+chi_sim"], "default": "eng", "description": "语言"}},
          screen_ocr, read_only=True),
        T("wol_send", "往 LAN1 (装机口) 发网络唤醒魔术包. 不保证醒.",
          {"mac": {"type": "string", "required": True, "maxLength": 17,
                   "pattern": r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", "description": "目标 MAC"}}, wol_send),
    ]


# ------------------------------------------------------------ 真机

def describe_dev(dev):
    """哪块卡 + 有没有信号; 探测失败不影响截图."""
    try:
        return mc.snap_label(dev, kc.video_friendly(dev), kc.video_signal(dev))
    except Exception:                   # noqa: BLE001
        return mc.snap_label(dev, None, None)


def real_opener(port, who):
    return kvm_broker.BrokerCh(port, src="agent", who=who)


def broker_up(port):
    try:
        kvm_broker.BrokerCh(port, src="agent", who="status").close()
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:                     # 代理在, 设备忙 / 拔了
        return True


def real_ocr(dev, lang):
    path, why, used = cart_snap.grab(dev)
    if path is None:
        raise mc.ToolError(why)
    try:
        lines = cart_ocr.ocr_image(path, lang=lang)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise mc.ToolError(f"OCR 失败 (tesseract / convert): {e}") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return lines, describe_dev(used)


def real_tools(store=None):
    return build_tools(store or LeaseStore(), ports=kvm_core.ports, opener=real_opener, ocr=real_ocr,
                       wol=wol_core.send, broker_up=broker_up)
