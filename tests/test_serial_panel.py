"""serial_panel 测试: 列表解析与命令构造, 不碰真串口."""

import json

import pytest
import serial_panel as sp


def test_run_passes_args_to_conbroker(monkeypatch):
    captured = {}
    class FakeResult:
        stdout = "OK\n"
        stderr = ""
    monkeypatch.setattr(sp.subprocess, "run",
                        lambda cmd, **kw: captured.update(cmd=list(cmd)) or FakeResult())
    out = sp._run(["list"])
    assert captured["cmd"] == sp._conbroker_cmd() + ["list"]
    assert captured["cmd"][0:2] == ["sudo", "-u"]
    assert "OK" in out


def test_run_appends_stderr_when_present(monkeypatch):
    class FakeResult:
        stdout = "out"
        stderr = "boom"
    monkeypatch.setattr(sp.subprocess, "run", lambda cmd, **kw: FakeResult())
    assert "[stderr] boom" in sp._run(["list"])


def test_run_catches_exception(monkeypatch):
    def boom(cmd, **kw):
        raise TimeoutError("slow")
    monkeypatch.setattr(sp.subprocess, "run", boom)
    out = sp._run(["list"])
    assert out.startswith("[错误]")


def test_bauds_default_9600():
    assert "9600" in sp.BAUDS
    assert "115200" in sp.BAUDS


def test_conbroker_command_shapes_subcommand_first():
    # 参数形状按接入文档: 子命令在前, 口在后
    assert ["serve", "ttyUSB0", "-b", "9600"][0] == "serve"
    assert ["send", "ttyUSB0", "display vlan"][0] == "send"
    assert ["send", "ttyUSB0", "--raw", "\\x03"][0] == "send"
    assert ["tail", "ttyUSB0", "-n", "80"][0] == "tail"
    assert ["stop", "ttyUSB0"][0] == "stop"


def test_serial_conf_default_is_generic():
    # 仓库默认配置不含任何个人路径
    c = sp.serial_conf_read(path="/nonexistent")
    assert c["user"] == "ops"
    assert "uaih3k9x" not in json.dumps(c)


def test_serial_conf_custom(tmp_path):
    p = tmp_path / "serial.conf"
    p.write_text(json.dumps({"user": "alice", "conbroker_home": "/srv/cb"}))
    c = sp.serial_conf_read(path=str(p))
    assert c["bin"] == "/srv/cb/.venv/bin/python"
    assert c["script"] == "/srv/cb/conbroker.py"


def test_conbroker_cmd_uses_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "SERIAL_CONF", str(tmp_path / "s.conf"))
    (tmp_path / "s.conf").write_text(json.dumps({"user": "alice", "conbroker_home": "/srv/cb"}))
    cmd = sp._conbroker_cmd()
    assert cmd[:3] == ["sudo", "-u", "alice", "-H"][:3]
    assert cmd[4] == "/srv/cb/.venv/bin/python"      # [3] 是 -H


# ---------------------------------------------------------- 分屏: 实时控制台 + 聊天

def test_data_dir_explicit_and_default(tmp_path):
    assert sp.data_dir({"user": "x", "data_dir": "/srv/d"}) == "/srv/d"
    # 没写 data_dir: 用该用户家目录下的 .conbroker (与 conbroker 的 HOME 默认一致)
    import pwd
    me = pwd.getpwuid(__import__("os").getuid()).pw_name
    assert sp.data_dir({"user": me}).endswith("/.conbroker")


def test_broker_port_reads_state_file(tmp_path):
    (tmp_path / "run").mkdir()
    # conbroker 的 safe(): 非 [A-Za-z0-9_.-] 换成 _
    (tmp_path / "run" / "_dev_ttyUSB0.json").write_text(json.dumps({"port": 41303}))
    assert sp.broker_port(str(tmp_path), "/dev/ttyUSB0") == 41303
    assert sp.broker_port(str(tmp_path), "/dev/ttyS9") is None


def test_key_bytes_printable_and_specials():
    assert sp.key_bytes("a", "a", 0) == b"a"
    assert sp.key_bytes("Return", "\r", 0) == b"\r"
    assert sp.key_bytes("BackSpace", "\x08", 0) == b"\x7f"
    assert sp.key_bytes("Tab", "\t", 0) == b"\t"
    assert sp.key_bytes("Up", "", 0) == b"\x1b[A"
    assert sp.key_bytes("question", "?", 1) == b"?"


def test_key_bytes_ctrl_letters():
    CTRL = 0x4
    assert sp.key_bytes("c", "\x03", CTRL) == b"\x03"
    assert sp.key_bytes("z", "\x1a", CTRL) == b"\x1a"
    assert sp.key_bytes("bracketright", "\x1d", CTRL) is None   # Ctrl+] 留给面板自己, 不进串口


def test_key_bytes_ignores_modifiers_alone():
    assert sp.key_bytes("Shift_L", "", 1) is None
    assert sp.key_bytes("Control_L", "", 0) is None


def test_term_feed_strips_ansi_and_cr():
    t = sp.TermFeed()
    assert t.feed(b"\x1b[1mHi\x1b[0m\r\n<HUAWEI>") == [("text", "Hi\n<HUAWEI>")]


def test_term_feed_backspace_and_pager_erase():
    t = sp.TermFeed()
    assert t.feed(b"ab\x08") == [("text", "ab"), ("bs", 1)]
    # 华为翻页: "---- More ----" 后跟 ESC[nD + n 个空格 + ESC[nD 擦掉, 光标左移按退格算
    assert t.feed(b"---- More ----\x1b[14D" + b" " * 14 + b"\x1b[14D") == [
        ("text", "---- More ----"), ("bs", 14), ("text", " " * 14), ("bs", 14)]


def test_term_feed_escape_split_across_chunks():
    t = sp.TermFeed()
    assert t.feed(b"x\x1b[1") == [("text", "x")]
    assert t.feed(b"6D") == [("bs", 16)]


def test_term_feed_utf8_split_across_chunks():
    t = sp.TermFeed()
    raw = "中".encode()
    assert t.feed(raw[:2]) == []
    assert t.feed(raw[2:]) == [("text", "中")]


def test_parse_chat_line():
    assert sp.parse_chat_line("[2026-10-19 10:21:05 Claude] 收到") == ("10:21", "Claude", "收到")
    assert sp.parse_chat_line("[2026-10-19 10:22:00 op] 已切 U6") == ("10:22", "op", "已切 U6")
    assert sp.parse_chat_line("garbage") is None
