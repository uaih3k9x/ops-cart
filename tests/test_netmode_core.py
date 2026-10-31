"""netmode_core 测试 (#12 第一批): undo 框架、状态文件、关闭/DHCP/静态三个基础模式、出口优先级 (#25). 不碰真机."""

import itertools
import json
import time

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


def static(iface="enp3s0", egress="never", gateway="", dns="", addr="192.0.2.5/24", routes=""):
    return nm.rec("static", iface, nm.static_params(iface, addr, gateway, dns, egress, routes=routes))


# ------------------------------------------------------------ 出口优先级常量 (#25, 和 #7 共用)

def test_egress_metrics_compare_against_wifi_in_one_place():
    assert wc.WIFI_METRIC < wc.WIRED_BACKUP_METRIC
    assert nm.EGRESS_METRIC == {"backup": wc.WIRED_BACKUP_METRIC}


def test_no_primary_egress_wifi_is_the_only_primary():
    """10/31: 主出口只有 Wi-Fi; 有线最多当备用出口."""
    assert nm.EGRESS == {"never": "不当出口", "backup": "备用出口"}
    with pytest.raises(ValueError):
        nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "", "primary")


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

def test_static_params_lan2_never_with_gateway_becomes_backup():
    """10/31 现场: 网关一直能填; LAN2 不当出口 + 网关 是矛盾组合, 按备用出口算 (排在无线后面)."""
    p = nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "192.0.2.53", "never")
    assert p == {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "192.0.2.53", "egress": "backup",
                 "routes": ""}
    p = nm.static_params("enp3s0", "192.0.2.5/24", "", "192.0.2.53", "never")
    assert p == {"addr": "192.0.2.5/24", "gateway": "", "dns": "192.0.2.53", "egress": "never", "routes": ""}


def test_egress_for_gateway_only_fixes_the_contradiction():
    assert nm.egress_for_gateway("enp3s0", "never", "192.0.2.1") == "backup"     # 填了网关: 自动备用出口
    assert nm.egress_for_gateway("enp3s0", "backup", "192.0.2.1") == "backup"
    assert nm.egress_for_gateway("enp3s0", "backup", "") == "never"              # 清空网关: 回到不当出口
    assert nm.egress_for_gateway("enp3s0", "backup", "  ") == "never"
    assert nm.egress_for_gateway("enp3s0", "never", "") == "never"
    assert nm.egress_for_gateway("eno1", "never", "192.0.2.1") == "never"        # LAN1 永远不当出口
    assert nm.egress_for_gateway("eno1", "backup", "192.0.2.1") == "never"


def test_static_params_backup_needs_gateway():
    with pytest.raises(ValueError, match="网关"):
        nm.static_params("enp3s0", "192.0.2.5/24", "", "", "backup")
    p = nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "", "backup")
    assert p["gateway"] == "192.0.2.1" and p["egress"] == "backup"


def test_static_params_lan1_never_egress_but_keeps_gateway():
    """LAN1 永远不当出口 (连备用都不行); 网关照样能填, 只给「经网关去的网段」用."""
    with pytest.raises(ValueError, match="LAN1"):
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "backup")
    p = nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never")
    assert p["egress"] == "never" and p["gateway"] == "192.0.2.1" and p["routes"] == ""


def test_static_params_routes_via_gateway():
    p = nm.static_params("eno1", "192.0.2.5/25", "192.0.2.1", "", "never",
                         routes=" 198.51.100.0/24 , 203.0.113.0/26")
    assert p["routes"] == "198.51.100.0/24, 203.0.113.0/26"
    with pytest.raises(ValueError, match="网关"):                       # 有网段没网关: 去不了
        nm.static_params("eno1", "192.0.2.5/24", "", "", "never", routes="198.51.100.0/24")
    with pytest.raises(ValueError, match="默认路由"):                   # 0.0.0.0/0 = 当出口, 不许
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never", routes="0.0.0.0/0")
    with pytest.raises(ValueError, match="本口网段"):                   # 和本口网段重叠
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never", routes="192.0.2.128/25")
    with pytest.raises(ValueError, match="网段"):
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never", routes="198.51.100.7")
    with pytest.raises(ValueError, match="网段"):
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never", routes="198.51.100.7/24")
    with pytest.raises(ValueError, match="网段"):
        nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never", routes="hello")


def test_static_params_reuses_static_form_checks():
    with pytest.raises(ValueError, match="前缀"):
        nm.static_params("enp3s0", "192.0.2.5")              # 不猜 /24
    with pytest.raises(ValueError, match="网段"):
        nm.static_params("enp3s0", "192.0.2.5/24", "198.51.100.1", "", "backup")
    with pytest.raises(ValueError, match="对端"):
        nm.static_params("enp3s0", "192.0.2.5/24", avoid="192.0.2.5")
    with pytest.raises(ValueError):
        nm.static_params("enp3s0", "192.0.2.5/24", egress="sideways")


def test_target_from_form():
    assert nm.target("eno1", {"mode": "off"}) == nm.rec("off", "eno1")
    with pytest.raises(ValueError, match="交还旧配置"):                  # 10/31 去掉了
        nm.target("eno1", {"mode": "legacy", "legacy": "Lan1-pxe"})
    r = nm.target("enp3s0", {"mode": "static", "addr": "192.0.2.1/24", "gateway": "192.0.2.254",
                             "egress": "never"})
    assert r["params"]["egress"] == "backup" and r["params"]["gateway"] == "192.0.2.254"
    r = nm.target("eno1", {"mode": "static", "addr": "192.0.2.1/24", "gateway": "192.0.2.254",
                           "egress": "never", "routes": "198.51.100.0/24"})
    assert r["params"]["egress"] == "never" and r["params"]["routes"] == "198.51.100.0/24"
    r = nm.target("enp3s0", {"mode": "static", "addr": " 192.0.2.5/25 ", "gateway": "192.0.2.1",
                             "dns": "", "egress": "backup", "peer": "192.0.2.9"})
    assert r["params"] == {"addr": "192.0.2.5/25", "gateway": "192.0.2.1", "dns": "", "egress": "backup",
                           "routes": ""}
    with pytest.raises(ValueError, match="对端"):
        nm.target("enp3s0", {"mode": "static", "addr": "192.0.2.9/24", "peer": "192.0.2.9"})


def test_form_hint_by_mode():
    assert "DHCP" in nm.form_hint("enp3s0", "dhcp", {})
    assert "不当出口" in nm.form_hint("eno1", "dhcp", {})
    assert "不配地址" in nm.form_hint("eno1", "off", {})
    assert "地址/前缀" in nm.form_hint("enp3s0", "static", {"addr": ""})
    h = nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5/25", "gateway": "192.0.2.1", "egress": "backup"})
    assert "192.0.2.0/25" in h and "1000" in h and "900" in h
    assert "前缀" in nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5"})          # 报错, 不猜
    assert "网关" in nm.form_hint("enp3s0", "static", {"addr": "192.0.2.5/24", "egress": "backup"})
    h = nm.form_hint("enp3s0", "static", {"addr": "192.0.2.1/24", "egress": "never"})
    assert "不当出口" in h and "填了网关" in h                          # 说清楚填网关会怎样
    h = nm.form_hint("enp3s0", "static", {"addr": "192.0.2.1/24", "gateway": "192.0.2.254", "egress": "never"})
    assert "备用出口" in h and "1000" in h and "清空网关" in h
    h = nm.form_hint("eno1", "static", {"addr": "192.0.2.1/24", "gateway": "192.0.2.254"})
    assert "不起作用" in h and "经网关去的网段" in h                     # 有网关没网段: 提示, 不拦
    h = nm.form_hint("eno1", "static", {"addr": "192.0.2.1/24", "gateway": "192.0.2.254",
                                        "routes": "198.51.100.0/24"})
    assert "198.51.100.0/24" in h and "192.0.2.254" in h and "不当出口" in h


def test_form_state_greys_out_what_does_not_apply():
    s = nm.form_state("eno1", "static", "never")
    assert s["addr"] and s["gateway"] and s["routes"] and s["egress"] == {"never": True, "backup": False}
    s = nm.form_state("enp3s0", "static", "never")
    assert s["gateway"] and s["routes"] and all(s["egress"].values())  # 网关在静态模式下永远能填
    s = nm.form_state("enp3s0", "dhcp", "never")
    assert not s["addr"] and not s["gateway"] and not s["routes"] and not any(s["egress"].values())


# ------------------------------------------------------------ apply 命令清单 (纯函数)

def test_static_backup_on_lan2():
    cmds, res = nm.apply_steps(static(egress="backup", gateway="192.0.2.1"), uuids())
    add, up = cmds
    assert add[:4] == ["nmcli", "con", "add", "type"]
    kv = kv_of(add)
    assert kv["ifname"] == "enp3s0" and kv["con-name"] == "cart-enp3s0-static" and kv["connection.uuid"] == "u1"
    assert kv["ipv4.method"] == "manual" and kv["ipv4.addresses"] == "192.0.2.5/24"
    assert kv["ipv4.gateway"] == "192.0.2.1"
    assert kv["ipv4.dns"] == ""                                   # 留空显式清掉
    assert kv["ipv4.never-default"] == "no" and kv["ipv6.never-default"] == "no"
    assert kv["ipv4.route-metric"] == kv["ipv6.route-metric"] == str(wc.WIRED_BACKUP_METRIC)
    assert kv["ipv4.routes"] == ""                                # 没网段也显式清掉
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


def test_lan1_routes_via_gateway_without_default_route():
    r = nm.rec("static", "eno1", nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never",
                                                  routes="198.51.100.0/24, 203.0.113.0/26"))
    kv = kv_of(nm.apply_steps(r, uuids())[0][1])
    assert kv["ipv4.never-default"] == "yes" and kv["ipv6.never-default"] == "yes"
    assert kv["ipv4.routes"] == "198.51.100.0/24 192.0.2.1, 203.0.113.0/26 192.0.2.1"
    assert kv["ipv4.gateway"] == ""                               # 网关只走静态路由, 不进默认路由
    r = nm.rec("static", "eno1", nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", "", "never"))
    kv = kv_of(nm.apply_steps(r, uuids())[0][1])
    assert kv["ipv4.routes"] == "" and kv["ipv4.gateway"] == ""   # 有网关没网段: 网关不起作用


def test_never_default_and_gateway_never_written_together():
    """状态文件里的老记录 (或手改的) 是 不当出口 + 网关: 按备用出口建, 不写出 never-default yes + 网关."""
    forged = nm.rec("static", "enp3s0", {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "",
                                          "egress": "never"})
    kv = kv_of(nm.apply_steps(forged, uuids())[0][0])
    assert kv["ipv4.gateway"] == "192.0.2.1" and kv["ipv4.never-default"] == "no"
    assert kv["ipv4.route-metric"] == str(wc.WIRED_BACKUP_METRIC)


@pytest.mark.parametrize("egress", ["backup", "primary"])
def test_lan1_egress_refused_even_if_params_forged(egress):
    forged = nm.rec("static", "eno1", {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "",
                                        "egress": egress})
    with pytest.raises(ValueError, match="LAN1"):
        nm.apply_steps(forged, uuids())


def test_lan2_primary_from_old_state_rebuilt_as_backup():
    """1.09.00 可能记过 LAN2 主出口: 不能因此切不走 (恢复原模式要重建它), 按备用出口建."""
    old = nm.rec("static", "enp3s0", {"addr": "192.0.2.5/24", "gateway": "192.0.2.1", "dns": "",
                                       "egress": "primary"})
    kv = kv_of(nm.apply_steps(old, uuids())[0][0])
    assert kv["ipv4.route-metric"] == str(wc.WIRED_BACKUP_METRIC) and kv["ipv4.never-default"] == "no"
    st = nm.empty_state()
    st["modes"].append(dict(old, res=[{"con": "u0", "name": "cart-enp3s0-static", "iface": "enp3s0"}],
                            status="ok"))
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    assert plan["undo"] == [["nmcli", "con", "delete", "uuid", "u0"]]
    assert nm.describe(old).endswith("主出口 (旧版, 再建按备用)")
    with pytest.raises(ValueError, match="主出口"):                   # 新填的表单照样不给主出口
        nm.static_params("enp3s0", "192.0.2.5/24", "192.0.2.1", "", "primary")


def test_off_disables_both_families():
    cmds, res = nm.apply_steps(nm.rec("off", "eno1"), uuids())
    kv = kv_of(cmds[1])
    assert kv["con-name"] == "cart-eno1-off"
    assert kv["ipv4.method"] == "disabled" and kv["ipv6.method"] == "disabled"
    assert cmds[-1] == ["nmcli", "con", "up", "uuid", "u1"] and res[0]["con"] == "u1"


def test_lan2_dhcp_is_backup_egress_behind_wifi():
    """10/31 现场: LAN2 DHCP 用了 NM 默认 metric (101), 抢在无线 (900) 前面当了主出口. 无线是唯一主出口."""
    cmds, _ = nm.apply_steps(nm.rec("dhcp", "enp3s0"), uuids())
    assert len(cmds) == 2 and "systemctl" not in flat(cmds)[0]
    kv = kv_of(cmds[0])
    assert kv["ipv4.method"] == "auto" and kv["ipv4.never-default"] == "no"
    assert int(kv["ipv4.route-metric"]) == nm.EGRESS_METRIC["backup"] > nm.wc.WIFI_METRIC
    assert int(kv["ipv6.route-metric"]) == nm.EGRESS_METRIC["backup"]


def test_legacy_restore_only_brings_it_up_and_owns_nothing():
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
                static(egress="backup", gateway="192.0.2.1"), static("eno1")):
        plan = nm.plan_switch(st, new, CONS, uuids())
        text = " ".join(flat(plan["commands"] + plan["rollback"] + plan["restore"] + plan["clean"]))
        assert "wlp4s0" not in text and "6c4d-1" not in text and "campus-net" not in text


def test_plan_without_link_only_creates_profile():
    """没插线 NM 不让 con up (设备 unavailable); 先摸设备网段再插线是常见用法: 只建好, 插上 NM 自动连."""
    st = nm.empty_state()
    plan = nm.plan_switch(st, static("eno1"), CONS, uuids(), links={"eno1": False})
    assert not any(c[:3] == ["nmcli", "con", "up"] for c in plan["apply"] + plan["clean"])
    assert any(c[:3] == ["nmcli", "con", "add"] for c in plan["apply"])
    assert plan["no_link"] == ["eno1"]
    assert "没插线" in nm.confirm_text(plan)
    plan = nm.plan_switch(st, static("eno1"), CONS, uuids(), links={"eno1": None})    # 读不到: 照常 up
    assert plan["apply"][-1][:3] == ["nmcli", "con", "up"] and plan["no_link"] == []


def test_plan_refuses_handing_back_to_legacy():
    """「交还旧配置」去掉了; 1.09.00 留下的 prev 可能还指着旧连接, 「回到上一个」要说人话, 不能去 up 一条删掉的."""
    with pytest.raises(ValueError, match="交还旧配置"):
        nm.plan_switch(nm.empty_state(), nm.legacy("eno1", "a737-4", "Lan1-pxe"), CONS, uuids())


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


def test_run_switch_undo_that_fails_is_kept_for_next_time():
    """undo 没撤掉的 (NM 忙之类) 留进 stale, 下次切换再撤, 不能就此忘了."""
    st = _managed_static_state()
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    box = Box("con delete uuid u0")
    ok, new, _ = nm.run_switch(plan, st, box.run, box.save, box.log)
    rec = nm.owner(new, "enp3s0")
    assert ok and rec["mode"] == "dhcp" and [x["con"] for x in rec["stale"]] == ["u0"]
    plan = nm.plan_switch(new, nm.rec("static", "enp3s0", static()["params"]), CONS, uuids())
    assert ["nmcli", "con", "delete", "uuid", "u0"] in plan["undo"]


def test_run_switch_failed_rollback_leftover_survives_legacy_restore():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("dhcp", "enp3s0"), CONS, uuids())
    box = Box("con up uuid u1", "con delete uuid u1")
    ok, new, _ = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert not ok and box.ran[-1] == "nmcli con up uuid 1603-3"
    rec = nm.owner(new, "enp3s0")                                     # 回到旧连接, 但记着 u1 没删掉
    assert rec["mode"] == "legacy" and [x["con"] for x in rec["stale"]] == ["u1"]
    lan2 = nm.port_views(new, CONS, IP_BR, ROUTES, LINK)[1]
    assert not lan2["managed"] and "旧配置「enp3s0」" in lan2["title"] and "没撤掉" in lan2["drift"]
    assert not any("1603-3" in c and "delete" in c for c in box.ran)  # 没切成: 旧连接一条不删


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


def test_view_managed_and_drift():
    st = _managed_static_state()
    lan2 = nm.port_views(st, CONS, IP_BR, ROUTES, LINK)[1]
    assert lan2["managed"] and lan2["mode"] == "static" and "192.0.2.5/24" in lan2["title"]
    assert "enp3s0" in lan2["drift"]                                   # 记录是 u0, 实际生效的是旧连接
    cons = CONS.replace("enp3s0:1603-3:802-3-ethernet:enp3s0:yes", "enp3s0:1603-3:802-3-ethernet::no") + \
        "\ncart-enp3s0-static:u0:802-3-ethernet:enp3s0:yes"
    lan2 = nm.port_views(st, cons, IP_BR, ROUTES, LINK)[1]
    assert lan2["drift"] == ""


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
    plan = nm.plan_switch(nm.empty_state(), static(egress="backup", gateway="192.0.2.1"), CONS, uuids())
    text = nm.confirm_text(plan, ip="198.51.100.7/24")
    assert "LAN2 (enp3s0)" in text and "原来" in text and "改为" in text
    assert "198.51.100.7/24" in text and "旧配置「enp3s0」" in text
    assert "将执行的命令" in text and "nmcli con add" in text
    assert "出问题时的 undo 命令" in text and "nmcli con delete uuid u1" in text
    assert "nmcli con up uuid 1603-3" in text                          # 回到原来
    assert "管理口" in text and "tailscale" in text                     # LAN2 远程会断
    assert "备用出口" in text and "主出口" not in text


def test_confirm_text_lan1_mentions_pxe_stop():
    text = nm.confirm_text(nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), CONS, uuids()))
    assert "PXE" in text and "管理口" not in text and "探测" not in text


def test_describe_records():
    assert nm.describe(nm.rec("off", "eno1")) == "关闭"
    assert nm.describe(static(egress="backup", gateway="192.0.2.1", dns="192.0.2.53")) == \
        "静态 IP 192.0.2.5/24 网关 192.0.2.1 DNS 192.0.2.53 · 备用出口"
    r = nm.rec("static", "eno1", nm.static_params("eno1", "192.0.2.5/24", "192.0.2.1", routes="198.51.100.0/24"))
    assert nm.describe(r) == "静态 IP 192.0.2.5/24 网关 192.0.2.1 经网关去 198.51.100.0/24 · 不当出口"
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


# ------------------------------------------------------------ 接管时删旧配置 (10/31: 交还旧配置去掉了)

def _detail(name, uuid, ifname):
    """nmcli -t con show uuid <u> 的样子 (只留要用的几行)."""
    return "\n".join([f"connection.id:{name}", f"connection.uuid:{uuid}", "connection.type:802-3-ethernet",
                      f"connection.interface-name:{ifname}", "ipv4.method:manual"])


CONS_OLD = CONS + "\nOld-lan1:cccc-6:802-3-ethernet::no"
DETAILS = {"1603-3": _detail("enp3s0", "1603-3", "enp3s0"), "a737-4": _detail("Lan1-pxe", "a737-4", "eno1"),
           "bbbb-5": _detail("cart-enp3s0-static-华为交换机", "bbbb-5", "enp3s0"),
           "eeee-8": _detail("Wired connection 1", "eeee-8", ""), "cccc-6": _detail("Old-lan1", "cccc-6", "eno1"),
           "6c4d-1": _detail("campus-net", "6c4d-1", "wlp4s0")}


def _names(cons):
    return [c["name"] for c in cons]


def test_old_cons_bound_to_this_port_or_active_here():
    st = nm.empty_state()
    assert _names(nm.old_cons(st, "eno1", CONS_OLD, DETAILS)) == ["Lan1-pxe", "Old-lan1"]
    # 没绑网口、也没在这个口上生效的 (Wired connection 1) 不删; 绑在 LAN1 的 Old-lan1 不归 LAN2; 无线永远不碰
    assert _names(nm.old_cons(st, "enp3s0", CONS_OLD, DETAILS)) == ["enp3s0", "cart-enp3s0-static-华为交换机"]


def test_old_cons_without_details_only_the_active_one():
    """读不到 interface-name 的: 只认正在这个口上生效的那条 (它不可能绑在别的口上), 别的一条不删."""
    assert _names(nm.old_cons(nm.empty_state(), "enp3s0", CONS_OLD, {})) == ["enp3s0"]
    assert _names(nm.old_cons(nm.empty_state(), "eno1", CONS_OLD, None)) == ["Lan1-pxe"]


def test_old_cons_unbound_but_active_here_counts():
    cons = CONS_OLD.replace("Wired connection 1:eeee-8:802-3-ethernet::no",
                            "Wired connection 1:eeee-8:802-3-ethernet:enp3s0:yes") \
        .replace("enp3s0:1603-3:802-3-ethernet:enp3s0:yes", "enp3s0:1603-3:802-3-ethernet::no")
    assert "Wired connection 1" in _names(nm.old_cons(nm.empty_state(), "enp3s0", cons, DETAILS))


def test_old_cons_skips_what_the_cart_owns():
    st = _managed_static_state()
    st["modes"][0]["res"][0]["con"] = "bbbb-5"                         # 推车自己记着的那条不算旧配置
    assert _names(nm.old_cons(st, "enp3s0", CONS_OLD, DETAILS)) == ["enp3s0"]


def test_old_cons_bound_to_other_port_never_deleted():
    details = dict(DETAILS, **{"1603-3": _detail("enp3s0", "1603-3", "eno1")})    # 绑在 LAN1 上: LAN2 不碰
    cons = CONS_OLD.replace("enp3s0:1603-3:802-3-ethernet:enp3s0:yes", "enp3s0:1603-3:802-3-ethernet::no")
    assert "enp3s0" not in _names(nm.old_cons(nm.empty_state(), "enp3s0", cons, details))


def test_plan_from_unmanaged_purges_old_cons_after_apply():
    plan = nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    assert _names(plan["purge"]) == ["Lan1-pxe", "Old-lan1"]
    assert plan["purge"][0]["uuid"] == "a737-4" and "interface-name:eno1" in plan["purge"][0]["dump"]
    assert ["nmcli", "con", "delete", "uuid", "a737-4"] not in plan["commands"]      # 不进 undo/apply
    assert ["nmcli", "con", "up", "uuid", "a737-4"] in plan["restore"]              # 失败时旧的还在, up 回去
    plan = nm.plan_switch(_managed_static_state(), nm.rec("dhcp", "enp3s0"), CONS_OLD, uuids(), details=DETAILS)
    assert plan["purge"] == []                                                       # 已托管: 不再删


def test_plan_purge_never_touches_wifi_or_other_port():
    plan = nm.plan_switch(nm.empty_state(), nm.rec("dhcp", "enp3s0"), CONS_OLD, uuids(), details=DETAILS)
    assert not any(c["uuid"] in ("6c4d-1", "a737-4", "cccc-6") for c in plan["purge"])


def _bk(box, where="/b/20261031-101500", fail=None):
    box.backed = None

    def backup(cons):
        box.backed = [c["name"] for c in cons]
        if fail:
            raise OSError(fail)
        return where
    return backup


def test_run_switch_backs_up_then_deletes_old_cons():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    box = Box()
    ok, new, msg = nm.run_switch(plan, st, box.run, box.save, box.log, backup=_bk(box))
    assert ok and box.backed == ["Lan1-pxe", "Old-lan1"]
    i = box.ran.index("nmcli con up uuid u1")
    assert box.ran[i + 1:] == ["nmcli con delete uuid a737-4", "nmcli con delete uuid cccc-6"]   # apply 成功之后才删
    assert "Lan1-pxe" in msg and "/b/20261031-101500" in msg
    assert any("删旧配置" in m and "Lan1-pxe" in m and "/b/20261031-101500" in m for m in box.logs)
    assert "eno1" not in new["prev"]                                     # 旧连接删了, 上一个不指向它
    assert nm.owner(new, "eno1")["mode"] == "off"


def test_run_switch_backup_fails_keeps_old_cons():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    box = Box()
    ok, _, msg = nm.run_switch(plan, st, box.run, box.save, box.log, backup=_bk(box, fail="disk full"))
    assert ok and not any("delete" in c for c in box.ran)                # fail-closed: 备份不成就不删
    assert "没删" in msg and "disk full" in msg
    assert any("备份失败" in m for m in box.logs)


def test_run_switch_without_backup_hook_deletes_nothing():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    box = Box()
    ok, _, msg = nm.run_switch(plan, st, box.run, box.save, box.log)
    assert ok and not any("delete" in c for c in box.ran) and "没删" in msg


def test_run_switch_failed_apply_never_deletes_old_cons():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    box = Box("con up uuid u1")
    ok, _, _ = nm.run_switch(plan, st, box.run, box.save, box.log, backup=_bk(box))
    assert not ok and box.backed is None and box.ran[-1] == "nmcli con up uuid a737-4"
    assert not any("a737-4" in c and "delete" in c for c in box.ran)


def test_run_switch_delete_that_fails_is_reported():
    st = nm.empty_state()
    plan = nm.plan_switch(st, nm.rec("off", "eno1"), CONS_OLD, uuids(), details=DETAILS)
    box = Box("con delete uuid cccc-6")
    ok, _, msg = nm.run_switch(plan, st, box.run, box.save, box.log, backup=_bk(box))
    assert ok and "Old-lan1" in msg and "没删掉" in msg
    assert any("Old-lan1" in m and "失败" in m for m in box.logs)


def _stamp(t):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t))


def test_backup_cons_copies_keyfile_or_saves_nmcli_text(tmp_path):
    nm_dir, root = tmp_path / "sc", tmp_path / "bk"
    nm_dir.mkdir()
    (nm_dir / "Lan1-pxe.nmconnection").write_text("[connection]\nid=Lan1-pxe\nuuid=a737-4\n")
    (nm_dir / "renamed.nmconnection").write_text("[connection]\nid=Old-lan1\nuuid=cccc-6\n")
    cons = [{"name": "Lan1-pxe", "uuid": "a737-4", "dump": DETAILS["a737-4"]},
            {"name": "Old-lan1", "uuid": "cccc-6", "dump": DETAILS["cccc-6"]},
            {"name": "a/b", "uuid": "eeee-8", "dump": DETAILS["eeee-8"]}]
    where = nm.backup_cons(cons, root=str(root), nm_dir=str(nm_dir), now=lambda: 1793440000)
    d = root / _stamp(1793440000)
    assert where == str(d)
    assert (d / "Lan1-pxe.nmconnection").read_text().endswith("uuid=a737-4\n")
    assert (d / "renamed.nmconnection").exists()                         # 文件名对不上就按 uuid 找
    assert "interface-name:" in (d / "a_b.nmcli.txt").read_text()        # 没 keyfile: 存 nmcli 输出


def test_backup_cons_fails_closed_without_anything_to_save(tmp_path):
    with pytest.raises(OSError):
        nm.backup_cons([{"name": "x", "uuid": "u", "dump": ""}], root=str(tmp_path / "bk"),
                       nm_dir=str(tmp_path / "none"))
    (tmp_path / "file").write_text("")
    with pytest.raises(OSError):                                         # 备份目录建不了
        nm.backup_cons([{"name": "x", "uuid": "u", "dump": "a:b"}], root=str(tmp_path / "file" / "bk"),
                       nm_dir=str(tmp_path / "none"))


def test_confirm_text_lists_old_cons_to_delete_and_pxe_warning():
    plan = nm.plan_switch(nm.empty_state(), static("eno1"), CONS_OLD, uuids(), details=DETAILS)
    text = nm.confirm_text(plan)
    assert "将删除旧配置: Lan1-pxe、Old-lan1" in text and nm.BACKUP_DIR in text
    assert "nmcli con delete uuid a737-4" in text
    assert "装机口地址要用静态 IP 重建, PXE 服务会停" in text
    plan = nm.plan_switch(nm.empty_state(), nm.rec("dhcp", "enp3s0"), CONS_OLD, uuids(), details=DETAILS)
    text = nm.confirm_text(plan)
    assert "将删除旧配置: enp3s0、cart-enp3s0-static-华为交换机" in text and "装机口地址" not in text


def test_confirm_text_pxe_warning_when_pxe_server_runs_on_port():
    details = dict(DETAILS, **{"a737-4": _detail("lab", "a737-4", "eno1")})
    cons = CONS_OLD.replace("Lan1-pxe:a737-4", "lab:a737-4")
    plan = nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), cons, uuids(), details=details)
    assert "装机口地址" not in nm.confirm_text(plan)
    plan = nm.plan_switch(nm.empty_state(), nm.rec("off", "eno1"), cons, uuids(), details=details,
                          pxe_active=True)
    assert "装机口地址要用静态 IP 重建" in nm.confirm_text(plan)
    plan = nm.plan_switch(nm.empty_state(), nm.rec("dhcp", "enp3s0"), cons, uuids(), details=details,
                          pxe_active=True)
    assert "装机口地址" not in nm.confirm_text(plan)                     # PXE 只在 LAN1 上
