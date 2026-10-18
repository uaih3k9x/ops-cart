"""serial_panel 测试: 列表解析与命令构造, 不碰真串口."""

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
    assert captured["cmd"] == sp.CONBROKER + ["list"]
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
    assert sp.CONBROKER[-2:] == ["/home/user/conbroker/.venv/bin/python",
                                 "/home/user/conbroker/conbroker.py"]
    assert ["serve", "ttyUSB0", "-b", "9600"][0] == "serve"
    assert ["send", "ttyUSB0", "display vlan"][0] == "send"
    assert ["send", "ttyUSB0", "--raw", "\\x03"][0] == "send"
    assert ["tail", "ttyUSB0", "-n", "80"][0] == "tail"
    assert ["stop", "ttyUSB0"][0] == "stop"


def test_kiosk_runs_conbroker_as_uaih3k9x():
    # Kiosk 是 root, 必须以 uaih3k9x 身份跑, HOME/数据目录才对齐
    assert sp.CONBROKER[:3] == ["sudo", "-u", "uaih3k9x", "-H"][:3]
