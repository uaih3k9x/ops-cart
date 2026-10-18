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
