"""udisp_usb 测试: 用假 pyusb 验开设备/选端点/帧序/假 zlp. 不碰真设备."""

import udisp
import udisp_usb


class FakeEp:
    def __init__(self, addr, mps=512, attr=0x02):
        self.bEndpointAddress = addr
        self.wMaxPacketSize = mps
        self.bmAttributes = attr


class FakeIntf:
    def __init__(self, cls, eps, num=0, istring=4):
        self.bInterfaceClass = cls
        self.bInterfaceNumber = num
        self.iInterface = istring
        self.eps = eps

    def __iter__(self):
        return iter(self.eps)


class FakeCfg:
    def __init__(self, intfs):
        self.intfs = intfs

    def __iter__(self):
        return iter(self.intfs)


class FakeDev:
    def __init__(self, cfg, pid=0x2987):
        self.idProduct = pid
        self.writes = []
        self.cfg = cfg

    def __iter__(self):
        return iter([self.cfg])

    def write(self, addr, data, timeout=None):
        self.writes.append((addr, bytes(data), timeout))
        return len(data)


class FakeUtil:
    @staticmethod
    def get_string(dev, index):
        return "esp32p4udisp0_R1280x800_Ejpg6_Fps46_Bl300000\x00\x00"

    @staticmethod
    def claim_interface(dev, num):
        dev.claimed = num

    @staticmethod
    def release_interface(dev, num):
        dev.released = num


class FakeCore:
    def __init__(self, devs):
        self.devs = devs

    def find(self, idVendor=None, idProduct=None):
        return self.devs.get(idProduct)


class FakeUsb:
    def __init__(self, devs):
        self.core = FakeCore(devs)
        self.util = FakeUtil()


def _dev():
    # 接口 0: 非 vendor(比如 HID) 先排掉; 接口 1: vendor, 先给一个 IN 端点再给 OUT
    return FakeDev(FakeCfg([
        FakeIntf(0x03, [FakeEp(0x81)]),
        FakeIntf(0xFF, [FakeEp(0x82), FakeEp(0x01)], num=1),
    ]))


def test_open_picks_vendor_intf_and_bulk_out():
    dev = _dev()
    with udisp_usb.open_display(usb=FakeUsb({0x2987: dev})) as d:
        assert d.intf.bInterfaceNumber == 1
        assert d.out_ep.bEndpointAddress == 0x01
        assert d.mps == 512
        assert (d.width, d.height) == (1280, 800)        # 从配置串解析
        assert d.cfg["fps"] == 46
    assert dev.claimed == 1 and dev.released == 1


def test_open_not_found():
    try:
        udisp_usb.open_display(usb=FakeUsb({}))
    except udisp_usb.DisplayNotFound:
        return
    raise AssertionError("没有设备时应抛 DisplayNotFound")


def test_send_frame_header_and_counter():
    dev = _dev()
    with udisp_usb.open_display(usb=FakeUsb({0x2987: dev})) as d:
        d.send(b"\xff\xd8" + b"x" * 99)
        d.send(b"\xff\xd8" + b"y" * 99)
    addr, data, _t = dev.writes[0]
    assert addr == 0x01
    hdr = udisp.HEADER.unpack(data[:16])
    assert hdr[1] == udisp.TYPE_JPG and (hdr[5], hdr[6]) == (1280, 800)
    assert hdr[7] & 0x3FF == 0                         # 第一帧 frame_id 0
    assert udisp.HEADER.unpack(dev.writes[1][1][:16])[7] & 0x3FF == 1
    assert len(dev.writes) == 2                        # 115 字节, 不是包长整数倍, 不补 zlp


def test_send_adds_fake_zlp_on_exact_multiple():
    dev = _dev()
    with udisp_usb.open_display(usb=FakeUsb({0x2987: dev})) as d:
        d.send(b"z" * (512 - 16))                      # 16+496 = 512, 整数倍
    assert len(dev.writes) == 2
    zlp = dev.writes[1][1]
    assert len(zlp) == 16 and zlp[2] == 0xFF


def test_send_uses_given_rect_for_partial_update():
    dev = _dev()
    with udisp_usb.open_display(usb=FakeUsb({0x2987: dev})) as d:
        d.send(b"\xff\xd8part", x=10, y=20, width=40, height=30)
    hdr = udisp.HEADER.unpack(dev.writes[0][1][:16])
    assert (hdr[3], hdr[4], hdr[5], hdr[6]) == (10, 20, 40, 30)


def test_frame_id_wraps():
    dev = _dev()
    with udisp_usb.open_display(usb=FakeUsb({0x2987: dev})) as d:
        d.frame_id = 1023
        d.send(b"a")
        assert d.frame_id == 0


def test_push_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("usb busy")
        return _FakeDisplay()

    monkeypatch.setattr(udisp_usb, "open_display", flaky)
    monkeypatch.setattr(udisp_usb.time, "sleep", lambda s: None)
    n, frames = udisp_usb.push(b"jpeg")
    assert calls["n"] == 3 and frames == 1


class _FakeDisplay:
    frames = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def send(self, payload, **kw):
        self.frames += 1
        return len(payload) + 16
