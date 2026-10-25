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
