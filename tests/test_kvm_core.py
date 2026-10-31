"""kvm_core 测试: CH9329 按 CH343 序列号认身份, 画面源 <-> 键鼠一对一绑定, 没绑/不在线 = 只看不操作."""

import kvm_core as kvm

BYID = [
    "usb-1a86_USB_Single_Serial_0000000001-if00",
    "usb-1a86_USB_Single_Serial_0000000002-if00",
    "usb-DC474C00_P050300_TEST02_2K_USB_Camera_DC474C00_P050300_TEST02-if05",   # 摄像头自带的串口, 不是键鼠
    "usb-FTDI_FT232R_USB_UART_TEST01-if00-port0",
]


def test_ports_only_ch343_keyed_by_serial():
    ports = kvm.ports_from_byid(BYID, "/dev/serial/by-id")
    assert ports == {
        "0000000001": "/dev/serial/by-id/usb-1a86_USB_Single_Serial_0000000001-if00",
        "0000000002": "/dev/serial/by-id/usb-1a86_USB_Single_Serial_0000000002-if00",
    }


def test_label_uses_name_or_short_serial():
    conf = {"names": {"0000000001": "KVM-1"}}
    assert kvm.label(conf, "0000000001") == "KVM-1"
    assert kvm.label(conf, "0000000002") == "键鼠 …0002"
    assert kvm.label(conf, None) == "不绑定"


def test_bind_and_unbind_do_not_mutate():
    conf = {"bind": {}}
    c2 = kvm.bind(conf, "0000:05:00.0", "0000000001")
    assert c2["bind"] == {"0000:05:00.0": "0000000001"} and conf["bind"] == {}
    assert kvm.bind(c2, "0000:05:00.0", None)["bind"] == {}


def test_resolve_bound_and_online():
    conf = {"bind": {"0000:05:00.0": "0000000001"}}
    ports = kvm.ports_from_byid(BYID, "/d")
    assert kvm.resolve(conf, "0000:05:00.0", ports) == (
        "/d/usb-1a86_USB_Single_Serial_0000000001-if00", "0000000001", "ok")


def test_resolve_unbound_never_falls_back():
    ports = kvm.ports_from_byid(BYID, "/d")
    assert kvm.resolve({}, "345f:2109", ports) == (None, None, "unbound")      # 摄像头: 只看
    assert kvm.resolve({"bind": {}}, None, ports) == (None, None, "unbound")


def test_resolve_offline_is_view_only():
    conf = {"bind": {"0000:05:00.0": "DEADBEEF00"}}
    assert kvm.resolve(conf, "0000:05:00.0", kvm.ports_from_byid(BYID, "/d")) == (None, "DEADBEEF00", "offline")


def test_next_choice_cycles_none_then_serials():
    serials = ["0000000001", "0000000002"]
    assert kvm.next_choice(serials, None) == "0000000001"
    assert kvm.next_choice(serials, "0000000001") == "0000000002"
    assert kvm.next_choice(serials, "0000000002") is None
    assert kvm.next_choice(serials, "GONE") is None            # 绑的那块不在了, 从"不绑定"重新转
    assert kvm.next_choice([], None) is None


def test_one_kvm_per_source_but_kvm_can_be_shared_warning():
    conf = kvm.bind({}, "0000:05:00.0", "0000000001")
    conf = kvm.bind(conf, "345f:2109", "0000000001")
    assert kvm.shared_with(conf, "0000000001", "345f:2109") == ["0000:05:00.0"]
    assert kvm.shared_with(conf, "0000000002", "x") == []


def test_load_save_roundtrip(tmp_path):
    p = str(tmp_path / "kvm.json")
    assert kvm.load(p) == {"names": {}, "bind": {}}
    kvm.save({"names": {"0000000001": "KVM-1"}, "bind": {"0000:05:00.0": "0000000001"}}, p)
    assert kvm.load(p)["bind"]["0000:05:00.0"] == "0000000001"


# ------------------------------------------------------------ #9 车机键鼠空闲放锁

from ch9329 import Ch9329Busy


class FakeDev:
    def __init__(self):
        self.sent, self.closed = [], False

    def send_report(self, rep):
        self.sent.append(("send_report", rep))

    def send_mouse(self, *a):
        self.sent.append(("send_mouse",) + a)

    def send_mouse_abs(self, *a):
        self.sent.append(("send_mouse_abs",) + a)

    def close(self):
        self.closed = True


class Opener:
    """按顺序给出结果: FakeDev 或要抛的异常; 用完了一直给新 FakeDev."""

    def __init__(self, *plan):
        self.plan, self.devs, self.calls = list(plan), [], 0

    def __call__(self):
        self.calls += 1
        r = self.plan.pop(0) if self.plan else FakeDev()
        if isinstance(r, BaseException):
            raise r
        self.devs.append(r)
        return r


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _ich(opener, **kw):
    clock, busy = Clock(), []
    ch = kvm.IdleCh(opener, on_busy=lambda: busy.append(clock.t), clock=clock, **kw)
    return ch, clock, busy


KEY_A = bytes([0, 0, 4, 0, 0, 0, 0, 0])


def test_idle_ch_opens_lazily_on_first_send():
    op = Opener()
    ch, _clock, _busy = _ich(op)
    assert op.calls == 0 and not ch.is_open          # 只开预览 / 进透传不占锁
    ch.send_report(KEY_A)
    ch.send_report(bytes(8))
    assert op.calls == 1 and ch.is_open
    assert op.devs[0].sent == [("send_report", KEY_A), ("send_report", bytes(8))]


def test_idle_ch_reap_releases_after_idle_and_reopens_on_next_input():
    op = Opener()
    ch, clock, _busy = _ich(op, idle_s=3.0)
    ch.send_mouse_abs(0, 10, 20, 0)
    clock.t += 2.9
    assert ch.reap() is False and ch.is_open           # 不到 3 秒不放
    clock.t += 0.2
    assert ch.reap() is True and not ch.is_open
    d = op.devs[0]
    assert d.closed and d.sent[-2:] == [("send_mouse", 0, 0, 0, 0), ("send_report", bytes(8))]
    assert ch.reap() is False                          # 已经放了: 什么都不做
    ch.send_mouse_abs(0, 11, 20, 0)                    # 人又动了: 重新拿
    assert op.calls == 2 and op.devs[1].sent == [("send_mouse_abs", 0, 11, 20, 0)]


def test_idle_ch_default_idle_matches_webkiosk():
    assert kvm.IDLE_RELEASE_S == 3.0


def test_idle_ch_held_key_or_button_is_not_idle():
    op = Opener()
    ch, clock, _busy = _ich(op, idle_s=3.0, hold_max_s=30.0)
    ch.send_report(KEY_A)                              # 按着 A 不放 (长按开机菜单键之类)
    clock.t += 10
    assert ch.reap() is False and ch.is_open
    ch.send_report(bytes(8))
    ch.send_mouse(1, 0, 0, 0)                          # 换成按着左键拖
    clock.t += 10
    assert ch.reap() is False
    ch.send_mouse_abs(0, 5, 5, 0)                      # 松开了
    clock.t += 3
    assert ch.reap() is True


def test_idle_ch_held_too_long_is_released_anyway():
    """松开事件丢了 (焦点被抢) 键会一直算按着: 超过 hold_max_s 照样放, 顺带把目标机上的键松开."""
    op = Opener()
    ch, clock, _busy = _ich(op, idle_s=3.0, hold_max_s=30.0)
    ch.send_report(KEY_A)
    clock.t += 29
    assert ch.reap() is False
    clock.t += 2
    assert ch.reap() is True and op.devs[0].sent[-1] == ("send_report", bytes(8))


def test_idle_ch_busy_drops_input_and_notifies_rate_limited():
    op = Opener(Ch9329Busy("键鼠被占用"), Ch9329Busy("键鼠被占用"), Ch9329Busy("键鼠被占用"))
    ch, clock, busy = _ich(op, busy_note_s=1.0)
    ch.send_report(KEY_A)                              # Agent 正占着: 丢弃, 不抛 (不算拔线)
    clock.t += 0.5
    ch.send_report(bytes(8))
    assert busy == [100.0] and not ch.is_open           # 1 秒内只提示一次
    clock.t += 0.6
    ch.send_mouse(0, 1, 0, 0)
    assert busy == [100.0, 101.1]
    ch.send_report(KEY_A)                              # 别人放了: 这次正常发, 之前丢的不重放
    assert op.calls == 4 and op.devs[0].sent == [("send_report", KEY_A)]


def test_idle_ch_other_open_errors_propagate():
    """设备没了 (拔线) 不是占用: 照常抛 OSError, 让 ChWorker 走断线那条路."""
    ch, _clock, busy = _ich(Opener(FileNotFoundError("没这个口")))
    try:
        ch.send_report(KEY_A)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("应当抛出")
    assert busy == []


def test_idle_ch_release_skips_when_closed():
    """退出透传的"全部松开": 已经放锁了就不为它重开 (放锁前已经松过)."""
    op = Opener()
    ch, _clock, _busy = _ich(op)
    ch.release((7, 8))
    assert op.calls == 0
    ch.send_report(KEY_A)
    ch.release((7, 8))
    assert op.devs[0].sent[-2:] == [("send_mouse_abs", 0, 7, 8, 0), ("send_report", bytes(8))]
    ch.release()
    assert op.devs[0].sent[-2:] == [("send_mouse", 0, 0, 0, 0), ("send_report", bytes(8))]


def test_idle_ch_reap_ignores_send_error_and_still_closes():
    class Broken(FakeDev):
        def send_report(self, rep):
            raise OSError("拔线")

    op = Opener(Broken())
    ch, clock, _busy = _ich(op)
    ch.send_mouse(0, 1, 1, 0)
    clock.t += 5
    assert ch.reap() is True and op.devs[0].closed and not ch.is_open


def test_idle_ch_close_closes_handle():
    op = Opener()
    ch, _clock, _busy = _ich(op)
    ch.close()                                         # 没开过也能关
    ch.send_report(KEY_A)
    ch.close()
    assert op.devs[0].closed and not ch.is_open


def test_kiosk_opener_goes_through_kvm_broker_as_human(monkeypatch):
    """车机透传是人在操作: IdleCh 的 opener 经键鼠代理算 human (#9); 代理不在 open_ch 退回直开."""
    import kvm_broker
    got = []
    monkeypatch.setattr(kvm_broker, "open_ch",
                        lambda port, src="agent", who="cli", **kw: got.append((port, src, who)) or "ch")
    op = kvm.human_opener("/dev/serial/by-id/x")
    assert got == []                                        # 造 opener 不开口, 有输入才开
    assert op() == "ch" and got == [("/dev/serial/by-id/x", "human", "kiosk")]
