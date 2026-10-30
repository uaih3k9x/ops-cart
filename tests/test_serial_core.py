"""serial_core 测试: 列表解析与命令构造, 不碰真串口, 不起 Tk."""

import json

import pytest
import serial_core as sc


def test_run_passes_args_to_conbroker(monkeypatch):
    captured = {}
    class FakeResult:
        stdout = "OK\n"
        stderr = ""
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda cmd, **kw: captured.update(cmd=list(cmd)) or FakeResult())
    out = sc._run(["list"])
    assert captured["cmd"] == sc._conbroker_cmd() + ["list"]
    assert captured["cmd"][0:2] == ["sudo", "-u"]
    assert "OK" in out


def test_run_appends_stderr_when_present(monkeypatch):
    class FakeResult:
        stdout = "out"
        stderr = "boom"
    monkeypatch.setattr(sc.subprocess, "run", lambda cmd, **kw: FakeResult())
    assert "[stderr] boom" in sc._run(["list"])


def test_run_catches_exception(monkeypatch):
    def boom(cmd, **kw):
        raise TimeoutError("slow")
    monkeypatch.setattr(sc.subprocess, "run", boom)
    out = sc._run(["list"])
    assert out.startswith("[错误]")


def test_bauds_default_9600():
    assert "9600" in sc.BAUDS
    assert "115200" in sc.BAUDS


def test_conbroker_command_shapes_subcommand_first():
    # 参数形状按接入文档: 子命令在前, 口在后
    assert ["serve", "ttyUSB0", "-b", "9600"][0] == "serve"
    assert ["send", "ttyUSB0", "display vlan"][0] == "send"
    assert ["send", "ttyUSB0", "--raw", "\\x03"][0] == "send"
    assert ["tail", "ttyUSB0", "-n", "80"][0] == "tail"
    assert ["stop", "ttyUSB0"][0] == "stop"


def test_serial_conf_default_is_generic():
    # 仓库默认配置不含任何个人路径
    c = sc.serial_conf_read(path="/nonexistent")
    assert c["user"] == "ops"
    assert all(v.startswith("/home/ops") for k, v in c.items() if k in ("conbroker_home", "bin", "script"))


def test_serial_conf_custom(tmp_path):
    p = tmp_path / "serial.conf"
    p.write_text(json.dumps({"user": "alice", "conbroker_home": "/srv/cb"}))
    c = sc.serial_conf_read(path=str(p))
    assert c["bin"] == "/srv/cb/.venv/bin/python"
    assert c["script"] == "/srv/cb/conbroker.py"


def test_conbroker_cmd_uses_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "SERIAL_CONF", str(tmp_path / "s.conf"))
    (tmp_path / "s.conf").write_text(json.dumps({"user": "alice", "conbroker_home": "/srv/cb"}))
    cmd = sc._conbroker_cmd()
    assert cmd[:3] == ["sudo", "-u", "alice", "-H"][:3]
    assert cmd[4] == "/srv/cb/.venv/bin/python"      # [3] 是 -H


# ---------------------------------------------------------- 分屏: 实时控制台 + 聊天

def test_data_dir_explicit_and_default(tmp_path):
    assert sc.data_dir({"user": "x", "data_dir": "/srv/d"}) == "/srv/d"
    # 没写 data_dir: 用该用户家目录下的 .conbroker (与 conbroker 的 HOME 默认一致)
    import pwd
    me = pwd.getpwuid(__import__("os").getuid()).pw_name
    assert sc.data_dir({"user": me}).endswith("/.conbroker")


def test_broker_port_reads_state_file(tmp_path):
    (tmp_path / "run").mkdir()
    # conbroker 的 safe(): 非 [A-Za-z0-9_.-] 换成 _
    (tmp_path / "run" / "_dev_ttyUSB0.json").write_text(json.dumps({"port": 41303}))
    assert sc.broker_port(str(tmp_path), "/dev/ttyUSB0") == 41303
    assert sc.broker_port(str(tmp_path), "/dev/ttyS9") is None


def test_key_bytes_printable_and_specials():
    assert sc.key_bytes("a", "a", 0) == b"a"
    assert sc.key_bytes("Return", "\r", 0) == b"\r"
    # 华为 VRP 只认 ^H 退格, DEL(0x7f) 响铃不删 (10/27 AR651C 实测); Cisco/Linux readline 也认 ^H
    assert sc.key_bytes("BackSpace", "\x08", 0) == b"\x08"
    assert sc.key_bytes("Tab", "\t", 0) == b"\t"
    assert sc.key_bytes("Up", "", 0) == b"\x1b[A"
    assert sc.key_bytes("question", "?", 1) == b"?"


def test_key_bytes_ctrl_letters():
    CTRL = 0x4
    assert sc.key_bytes("c", "\x03", CTRL) == b"\x03"
    assert sc.key_bytes("z", "\x1a", CTRL) == b"\x1a"
    assert sc.key_bytes("bracketright", "\x1d", CTRL) is None   # Ctrl+] 留给面板自己, 不进串口


def test_key_bytes_ignores_modifiers_alone():
    assert sc.key_bytes("Shift_L", "", 1) is None
    assert sc.key_bytes("Control_L", "", 0) is None


def test_term_feed_strips_ansi_and_cr():
    t = sc.TermFeed()
    assert t.feed(b"\x1b[1mHi\x1b[0m\r\n<HUAWEI>") == [("text", "Hi\n<HUAWEI>")]


def test_term_feed_backspace_and_pager_erase():
    t = sc.TermFeed()
    assert t.feed(b"ab\x08") == [("text", "ab"), ("bs", 1)]
    # 华为翻页: "---- More ----" 后跟 ESC[nD + n 个空格 + ESC[nD 擦掉, 光标左移按退格算
    assert t.feed(b"---- More ----\x1b[14D" + b" " * 14 + b"\x1b[14D") == [
        ("text", "---- More ----"), ("bs", 14), ("text", " " * 14), ("bs", 14)]


def test_term_feed_escape_split_across_chunks():
    t = sc.TermFeed()
    assert t.feed(b"x\x1b[1") == [("text", "x")]
    assert t.feed(b"6D") == [("bs", 16)]


def test_term_feed_utf8_split_across_chunks():
    t = sc.TermFeed()
    raw = "中".encode()
    assert t.feed(raw[:2]) == []
    assert t.feed(raw[2:]) == [("text", "中")]


def test_parse_chat_line():
    assert sc.parse_chat_line("[2026-10-19 10:21:05 Claude] 收到") == ("10:21", "Claude", "收到")
    assert sc.parse_chat_line("[2026-10-19 10:22:00 ops] 已切 U6") == ("10:22", "ops", "已切 U6")
    assert sc.parse_chat_line("garbage") is None


def test_chat_name_comes_from_conf(tmp_path):
    assert sc.serial_conf_read(str(tmp_path / "none"))["chat_name"] == "operator"
    p = tmp_path / "serial.conf"
    p.write_text('{"chat_name": "alice"}')
    assert sc.serial_conf_read(str(p))["chat_name"] == "alice"


# ============================================================ C 线: 列表解析 / by-id / 键鼠口 / 粘贴 / 线程

LIST_OUT = """串口:
  /dev/ttyUSB0  USB Serial
  /dev/ttyACM0  USB Single Serial
服务:
  /dev/serial/by-id/usb-FTDI_X_0001-if00-port0  115200  在跑  2026-10-25 01:00:00
  /dev/ttyUSB9  9600  已死(残留)  2026-10-24 01:00:00

[stderr] some warning
"""


def test_parse_list_only_alive_services_with_baud():
    ports, serves = sc.parse_list(LIST_OUT)
    assert ports == ["/dev/ttyUSB0", "/dev/ttyACM0"]
    assert serves == {"/dev/serial/by-id/usb-FTDI_X_0001-if00-port0": "115200"}


def test_parse_list_garbage_is_empty():
    assert sc.parse_list("[错误] timeout") == ([], {})


BYID = {"/dev/serial/by-id/usb-FTDI_X_0001-if00-port0": "/dev/ttyUSB0",
        "/dev/serial/by-id/usb-1a86_USB_Single_Serial_0000000001-if00": "/dev/ttyACM0"}


def fake_real(p):
    return BYID.get(p, p)


def test_prefer_byid():
    assert sc.prefer_byid("/dev/ttyUSB0", list(BYID), fake_real) == "/dev/serial/by-id/usb-FTDI_X_0001-if00-port0"
    assert sc.prefer_byid("/dev/ttyS0", list(BYID), fake_real) == "/dev/ttyS0"


def test_match_service_by_name_or_same_device():
    serves = {"/dev/serial/by-id/usb-FTDI_X_0001-if00-port0": "115200"}
    assert sc.match_service("/dev/ttyUSB0", serves, fake_real) == "/dev/serial/by-id/usb-FTDI_X_0001-if00-port0"
    assert sc.match_service("/dev/ttyACM0", serves, fake_real) is None


def test_ch9329_ports_are_marked():
    kvm_ports = {"0000000001": "/dev/serial/by-id/usb-1a86_USB_Single_Serial_0000000001-if00"}
    assert sc.kvm_devices(kvm_ports, fake_real) == {"/dev/ttyACM0"}
    assert sc.port_label("/dev/ttyACM0", {"/dev/ttyACM0"}) == "/dev/ttyACM0 (键鼠 CH9329)"
    assert sc.port_label("/dev/ttyUSB0", {"/dev/ttyACM0"}) == "/dev/ttyUSB0"


def test_paste_chunks_line_by_line():
    assert sc.paste_chunks("a\r\nb\nc") == [b"a\r", b"b\r", b"c"]
    assert sc.paste_chunks("") == []
    assert sc.paste_chunks("x\n") == [b"x\r"]


def test_serve_error_text():
    assert sc.serve_error(1, "Traceback...\nserial.SerialException: busy\n") == "serial.SerialException: busy"
    assert sc.serve_error(2, "") == "退出码 2"


# ============================================================ 推车 hub 口别名 /dev/con-N (仿片序列号全一样, by-id 认不出)

CON = {"/dev/con-1": "/dev/ttyUSB0", "/dev/con-2": "/dev/ttyUSB1",
       "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0": "/dev/ttyUSB1"}


def con_real(p):
    return CON.get(p, p)


def test_console_ports_use_alias_and_hide_onboard():
    ports = ["/dev/ttyS1", "/dev/ttyS0", "/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyUSB7", "/dev/ttyACM0"]
    aliases = ["/dev/con-1", "/dev/con-2"]
    assert sc.console_ports(ports, aliases, con_real) == [
        "/dev/con-1", "/dev/con-2", "/dev/ttyACM0", "/dev/ttyUSB7"]


def test_serve_target_keeps_alias_over_byid():
    byid = ["/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0"]
    assert sc.serve_target("/dev/con-2", ["/dev/con-1", "/dev/con-2"], byid, con_real) == "/dev/con-2"
    assert sc.serve_target("/dev/ttyUSB1", [], byid, con_real) == byid[0]


def test_alias_paths(tmp_path):
    for n in ("con-2", "con-1", "ttyUSB0"):
        (tmp_path / n).write_text("")
    assert sc.alias_paths(str(tmp_path / "con-*")) == [str(tmp_path / "con-1"), str(tmp_path / "con-2")]


# ---------------------------------------------------------------- issue #5: 指定操作头

def test_select_port_exact_hit():
    assert sc.select_port(["/dev/con-1", "/dev/con-2"], "/dev/con-2") == ("/dev/con-2", True)


def test_select_port_missing_slot_still_selected():
    """没插线的那一路照样要选中, 只是标明不在列表里 (面板提示"未插")."""
    assert sc.select_port(["/dev/con-1"], "/dev/con-3") == ("/dev/con-3", False)


def test_select_port_matches_by_realpath():
    """别名和 by-id 指向同一个设备时, 选别名. 这里用假的 realpath 模拟."""
    real = {"/dev/con-2": "/dev/ttyUSB1", "/dev/serial/by-id/x": "/dev/ttyUSB1"}
    got = sc.select_port(["/dev/serial/by-id/x"], "/dev/con-2", realpath=real.get)
    assert got == ("/dev/serial/by-id/x", True)


# ============================================================ 无界面: 不许拖进 tkinter

@pytest.mark.parametrize("mod", ["serial_core", "cart_dream", "webkiosk", "dash_core", "dash_service"])
def test_headless_modules_do_not_import_tkinter(mod):
    import os
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.modules['tkinter'] = None; import {mod}"],
                       cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
