"""netmode_core 测试 (#12 第一批): undo 框架、状态文件、关闭/DHCP/静态三个基础模式、出口优先级 (#25). 不碰真机."""

import itertools
import json

import pytest

import netmode_core as nm
import wifi_core as wc

CONS = "\n".join([
    "campus-net:6c4d-1:802-11-wireless:wlp4s0:yes",
    "tailscale0:0659-2:tun:tailscale0:yes",
    "enp3s0:1603-3:802-3-ethernet:enp3s0:yes",
    "Lan1-pxe:a737-4:802-3-ethernet:eno1:yes",
    "cart-enp3s0-static-华为交换机:bbbb-5:802-3-ethernet::no",
    "Wired connection 1:eeee-8:802-3-ethernet::no",
])
IP_BR = "\n".join([
    "lo               UNKNOWN        127.0.0.1/8",
    "eno1             UP             192.0.2.1/24",
    "enp3s0           UP             198.51.100.7/24",
    "wlp4s0           UP             203.0.113.9/24",
])
ROUTES = "\n".join([
    "default via 198.51.100.1 dev enp3s0 proto dhcp src 198.51.100.7 metric 100",
    "default via 203.0.113.1 dev wlp4s0 proto dhcp src 203.0.113.9 metric 900",
    "198.51.100.0/24 dev enp3s0 proto kernel scope link src 198.51.100.7 metric 100",
])
LINK = {"eno1": True, "enp3s0": True}


def uuids():
    it = (f"u{n}" for n in itertools.count(1))
    return lambda: next(it)


def kv_of(cmd):
    """nmcli con add 的 键 值 对 (从 type 起)."""
    pairs = cmd[3:]
    return {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}


def flat(cmds):
    return [" ".join(c) for c in cmds]


def static(iface="enp3s0", egress="never", gateway="", dns="", addr="192.0.2.5/24"):
    return nm.rec("static", iface, nm.static_params(iface, addr, gateway, dns, egress))


# ------------------------------------------------------------ 出口优先级常量 (#25, 和 #7 共用)

def test_egress_metrics_compare_against_wifi_in_one_place():
    assert wc.WIRED_PRIMARY_METRIC < wc.WIFI_METRIC < wc.WIRED_BACKUP_METRIC
    assert nm.EGRESS_METRIC == {"primary": wc.WIRED_PRIMARY_METRIC, "backup": wc.WIRED_BACKUP_METRIC}


# ------------------------------------------------------------ 规则

def test_modes_per_port_and_clean_state():
    assert nm.CLEAN == {"eno1": "off", "enp3s0": "dhcp"}
    assert nm.check_mode("eno1", "off") is None
    assert "LAN2" in nm.check_mode("enp3s0", "off")         # 管理口没有"关闭"
    assert nm.check_mode("enp3s0", "static") is None
    assert nm.check_mode("eno1", "legacy") is None
    assert nm.check_mode("eno1", "pxe") is not None          # 后续批次
    assert "无线" in nm.check_mode("wlp4s0", "dhcp")          # Wi-Fi 永远不碰


def test_rec_refuses_wifi_and_unknown_ports():
    with pytest.raises(ValueError, match="无线"):
        nm.rec("dhcp", "wlp4s0")
    with pytest.raises(ValueError):
        nm.rec("dhcp", "eth9")


# ------------------------------------------------------------ 静态表单 (一页填完)

def test_static_params_never_drops_gateway():
    p = nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "192.0.2.53", "never")
    assert p == {"addr": "192.0.2.5/24", "gateway": "", "dns": "192.0.2.53", "egress": "never"}


@pytest.mark.parametrize("egress", ["backup", "primary"])
def test_static_params_egress_needs_gateway(egress):
    with pytest.raises(ValueError, match="网关"):
        nm.static_params("enp3s0", "192.0.2.5/24", "", "", egress)
    p = nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "", egress)
    assert p["gateway"] == "192.0.2.1" and p["egress"] == egress


@pytest.mark.parametrize("egress", ["backup", "primary"])
def test_static_params_lan1_never_egress(egress):
    with pytest.raises(ValueError, match="LAN1"):
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", egress)


def test_static_params_reuses_static_form_checks():
    with pytest.raises(ValueError, match="前缀"):
        nm.static_params("enp3s0", "192.0.2.5")              # 不猜 /24
    with pytest.raises(ValueError, match="网段"):
        nm.static_params("enp3s0", "192.0.2.5/24", "198.51.100.1", "", "primary")
    with pytest.raises(ValueError, match="对端"):
        nm.static_params("enp3s0", "192.0.2.5/24", avoid="192.0.2.5")
    with pytest.raises(ValueError):
        nm.static_params("enp3s0", "192.0.2.5/24", egress="sideways")


def test_target_from_form():
    olds = nm.port_views(nm.empty_state(), CONS, IP_BR, ROUTES, LINK)[0]["legacy"]
    assert nm.target("eno1", {"mode": "off"}) == nm.rec("off", "eno1")
    assert nm.target("eno1", {"mode": "legacy", "legacy": "Lan1-pxe"}, olds) == \
        nm.legacy("eno1", "a737-4", "Lan1-pxe")
    with pytest.raises(ValueError, match="旧配置"):
        nm.target("eno1", {"mode": "legacy", "legacy": ""}, olds)
    r = nm.target("enp3s0", {"mode": "static", "addr": " 192.0.2.5/25 ", "gateway": "192.0.2.1",
                             "dns": "", "egress": "backup", "peer": "192.0.2.9"})
    assert r["params"] == {"addr": "192.0.2.5/25", "gateway": "192.0.2.1", "dns": "", "egress": "backup"}
    with pytest.raises(ValueError, match="对端"):
        nm.target("enp3s0", {"mode": "static", "addr": "192.0.2.9/24", "peer": "192.0.2.9"})


def test_form_hint_by_mode():
    assert "DHCP" in nm.form_hint("enp3s0", "dhcp", {})
    assert "不当出口" in nm.form_hint("eno1", "dhcp", {})
    assert "不配地址" in nm.form_hint("eno1", "off", {})
    assert "不会删" in nm.form_hint("eno1", "legacy", {})
    assert "地址/前缀" in nm.form_hint("enp3s0", "static", {"addr": ""})
    h = nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5/25", "gateway": "192.0.2.1", "egress": "backup"})
    assert "192.0.2.0/25" in h and "1000" in h and "900" in h
    assert "前缀" in nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5"})          # 报错, 不猜
    assert "网关" in nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5/24", "egress": "primary"})


def test_form_state_greys_out_what_does_not_apply():
    s = nm.form_state("eno1", "static", "never")
    assert s["addr"] and not s["gateway"] and s["egress"] == {"never": True, "backup": False, "primary": False}
    s = nm.form_state("enp3s0", "static", "primary")
    assert s["gateway"] and all(s["egress"].values())
    s = nm.form_state("enp3s0", "dhcp", "never")
    assert not s["addr"] and not s["gateway"] and not any(s["egress"].values())


# ------------------------------------------------------------ apply 命令清单 (纯函数)

def test_static_primary_on_lan2():
    cmds, res = nm.apply_steps(static(egress="primary", gateway="192.0.2.1"), uuids())
    add, up = cmds
    assert add[:4] == ["nmcli", "con", "add", "type"]
    kv = kv_of(add)
    assert kv["ifname"] == "enp3s0" and kv["con-name"] == "cart-enp3s0-static" and kv["connection.uuid"] == "u1"
    assert kv["ipv4.method"] == "manual" and kv["ipv4.addresses"] == "192.0.2.5/24"
    assert kv["ipv4.gateway"] == "192.0.2.1"
    assert kv["ipv4.dns"] == ""                                   # 留空显式清掉
    assert kv["ipv4.never-default"] == "no" and kv["ipv6.never-default"] == "no"
    assert kv["ipv4.route-metric"] == kv["ipv6.route-metric"] == str(wc.WIRED_PRIMARY_METRIC)
    assert up == ["nmcli", "con", "up", "uuid", "u1"]
    assert res == [{"con": "u1", "name": "cart-enp3s0-static", "iface": "enp3s0"}]


def test_static_backup_metric_above_wifi():
    cmds, _ = nm.apply_steps(static(egress="backup", gateway="192.0.2.1"), uuids())
    kv = kv_of(cmds[0])
    assert int(kv["ipv4.route-metric"]) > wc.WIFI_METRIC and kv["ipv6.route-metric"] == kv["ipv4.route-metric"]


def test_static_never_is_never_default_v4_and_v6():
    cmds, _ = nm.apply_steps(static(dns="192.0.2.53"), uuids())
    kv = kv_of(cmds[0])
    assert kv["ipv4.never-default"] == "yes" and kv["ipv6.never-default"] == "yes"
    assert kv["ipv4.gateway"] == "" and kv["ipv4.dns"] == "192.0.2.53"
    assert "ipv4.route-metric" not in kv


def test_lan1_modes_stop_pxe_and_never_default():
    cmds, _ = nm.apply_steps(nm.rec("dhcp", "eno1"), uuids())
    assert cmds[0][:2] == ["systemctl", "stop"] and "pxe-server" in cmds[0]
    kv = kv_of(cmds[1])
    assert kv["ipv4.method"] == "auto" and kv["ipv4.never-default"] == "yes" and kv["ipv6.never-default"] == "yes"


def test_lan1_egress_refused_even_if_params_forged():
    forged = nm.rec("static", "eno1", {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "",
                                        "egress": "primary"})
    with pytest.raises(ValueError, match="LAN1"):
        nm.apply_steps(forged, uuids())


def test_off_disables_both_families():
    cmds, res = nm.apply_steps(nm.rec("off", "eno1"), uuids())
    kv = kv_of(cmds[1])
    assert kv["con-name"] == "cart-eno1-off"
    assert kv["ipv4.method"] == "disabled" and kv["ipv6.method"] == "disabled"
    assert cmds[-1] == ["nmcli", "con", "up", "uuid", "u1"] and res[0]["con"] == "u1"


def test_lan2_dhcp_is_plain_dhcp():
    cmds, _ = nm.apply_steps(nm.rec("dhcp", "enp3s0"), uuids())
    assert len(cmds) == 2 and "systemctl" not in flat(cmds)[0]
    kv = kv_of(cmds[0])
    assert kv["ipv4.method"] == "auto" and "ipv4.never-default" not in kv


def test_legacy_apply_only_brings_it_up_and_owns_nothing():
    cmds, res = nm.apply_steps(nm.legacy("eno1", "a737-4", "Lan1-pxe"), uuids())
    assert cmds == [["nmcli", "con", "up", "uuid", "a737-4"]] and res == []
    assert nm.apply_steps(nm.legacy("eno1"), uuids()) == ([], [])     # 原来什么都没生效: 没东西可恢复


# ------------------------------------------------------------ undo

def test_undo_reverses_and_restores_recorded_sysctl():
    r = dict(nm.rec("dhcp", "eno1"), res=[{"con": "u1", "name": "cart-eno1-x", "iface": "eno1"},
                                         nm.sysctl_res("net.ipv4.ip_forward", "0")])
    assert nm.undo_steps(r) == [["sysctl", "-w", "net.ipv4.ip_forward=0"],
                                ["nmcli", "con", "delete", "uuid", "u1"]]


def test_set_sysctl_records_original_value():
    cmd, res = nm.set_sysctl("net.ipv4.ip_forward", "1", orig="0")
    assert cmd == ["sysctl", "-w", "net.ipv4.ip_forward=1"]
    assert res == {"sysctl": "net.ipv4.ip_forward", "orig": "0"}


def test_undo_includes_stale_leftovers_and_never_legacy():
    r = dict(nm.rec("dhcp", "enp3s0"), res=[{"con": "u2", "name": "n", "iface": "enp3s0"}],
             stale=[{"con": "u1", "name": "m", "iface": "enp3s0"}])
    assert flat(nm.undo_steps(r)) == ["nmcli con delete uuid u2", "nmcli con delete uuid u1"]
    assert nm.undo_steps(nm.legacy("eno1", "a737-4", "Lan1-pxe")) == []    # 旧连接不归它删


def test_undo_unknown_resource_refuses():
    with pytest.raises(ValueError):
        nm.undo_steps(dict(nm.rec("dhcp", "eno1"), res=[{"nft": "cart"}]))


# ------------------------------------------------------------ 状态文件

def test_state_missing_file_is_empty(tmp_path):
    st = nm.load_state(str(tmp_path / "none.json"))
    assert st["modes"] == [] and st["prev"] == {} and not st.get("broken")


def test_state_roundtrip_atomic(tmp_path):
    path = str(tmp_path / "sub" / "netmode.json")
    st = nm.empty_state()
    st["modes"].append(dict(static(), res=[{"con": "u1", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
                            status="ok"))
    nm.save_state(path, st)
    assert nm.load_state(path) == st
    assert not (tmp_path / "sub" / "netmode.json.tmp").exists()


def test_state_broken_file_reports_and_keeps_going(tmp_path):
    p = tmp_path / "netmode.json"
    p.write_text("{half")
    st = nm.load_state(str(p))
    assert st["modes"] == [] and st["broken"]


def test_state_lookup_by_port_supports_multi_port_records():
    st = nm.empty_state()
    st["modes"].append({"mode": "bridge", "ports": ["eno1", "enp3s0"], "params": {}, "res": [], "status": "ok"})
    assert nm.owner(st, "enp3s0")["mode"] == "bridge" and nm.owner(st, "eno1") is nm.owner(st, "enp3s0")


# ------------------------------------------------------------ 计划: undo 当前 -> apply 新

def test_plan_from_unmanaged_does_not_touch_legacy():
    plan = nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), CONS, uuids())
    assert plan["undo"] == []                                     # 未托管: 什么都不删
    assert [c["mode"] for c in plan["cur"]] == ["legacy"]
    assert plan["cur"][0]["params"]["name"] == "Lan1-pxe"
    assert ["nmcli", "con", "up", "uuid", "a737-4"] in plan["restore"]
    assert ["nmcli", "con", "delete", "uuid", "u1"] in plan["rollback"]
    assert not any("a737-4" in c and "delete" in c for c in plan["undo"] + plan["apply"] + plan["rollback"])


def test_plan_from_managed_deletes_only_what_it_recorded():
    st = nm.empty_state()
    st["modes"].append(dict(static(), res=[{"con": "u0", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
                            status="ok"))
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    assert plan["undo"] == [["nmcli", "con", "delete", "uuid", "u0"]]
    assert flat(plan["commands"])[0] == "nmcli con delete uuid u0"   # 先 undo 再 apply
    assert "con add" in flat(plan["commands"])[1]
    # 回到原来 = 用记下的参数重建一份 (新 uuid)
    assert any("192.0.2.5/24" in c for c in flat(plan["restore"]))


def test_plan_multi_port_mode_blocks_single_port_switch():
    st = nm.empty_state()
    st["modes"].append({"mode": "bridge", "ports": ["eno1", "enp3s0"], "params": {}, "res": [], "status": "ok"})
    with pytest.raises(ValueError, match="先撤"):
        nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())


def test_plan_never_mentions_wifi():
    st = nm.empty_state()
    for new in (nm.rec("off", "eno1"), nm.rec("dhcp", "eno1"), nm.rec("dhcp", "enp3s0"),
                static(egress="primary", gateway="192.0.2.1"), static("eno1")):
        plan = nm.plan_switch(st, new, CONS, uuids())
        text = " ".join(flat(plan["commands"] + plan["rollback"] + plan["restore"] + plan["clean"]))
        assert "wlp4s0" not in text and "6c4d-1" not in text and "campus-net" not in text


def test_plan_probe_only_when_handing_back_to_serve_profile():
    plan = nm.plan_switch(nm.empty_state(), nm.legacy("eno1", "a737-4", "Lan1-pxe"), CONS, uuids())
    assert plan["probe"] is True
    assert not any("systemctl" in c for c in plan["apply"])        # 回装机配置不停 PXE
    plan = nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), CONS, uuids())
    assert plan["probe"] is False


# ------------------------------------------------------------ 执行: 失败自动 undo 并恢复原模式

class Box:
    """假 nmcli: 记下跑过的命令; fail 里的子串匹配到就返回失败."""

    def __init__(self, *fail):
        self.ran, self.fail, self.saved, self.logs = [], fail, [], []

    def run(self, cmd):
        line = " ".join(cmd)
        self.ran.append(line)
        return (1, "boom") if any(f in line for f in self.fail) else (0, "")

    def save(self, st):
        self.saved.append(json.loads(json.dumps(st)))

    def log(self, msg):
        self.logs.append(msg)


def _managed_static_state():
    st = nm.empty_state()
    st["modes"].append(dict(static(), res=[{"con": "u0", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
                            status="ok"))
    return st


def test_run_switch_success_writes_intent_then_result():
    st = _managed_static_state()
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    box = Box()
    ok, new, msg = nm.run_switch(plan, st, box.run, box.save, box.log, now=lambda: 100)
    assert ok and "成功" in msg
    assert box.ran[:3] == ["nmcli con delete uuid u0", box.ran[1], "nmcli con up uuid u1"]
    first, last = box.saved
    assert nm.owner(first, "enp3s0")["status"] == "applying"           # 先记意图: 断电也知道要撤什么
    assert {r["con"] for r in nm.owner(first, "enp3s0")["stale"]} == {"u0"}
    rec = nm.owner(last, "enp3s0")
    assert rec["mode"] == "dhcp" and rec["status"] == "ok" and "stale" not in rec and rec["since"] == 100
    assert last["prev"]["enp3s0"]["mode"] == "static"                  # 「回到上一个」用
    assert new == last and len(box.logs) >= 2                           # undo / apply 各一行


def test_run_switch_apply_fails_undoes_and_restores_original():
    st = _managed_static_state()
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    box = Box("con up uuid u1")
    ok, new, msg = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert not ok and "回到原来" in msg
    i = box.ran.index("nmcli con up uuid u1")
    assert box.ran[i + 1] == "nmcli con delete uuid u1"                # 先撤掉没起来的新模式
    rec = nm.owner(new, "enp3s0")
    assert rec["mode"] == "static" and rec["status"] == "ok" and rec["params"]["addr"] == "192.0.2.5/24"
    assert rec["res"][0]["con"] != "u0" and box.ran[-1] == f"nmcli con up uuid {rec['res'][0]['con']}"
    assert "enp3s0" not in new["prev"]                                  # 没换成, 上一个不变


def test_run_switch_restore_fails_falls_to_clean_state():
    st = _managed_static_state()
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    restore_uuid = plan["restore"][-1][-1]
    box = Box("con up uuid u1", f"con up uuid {restore_uuid}")
    ok, new, msg = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert not ok and "干净状态" in msg
    rec = nm.owner(new, "enp3s0")
    assert rec["mode"] == "dhcp" and rec["status"] == "ok"
    assert f"nmcli con delete uuid {restore_uuid}" in box.ran           # 恢复失败的那份也撤掉


def test_run_switch_from_unmanaged_failure_brings_legacy_back():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS, uuids())
    box = Box("con up uuid u1")
    ok, new, msg = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert not ok and box.ran[-1] == "nmcli con up uuid a737-4"
    assert nm.owner(new, "eno1") is None                                # 回到未托管, 不留记录
    assert "Lan1-pxe" in msg


def test_run_switch_handback_to_legacy_leaves_port_unmanaged():
    st = _managed_static_state()
    plan = nm.plan_switch(st, nm.legacy("enp3s0", "1603-3", "enp3s0"), CONS, uuids())
    box = Box()
    ok, new, _ = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert ok and box.ran == ["nmcli con delete uuid u0", "nmcli con up uuid 1603-3"]
    assert nm.owner(new, "enp3s0") is None and new["prev"]["enp3s0"]["mode"] == "static"


def test_run_switch_cannot_write_state_touches_nothing():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS, uuids())
    box = Box()

    def bad_save(_st):
        raise OSError("read-only")
    ok, new, msg = nm.run_switch(plan, st, box.run, bad_save, box.log)
    assert not ok and box.ran == [] and "状态文件" in msg and new is st


def test_run_switch_keeps_other_port_record():
    st = _managed_static_state()
    st["modes"].append(dict(nm.rec("off", "eno1"), res=[{"con": "uL", "name": "cart-eno1-off", "iface": "eno1"}],
                            status="ok"))
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    box = Box()
    _, new, _ = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert nm.owner(new, "eno1")["res"][0]["con"] == "uL"
    assert not any("uL" in c for c in box.ran)


# ------------------------------------------------------------ 界面口径: 按网口看

def test_parse_default_routes_sorted_by_metric():
    assert nm.parse_default_routes(ROUTES) == [("enp3s0", 100), ("wlp4s0", 900)]
    assert nm.parse_default_routes("default via 192.0.2.1 dev eno1") == [("eno1", 0)]
    assert nm.parse_default_routes("") == []


def test_view_first_run_infers_unmanaged_without_changing_anything():
    vs = nm.port_views(nm.empty_state(), CONS, IP_BR, ROUTES, LINK)
    assert [v["iface"] for v in vs] == ["eno1", "enp3s0"]               # 无线不列
    lan1, lan2 = vs
    assert lan1["managed"] is False and lan1["mode"] == "legacy" and "Lan1-pxe" in lan1["title"]
    assert lan1["ip"] == "192.0.2.1/24" and lan1["egress"] == ""
    assert lan2["egress"] == "默认出口" and lan2["ip"] == "198.51.100.7/24"
    assert [c["name"] for c in lan2["legacy"]] == ["enp3s0", "cart-enp3s0-static-华为交换机"]
    assert [c["name"] for c in lan1["legacy"]] == ["Lan1-pxe"]
    assert lan1["legacy"][0]["serve"] is True


def test_view_managed_and_drift():
    st = _managed_static_state()
    lan2 = nm.port_views(st, CONS, IP_BR, ROUTES, LINK)[1]
    assert lan2["managed"] and lan2["mode"] == "static" and "192.0.2.5/24" in lan2["title"]
    assert "enp3s0" in lan2["drift"]                                   # 记录是 u0, 实际生效的是旧连接
    cons = CONS.replace("enp3s0:1603-3:802-3-ethernet:enp3s0:yes", "enp3s0:1603-3:802-3-ethernet::no") + \
        "\ncart-enp3s0-static:u0:802-3-ethernet:enp3s0:yes"
    lan2 = nm.port_views(st, cons, IP_BR, ROUTES, LINK)[1]
    assert lan2["drift"] == ""
    assert [c["name"] for c in lan2["legacy"]] == ["enp3s0", "cart-enp3s0-static-华为交换机"]   # 自己的不算旧配置


def test_view_unfinished_switch_is_flagged():
    st = _managed_static_state()
    st["modes"][0]["status"] = "applying"
    lan2 = nm.port_views(st, CONS, IP_BR, ROUTES, LINK)[1]
    assert "没做完" in lan2["drift"]


def test_view_backup_egress_and_no_link():
    routes = ROUTES.replace("metric 100", "metric 1000")
    vs = nm.port_views(nm.empty_state(), CONS, IP_BR, routes, {"eno1": False, "enp3s0": True})
    assert vs[1]["egress"].startswith("备用出口") and vs[0]["link"] is False


# ------------------------------------------------------------ 确认框 / 日志

def test_confirm_text_shows_commands_and_undo():
    plan = nm.plan_switch(nm.empty_state(), static(egress="primary", gateway="192.0.2.1"), CONS, uuids())
    text = nm.confirm_text(plan, ip="198.51.100.7/24")
    assert "LAN2 (enp3s0)" in text and "原来" in text and "改为" in text
    assert "198.51.100.7/24" in text and "旧配置「enp3s0」" in text
    assert "将执行的命令" in text and "nmcli con add" in text
    assert "出问题时的 undo 命令" in text and "nmcli con delete uuid u1" in text
    assert "nmcli con up uuid 1603-3" in text                          # 回到原来
    assert "管理口" in text and "tailscale" in text                     # LAN2 远程会断 + 主出口提示
    assert "主出口" in text


def test_confirm_text_lan1_mentions_pxe_stop_and_probe():
    text = nm.confirm_text(nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), CONS, uuids()))
    assert "PXE" in text and "管理口" not in text
    text = nm.confirm_text(nm.plan_switch(nm.empty_state(), nm.legacy("eno1", "a737-4", "Lan1-pxe"),
                                          CONS, uuids()))
    assert "探测" in text


def test_describe_records():
    assert nm.describe(nm.rec("off", "eno1")) == "关闭"
    assert nm.describe(static(egress="backup", gateway="192.0.2.1", dns="192.0.2.53")) == \
        "静态 IP 192.0.2.5/24 网关 192.0.2.1 DNS 192.0.2.53 · 备用出口"
    assert nm.describe(nm.legacy("eno1", "a737-4", "Lan1-pxe")) == "未托管 · 旧配置「Lan1-pxe」"
    assert nm.describe(nm.legacy("eno1")) == "未托管 · 没有生效的连接"


def test_same_target_detects_no_op():
    a = static(egress="never")
    assert nm.same(dict(a, res=[{"con": "u0"}], status="ok"), static(egress="never"))
    assert not nm.same(a, static(egress="never", dns="192.0.2.53"))
    assert not nm.same(None, a)


def test_crash_during_handback_still_knows_what_to_undo():
    """交还旧连接时断电: 意图记录是 legacy + stale, 下次切换照样把上次没撤的撤掉."""
    st = nm.empty_state()
    st["modes"].append(dict(nm.legacy("enp3s0", "1603-3", "enp3s0"), status="applying",
                            stale=[{"con": "u0", "name": "cart-enp3s0-static", "iface": "enp3s0"}]))
    lan2 = nm.port_views(st, CONS, IP_BR, ROUTES, LINK)[1]
    assert not lan2["managed"] and "没做完" in lan2["drift"]
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    assert plan["undo"] == [["nmcli", "con", "delete", "uuid", "u0"]]
    assert plan["restore"] == [["nmcli", "con", "up", "uuid", "1603-3"]]
