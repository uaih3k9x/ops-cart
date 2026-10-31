"""netmode_panel 测试: 不起窗口, 用假面板调方法; 对话框、nmcli、探测、日志全部打桩."""

from types import SimpleNamespace

import pytest

import netmode_core as nmc
import netmode_panel as panel
import netprof_core as npc

CONS = "\n".join([
    "campus-net:6c4d-1:802-11-wireless:wlp4s0:yes",
    "enp3s0:1603-3:802-3-ethernet:enp3s0:yes",
    "Lan1-pxe:a737-4:802-3-ethernet:eno1:yes",
])
IP_BR = "eno1 UP 192.0.2.1/24\nenp3s0 UP 198.51.100.7/24"
ROUTES = "default via 198.51.100.1 dev enp3s0 proto dhcp metric 100"


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _fake(monkeypatch, tmp_path, port="enp3s0", form=None, yes=True, state=None, links=None):
    """假面板: 只带 apply/_switch 用到的字段; 记下弹了哪些确认框、进没进后台、说了什么."""
    calls = {"bg": [], "say": [], "ask": [], "events": [], "ran": []}
    monkeypatch.setattr(panel.messagebox, "askyesno", lambda t, m, **k: calls["ask"].append(m) or yes)
    monkeypatch.setattr(panel.cartlog, "event", lambda kind, msg: calls["events"].append((kind, msg)))
    monkeypatch.setattr(panel, "_run", lambda cmd, timeout=30: calls["ran"].append(" ".join(cmd)) or (0, ""))
    state = state or nmc.empty_state()
    fake = SimpleNamespace(
        port=port, state=state, cons_text=CONS, state_path=str(tmp_path / "netmode.json"),
        views=nmc.port_views(state, CONS, IP_BR, ROUTES, links or {"eno1": True, "enp3s0": True}),
        _form=lambda: dict(form or {}), after=lambda ms, fn: None, reload=lambda: None,
        _say=lambda *a, **k: calls["say"].append((a[0], a[1] if len(a) > 1 else k.get("ok", True))),
        _bg=lambda work, done, note="": calls["bg"].append((work, done)))
    for name in ("_view", "_switch"):
        setattr(fake, name, getattr(panel.NetModePanel, name).__get__(fake))
    return calls, fake


def _finish(calls):
    """把进了后台的那件事跑完 (同一线程), 再回调 done."""
    work, done = calls["bg"][-1]
    done(work())


# ------------------------------------------------- 静态一页表单: 校验不过不弹框、不动网口

def test_apply_static_without_prefix_says_why(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, form={"mode": "static", "addr": "192.0.2.5"})
    panel.NetModePanel.apply(fake)
    assert calls["ask"] == [] and calls["bg"] == []
    assert "前缀" in calls["say"][-1][0] and calls["say"][-1][1] is False


def test_apply_lan1_as_egress_refused(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1",
                        form={"mode": "static", "addr": "192.0.2.5/24", "gateway": "192.0.2.254",
                              "egress": "primary"})
    panel.NetModePanel.apply(fake)
    assert calls["bg"] == [] and "LAN1" in calls["say"][-1][0]


def test_apply_confirm_no_touches_nothing(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, yes=False,
                        form={"mode": "static", "addr": "192.0.2.5/24", "egress": "never"})
    panel.NetModePanel.apply(fake)
    assert len(calls["ask"]) == 1 and calls["bg"] == []
    text = calls["ask"][0]
    assert "将执行的命令" in text and "出问题时的 undo 命令" in text and "198.51.100.7/24" in text


def test_apply_yes_switches_writes_state_and_events(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path,
                        form={"mode": "static", "addr": "192.0.2.5/24", "gateway": "192.0.2.1",
                              "dns": "", "egress": "backup"})
    panel.NetModePanel.apply(fake)
    assert len(calls["bg"]) == 1 and calls["ran"] == []           # 确认后才进后台
    _finish(calls)
    assert any("con add" in c and "ipv4.route-metric 1000" in c for c in calls["ran"])
    assert not any("wlp4s0" in c for c in calls["ran"])
    rec = nmc.owner(nmc.load_state(fake.state_path), "enp3s0")
    assert rec["mode"] == "static" and rec["params"]["egress"] == "backup"
    assert fake.state is not None and nmc.owner(fake.state, "enp3s0")["status"] == "ok"
    assert [k for k, _ in calls["events"]] == ["netmode", "netmode"]   # undo / apply 各一行
    assert calls["say"][-1][1] is True


def test_apply_on_unplugged_port_only_creates_profile(monkeypatch, tmp_path):
    """先按设备出厂网段配好 LAN1 再插线: 没插线不 con up (NM 会拒), 不该当成失败回滚."""
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1", links={"eno1": False, "enp3s0": True},
                        form={"mode": "static", "addr": "192.0.2.250/24", "egress": "never"})
    panel.NetModePanel.apply(fake)
    assert "没插线" in calls["ask"][0]
    _finish(calls)
    assert any("con add" in c for c in calls["ran"]) and not any(c.startswith("nmcli con up") for c in calls["ran"])
    assert calls["say"][-1][1] is True and "插上线" in calls["say"][-1][0]


def test_apply_failure_says_rolled_back(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, form={"mode": "dhcp"})
    panel.NetModePanel.apply(fake)
    monkeypatch.setattr(panel, "_run", lambda cmd, timeout=30: calls["ran"].append(" ".join(cmd)) or (
        (1, "no dhcp") if cmd[:3] == ["nmcli", "con", "up"] and cmd[-1] != "1603-3" else (0, "")))
    _finish(calls)
    assert calls["ran"][-1] == "nmcli con up uuid 1603-3"            # 回到原来的旧连接
    assert "回到原来" in calls["say"][-1][0] and calls["say"][-1][1] is False
    assert nmc.owner(nmc.load_state(fake.state_path), "enp3s0") is None


def test_same_mode_again_is_a_no_op(monkeypatch, tmp_path):
    st = nmc.empty_state()
    st["modes"].append(dict(nmc.rec("dhcp", "enp3s0"), res=[{"con": "1603-3", "name": "x", "iface": "enp3s0"}],
                            status="ok"))
    calls, fake = _fake(monkeypatch, tmp_path, form={"mode": "dhcp"}, state=st)
    panel.NetModePanel.apply(fake)
    assert calls["ask"] == [] and calls["bg"] == [] and "已经是" in calls["say"][-1][0]


# ------------------------------------------------- 撤到干净状态 / 回到上一个

def test_undo_to_clean_on_lan1_goes_off(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1", yes=False)
    panel.NetModePanel.undo_clean(fake)
    assert "改为: 关闭" in calls["ask"][0] and "PXE" in calls["ask"][0]


def test_back_without_history(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path)
    panel.NetModePanel.back(fake)
    assert calls["ask"] == [] and calls["say"][-1][1] is False


def test_back_reapplies_previous_mode(monkeypatch, tmp_path):
    st = nmc.empty_state()
    st["prev"]["enp3s0"] = nmc.legacy("enp3s0", "1603-3", "enp3s0")
    st["modes"].append(dict(nmc.rec("dhcp", "enp3s0"), res=[{"con": "u9", "name": "x", "iface": "enp3s0"}],
                            status="ok"))
    calls, fake = _fake(monkeypatch, tmp_path, state=st, yes=False)
    panel.NetModePanel.back(fake)
    assert "旧配置「enp3s0」" in calls["ask"][0] and "nmcli con delete uuid u9" in calls["ask"][0]


# ------------------------------------------------- 交还装机配置: 先探测别人的 DHCP (旧面板的保护照旧)

def test_handback_to_serve_profile_probes_and_refuses(monkeypatch, tmp_path):
    st = nmc.empty_state()
    st["modes"].append(dict(nmc.rec("off", "eno1"), res=[{"con": "u7", "name": "cart-eno1-off", "iface": "eno1"}],
                            status="ok"))
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1", state=st,
                        form={"mode": "legacy", "legacy": "Lan1-pxe"})
    monkeypatch.setattr(panel.npc, "dhcp_probe",
                        lambda iface: npc.ProbeResult([{"server": "192.0.2.254", "yiaddr": "192.0.2.50"}]))
    panel.NetModePanel.apply(fake)
    assert "探测" in calls["ask"][0]
    _finish(calls)
    assert calls["ran"] == ["systemctl stop pxe-server"]              # 只停了自己去探测, 没动网口
    assert "192.0.2.254" in calls["say"][-1][0] and calls["say"][-1][1] is False
    assert calls["events"] and "未切" in calls["events"][-1][1]


@pytest.mark.parametrize("carrier", [False, None])
def test_handback_to_serve_profile_without_link_refuses(monkeypatch, tmp_path, carrier):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1",
                        form={"mode": "legacy", "legacy": "Lan1-pxe"},
                        state=dict(nmc.empty_state(), modes=[dict(nmc.rec("off", "eno1"), status="ok",
                                                                   res=[{"con": "u7", "name": "n", "iface": "eno1"}])]))
    monkeypatch.setattr(panel.npc, "dhcp_probe", lambda iface: npc.ProbeResult(carrier=carrier))
    panel.NetModePanel.apply(fake)
    _finish(calls)
    assert "插线" in calls["say"][-1][0] and calls["ran"] == ["systemctl stop pxe-server"]


# ------------------------------------------------- 按对端预填 (#13 行为不变: 对端也要带前缀)

def test_prefill_from_peer_needs_prefix(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path)
    fake.addr_var, fake.peer_var = _Var("192.0.2.5/24"), _Var("192.0.2.1")
    panel.NetModePanel.prefill(fake)
    assert fake.addr_var.get() == "192.0.2.5/24" and "前缀" in calls["say"][-1][0]
    fake.peer_var.set("192.0.2.1/25")
    panel.NetModePanel.prefill(fake)
    assert fake.addr_var.get() == "192.0.2.126/25"
