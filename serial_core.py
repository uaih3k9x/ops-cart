"""串口面板的纯逻辑: 读 serial.conf、拼 conbroker 命令、解析 list 输出、选口、按键/终端字节. 不碰 Tk.

后端是 conbroker (以 serial.conf 里的 user 身份跑); 这里只读它的命令行输出和数据目录 (run/ logs/ chat.log),
不直接碰串口设备. 界面在 serial_panel.py, 无界面的 (cart_dream / webkiosk / 灯条采样) 直接用这里.
口的选法: hub 口别名 /dev/con-N 优先, 其次 /dev/serial/by-id; 板载 ttyS* 不列; 键鼠 CH9329 的口标出来.
"""

import codecs
import glob
import json as _json
import os
import pwd
import re
import subprocess

SERIAL_CONF = "/opt/pxe/serial.conf"
_SERIAL_DEFAULT = {"user": "ops", "conbroker_home": "/home/ops/conbroker", "chat_name": "operator"}


def serial_conf_read(path=None):
    """部署配置: 串口服务以哪个用户跑 conbroker (GUI 是 root, 须降权对齐数据目录)."""
    if path is None:
        path = SERIAL_CONF
    cfg = dict(_SERIAL_DEFAULT)
    try:
        cfg.update(_json.load(open(path)))
    except (OSError, ValueError):
        pass
    home = cfg["conbroker_home"].rstrip("/")
    cfg["bin"] = f"{home}/.venv/bin/python"
    cfg["script"] = f"{home}/conbroker.py"
    return cfg


def _conbroker_cmd():
    c = serial_conf_read()
    return ["sudo", "-u", c["user"], "-H", c["bin"], c["script"]]


# CONBROKER 不做模块级缓存: serial.conf 可能在运行中被改 (Kiosk 里保存)

BAUDS = ["9600", "115200", "57600", "38400", "4800"]
BYID_DIR = "/dev/serial/by-id"
ALIAS_GLOB = "/dev/con-*"   # /etc/udev/rules.d/99-cart-console.rules
HIDDEN_PORT = re.compile(r"^/dev/ttyS\d+$")   # 板载 COM 口, 推车上不接线


def _run(args, timeout=8):
    try:
        r = subprocess.run(_conbroker_cmd() + args, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
    except Exception as e:
        return f"[错误] {e}"


def data_dir(cfg):
    """conbroker 的数据目录 (run/ logs/ chat.log): 跑服务那个用户的 ~/.conbroker."""
    if cfg.get("data_dir"):
        return cfg["data_dir"]
    try:
        home = pwd.getpwnam(cfg["user"]).pw_dir
    except KeyError:
        home = f"/home/{cfg['user']}"
    return f"{home}/.conbroker"


def broker_port(ddir, name):
    """读 conbroker 的状态文件拿本地端口; 没在跑返回 None."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    try:
        return _json.load(open(os.path.join(ddir, "run", f"{safe}.json")))["port"]
    except (OSError, ValueError, KeyError):
        return None


def parse_list(text):
    """conbroker list 的输出 -> (串口列表, {活着的服务名: 波特率}). 已死的残留状态不算服务."""
    ports, serves, section = [], {}, None
    for ln in text.splitlines():
        if ln.startswith("串口"):
            section = "p"
            continue
        if ln.startswith("服务"):
            section = "s"
            continue
        if not ln.startswith(" "):           # [stderr] / [错误] 之类
            section = None if ln.strip() else section
            continue
        parts = ln.split()
        if section == "p" and parts:
            ports.append(parts[0])
        elif section == "s" and len(parts) >= 3 and parts[2] == "在跑":
            serves[parts[0]] = parts[1]
    return ports, serves


def byid_paths(byid_dir=BYID_DIR):
    try:
        return [os.path.join(byid_dir, n) for n in sorted(os.listdir(byid_dir))]
    except OSError:
        return []


def prefer_byid(dev, paths, realpath=os.path.realpath):
    """同一个口有 by-id 路径就用它 (按序列号认口, 重插不变)."""
    r = realpath(dev)
    return next((p for p in sorted(paths) if realpath(p) == r), dev)


def alias_paths(pattern=ALIAS_GLOB):
    return sorted(glob.glob(pattern))


def console_ports(ports, aliases, realpath=os.path.realpath):
    """下拉列表: 有 hub 口别名的换成别名, 板载口去掉; 别名排前面."""
    out = []
    for p in ports:
        if HIDDEN_PORT.match(p):
            continue
        out.append(next((a for a in aliases if realpath(a) == realpath(p)), p))
    return sorted(out, key=lambda p: (p not in aliases, p))


def serve_target(port, aliases, byid, realpath=os.path.realpath):
    """开服务用的路径: hub 口别名原样用, 别的口换成 by-id."""
    return port if port in aliases else prefer_byid(port, byid, realpath)


def match_service(dev, serves, realpath=os.path.realpath):
    """选中的口对应哪个在跑的服务 (服务名可能是 by-id 路径, 也可能是 ttyUSBn)."""
    if dev in serves:
        return dev
    r = realpath(dev)
    return next((n for n in serves if realpath(n) == r), None)


def select_port(ports, want, realpath=os.path.realpath):
    """issue #5: 把点中那一路口对齐到下拉列表里的条目.

    返回 (要显示的路径, 是否在列表里). 不在列表里说明这一路没插线,
    仍然返回 want, 由面板提示"未插"; 指向同一设备时优先用列表里的写法.
    """
    if want in ports:
        return want, True
    r = realpath(want)
    hit = next((p for p in ports if realpath(p) == r), None)
    return (hit, True) if hit else (want, False)


def kvm_devices(kvm_ports, realpath=os.path.realpath):
    """键鼠 CH9329 占着的真实设备 (kvm_core.ports() 的 by-id 路径解析出来)."""
    return {realpath(p) for p in kvm_ports.values()}


def port_label(dev, kvm_devs):
    return f"{dev} (键鼠 CH9329)" if dev in kvm_devs else dev


def paste_chunks(text):
    """粘贴的文字 -> 按行切的字节块, 换行统一成回车 (串口设备认 \\r)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = [ln.encode() + b"\r" for ln in lines[:-1]]
    return out + ([lines[-1].encode()] if lines[-1] else [])


def serve_error(rc, stderr):
    """开服务的进程提前退出时给人看的一句: stderr 最后一行, 没有就退出码."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else f"退出码 {rc}"


_KEY_SEQ = {
    "Return": b"\r", "KP_Enter": b"\r", "BackSpace": b"\x08", "Tab": b"\t",
    "Escape": b"\x1b", "Up": b"\x1b[A", "Down": b"\x1b[B", "Right": b"\x1b[C",
    "Left": b"\x1b[D", "Home": b"\x1b[H", "End": b"\x1b[F", "Delete": b"\x1b[3~",
    "Prior": b"\x1b[5~", "Next": b"\x1b[6~",
}
_CTRL = 0x4
_RESERVED = {"bracketright"}     # Ctrl+] 和 conbroker attach 一样, 不进串口


def key_bytes(keysym, char, state):
    """Tk 按键 -> 发给串口的字节; 不该发的 (单独修饰键/保留键) 返回 None."""
    if keysym in _KEY_SEQ:
        return _KEY_SEQ[keysym]
    if state & _CTRL:
        if keysym in _RESERVED:
            return None
        if len(keysym) == 1 and keysym.isalpha():
            return bytes([ord(keysym.lower()) - 96])
    if char and (char.isprintable() or char == "\x03"):
        return char.encode()
    return None


_CSI = re.compile(rb"\x1b\[([0-9;?]*)([A-Za-z])|\x1b[()][AB012]")
_CSI_TAIL = re.compile(rb"\x1b(\[[0-9;?]*)?$|\x1b[()]$")


class TermFeed:
    """串口原始字节 -> 文本/退格操作. 不做完整终端仿真, 够看交换机 CLI 就行."""

    def __init__(self):
        self._pending = b""
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data):
        data = self._pending + data
        m = _CSI_TAIL.search(data)
        self._pending, data = (data[m.start():], data[:m.start()]) if m else (b"", data)
        ops = []
        pos = 0
        for m in _CSI.finditer(data):
            self._text(data[pos:m.start()], ops)
            if m.group(2) == b"D":                     # 光标左移 n 格 = 退格 n 次
                self._bs(int(m.group(1) or b"1"), ops)
            pos = m.end()
        self._text(data[pos:], ops)
        return ops

    def _text(self, raw, ops):
        if not raw:
            return
        buf = ""
        for ch in self._dec.decode(raw):
            if ch == "\b":
                if buf:
                    ops.append(("text", buf)); buf = ""
                self._bs(1, ops)
            elif ch in "\n\t" or ch.isprintable():
                buf += ch
        if buf:
            if ops and ops[-1][0] == "text":
                ops[-1] = ("text", ops[-1][1] + buf)
            else:
                ops.append(("text", buf))

    @staticmethod
    def _bs(n, ops):
        if ops and ops[-1][0] == "bs":
            ops[-1] = ("bs", ops[-1][1] + n)
        else:
            ops.append(("bs", n))


_CHAT_LINE = re.compile(r"^\[\d{4}-\d\d-\d\d (\d\d:\d\d):\d\d (\S+)\] (.*)$")


def parse_chat_line(line):
    m = _CHAT_LINE.match(line)
    return m.groups() if m else None
