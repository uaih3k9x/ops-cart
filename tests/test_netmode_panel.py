"""netmode_panel 测试: 不起窗口, 用假面板调方法; 对话框、nmcli、探测、日志全部打桩."""

from types import SimpleNamespace

import netmode_core as nmc
import netmode_panel as panel

CONS = "\n".join([
    "campus-net:6c4d-1:802-11-wireless:wlp4s0:yes",
    "enp3s0:1603-3:802-3-ethernet:enp3s0:yes",
    "Lan1-pxe:a737-4:802-3-ethernet:eno1:yes",
])
IP_BR = "eno1 UP 192.0.2.1/24\nenp3s0 UP 198.51.100.7/24"
ROUTES = "default via 198.51.100.1 dev enp3s0 proto dhcp metric 100"
DETAILS = {"1603-3": "connection.id:enp3s0\nconnection.uuid:1603-3\nconnection.interface-name:enp3s0",
           "a737-4": "connection.id:Lan1-pxe\nconnection.uuid:a737-4\nconnection.interface-name:eno1"}


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
    (tmp_path / "sc").mkdir(exist_ok=True)
    fake = SimpleNamespace(
        port=port, state=state, cons_text=CONS, state_path=str(tmp_path / "netmode.json"),
        details=dict(DETAILS), pxe_active=False, backup_root=str(tmp_path / "bk"), nm_dir=str(tmp_path / "sc"),
        views=nmc.port_views(state, CONS, IP_BR, ROUTES, links or {"eno1": True, "enp3s0": True}),
        _form=lambda: dict(form or {}), after=lambda ms, fn: None, reload=lambda: None,
        _say=lambda *a, **k: calls["say"].append((a[0], a[1] if len(a) > 1 else k.get("ok", True))),
        _bg=lambda work, done, note="": calls["bg"].append((work, done)))
    for name in ("_view", "_switch", "_gateway_changed"):
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
                              "egress": "backup"})
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
    msgs = [m for k, m in calls["events"] if k == "netmode"]
    assert len(msgs) == 3 and "删旧配置" in msgs[-1]                  # undo / apply / 删旧配置 各一行
    assert calls["ran"][-1] == "nmcli con delete uuid 1603-3"           # 接管: 备份后删旧的 enp3s0
    saved = list((tmp_path / "bk").glob("*/enp3s0.nmcli.txt"))          # 没 keyfile: 存了 nmcli 详情
    assert saved and "interface-name:enp3s0" in saved[0].read_text()
    assert calls["say"][-1][1] is True and "已删" in calls["say"][-1][0]


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
    assert not any("delete uuid 1603-3" in c for c in calls["ran"])  # 没切成: 旧连接不删
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
    st["prev"]["enp3s0"] = nmc.rec("static", "enp3s0", nmc.static_params("enp3s0", "192.0.2.5/24"))
    st["modes"].append(dict(nmc.rec("dhcp", "enp3s0"), res=[{"con": "u9", "name": "x", "iface": "enp3s0"}],
                            status="ok"))
    calls, fake = _fake(monkeypatch, tmp_path, state=st, yes=False)
    panel.NetModePanel.back(fake)
    assert "改为: 静态 IP 192.0.2.5/24" in calls["ask"][0] and "nmcli con delete uuid u9" in calls["ask"][0]


def test_back_to_legacy_from_old_state_is_refused(monkeypatch, tmp_path):
    """1.09.00 记下的「上一个」可能是旧连接; 交还旧配置去掉了, 说人话, 不弹框."""
    st = nmc.empty_state()
    st["prev"]["enp3s0"] = nmc.legacy("enp3s0", "1603-3", "enp3s0")
    st["modes"].append(dict(nmc.rec("dhcp", "enp3s0"), res=[{"con": "u9", "name": "x", "iface": "enp3s0"}],
                            status="ok"))
    calls, fake = _fake(monkeypatch, tmp_path, state=st)
    panel.NetModePanel.back(fake)
    assert calls["ask"] == [] and "交还旧配置" in calls["say"][-1][0] and calls["say"][-1][1] is False


# ------------------------------------------------- 接管时删旧配置 (交还旧配置 10/31 去掉了)

def test_takeover_lan1_confirm_lists_old_cons_and_pxe_rebuild(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1", yes=False,
                        form={"mode": "static", "addr": "192.0.2.1/24", "egress": "never"})
    panel.NetModePanel.apply(fake)
    text = calls["ask"][0]
    assert "将删除旧配置: Lan1-pxe" in text and str(tmp_path / "bk") in text
    assert "装机口地址要用静态 IP 重建, PXE 服务会停" in text
    assert calls["bg"] == []                                           # 点「否」什么都不动


def test_takeover_lan1_backs_up_keyfile_then_deletes(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1",
                        form={"mode": "static", "addr": "192.0.2.1/24", "egress": "never"})
    (tmp_path / "sc" / "Lan1-pxe.nmconnection").write_text("[connection]\nid=Lan1-pxe\nuuid=a737-4\n")
    panel.NetModePanel.apply(fake)
    _finish(calls)
    assert calls["ran"][-1] == "nmcli con delete uuid a737-4"
    assert list((tmp_path / "bk").glob("*/Lan1-pxe.nmconnection"))
    assert not any("1603-3" in c for c in calls["ran"])                # 别的口一律不碰
    assert any("删旧配置" in m and "Lan1-pxe" in m for _, m in calls["events"])


def test_takeover_backup_fails_keeps_old_con(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path, port="eno1",
                        form={"mode": "static", "addr": "192.0.2.1/24", "egress": "never"})
    fake.details = {}                                                  # 没 keyfile 也读不到详情: 没法备份
    panel.NetModePanel.apply(fake)
    _finish(calls)
    assert not any("delete" in c for c in calls["ran"])
    assert calls["say"][-1][1] is True and "没删" in calls["say"][-1][0]


# ------------------------------------------------- 网关一直能填: 填了自动备用出口, 清空回到不当出口 (10/31)

def _gw_fake(monkeypatch, tmp_path, port, egress, gw):
    calls, fake = _fake(monkeypatch, tmp_path, port=port)
    fake.egress_var, fake.gw_var = _Var(egress), _Var(gw)
    fake._form_changed = lambda: calls.setdefault("changed", []).append(fake.egress_var.get())
    return calls, fake


def test_gateway_filled_on_lan2_switches_to_backup(monkeypatch, tmp_path):
    calls, fake = _gw_fake(monkeypatch, tmp_path, "enp3s0", "never", "192.0.2.1")
    fake._gateway_changed()
    assert fake.egress_var.get() == "backup" and calls["changed"] == ["backup"]
    fake.gw_var.set("")
    fake._gateway_changed()
    assert fake.egress_var.get() == "never"


def test_gateway_on_lan1_never_makes_it_egress(monkeypatch, tmp_path):
    _, fake = _gw_fake(monkeypatch, tmp_path, "eno1", "never", "192.0.2.254")
    fake._gateway_changed()
    assert fake.egress_var.get() == "never"


# ------------------------------------------------- 按对端预填 (#13 行为不变: 对端也要带前缀)

def test_prefill_from_peer_needs_prefix(monkeypatch, tmp_path):
    calls, fake = _fake(monkeypatch, tmp_path)
    fake.addr_var, fake.peer_var = _Var("192.0.2.5/24"), _Var("192.0.2.1")
    panel.NetModePanel.prefill(fake)
    assert fake.addr_var.get() == "192.0.2.5/24" and "前缀" in calls["say"][-1][0]
    fake.peer_var.set("192.0.2.1/25")
    panel.NetModePanel.prefill(fake)
    assert fake.addr_var.get() == "192.0.2.126/25"
