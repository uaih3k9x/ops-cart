"""对 Monita 固件模拟器 (fwsim) 的联调. 可选: 环境变量 CS80_FWSIM 指向本机自己编好的 fwsim 才跑, 否则 skip.

fwsim 在 Monita-CS80 仓库 (PolyForm Noncommercial) 的 tools/fwsim, 不 vendor 进本仓库、不在 CI 跑.
编法 (在任意临时目录): cc -std=c99 -O2 -Ifw/link -o fwsim tools/fwsim/fwsim.c   (取自固件 v10 分支)
只走 fwsim README 里的 stdin 命令 (stat / tap / quit), 不用它的对拍脚本. 模拟器用真实时钟, "主机走了" 那条要等约 6 s.
"""

import io
import json
import os
import subprocess
import time
import zlib

import pytest

SIM = os.environ.get("CS80_FWSIM")
pytestmark = pytest.mark.skipif(not (SIM and os.access(SIM, os.X_OK)),
                                reason="没设 CS80_FWSIM (指向本机编好的 Monita fwsim), 跳过联调")

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

import cs80_proto as p  # noqa: E402
import monita_cs80 as mc  # noqa: E402


class Sim:
    def __init__(self, *args):
        self.proc = subprocess.Popen([SIM] + list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        first = self.proc.stdout.readline().split()
        assert first[0] == "PTY", first
        self.pty = first[1]

    def cmd(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def stat(self):
        return json.loads(self.cmd("stat"))

    def tap(self, key):
        assert self.cmd("tap %d" % key) == "ok"

    def close(self):
        try:
            self.cmd("quit")
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def sim():
    s = Sim()
    yield s
    s.close()


def _open(sim, log=None):
    return mc.open_display(port=sim.pty, log=log or (lambda s: None))


def _img(seed=0):
    img = Image.new("RGB", (800, 1280), (20 + seed, 40, 60))
    for i in range(0, 800, 50):
        img.paste((i % 256, (i * 3) % 256, (seed * 40) % 256), (i, (i * 7) % 1200, i + 30, (i * 7) % 1200 + 40))
    return img


def _crc(img):
    return zlib.crc32(p.rgb565(img)) & 0xFFFFFFFF


def _ticks(d, secs, view, snap=None):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        d.tick(time.time(), snap or {}, view)
        time.sleep(0.2)


VIEW = {"net": "warn", "checks": {"ok": 3, "warn": 1, "fail": 1}, "stale": False}


def test_hello_caps_and_firmware_clock_matches_our_seg_encoder(sim):
    """HELLO 后固件自己画 "------ hh:mm:ss": 拿它的显存核对我们独立写的 7 段 / 冒号编码."""
    d = _open(sim)
    caps = d.info["caps"]
    assert caps & p.CAP_KEYS and caps & p.CAP_LED and caps & p.CAP_SLEEP
    st = sim.stat()
    assert st["link_up"] == 1 and st["boot_live"] == 0 and st["clk_valid"] == 1
    sod = st["clk_sod"]
    small = "%02d%02d%02d" % (sod // 3600, sod // 60 % 60, sod % 60)
    want = p.seg_encode(main="------", small=small, icons={"CLK_COLON_A", "CLK_COLON_B"})
    assert bytes.fromhex(st["fg"]) == want
    lt = time.localtime()
    drift = (sod - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)) % 86400
    assert min(drift, 86400 - drift) <= 2                   # HELLO 带的时间和时区对
    d.close()


def test_full_frame_then_diff_lands_on_screen(sim):
    d = _open(sim)
    a = _img(1)
    assert d.send(a) == 80
    st = sim.stat()
    assert st["bad_param"] == 0 and st["crc_err"] == 0 and st["rects"] == 80
    assert st["screen_crc"] == _crc(a)
    b = a.copy()
    b.paste((255, 255, 255), (100, 600, 180, 630))
    n = d.send(b)
    assert 0 < n < 10 and sim.stat()["screen_crc"] == _crc(b)
    d.close()


def test_seg_led_and_keys(sim):
    d = _open(sim)
    d.send(_img(2))
    now = time.time()
    d.tick(now, {"ups": {"percent": 80, "status": "OL"}}, VIEW)
    st = sim.stat()
    assert bytes.fromhex(st["fg"]) == p.seg_state({"ups": {"percent": 80, "status": "OL"}}, VIEW, now)
    assert st["led_mode"] == 2 and st["led_mask"] == p.LED_RED                 # 有 FAIL: 红闪
    sim.tap(p.KEY_MINUS)
    _ticks(d, 0.6, VIEW)
    assert sim.stat()["backlight"] == 0
    sim.tap(p.KEY_PLUS)
    _ticks(d, 0.6, VIEW)
    assert sim.stat()["backlight"] == 1
    d.close()


def test_host_gone_then_backend_heals(sim):
    """停发 > 5 s 固件回开机页; 下一次 tick 先 HELLO 再推整帧, 屏上回到我们的画面."""
    d = _open(sim)
    a = _img(3)
    d.send(a)
    _ticks(d, 1.0, VIEW)
    time.sleep(5.6)
    st = sim.stat()
    assert st["link_up"] == 0 and st["boot_screen"] == 1
    d.tick(time.time(), {}, VIEW)
    st = sim.stat()
    assert st["link_up"] == 1 and st["boot_screen"] == 0 and st["screen_crc"] == _crc(a)
    assert st["led_mode"] == 2                  # 灯也重发了 (主机走了时回了蓝灯心跳)
    d.close()


def test_reopen_pty_and_reconnect(sim):
    d = _open(sim)
    d.send(_img(4))
    d.close()
    d = _open(sim)
    b = _img(5)
    assert d.send(b) == 80 and sim.stat()["screen_crc"] == _crc(b)
    d.close()


@pytest.mark.parametrize("args", [("--max-payload", "16384"), ("--nbuf", "1"), ("--no-led", "--no-keys")])
def test_other_device_configs(args):
    s = Sim(*args)
    try:
        d = _open(s)
        a = _img(6)
        d.send(a)
        _ticks(d, 0.5, VIEW)
        st = s.stat()
        assert st["bad_param"] == 0 and st["screen_crc"] == _crc(a)
        if "--no-led" in args:
            assert not d.led_ok and st["led_mode"] == 0
        d.close()
    finally:
        s.close()


def test_dash_service_end_to_end(sim, tmp_path, monkeypatch):
    """dash_service --display cs80 --cs80-port <PTY> 跑几轮: 推帧、段码屏接管、灯都到了模拟器上."""
    import dash_service as ds
    import threading
    rounds = {"n": 0}
    real_sleep = time.sleep
    me = threading.current_thread()

    def sleep(s):                   # ds.time 就是全局 time 模块: 别的线程 (别的测试留下的) 照常睡
        if threading.current_thread() is not me:
            return real_sleep(s)
        rounds["n"] += 1
        if rounds["n"] >= 12:
            raise KeyboardInterrupt
        real_sleep(s)
    monkeypatch.setattr(ds.time, "sleep", sleep)
    ctx = {"now": time.time, "net": lambda: "on", "rows": lambda: [{"label": "LAN2", "state": "OK"}],
           "temps": lambda: [], "fans": lambda: [], "uptime": lambda: "1d", "lamps": lambda: {},
           "kiosk": lambda: {"pid": 1, "alive": True}, "todo": lambda: [], "dream": lambda: "",
           "ups": lambda: {"percent": 90, "status": "OL"}}
    out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        ds.run(state_path=str(tmp_path / "s.json"), ctx=ctx, out=out, display="cs80", cs80_port=sim.pty)
    st = sim.stat()
    assert "副屏已连接" in out.getvalue() and "Monita-CS80" in out.getvalue()
    assert st["presents"] >= 1 and st["bad_param"] == 0 and st["seg_host_seen"] == 1
    assert st["led_mask"] == p.LED_GREEN and st["link_up"] == 1


def test_fast_reconnect_resends_backlight(sim):
    """− 关背光后不到 5 s 就重连: 固件 HELLO 不动背光, 新会话得自己发背光开, 之后 + / − 照常."""
    d = _open(sim)
    d.send(_img(8))
    sim.tap(p.KEY_MINUS)
    _ticks(d, 0.6, VIEW)
    assert sim.stat()["backlight"] == 0
    d.close()
    d = _open(sim)
    assert sim.stat()["backlight"] == 1
    sim.tap(p.KEY_MINUS)
    _ticks(d, 0.6, VIEW)
    assert sim.stat()["backlight"] == 0
    sim.tap(p.KEY_PLUS)
    _ticks(d, 0.6, VIEW)
    assert sim.stat()["backlight"] == 1
    d.close()


def test_bad_diff_rect_repaired_by_next_tick(sim, monkeypatch):
    """diff 里一条 RECT 的 CRC 坏了: 设备丢掉、计数; 下一轮 tick 就推整帧补上, 不等下一次 send."""
    d = _open(sim)
    a = _img(9)
    d.send(a)
    b = a.copy()
    b.paste((255, 255, 255), (100, 600, 180, 630))
    real = p.encode
    hit = []

    def bad(ptype, seq, payload=b""):
        f = bytearray(real(ptype, seq, payload))
        if ptype == p.RECT and not hit:
            hit.append(1)
            f[-1] ^= 0xFF
        return bytes(f)
    monkeypatch.setattr(p, "encode", bad)
    d.send(b)
    monkeypatch.setattr(p, "encode", real)
    st = sim.stat()
    assert st["crc_err"] == 1 and st["screen_crc"] != _crc(b)
    d.tick(time.time(), {}, VIEW)
    assert sim.stat()["screen_crc"] == _crc(b)
    d.close()
