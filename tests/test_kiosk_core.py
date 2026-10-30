"""kiosk_core 测试: 纯函数与配置读写, 不碰真机服务."""

import time
import json

import pytest

import kiosk_core as kc


@pytest.fixture(autouse=True)
def _no_real_cartlog(monkeypatch):
    """装机/PIN 事件默认写 /var/log/ops-cart; 测试里不碰真目录."""
    monkeypatch.setattr(kc.cartlog, "event", lambda *a, **k: True)


# ------------------------------------------------------------ 装机网段: 只来自部署配置 /etc/ops-cart/pxe.env

def test_pxe_env_missing_gives_example_defaults(tmp_path):
    env = kc.pxe_env_read(tmp_path / "none")
    assert env == kc.DEFAULT_PXE_ENV and env["PXE_IP"] == "192.0.2.1"


def test_pxe_env_parses_shell_style(tmp_path):
    p = tmp_path / "pxe.env"
    p.write_text('# 注释\nPXE_IP=198.51.100.1\nPXE_PREFIX="25"\n\nPXE_POOL=\'198.51.100.10,198.51.100.20\'\n'
                 "JUNK LINE\n")
    env = kc.pxe_env_read(p)
    assert env == {"PXE_IP": "198.51.100.1", "PXE_PREFIX": "25",
                   "PXE_POOL": "198.51.100.10,198.51.100.20"}


def test_menus_use_env_address(tmp_path):
    p = tmp_path / "pxe.env"
    p.write_text("PXE_IP=198.51.100.1\n")
    for cfg in (kc.grub_cfg_rescue(env_path=p), kc.grub_cfg_install(env_path=p)):
        assert "(http,198.51.100.1)/vmlinuz" in cfg and "http://198.51.100.1:8000/rocky" in cfg


def test_lan1_unit_rewrite_path_is_gone():
    """LAN1 地址改为部署配置, 车机不再重写 systemd 单元."""
    for name in ("apply_pxe_conf", "render_pxe_service", "pxe_conf_read", "pxe_conf_write"):
        assert not hasattr(kc, name)


def test_nmcli_set_static_builds_correct_cmd():
    cmds = kc.nmcli_set_static("Lan1-pxe", "192.0.2.5/24", "192.0.2.254", "198.51.100.53")
    assert cmds[0][:6] == ["nmcli", "con", "mod", "Lan1-pxe", "ipv4.method", "manual"]
    assert "192.0.2.5/24" in cmds[0]
    assert cmds[0][-1] == "198.51.100.53"     # dns
    assert cmds[0][cmds[0].index("ipv4.gateway") + 1] == "192.0.2.254"
    assert cmds[1] == ["nmcli", "con", "up", "Lan1-pxe"]


def test_nmcli_set_static_empty_gateway_dns():
    cmds = kc.nmcli_set_static("x", "10.0.0.1/24")
    assert cmds[0][cmds[0].index("ipv4.gateway") + 1] == ""
    assert "ipv4.dns" not in cmds[0]


def test_nmcli_set_dhcp_clears_static():
    cmds = kc.nmcli_set_dhcp("enp3s0")
    m = cmds[0]
    assert m[m.index("ipv4.method") + 1] == "auto"
    assert m[m.index("ipv4.addresses") + 1] == ""
    assert m[m.index("ipv4.gateway") + 1] == ""


SHOW = """Id=pxe-server.service
ActiveState=active
UnitFileState=enabled

UnitFileState=disabled
ActiveState=inactive
Id=pxe-http-repo.service

Id=pxe-http-boot.service
ActiveState=failed
UnitFileState=enabled
"""


def test_services_status_one_systemctl_call(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=5):
        calls.append(cmd)
        return 0, SHOW
    monkeypatch.setattr(kc, "run", fake_run)
    st = kc.services_status()
    assert st == {"pxe-server": (True, True), "pxe-http-repo": (False, False),
                  "pxe-http-boot": (True, False)}
    assert len(calls) == 1 and calls[0][:2] == ["systemctl", "show"]
    assert "-p" in calls[0] and calls[0][-3:] == ["pxe-server", "pxe-http-repo", "pxe-http-boot"]


def test_parse_systemctl_show_matches_by_id_and_tolerates_junk():
    st = kc.parse_systemctl_show(SHOW, ["pxe-http-boot", "pxe-server", "nope"])
    assert st == {"pxe-http-boot": (True, False), "pxe-server": (True, True), "nope": (False, False)}
    assert kc.parse_systemctl_show("", ["a"]) == {"a": (False, False)}
    assert kc.parse_systemctl_show("garbage\n\n=x", ["a"]) == {"a": (False, False)}


def test_services_status_systemctl_failure(monkeypatch):
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (127, ""))
    assert kc.services_status(("pxe-server",)) == {"pxe-server": (False, False)}


def test_nics_status_reads_carrier_from_sysfs(monkeypatch, tmp_path):
    (tmp_path / "eno1").mkdir()
    (tmp_path / "eno1" / "carrier").write_text("1\n")       # enp3s0 没有文件 = 读不到 = 没插线

    def fake_run(cmd, timeout=5):
        assert cmd[0] != "cat"
        if cmd[:2] == ["ip", "-4"]:
            return 0, "eno1            UP      203.0.113.1/24"
        if cmd[0] == "nmcli":
            return 0, "manual"
        return 1, ""
    monkeypatch.setattr(kc, "run", fake_run)
    nics = kc.nics_status(sysfs=str(tmp_path))
    assert nics[0]["ip"] == "203.0.113.1/24"
    assert nics[0]["link"] is True and nics[1]["link"] is False
    assert nics[0]["method"] == "manual"


@pytest.mark.parametrize("secs,text", [
    (30, "up 0 minutes"), (61, "up 1 minute"), (3600 * 2 + 60 * 5, "up 2 hours, 5 minutes"),
    (86400 + 3600, "up 1 day, 1 hour, 0 minutes"), (86400 * 3, "up 3 days, 0 hours, 0 minutes"),
])
def test_uptime_text(secs, text):
    assert kc.uptime_text(secs) == text


def test_misc_status_uptime_from_proc(monkeypatch, tmp_path):
    p = tmp_path / "uptime"
    p.write_text("7265.13 14000.00\n")
    monkeypatch.setattr(kc, "PROC_UPTIME", str(p))
    calls = []
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (calls.append(cmd) or (0, "192.0.2.10")))
    m = kc.misc_status()
    assert m["uptime"] == "up 2 hours, 1 minute"
    assert all(c[0] != "uptime" for c in calls)


def test_screensaver_due_basic():
    assert kc.screensaver_due(100.0, 200.0, 180) is False   # 空闲100s, 不到180
    assert kc.screensaver_due(100.0, 280.0, 180) is True   # 空闲180s, 该出
    assert kc.screensaver_due(100.0, 400.0, 180, showing=True) is False  # 已在屏保, 不重复触发


# ---- Kickstart 编辑

KS_SAMPLE = """#version=RHEL9
text
reboot
lang en_US.UTF-8
rootpw --iscrypted $6$old$hash
network --bootproto=dhcp --device=link --activate --hostname rocky9
clearpart --all --initlabel
autopart --type=lvm
"""


def test_ks_roundtrip_and_backup_rotation(tmp_path, monkeypatch):
    import kiosk_core as kc
    p = str(tmp_path / "ks.cfg")
    kc.ks_write(KS_SAMPLE, path=p)                      # 首次写, 无备份可做
    kc.ks_write(KS_SAMPLE.replace("rocky9", "x1"), path=p)  # 第二次, 产生 1 份备份
    kc.ks_write(KS_SAMPLE, path=p)                      # 第三次, 2 份
    import glob
    assert len(glob.glob(p + ".bak-*")) == 2
    assert kc.ks_read(p).startswith("#version=RHEL9")


def test_ks_write_backup_prunes_old(tmp_path):
    import glob
    import kiosk_core as kc
    p = str(tmp_path / "ks.cfg")
    for i in range(8):
        kc.ks_write(f"rev{i}\n", path=p)
    assert len(glob.glob(p + ".bak-*")) == kc.KS_BACKUPS


def test_ks_set_rootpw(tmp_path):
    import kiosk_core as kc
    new = kc.ks_set_rootpw(KS_SAMPLE, "$6$salt$newhash")
    assert "rootpw --iscrypted $6$salt$newhash" in new
    assert "old" not in new


def test_ks_set_rootpw_rejects_plain():
    import kiosk_core as kc
    with pytest.raises(ValueError):
        kc.ks_set_rootpw(KS_SAMPLE, "plainpassword")


def test_ks_set_hostname(tmp_path):
    import kiosk_core as kc
    new = kc.ks_set_hostname(KS_SAMPLE, "lab-node-01")
    assert "--hostname lab-node-01" in new
    assert "rocky9" not in new


def test_ks_set_hostname_rejects_bad():
    import kiosk_core as kc
    with pytest.raises(ValueError):
        kc.ks_set_hostname(KS_SAMPLE, "bad host; name")


def test_ks_hash_password_openssl(monkeypatch):
    import kiosk_core as kc
    class FakeR:
        returncode = 0; stdout = "$6$abc$xyz\n"; stderr = ""
    monkeypatch.setattr(kc.subprocess, "run", lambda *a, **k: FakeR())
    assert kc.ks_hash_password("pw") == "$6$abc$xyz"


def test_ks_validate_warns_on_destructive():
    import kiosk_core as kc
    warns = kc.ks_validate(KS_SAMPLE)
    assert any("clearpart" in w for w in warns)
    assert kc.ks_validate("text\nreboot\nrootpw --iscrypted $6$a$b\n") == []


# ---------------------------------------------------------- PIN 锁屏

def test_pin_hash_verify_roundtrip():
    h = kc.pin_hash("114514")
    assert h.startswith("pbkdf2$") and "114514" not in h
    assert kc.pin_verify("114514", h)
    assert not kc.pin_verify("114515", h)


def test_pin_hash_is_salted():
    assert kc.pin_hash("1234") != kc.pin_hash("1234")


def test_pin_verify_rejects_garbage():
    assert not kc.pin_verify("1234", "")
    assert not kc.pin_verify("1234", "plain-text")


def test_pin_conf_roundtrip_and_mode(tmp_path):
    p = tmp_path / "kiosk.conf"
    assert kc.pin_conf_read(str(p)) is None          # 没设过 PIN
    kc.pin_set("114514", str(p))
    assert kc.pin_verify("114514", kc.pin_conf_read(str(p)))
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_pin_set_rejects_bad_pin(tmp_path):
    import pytest
    for bad in ("", "12", "abc123", "1" * 13):
        with pytest.raises(ValueError):
            kc.pin_set(bad, str(tmp_path / "k.conf"))


def test_pin_gate_lockout_after_failures():
    h = kc.pin_hash("114514")
    g = kc.PinGate(h, max_fails=3, lock_s=60)
    assert g.attempt("0000", now=0) == "bad"
    assert g.attempt("0000", now=1) == "bad"
    assert g.attempt("0000", now=2) == "locked"       # 第 3 次错 → 冷却
    assert g.attempt("114514", now=30) == "locked"    # 冷却中，对的也不收
    assert g.attempt("114514", now=63) == "ok"        # 冷却过了
    assert g.attempt("0000", now=64) == "bad"         # 成功后计数清零


def test_pin_gate_open_when_no_pin():
    g = kc.PinGate(None)
    assert not g.required
    assert g.attempt("", now=0) == "ok"


def _hwmon(root, idx, name, temps):
    d = root / f"hwmon{idx}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for i, (label, milli) in enumerate(temps, 1):
        (d / f"temp{i}_input").write_text(f"{milli}\n")
        if label:
            (d / f"temp{i}_label").write_text(label + "\n")


def test_temps_status_picks_cpu_package_gpu_board(tmp_path):
    _hwmon(tmp_path, 0, "acpitz", [(None, 27800)])
    _hwmon(tmp_path, 1, "coretemp", [("Package id 0", 56000), ("Core 0", 53000)])
    _hwmon(tmp_path, 2, "nouveau", [(None, 36000)])
    assert kc.temps_status(str(tmp_path), smart=lambda: None) == [
        ("CPU", 56.0), ("显卡", 36.0), ("主板", 27.8)]


def test_temps_status_adds_ssd_and_skips_missing(tmp_path):
    _hwmon(tmp_path, 0, "coretemp", [("Package id 0", 61000)])
    assert kc.temps_status(str(tmp_path), smart=lambda: 38) == [
        ("CPU", 61.0), ("SSD", 38.0)]
    assert kc.temps_status(str(tmp_path / "nope"), smart=lambda: None) == []


def test_smart_temp_parses_smartctl_json(monkeypatch):
    out = json.dumps({"temperature": {"current": 41}})
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (0, out))
    assert kc.smart_temp() == 41
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (1, "boom"))
    assert kc.smart_temp() is None


def test_temp_level_thresholds():
    assert kc.temp_level(69.9) == "ok"
    assert kc.temp_level(70) == "warn"
    assert kc.temp_level(85) == "hot"


def _fans(root, idx, name, fans):
    d = root / f"hwmon{idx}"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for n, rpm in fans.items():
        (d / f"fan{n}_input").write_text(f"{rpm}\n")


def test_fans_status_reads_it87_family(tmp_path):
    _hwmon(tmp_path, 0, "coretemp", [("Package id 0", 50000)])
    _fans(tmp_path, 1, "it8625", {1: 0, 2: 2689, 3: 2500})
    assert kc.fans_status(str(tmp_path)) == [("fan1", 0), ("fan2", 2689), ("fan3", 2500)]
    assert kc.fans_status(str(tmp_path / "nope")) == []


def test_fans_to_show_keeps_fans_seen_spinning():
    seen = set()
    shown = kc.fans_to_show([("fan1", 0), ("fan2", 2689)], seen)
    assert shown == [("fan2", 2689)] and seen == {"fan2"}
    # 转过的风扇停了也要继续显示(0 RPM 才能报警)
    assert kc.fans_to_show([("fan1", 0), ("fan2", 0)], seen) == [("fan2", 0)]


def test_notice_read_missing_and_blank(tmp_path):
    assert kc.notice_read(str(tmp_path / "nope.txt")) == ""
    p = tmp_path / "n.txt"
    p.write_text("\n  \n")
    assert kc.notice_read(str(p)) == ""


def test_notice_read_strips_and_limits(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("\nA101 机房 → 左转\n\n" + "\n".join(f"l{i}" for i in range(30)) + "\n")
    text = kc.notice_read(str(p), max_lines=5)
    assert text.splitlines()[0] == "A101 机房 → 左转"
    assert len(text.splitlines()) == 5


def test_notice_read_limits_chars(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("长" * 1000)
    assert len(kc.notice_read(str(p), max_chars=300)) == 300


# ---------------------------------------------------------------- 开机自检 / 品牌

NICS_OK = [{"name": "eno1", "role": "PXE 装机口", "ip": "-", "link": False},
           {"name": "enp3s0", "role": "管理口", "ip": "192.0.2.228/24", "link": True}]
MISC_OK = {"tailscale_ip": "192.0.2.10", "kvm": {"online": 0, "bound": 0}, "uptime": "up 1 min"}


def _rows(**kw):
    args = dict(nics=NICS_OK, misc=MISC_OK, svcs={"pxe-server": (True, False)},
                temps=[("CPU", 45.0)], fans=[("fan2", 2689), ("fan3", 2500)], serial_ok=True)
    args.update(kw)
    return {r["label"]: r for r in kc.selftest_rows(**args)}


def test_selftest_all_good():
    rows = _rows()
    assert rows["LAN2"]["state"] == "OK" and rows["LAN2"]["detail"] == "192.0.2.228"
    assert rows["TAILSCALE"]["state"] == "OK"
    assert rows["CONSOLE"]["state"] == "OK"
    assert rows["CH9329"]["state"] == "--"          # 没插不算故障
    assert rows["LAN1 / PXE"]["state"] == "--"
    assert rows["CPU"]["detail"] == "45°C" and rows["CPU"]["state"] == "OK"
    assert rows["FAN"]["detail"] == "2689 / 2500 RPM"
    assert not any(r["state"] == "FAIL" for r in rows.values())


def test_selftest_failures():
    nics = [NICS_OK[0], dict(NICS_OK[1], link=False, ip="-")]
    rows = _rows(nics=nics, temps=[("CPU", 90.0)], fans=[("fan2", 0)],
                 misc=dict(MISC_OK, tailscale_ip="-"))
    assert rows["LAN2"]["state"] == "FAIL"
    assert rows["TAILSCALE"]["state"] == "WARN"
    assert rows["CPU"]["state"] == "FAIL"
    assert rows["FAN"]["state"] == "FAIL"


def test_selftest_pxe_running_and_no_sensors():
    nics = [dict(NICS_OK[0], link=True, ip="203.0.113.1/24"), NICS_OK[1]]
    rows = _rows(nics=nics, svcs={"pxe-server": (True, True)}, temps=[], fans=[])
    assert rows["LAN1 / PXE"]["state"] == "OK" and "RUNNING" in rows["LAN1 / PXE"]["detail"]
    assert rows["CPU"]["state"] == "--" and rows["FAN"]["state"] == "--"


def _ctx(**kw):
    ctx = dict(nics=NICS_OK, misc=MISC_OK, svcs={"pxe-server": (True, False)},
               temps=[("CPU", 45.0)], fans=[("fan2", 2689)],
               exists=lambda p: True, ping=lambda h: True, pci=lambda vid, did: [])
    ctx.update(kw)
    return ctx


PROXY_TEXT = "代理模式：链路上已有别的 DHCP，推车不发地址，只回 PXE 引导"


def _pxe_ctx(guard):
    return dict(_ctx(), svcs={"pxe-server": (True, True)}, pxe_guard=lambda: guard)


def test_selftest_pxe_proxy_mode_is_warn_with_plain_text():
    """#11: 装机口上有别的 DHCP 时守卫转代理模式: 黄灯 + 人话."""
    assert kc.check_pxe(_pxe_ctx({"state": "proxy", "leases": False})) == ("WARN", PROXY_TEXT)


def test_selftest_pxe_proxy_mode_shows_their_subnet():
    g = {"state": "proxy", "ip": "192.0.2.1", "prefix": 24, "offered": "192.0.2.150"}
    assert kc.check_pxe(_pxe_ctx(g)) == ("WARN", PROXY_TEXT + "；对方网段 192.0.2.0/24")


def test_selftest_pxe_proxy_mode_warns_when_cart_outside_their_subnet():
    g = {"state": "proxy", "ip": "192.0.2.1", "prefix": 24, "offered": "198.51.100.50"}
    assert kc.check_pxe(_pxe_ctx(g)) == (
        "WARN", PROXY_TEXT + "；对方网段 198.51.100.0/24；推车 LAN1 不在对方网段，PXE 引导可能走不通")


@pytest.mark.parametrize("g", [{"state": "proxy", "offered": "198.51.100.50"},          # 没 ip/prefix
                               {"state": "proxy", "ip": "x", "prefix": 24, "offered": "y"},
                               {"state": "proxy", "ip": "192.0.2.1", "prefix": 24, "offered": None},
                               {"state": "proxy", "ip": "192.0.2.1", "prefix": 99, "offered": "192.0.2.9"}])
def test_selftest_pxe_proxy_mode_bad_fields_fall_back_to_plain_text(g):
    assert kc.check_pxe(_pxe_ctx(g)) == ("WARN", PROXY_TEXT)


@pytest.mark.parametrize("g", [{"state": "serving", "leases": True}, {}, {"state": "no-link"}])
def test_selftest_pxe_other_guard_states_unchanged(g):
    assert kc.check_pxe(_pxe_ctx(g)) == ("OK", "RUNNING")
    ctx = _pxe_ctx(g)
    del ctx["pxe_guard"]
    assert kc.check_pxe(ctx) == ("OK", "RUNNING")


def test_proxy_mode_turns_lan1_lamp_yellow_on_dash():
    """车机状态板和副屏灯条都吃同一个 check_pxe: 代理模式下 LAN1 灯黄, 副屏问题列表里有这一行."""
    import dash_core
    import lamps_core
    rows = kc.selftest_run(kc.DEFAULT_PLAN, _pxe_ctx({"state": "proxy"}))
    assert lamps_core.health_of(rows, "LAN1 / PXE") == "WARN"
    lower = {x["name"]: x["health"] for x in dash_core._lower(rows, set())}
    assert lower["LAN1"] == "WARN"
    assert {"label": "LAN1 / PXE", "state": "WARN", "detail": PROXY_TEXT} in dash_core._checks(rows)["problems"]


def test_selftest_context_reads_guard_state(tmp_path, monkeypatch):
    p = tmp_path / "pxe-guard.json"
    monkeypatch.setattr(kc, "PXE_GUARD", str(p))
    for name in ("nics_status", "misc_status", "services_status", "temps_status", "fans_status"):
        monkeypatch.setattr(kc, name, lambda *a, **k: {})
    ctx = kc.selftest_context()
    assert ctx["pxe_guard"]() == {}
    p.write_text('{"state": "proxy", "leases": false}')
    assert ctx["pxe_guard"]()["state"] == "proxy"
    p.write_text("not json")
    assert ctx["pxe_guard"]() == {}


def test_selftest_run_follows_plan_order_and_labels():
    plan = [{"check": "cpu"}, {"check": "lan", "label": "MGMT", "iface": "enp3s0"}]
    rows = kc.selftest_run(plan, _ctx())
    assert [(r["label"], r["state"]) for r in rows] == [("CPU", "OK"), ("MGMT", "OK")]


def test_selftest_run_isolates_crashing_check():
    def boom(ctx):
        raise RuntimeError("x")
    rows = kc.selftest_run([{"check": "boom", "label": "BOOM"}, {"check": "cpu"}], _ctx(),
                           checks=dict(kc.CHECKS, boom=boom))
    assert rows[0] == {"label": "BOOM", "state": "FAIL", "detail": "ERROR", "check": "boom"}
    assert rows[1]["state"] == "OK"


def test_selftest_run_unknown_check():
    rows = kc.selftest_run([{"check": "nope"}], _ctx())
    assert rows == [{"label": "NOPE", "state": "FAIL", "detail": "UNKNOWN CHECK", "check": "nope"}]


def test_check_serial_uses_dev_param():
    seen = []
    ctx = _ctx(exists=lambda p: seen.append(p) or False)
    assert kc.check_serial(ctx, dev="/dev/ttyS1") == ("--", "NOT CONNECTED")
    assert seen == ["/dev/ttyS1"]


def test_check_serial_devs_counts_present():
    devs = ["/dev/con-1", "/dev/con-2", "/dev/con-3", "/dev/con-4"]
    assert kc.check_serial(_ctx(exists=lambda p: True), devs=devs) == ("OK", "4/4")
    assert kc.check_serial(_ctx(exists=lambda p: p != "/dev/con-2"), devs=devs) == ("WARN", "3/4 NO CON-2")
    assert kc.check_serial(_ctx(exists=lambda p: False), devs=devs) == ("--", "NOT CONNECTED")


def test_check_ping():
    assert kc.check_ping(_ctx(), host="192.0.2.8") == ("OK", "192.0.2.8")
    assert kc.check_ping(_ctx(ping=lambda h: False), host="nas") == ("WARN", "UNREACHABLE")


def test_check_pci_counts_and_driver():
    found = [{"slot": "02:00.0", "driver": None}, {"slot": "04:00.0", "driver": None}]
    ctx = _ctx(pci=lambda v, d: found)
    assert kc.check_pci(ctx, id="12ab:0710", count=2) == ("OK", "x2 NO DRIVER")
    assert kc.check_pci(ctx, id="12ab:0710", count=3) == ("FAIL", "x2 / 3")
    assert kc.check_pci(ctx, id="12ab:0710", driver="sc0710")[0] == "WARN"
    assert kc.check_pci(_ctx(), id="12ab:0710") == ("FAIL", "NOT FOUND")
    ok = _ctx(pci=lambda v, d: [{"slot": "02:00.0", "driver": "sc0710"}])
    assert kc.check_pci(ok, id="12ab:0710", driver="sc0710") == ("OK", "x1 sc0710")


def test_pci_devices_reads_sysfs(tmp_path):
    for slot, dev, drv in (("0000:02:00.0", "0x0710", None), ("0000:01:00.0", "0x0fc8", "nouveau")):
        d = tmp_path / slot
        d.mkdir()
        (d / "vendor").write_text("0x12ab\n" if dev == "0x0710" else "0x10de\n")
        (d / "device").write_text(dev + "\n")
        if drv:
            (tmp_path / drv).mkdir()
            (d / "driver").symlink_to(tmp_path / drv)
    assert kc.pci_devices("12ab", "0710", root=str(tmp_path)) == [{"slot": "0000:02:00.0", "driver": None}]
    assert kc.pci_devices("10de", "0fc8", root=str(tmp_path))[0]["driver"] == "nouveau"


def test_selftest_plan_read(tmp_path):
    assert kc.selftest_plan_read(str(tmp_path / "missing.json")) == kc.DEFAULT_PLAN
    p = tmp_path / "s.json"
    p.write_text('[{"check": "cpu"}]')
    assert kc.selftest_plan_read(str(p)) == [{"check": "cpu"}]
    p.write_text("{broken")
    assert kc.selftest_plan_read(str(p)) == kc.DEFAULT_PLAN


def test_selftest_line_format():
    line = kc.selftest_line({"label": "LAN2", "state": "OK", "detail": "203.0.113.4"}, width=20)
    assert line.startswith("LAN2 ") and ". OK" in line and line.endswith("203.0.113.4")


def test_branding_defaults_and_override(tmp_path):
    assert kc.branding_read(str(tmp_path))["title"] == "OPS-CART"
    assert kc.branding_read(str(tmp_path))["logo"] is None
    (tmp_path / "branding.json").write_text('{"title": "ILLUSION", "subtitle": "x"}')
    (tmp_path / "logo.png").write_bytes(b"png")
    b = kc.branding_read(str(tmp_path))
    assert b["title"] == "ILLUSION" and b["subtitle"] == "x"
    assert b["logo"] == str(tmp_path / "logo.png")


def test_branding_bad_json_falls_back(tmp_path):
    (tmp_path / "branding.json").write_text("{oops")
    assert kc.branding_read(str(tmp_path))["title"] == "OPS-CART"


def test_version_string(tmp_path):
    (tmp_path / "VERSION").write_text("1.00.00\n")
    (tmp_path / "BUILD").write_text("12 4265fc8\n")
    assert kc.version_string(str(tmp_path)) == "Ver 1.00.00   Build 0012 (4265fc8)"


def test_version_string_dirty_and_missing(tmp_path):
    assert kc.version_string(str(tmp_path)) == "Ver dev"
    (tmp_path / "VERSION").write_text("1.00.00")
    assert kc.version_string(str(tmp_path)) == "Ver 1.00.00"
    (tmp_path / "BUILD").write_text("13 abc1234-dirty")
    assert kc.version_string(str(tmp_path)) == "Ver 1.00.00   Build 0013 (abc1234-dirty)"


def test_notice_font_px_fills_box():
    # 中文一格=1 字宽, ASCII 0.6; 行高 1.3
    px = kc.notice_font_px("请前往101上课", 1000, 1000)    # 5 中文 + 3 数字 = 6.8 字宽
    assert px == int(1000 / 6.8)


def test_notice_font_px_limited_by_height_and_clamped():
    assert kc.notice_font_px("一\n二\n三\n四", 2000, 520) == int(520 / (4 * 1.3))
    assert kc.notice_font_px("短", 5000, 5000) == 220       # 上限
    assert kc.notice_font_px("长" * 400, 800, 400) == 24    # 下限
    assert kc.notice_font_px("", 800, 400) == 24


# ---------------------------------------------------------------- 透传释放 / 画面源

def test_release_combo_by_keysym_and_state():
    assert kc.is_release_combo("Alt_L", 0x4)          # 先按 Ctrl 再按 Alt
    assert kc.is_release_combo("Control_L", 0x8)      # 先按 Alt 再按 Ctrl
    assert kc.is_release_combo("Meta_L", 0x4)         # 无 WM 时 Alt 常报成 Meta
    assert not kc.is_release_combo("Alt_L", 0)
    assert not kc.is_release_combo("a", 0xC)


def test_video_devices_sorted_numerically(tmp_path):
    for n in ("video10", "video1", "video0", "vbi0"):
        (tmp_path / n).touch()
    assert kc.video_devices(str(tmp_path)) == [str(tmp_path / f"video{i}") for i in (0, 1, 10)]
    assert kc.video_devices(str(tmp_path / "none")) == []


def test_next_video_device_cycles():
    devs = ["/dev/video0", "/dev/video1"]
    assert kc.next_video_device(devs, "/dev/video0") == "/dev/video1"
    assert kc.next_video_device(devs, "/dev/video1") == "/dev/video0"
    assert kc.next_video_device(devs, "/dev/video9") == "/dev/video0"
    assert kc.next_video_device([], "/dev/video0") == "/dev/video0"


# ---------------------------------------------------------------- CH9329: 状态只报在线数和绑定数
# 键鼠按 kvm_core 的序列号绑定认人, 不再有"那一块 CH9329"; ch9329_port 已删

def test_ch9329_port_is_gone():
    assert not hasattr(kc, "ch9329_port") and not hasattr(kc, "CH9329_BY_ID")


def test_kvm_summary_counts_online_and_bound():
    conf = {"names": {}, "bind": {"pci:01:00.0": "SN1", "usb:534d:2109": "SN9"}}
    assert kc.kvm_summary({"SN1": "/dev/x", "SN2": "/dev/y"}, conf) == {"online": 2, "bound": 2}
    assert kc.kvm_summary({}, {"names": {}, "bind": {}}) == {"online": 0, "bound": 0}


def test_misc_status_reports_kvm_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (0, "192.0.2.10"))
    monkeypatch.setattr(kc, "PROC_UPTIME", str(tmp_path / "none"))
    m = kc.misc_status(kvm_ports=lambda: {"SN1": "/dev/x"}, kvm_conf=lambda: {"bind": {"a": "SN1"}})
    assert m["kvm"] == {"online": 1, "bound": 1} and "ch9329" not in m


def test_check_ch9329_shows_counts():
    assert kc.check_ch9329({"misc": {"kvm": {"online": 2, "bound": 1}}}) == ("OK", "2 ONLINE / 1 BOUND")
    assert kc.check_ch9329({"misc": {"kvm": {"online": 0, "bound": 1}}}) == ("--", "NOT CONNECTED")
    assert kc.check_ch9329({"misc": {}}) == ("--", "NOT CONNECTED")


# ------------------------------------------------------------ 装机模式 (2026-10-21)
# 默认只有救援菜单; 推车上输屏幕随机码解锁, 只对选定 MAC 下发装机菜单, 30 分钟或被取走即上锁

MAC = "02:00:00:00:00:01"


def test_mac_cfg_name_is_grub_per_mac_file():
    assert kc.mac_cfg_name(MAC) == "grub.cfg-01-02-00-00-00-00-01"
    assert kc.mac_cfg_name("02-00-00-00-00-01") == "grub.cfg-01-02-00-00-00-00-01"
    with pytest.raises(ValueError):
        kc.mac_cfg_name("not-a-mac")


def test_rescue_menu_never_installs():
    cfg = kc.grub_cfg_rescue()
    assert "inst.rescue" in cfg and "inst.ks" not in cfg and "set default=0" in cfg


def test_install_menu_has_kickstart():
    cfg = kc.grub_cfg_install()
    assert "inst.ks=" in cfg and "inst.repo=" in cfg


def test_arm_writes_per_mac_menu_only(tmp_path):
    state = tmp_path / "pxe-mode.json"
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=state)
    assert "inst.ks" in (tmp_path / "grub.cfg-01-02-00-00-00-00-01").read_text()
    assert "inst.ks" not in (tmp_path / "grub.cfg").read_text()        # 别的机器照样只能救援
    st = kc.pxe_mode_read(path=state, now=1001)
    assert st == {"mode": "install", "mac": MAC, "since": 1000, "until": 1000 + 1800}


def test_disarm_and_expiry(tmp_path):
    state = tmp_path / "pxe-mode.json"
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=state)
    assert kc.pxe_mode_read(path=state, now=1000 + 1801)["mode"] == "expired"
    kc.pxe_disarm(root=tmp_path, path=state)
    assert not (tmp_path / "grub.cfg-01-02-00-00-00-00-01").exists()
    assert kc.pxe_mode_read(path=state, now=2000) == {"mode": "rescue"}
    assert "inst.ks" not in (tmp_path / "grub.cfg").read_text()


def test_mode_read_missing_or_garbage_is_rescue(tmp_path):
    assert kc.pxe_mode_read(path=tmp_path / "nope", now=0) == {"mode": "rescue"}
    (tmp_path / "bad").write_text("{oops")
    assert kc.pxe_mode_read(path=tmp_path / "bad", now=0) == {"mode": "rescue"}


LOG = """2026-10-21 15:11:01,729 TFTP RRQ /grub.cfg-01-02-00-00-00-00-01 失败: [Errno 2] No such file
2026-10-21 15:19:22,118 DHCP 客户端 02:00:00:00:00:01 vendor='PXEClient:Arch:00007' arch=7 src=0.0.0.0:68
2026-10-21 15:19:30,001 DHCP 客户端 aa:bb:cc:dd:ee:ff vendor='PXEClient:Arch:00007' arch=7 src=0.0.0.0:68
2026-10-21 15:20:00,000 TFTP RRQ /grub.cfg-01-02-00-00-00-00-01 (900 bytes, blksize=1024) from 203.0.113.10
"""


def _ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def test_consumed_only_by_successful_fetch_after_arm():
    assert kc.pxe_consumed(LOG, MAC, since=_ts("2026-10-21 15:19:00"))
    assert not kc.pxe_consumed(LOG, MAC, since=_ts("2026-10-21 15:21:00"))     # 解锁前的旧记录不算
    assert not kc.pxe_consumed(LOG.splitlines()[0], MAC, since=0)              # 失败的 RRQ 不算


def test_recent_clients_latest_first_and_windowed():
    now = _ts("2026-10-21 15:30:00")
    assert kc.recent_pxe_clients(LOG, now) == ["aa:bb:cc:dd:ee:ff", MAC]
    assert kc.recent_pxe_clients(LOG, now + 7200) == []


def test_unlock_code():
    c = kc.new_code()
    assert len(c) == 6 and c.isdigit()
    assert kc.code_matches(f" {c} ", c) and not kc.code_matches("000000x", c)


# ------------------------------------------------------------ mpv 预览参数
# 2026-10-21: 有画面时点不进透传 -- mpv 订阅了鼠标事件, 点击被它吃掉, 到不了 Tk 的 Frame

def test_mpv_does_not_eat_mouse_or_keys():
    cmd = kc.mpv_cmd(4196677, "/dev/video0")
    assert "--wid=4196677" in cmd
    assert "--input-cursor=no" in cmd and "--input-vo-keyboard=no" in cmd
    assert "--profile=low-latency" in cmd


def test_mpv_opens_device_as_v4l2_capture():
    """2026-10-21: 裸路径 /dev/videoN 被 mpv 当文件 read(); SC0710 厂商驱动的 read() 不出帧 (黑屏),
    UVC 不支持 read() 才碰巧退回 lavf. 显式 av://v4l2: 走 mmap 采集."""
    assert kc.mpv_cmd(1, "/dev/video3")[-1] == "av://v4l2:/dev/video3"


def test_mpv_avoids_gl_output():
    """2026-10-21: GT740 + nouveau 无合成器, mpv 默认 GL 输出会把碎片画到盖在上面的锁屏上;
    改用 Xv, 交给 X 服务器画 (正确处理遮挡, 仍有硬件缩放)."""
    assert "--vo=xv" in kc.mpv_cmd(1, "/dev/video0")


# ------------------------------------------------------------ 透传释放组合 (2026-10-21)
# Ctrl+Alt 按下又松开、中间没按别的键 -> 释放; 中间按过 Del 等 -> 组合被"用掉", 不释放

def _feed(seq):
    r = kc.ReleaseCombo()
    return [r.key(k, d) for k, d in seq]


def test_ctrl_alt_tap_releases_on_keyup():
    out = _feed([("ctrl", True), ("alt", True), ("alt", False)])
    assert out == [False, False, True]


def test_ctrl_alt_del_goes_through():
    out = _feed([("ctrl", True), ("alt", True), ("delete", True), ("delete", False),
                 ("alt", False), ("ctrl", False)])
    assert not any(out)


def test_alt_tab_never_releases():
    assert not any(_feed([("alt", True), ("tab", True), ("tab", False), ("alt", False)]))


def test_combo_rearms_after_all_keys_up():
    r = kc.ReleaseCombo()
    for k, d in [("ctrl", True), ("alt", True), ("delete", True), ("delete", False),
                 ("alt", False), ("ctrl", False)]:
        r.key(k, d)
    assert [r.key("ctrl", True), r.key("alt", True), r.key("ctrl", False)] == [False, False, True]


# ------------------------------------------------------------ 透传键盘: 物理键码同步按住/松开 (2026-10-21)
# X 键码 = evdev + 8; 不看 keysym (Shift+1 会变 exclam、受输入法影响), 直接映射到 HID usage

def test_xkeycode_maps_to_hid():
    k = kc.X_KEYCODE_HID
    assert k[38] == ("key", 0x04) and k[24] == ("key", 0x14)      # a, q
    assert k[10] == ("key", 0x1E) and k[9] == ("key", 0x29)       # 1, Esc
    assert k[119] == ("key", 0x4C) and k[111] == ("key", 0x52)    # Delete, Up
    assert k[23] == ("key", 0x2B) and k[65] == ("key", 0x2C)      # Tab, Space
    assert k[37] == ("mod", 0x01) and k[50] == ("mod", 0x02)      # Ctrl_L, Shift_L
    assert k[64] == ("mod", 0x04) and k[133] == ("mod", 0x08)     # Alt_L, Super_L
    assert k[105] == ("mod", 0x10) and k[108] == ("mod", 0x40)    # Ctrl_R, Alt_R


def test_mod_names_for_release_combo():
    assert kc.mod_name(37) == "ctrl" and kc.mod_name(105) == "ctrl"
    assert kc.mod_name(64) == "alt" and kc.mod_name(108) == "alt"
    assert kc.mod_name(50) == "shift" and kc.mod_name(133) == "gui"
    assert kc.mod_name(38) is None


def test_hid_keyboard_holds_alt_while_tabbing():
    kb = kc.HidKeyboard()
    assert kb.press(64) == bytes([0x04, 0, 0, 0, 0, 0, 0, 0])
    assert kb.press(23) == bytes([0x04, 0, 0x2B, 0, 0, 0, 0, 0])
    assert kb.release(23) == bytes([0x04, 0, 0, 0, 0, 0, 0, 0])
    assert kb.press(23) == bytes([0x04, 0, 0x2B, 0, 0, 0, 0, 0])     # Alt 一直按着, 再切一次
    assert kb.release(23)[2] == 0
    assert kb.release(64) == bytes(8)


def test_hid_keyboard_ignores_repeat_unknown_and_overflow():
    kb = kc.HidKeyboard()
    assert kb.press(38) is not None
    assert kb.press(38) is None            # X 自动重复: 状态没变不重发
    assert kb.press(999) is None and kb.release(999) is None
    for code in (24, 25, 26, 27, 28):      # q w e r t -> 加上 a 一共 6 个
        assert kb.press(code) is not None
    assert kb.press(29) is None            # 第 7 个普通键丢掉
    assert kb.release_all() == bytes(8)


# ------------------------------------------------------------ 绝对鼠标坐标 (2026-10-21)

def test_abs_point_same_aspect():
    assert kc.abs_point(0, 0, 1600, 900, 1920, 1080) == (0, 0)
    assert kc.abs_point(1599, 899, 1600, 900, 1920, 1080) == (4095, 4095)
    assert kc.abs_point(800, 450, 1600, 900, 1920, 1080) == (2049, 2050)


def test_abs_point_letterbox():
    # 1600x1000 的框放 16:9 画面: 实际 1600x900, 上下各 50 黑边
    assert kc.abs_point(0, 50, 1600, 1000, 1920, 1080) == (0, 0)
    assert kc.abs_point(0, 40, 1600, 1000, 1920, 1080) is None           # 点在黑边上
    assert kc.abs_point(1599, 949, 1600, 1000, 1920, 1080) == (4095, 4095)
    # 4:3 画面放宽框: 左右黑边
    assert kc.abs_point(100, 300, 1600, 900, 1024, 768) is None
    assert kc.abs_point(200, 0, 1600, 900, 1024, 768) == (0, 0)


def test_parse_v4l2_size():
    out = "Format Video Capture:\n\tWidth/Height      : 1920/1080\n\tPixel Format      : 'MJPG'\n"
    assert kc.parse_v4l2_size(out) == (1920, 1080)
    assert kc.parse_v4l2_size("garbage") is None


# ------------------------------------------------------------ 采集设备友好名称 + 自检 (2026-10-21)

def _fake_v4l(tmp_path):
    """照推车实测造一份 /sys/class/video4linux: MS2130 占 video0(画面)+video1(元数据), 两张 SC0710."""
    sysv, devs = tmp_path / "v4l", tmp_path / "devices"
    for node, name, index, dev in (
            ("video0", "UVC Camera (345f:2109): USB Vid", 0, "usb1/1-4/1-4.4/1-4.4:1.0"),
            ("video1", "UVC Camera (345f:2109): USB Vid", 1, "usb1/1-4/1-4.4/1-4.4:1.0"),
            ("video3", "SC0710:RAW 01.00 0001f715", 0, "pci0000:00/0000:00:1d.0/0000:04:00.0"),
            ("video2", "SC0710:RAW 00.00 0001f715", 0, "pci0000:00/0000:00:06.0/0000:02:00.0")):
        d = sysv / node
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(f"{index}\n")
        (devs / dev).mkdir(parents=True, exist_ok=True)
        (d / "device").symlink_to(devs / dev)
    return sysv


def test_video_nodes_friendly_and_capture_only(tmp_path):
    nodes = kc.video_nodes(_fake_v4l(tmp_path), dev_root="/dev")
    assert [(n["dev"], n["friendly"]) for n in nodes] == [
        ("/dev/video0", "MS2130 USB"),
        ("/dev/video2", "SC0710 卡1"),          # 按 PCI 插槽排: 02:00.0 是卡1
        ("/dev/video3", "SC0710 卡2"),
    ]                                           # video1 是元数据节点, 不出画面, 跳过
    assert nodes[1]["key"] == "0000:02:00.0" and nodes[0]["key"] == "345f:2109"


def test_video_nodes_name_overrides(tmp_path):
    names = {"0000:04:00.0": "X光右屏"}
    nodes = kc.video_nodes(_fake_v4l(tmp_path), dev_root="/dev", names=names)
    assert [n["friendly"] for n in nodes] == ["MS2130 USB", "SC0710 卡1", "X光右屏"]


def test_video_nodes_missing_sysfs(tmp_path):
    assert kc.video_nodes(tmp_path / "nope") == []


# 2026-10-21: SC0710 延迟比 MS2130 低不少, 开机默认进有信号的那张卡

def test_parse_input_signal():
    sc = "Video input : 0 (HDMI k0/TVI/GMSL/SDVOE INPUT(0): {})"
    assert kc.parse_input_signal(sc.format("ok")) is True
    assert kc.parse_input_signal(sc.format("no signal, no hsync lock")) is False
    assert kc.parse_input_signal("Video input : 0 (Camera 1: ok)") is True
    assert kc.parse_input_signal("VIDIOC_G_INPUT: failed: Inappropriate ioctl for device") is None


_NODES = [{"dev": "/dev/video0", "name": "UVC Camera (345f:2109): USB Vid"},
          {"dev": "/dev/video2", "name": "SC0710:RAW 00.00 0001f715"},
          {"dev": "/dev/video3", "name": "SC0710:RAW 01.00 0001f715"}]


def test_default_video_prefers_sc0710_with_signal():
    sig = {"/dev/video0": True, "/dev/video2": False, "/dev/video3": True}
    assert kc.default_video(_NODES, sig.get) == "/dev/video3"


def test_default_video_falls_back_to_other_signal_then_first():
    assert kc.default_video(_NODES, {"/dev/video0": True}.get) == "/dev/video0"
    assert kc.default_video(_NODES, lambda d: False) == "/dev/video0"
    assert kc.default_video([], lambda d: True) is None


def test_check_video_by_match():
    nodes = [{"dev": "/dev/video0", "friendly": "MS2130 USB", "key": "345f:2109", "name": "UVC Camera"},
             {"dev": "/dev/video2", "friendly": "SC0710 卡1", "key": "0000:02:00.0", "name": "SC0710:RAW"}]
    ctx = {"videos": lambda: nodes}
    assert kc.check_video(ctx, "345f:2109") == ("OK", "video0")
    assert kc.check_video(ctx, "SC0710", count=2) == ("FAIL", "x1 / 2")
    assert kc.check_video(ctx, "dead:beef") == ("FAIL", "NOT FOUND")


# ------------------------------------------------------------ 右侧二级菜单 (2026-10-21)
# 菜单搬到 kiosk_ui_core.MENU (测试在 test_kiosk_ui_core), 这里只守着别留两份

def test_menu_lives_only_in_ui_core():
    assert not hasattr(kc, "MENU")


# ------------------------------------------------------------ 状态板 = 实时自检 (2026-10-21)
# 状态板和开机自检共用 CHECKS + selftest.json, 以后加检查项只改计划表

def test_check_service_states():
    svcs = {"pxe-server": (True, True), "pxe-http-repo": (True, False), "webkiosk": (False, False)}
    ctx = _ctx(svcs=svcs)
    assert kc.check_service(ctx, unit="pxe-server") == ("OK", "RUNNING")
    # 开了自启却没跑: 提醒 (LAN1 没插线时 http 服务就会这样); 标了 required 才算故障
    assert kc.check_service(ctx, unit="pxe-http-repo") == ("WARN", "NOT RUNNING")
    assert kc.check_service(ctx, unit="pxe-http-repo", required=True) == ("FAIL", "NOT RUNNING")
    assert kc.check_service(ctx, unit="webkiosk") == ("--", "OFF")              # 没打算跑, 不算故障
    assert kc.check_service(ctx, unit="nope") == ("--", "OFF")


def test_check_wifi():
    st = lambda **k: (lambda: {"ssid": "CAMPUS-NET", "signal": 68, "ip": "10.0.0.5/20", "egress": False, **k})
    assert kc.check_wifi(_ctx(wifi=st())) == ("OK", "CAMPUS-NET 68% STANDBY")
    assert kc.check_wifi(_ctx(wifi=st(egress=True))) == ("WARN", "CAMPUS-NET 68% IN USE")   # 网线断了在顶班
    assert kc.check_wifi(_ctx(wifi=lambda: {"ssid": None})) == ("--", "NOT CONNECTED")


def test_check_signal_by_friendly_name():
    nodes = [{"dev": "/dev/video2", "friendly": "SC0710 卡1", "key": "0000:02:00.0", "name": "SC0710:RAW"},
             {"dev": "/dev/video3", "friendly": "SC0710 卡2", "key": "0000:05:00.0", "name": "SC0710:RAW"}]
    sig = {"/dev/video2": False, "/dev/video3": True}
    ctx = _ctx(videos=lambda: nodes, signal=sig.get)
    assert kc.check_signal(ctx, match="卡2") == ("OK", "SIGNAL video3")
    assert kc.check_signal(ctx, match="卡1") == ("--", "NO SIGNAL video2")         # 没接线不算故障
    assert kc.check_signal(ctx, match="卡9") == ("FAIL", "NOT FOUND")


def test_status_board_lines_tags_by_state():
    rows = [{"label": "LAN2", "state": "FAIL", "detail": "NO LINK"},
            {"label": "WIFI", "state": "WARN", "detail": "CAMPUS-NET 68% IN USE"},
            {"label": "TAILSCALE", "state": "OK", "detail": "192.0.2.10"},
            {"label": "CONSOLE", "state": "--", "detail": "NOT CONNECTED"}]
    lines = kc.status_board_lines(rows, temps=[("CPU", 57.0)], fans=[("fan2", 2393)], uptime="up 1 hour")
    assert [t for _, t in lines[:4]] == ["bad", "t_warn", "ok", "dim"]
    assert lines[0][0].startswith("LAN2 ") and lines[0][0].endswith("FAIL NO LINK")
    texts = [t for t, _ in lines]
    assert "up 1 hour" in texts and any("CPU" in t and "57°C" in t for t in texts)
    assert any("fan2" in t and "2393" in t for t in texts)


def test_status_board_lines_show_lldp_peers_after_checks():
    rows = [{"label": "LAN2", "state": "OK", "detail": "192.0.2.5"}]
    peers = [{"name": "LAN1", "text": "对端 SW-A Gi0/1", "level": "ok"},
             {"name": "LAN2", "text": "没收到 LLDP（交换机可能关了）", "level": "dim"}]
    lines = kc.status_board_lines(rows, temps=[], fans=[], uptime="up", peers=peers)
    assert lines[1] == ("LAN1  对端 SW-A Gi0/1", "ok")
    assert lines[2] == ("LAN2  没收到 LLDP（交换机可能关了）", "dim")
    assert kc.status_board_lines(rows, [], [], "up") == kc.status_board_lines(rows, [], [], "up", peers=[])


def test_status_board_lines_before_first_check():
    lines = kc.status_board_lines(None, temps=[], fans=[], uptime="up 1 min")
    assert lines[0] == ("检查中…", "dim")


def test_default_plan_covers_old_status_board():
    checks = [it["check"] for it in kc.DEFAULT_PLAN]
    units = {it.get("unit") for it in kc.DEFAULT_PLAN if it["check"] == "service"}
    assert {"pxe-server", "pxe-http-repo", "pxe-http-boot"} <= units
    assert "wifi" in checks and "lan" in checks and "tailscale" in checks


def test_status_board_skips_cpu_fan_rows_shown_in_temps_block():
    rows = kc.selftest_run([{"check": "cpu"}, {"check": "fan"}, {"check": "tailscale"}], _ctx())
    assert rows[0]["check"] == "cpu"
    texts = [t for t, _ in kc.status_board_lines(rows, temps=[], fans=[], uptime="up")]
    assert texts[0].startswith("TAILSCALE") and not any(t.startswith(("CPU ", "FAN ")) for t in texts)


class _FakeRoot:
    def __init__(self):
        self.binds = {}

    def bind_class(self, cls, seq, fn):
        self.binds[(cls, seq)] = fn


class _FakeWidget:
    def __init__(self):
        self.generated = []

    def event_generate(self, seq):
        self.generated.append(seq)


def test_kp_enter_acts_as_return_in_every_entry():
    """小键盘回车 (KP_Enter) 在所有输入框里等同主键盘回车, 弹窗(simpledialog)也算."""
    root = _FakeRoot()
    kc.kp_enter_as_return(root)
    fn = root.binds[("Entry", "<KP_Enter>")]
    w = _FakeWidget()

    class Ev:
        widget = w
    fn(Ev())
    assert w.generated == ["<Return>"]


# ------------------------------------------------------------ 滚轮 (2026-10-22)
# 推车 Tk 8.6 + X11: 滚轮是 4/5 号键, 不发 <MouseWheel>; Windows/macOS 才发 MouseWheel(delta)

def test_wheel_step_x11_buttons():
    assert kc.wheel_step("4", 4, 0) == 1        # ButtonPress-4 = 上滚 = HID 正
    assert kc.wheel_step("4", 5, 0) == -1
    assert kc.wheel_step("5", 4, 0) == 0        # 松开不算, 否则一格滚两下


def test_wheel_step_mousewheel_delta():
    assert kc.wheel_step("38", "??", 120) == 1
    assert kc.wheel_step("38", "??", -120) == -1
    assert kc.wheel_step("38", "??", 360) == 3      # 快速滚动别吞格
    assert kc.wheel_step("38", "??", -40) == -1     # 小 delta 正负对称, 不能一边是 0 一边是 -1
    assert kc.wheel_step("38", "??", 40) == 1


def test_wheel_step_ignores_other_buttons():
    assert kc.wheel_step("4", 1, 0) == 0
    assert kc.wheel_step("6", "??", 0) == 0


def test_mouse_bit_only_for_real_buttons():
    assert kc.mouse_bit(1) == 0x01 and kc.mouse_bit(3) == 0x02 and kc.mouse_bit(2) == 0x04
    assert kc.mouse_bit(4) is None and kc.mouse_bit(5) is None


def test_video_key_of_dev():
    nodes = [{"dev": "/dev/video0", "key": "0000:05:00.0"}, {"dev": "/dev/video2", "key": "345f:2109"}]
    assert kc.video_key("/dev/video2", nodes) == "345f:2109"
    assert kc.video_key("/dev/video9", nodes) is None


def test_video_nodes_usb_key_from_sysfs_when_name_has_no_ids(tmp_path):
    """2K USB Camera 的名字里没有 vid:pid -> 从 USB 设备目录读, 不能退回 video4 这种会变的编号."""
    sysv = _fake_v4l(tmp_path)
    usb = tmp_path / "devices" / "usb1" / "1-2" / "1-2.1"
    (usb / "1-2.1:1.0").mkdir(parents=True)
    (usb / "idVendor").write_text("2bdf\n")
    (usb / "idProduct").write_text("0294\n")
    d = sysv / "video4"
    d.mkdir()
    (d / "name").write_text("2K USB Camera: 2K USB Camera\n")
    (d / "index").write_text("0\n")
    (d / "device").symlink_to(usb / "1-2.1:1.0")
    cam = next(n for n in kc.video_nodes(sysv) if n["dev"] == "/dev/video4")
    assert cam["key"] == "2bdf:0294"


ROUTE_WIRED = "default via 192.0.2.1 dev enp3s0 proto dhcp src 192.0.2.228 metric 100\n" \
              "default via 198.51.100.1 dev wlp4s0 proto dhcp metric 600\n"
ROUTE_WIFI = "default via 198.51.100.1 dev wlp4s0 proto dhcp src 198.51.100.20 metric 600\n"


def test_default_route_dev_takes_first_default():
    assert kc.default_route_dev(ROUTE_WIRED) == "enp3s0"
    assert kc.default_route_dev("10.0.0.0/8 via 203.0.113.11 dev eno1\n") is None
    assert kc.default_route_dev("") is None


def test_net_state_three_levels():
    assert kc.net_state(ROUTE_WIRED, internet=True) == ("on", "有线")
    assert kc.net_state(ROUTE_WIFI, internet=True) == ("on", "Wi-Fi")
    assert kc.net_state(ROUTE_WIFI, internet=False) == ("warn", "Wi-Fi 无外网")   # 连上了但出不去: 没认证/上游断
    assert kc.net_state("", internet=False) == ("off", "未连接")
    assert kc.net_state("default dev tailscale0\n", internet=True) == ("on", "tailscale0")


def test_net_icon_paths(tmp_path):
    assert kc.net_icon("warn", d=str(tmp_path)) == str(tmp_path / "net-warn.png")


# ------------------------------------------------------------ disarm 扫整棵 tftp 树 (1.06.00)

def test_disarm_scrubs_install_menus_anywhere_in_tree(tmp_path):
    (tmp_path / "EFI" / "BOOT").mkdir(parents=True)
    auto = "set default=0\nmenuentry 'x' { linux /vmlinuz inst.ks=http://192.0.2.1:8000/ks.cfg }\n"
    (tmp_path / "EFI" / "BOOT" / "grub.cfg").write_text(auto)
    (tmp_path / "grub.cfg-C000020A").write_text(auto)              # 按 IP (十六进制) 命名的菜单
    (tmp_path / "grub.cfg").write_text(auto)
    (tmp_path / "grub.cfg.bak-20261021").write_text(auto)          # 手工备份: GRUB 不会自己去取, 留着
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "pxe-mode.json")
    menus = {str(p.relative_to(tmp_path)): p.read_text() for p in tmp_path.rglob("grub.cfg*")
             if not p.name.startswith("grub.cfg.")}
    assert not any("inst.ks" in t for t in menus.values()), menus
    assert set(menus) == {"grub.cfg"}
    assert (tmp_path / "grub.cfg.bak-20261021").exists()


# ------------------------------------------------------------ PinGate 退避 + 线程安全 (1.06.00)

def test_pin_gate_backoff_grows_and_caps():
    h = kc.pin_hash("2468")
    g = kc.PinGate(h, max_fails=2, lock_s=60, max_lock_s=200)
    t, locks = 0, []
    for _ in range(4):
        g.attempt("0000", now=t)
        assert g.attempt("0000", now=t) == "locked"
        locks.append(g.locked_until - t)
        t = g.locked_until
    assert locks == [60, 120, 200, 200]
    assert g.attempt("2468", now=t) == "ok"
    g.attempt("0000", now=t)
    g.attempt("0000", now=t)
    assert g.locked_until - t == 60                       # 输对一次就回到起步


def test_pin_gate_none_cap_keeps_fixed_lock():
    """max_lock_s=None 显式要求固定冷却 (网页口 tailnet 用)."""
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=1, lock_s=60, max_lock_s=None)
    g.attempt("0000", now=0)
    g.attempt("0000", now=60)
    assert g.locked_until == 120


def test_pin_gate_default_backoff_grows_to_cap():
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=1, lock_s=60, event=lambda m: None)
    t, spans = 0, []
    for _ in range(6):
        g.attempt("0000", now=t)
        spans.append(g.locked_until - t)
        t = g.locked_until
    assert spans == [60, 120, 240, 480, 900, 900] and kc.PIN_MAX_LOCK_S == 900


def test_pin_gate_persists_failures_across_restart(tmp_path):
    state = tmp_path / "run" / "pin-gate.json"
    h = kc.pin_hash("2468")
    g = kc.PinGate(h, max_fails=3, lock_s=60, state_path=str(state), event=lambda m: None)
    g.attempt("0000", now=0)
    g.attempt("0000", now=1)
    g2 = kc.PinGate(h, max_fails=3, lock_s=60, state_path=str(state), event=lambda m: None)
    assert g2.attempt("0000", now=2) == "locked"          # kiosk 重启不清零
    g3 = kc.PinGate(h, max_fails=3, lock_s=60, state_path=str(state), event=lambda m: None)
    assert g3.attempt("2468", now=30) == "locked"         # 冷却也跟着留下
    assert g3.attempt("2468", now=63) == "ok"
    g4 = kc.PinGate(h, max_fails=3, lock_s=60, state_path=str(state), event=lambda m: None)
    assert (g4.fails, g4.lockouts) == (0, 0)


def test_pin_gate_bad_state_file_starts_clean(tmp_path):
    state = tmp_path / "pin-gate.json"
    state.write_text("{oops")
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=2, state_path=str(state), event=lambda m: None)
    assert g.attempt("0000", now=0) == "bad"


def test_pin_gate_unwritable_state_does_not_break(tmp_path):
    (tmp_path / "f").write_text("x")
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=2, state_path=str(tmp_path / "f" / "s.json"),
                   event=lambda m: None)
    assert g.attempt("0000", now=0) == "bad" and g.attempt("2468", now=1) == "ok"


def test_pin_gate_logs_cooldown_event():
    events = []
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=2, lock_s=60, event=events.append)
    g.attempt("0000", now=0)
    assert events == []
    assert g.attempt("0000", now=1) == "locked"
    assert len(events) == 1 and "60" in events[0]


def test_pin_gate_default_event_goes_to_cartlog_pin(monkeypatch):
    calls = []
    monkeypatch.setattr(kc.cartlog, "event", lambda kind, msg, **k: calls.append(kind))
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=1)
    g.attempt("0000", now=0)
    assert calls == ["pin"]


def test_pin_gate_concurrent_bad_attempts_counted_once_each():
    import threading
    g = kc.PinGate(kc.pin_hash("2468"), max_fails=5, lock_s=60)
    res = []
    ts = [threading.Thread(target=lambda: res.append(g.attempt("0000", now=0))) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res.count("bad") == 4 and res.count("locked") == 16


# ------------------------------------------------------------ run(): 出错不把错误文字当输出 (1.06.00)

def test_run_failure_gives_empty_stdout():
    rc, out = kc.run(["definitely-not-a-command-xyz"])
    assert rc != 0 and out == ""


def test_run_timeout_gives_empty_stdout():
    rc, out = kc.run(["sleep", "3"], timeout=0.2)
    assert rc != 0 and out == ""


def test_status_fields_dash_when_commands_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(kc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")))
    monkeypatch.setattr(kc, "PROC_UPTIME", str(tmp_path / "none"))
    for n in kc.nics_status(sysfs=str(tmp_path)):
        assert n["ip"] == "-" and n["method"] == "-" and n["link"] is False
    m = kc.misc_status()
    assert m["tailscale_ip"] == "-" and m["uptime"] == "-"


# ------------------------------------------------------------ A3: --write-rescue 入口, 菜单只从一处生成

def test_write_rescue_cli_scrubs_tree(tmp_path):
    (tmp_path / "EFI" / "BOOT").mkdir(parents=True)
    (tmp_path / "EFI" / "BOOT" / "grub.cfg").write_text("set default=0\nlinux /vmlinuz inst.ks=x\n")
    (tmp_path / "grub.cfg-01-02-00-00-00-00-01").write_text("inst.ks=x\n")
    assert kc.main(["--write-rescue", "--root", str(tmp_path),
                    "--state", str(tmp_path / "pxe-mode.json")]) == 0
    cfgs = [p for p in tmp_path.rglob("grub.cfg*")]
    assert [p.name for p in cfgs] == ["grub.cfg"]
    assert "inst.rescue" in cfgs[0].read_text()


def test_main_without_command_fails():
    assert kc.main([]) != 0


def test_menus_come_from_pxeserver_template(tmp_path):
    import pxeserver
    p = tmp_path / "pxe.env"
    p.write_text("PXE_IP=198.51.100.1\n")
    assert pxeserver.grub_menu("198.51.100.1") in kc.grub_cfg_rescue(env_path=p)
    assert pxeserver.grub_menu("198.51.100.1", install=True) in kc.grub_cfg_install(env_path=p)


def test_arm_and_disarm_log_pxe_events(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json")
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "m.json")
    assert len(events) == 2 and MAC in events[0] and "上锁" in events[1]


# ------------------------------------------------------------ A9: 自检连上下文采集也兜住

def test_selftest_run_context_factory_crash_gives_fail_row():
    def ctx():
        raise RuntimeError("nmcli hung")
    rows = kc.selftest_run([{"check": "cpu"}], ctx)
    assert rows == [{"label": "SELFTEST", "state": "FAIL", "detail": "CONTEXT ERROR", "check": "selftest"}]


def test_selftest_run_accepts_context_factory():
    rows = kc.selftest_run([{"check": "cpu"}], _ctx)
    assert rows[0]["label"] == "CPU" and rows[0]["state"] in ("OK", "WARN", "FAIL", "--")


@pytest.mark.parametrize("plan", [None, 42, "cpu", [None, 3, "x"], [{"check": "cpu", "label": 5}]])
def test_selftest_run_never_raises_on_bad_plan(plan):
    rows = kc.selftest_run(plan, _ctx())
    assert rows and all(set(r) == {"label", "state", "detail", "check"} for r in rows)


def test_selftest_context_survives_collector_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    for name in ("nics_status", "misc_status", "services_status", "temps_status", "fans_status"):
        monkeypatch.setattr(kc, name, boom)
    ctx = kc.selftest_context()
    assert ctx["nics"] == [] and ctx["misc"] == {} and ctx["svcs"] == {}
    assert ctx["temps"] == [] and ctx["fans"] == []
    rows = kc.selftest_run(kc.DEFAULT_PLAN, ctx)
    assert len(rows) == len(kc.DEFAULT_PLAN)


# ------------------------------------------------------------ #11: WinPE 只进解锁后的 MAC 菜单

import pxeserver as _px  # noqa: E402

WINPE_MARK = "/win/ipxe.efi"

from test_grub_templates import default_entry  # noqa: E402


def _winpe_files(root, skip=()):
    (root / "win").mkdir(exist_ok=True)
    for n in _px.WINPE_FILES:
        if n not in skip:
            (root / "win" / n).write_bytes(b"x")


def test_arm_default_purpose_is_rocky_without_winpe(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    _winpe_files(tmp_path)                                                 # 文件齐了也不放
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json")
    cfg = (tmp_path / "grub.cfg-01-02-00-00-00-00-01").read_text()
    assert cfg == kc.grub_cfg_install() and WINPE_MARK not in cfg
    assert not (tmp_path / "boot.ipxe").exists()
    assert "rocky" in events[0]


def test_arm_winpe_menu_has_no_ks_and_writes_boot_ipxe(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    _winpe_files(tmp_path)
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json", purpose="winpe")
    cfg = (tmp_path / "grub.cfg-01-02-00-00-00-00-01").read_text()
    assert "inst.ks" not in cfg and "set timeout=15" in cfg
    assert WINPE_MARK in default_entry(cfg) and _px.WINPE_TITLE in cfg
    assert WINPE_MARK not in (tmp_path / "grub.cfg").read_text()          # 救援菜单里永远没有
    assert (tmp_path / "boot.ipxe").read_text() == _px.ipxe_script()
    assert kc.pxe_mode_read(path=tmp_path / "m.json", now=1001)["mode"] == "install"
    assert "winpe" in events[0]


def test_arm_winpe_refused_when_win_dir_incomplete(tmp_path):
    _winpe_files(tmp_path, skip=("bootmgfw.efi",))
    with pytest.raises(ValueError, match="win/bootmgfw.efi"):
        kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json", purpose="winpe")
    assert not (tmp_path / "grub.cfg-01-02-00-00-00-00-01").exists()       # 不退化成 rocky 菜单
    assert not (tmp_path / "m.json").exists() and not (tmp_path / "boot.ipxe").exists()


def test_arm_unknown_purpose_refused(tmp_path):
    with pytest.raises(ValueError):
        kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json", purpose="win11")
    assert not (tmp_path / "m.json").exists()


def test_winpe_missing_defaults_to_tftp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "TFTP_ROOT", str(tmp_path))
    assert kc.winpe_missing() == [f"win/{n}" for n in _px.WINPE_FILES]
    _winpe_files(tmp_path)
    assert kc.winpe_missing() == []


def test_rescue_menu_never_has_winpe_even_with_files(tmp_path):
    _winpe_files(tmp_path)
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "m.json")
    assert WINPE_MARK not in (tmp_path / "grub.cfg").read_text()


def test_disarm_scrubs_winpe_menus_anywhere_in_tree(tmp_path):
    _winpe_files(tmp_path)
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json", purpose="winpe")
    (tmp_path / "EFI" / "BOOT").mkdir(parents=True)
    winpe = "set default=0\nmenuentry 'WinPE' {\n    chainloader (http,192.0.2.1)/win/ipxe.efi\n}\n"
    (tmp_path / "EFI" / "BOOT" / "grub.cfg").write_text(winpe)             # 手写进共享位置的 WinPE 菜单
    (tmp_path / "grub.cfg-C000020A").write_text(winpe)
    (tmp_path / "grub.cfg.bak-20261030").write_text(winpe)                 # 手工备份: 不动
    kc.pxe_disarm(root=tmp_path, path=tmp_path / "m.json")
    menus = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("grub.cfg*")
             if not p.name.startswith("grub.cfg.")}
    assert menus == {"grub.cfg"}
    assert (tmp_path / "grub.cfg.bak-20261030").exists()
    assert (tmp_path / "boot.ipxe").exists() and (tmp_path / "win" / "boot.wim").exists()


# ---------------------------------------------------------------- #18 克隆 (Clonezilla)

CMAC2, CMAC3 = "02:00:00:00:00:02", "02:00:00:00:00:03"
DISK1 = {"name": "nvme0n1", "model": "EXAMPLE NVME 512GB", "serial": "SN-EXAMPLE-1", "size": 512110190592,
         "type": "disk", "tran": "nvme"}
CRULE = {"model": "EXAMPLE NVME 512GB"}


def _clone_env(tmp_path, files=True, image=True, machines=None):
    root = tmp_path / "tftp"
    root.mkdir(parents=True)
    if files:
        (root / "clonezilla").mkdir()
        for n in _px.CLONE_FILES:
            (root / "clonezilla" / n).write_bytes(b"x")
    repo = tmp_path / "repo"
    (repo / "lab-img").mkdir(parents=True) if image else repo.mkdir()
    conf = tmp_path / "clone.json"
    conf.write_text(json.dumps({"proto": "smb", "user": "clonesave", "password": "Example-pass1",
                                "save_share": "clone-rw", "restore_share": "clone-ro", "local_dir": str(repo)}))
    batch = tmp_path / "clone-batch.json"
    batch.write_text(json.dumps({"image": "lab-img", "rule": CRULE, "machines": machines or [
        {"mac": MAC, "disks": [DISK1]}, {"mac": CMAC2, "disks": [dict(DISK1, serial="SN-EXAMPLE-2")]}]}))
    return {"root": root, "path": tmp_path / "m.json", "conf_path": conf, "batch_path": batch}


def _cplan(env, purpose="clone-restore", mac=None):
    return kc.clone_plan(purpose, mac=mac, root=env["root"], conf_path=env["conf_path"],
                         batch_path=env["batch_path"])


def _carm(env, purpose="clone-restore", mac=None, confirm="auto", now=1000):
    if confirm == "auto":
        confirm = _cplan(env, purpose, mac)["digest"]
    kc.pxe_arm(mac, now=now, purpose=purpose, confirm=confirm, **env)


def _menu(env, mac):
    p = env["root"] / kc.mac_cfg_name(mac)
    return p.read_text() if p.exists() else None


def test_clone_purposes_are_arm_purposes():
    assert {"clone-save", "clone-restore"} <= set(kc.ARM_PURPOSES)


def test_arm_clone_restore_writes_each_batch_mac_menu(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(kc, "_pxe_event", events.append)
    env = _clone_env(tmp_path)
    _carm(env)
    for mac, serial in ((MAC, "SN-EXAMPLE-1"), (CMAC2, "SN-EXAMPLE-2")):
        cfg = _menu(env, mac)
        assert f"restoredisk lab-img SERIALNO={serial}" in default_entry(cfg)
        assert "inst.ks" not in cfg and "smb://192.0.2.1/clone-ro/" in cfg
        assert "clonesave" not in cfg                                     # 恢复不带可写账号
    rescue = (env["root"] / "grub.cfg").read_text()
    assert "ocs_live_run" not in rescue and "clonezilla" not in rescue
    st = kc.pxe_mode_read(path=env["path"], now=1001)
    assert st["mode"] == "install" and st["macs"] == [MAC, CMAC2]
    assert "clone-restore" in events[0] and "2 台" in events[0]


def test_arm_clone_save_only_the_chosen_mac(tmp_path):
    env = _clone_env(tmp_path, image=False)
    _carm(env, "clone-save", mac=CMAC2)
    cfg = _menu(env, CMAC2)
    assert "savedisk lab-img SERIALNO=SN-EXAMPLE-2" in default_entry(cfg)
    assert "smb://clonesave:Example-pass1@192.0.2.1/clone-rw/" in cfg
    assert _menu(env, MAC) is None
    assert kc.pxe_mode_read(path=env["path"], now=1001) == {"mode": "install", "mac": CMAC2, "since": 1000,
                                                            "until": 1000 + 1800}


def _nothing_written(env):
    menus = [p.name for p in env["root"].glob("grub.cfg-01-*")]
    return menus == [] and not env["path"].exists()


def test_arm_clone_refused_when_files_missing(tmp_path):
    env = _clone_env(tmp_path, files=False)
    with pytest.raises(ValueError, match="clonezilla/vmlinuz"):
        _carm(env, confirm="whatever")
    assert _nothing_written(env)


def test_arm_clone_refused_without_matching_confirmation(tmp_path):
    """车机上确认的清单和真正写的不一样 (中途有人改了清单) -> 不写."""
    env = _clone_env(tmp_path)
    old = _cplan(env)["digest"]
    batch = json.loads(env["batch_path"].read_text())
    batch["machines"].append({"mac": CMAC3, "disks": [dict(DISK1, serial="SN-EXAMPLE-3")]})
    env["batch_path"].write_text(json.dumps(batch))
    for confirm in (old, None, ""):
        with pytest.raises(ValueError, match="确认"):
            _carm(env, confirm=confirm)
    assert _nothing_written(env)


def test_arm_clone_restore_match_failure_gets_no_menu(tmp_path):
    env = _clone_env(tmp_path, machines=[{"mac": MAC, "disks": [DISK1]},
                                         {"mac": CMAC2, "disks": [dict(DISK1, serial="SN-EXAMPLE-2"),
                                                                  dict(DISK1, serial="SN-EXAMPLE-3")]}])
    _carm(env)
    assert "SERIALNO=SN-EXAMPLE-1" in _menu(env, MAC)
    assert _menu(env, CMAC2) is None                                      # 两块一样的盘: 不猜
    assert kc.pxe_mode_read(path=env["path"], now=1001)["mac"] == MAC


def test_arm_clone_restore_nobody_matches_refused(tmp_path):
    env = _clone_env(tmp_path, machines=[{"mac": MAC, "disks": []}])
    with pytest.raises(ValueError):
        _carm(env, confirm="x")
    assert _nothing_written(env)


def test_arm_clone_restore_refused_when_image_not_in_repo(tmp_path):
    env = _clone_env(tmp_path, image=False)
    with pytest.raises(ValueError):
        _carm(env, confirm="x")
    assert _nothing_written(env)


def test_arm_clone_save_refused_when_image_exists(tmp_path):
    env = _clone_env(tmp_path, image=True)
    with pytest.raises(ValueError, match="已"):
        _carm(env, "clone-save", mac=MAC, confirm="x")
    assert _nothing_written(env)


def test_clone_plan_missing_config_files(tmp_path):
    env = _clone_env(tmp_path)
    env["conf_path"].unlink()
    assert "clone.json" in _cplan(env)["error"]
    env = _clone_env(tmp_path / "b")
    env["batch_path"].write_text("{oops")
    assert "clone-batch.json" in _cplan(env)["error"]


def test_disarm_scrubs_clone_menus_and_leaves_live_files(tmp_path):
    env = _clone_env(tmp_path)
    _carm(env)
    (env["root"] / "EFI" / "BOOT").mkdir(parents=True)
    (env["root"] / "EFI" / "BOOT" / "grub.cfg").write_text(_menu(env, MAC))  # 手抄进共享位置的
    kc.pxe_disarm(root=env["root"], path=env["path"])
    menus = {str(p.relative_to(env["root"])) for p in env["root"].rglob("grub.cfg*")}
    assert menus == {"grub.cfg"} and not env["path"].exists()
    assert (env["root"] / "clonezilla" / "filesystem.squashfs").exists()


def test_retire_removes_only_given_mac_menus(tmp_path):
    env = _clone_env(tmp_path)
    _carm(env)
    kc.pxe_retire([MAC], root=env["root"])
    assert _menu(env, MAC) is None and _menu(env, CMAC2) is not None


def test_arm_rocky_and_winpe_unaffected_by_clone_kwargs(tmp_path):
    """老调用方式 (不带 confirm) 照旧."""
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=tmp_path / "m.json")
    assert "inst.ks" in (tmp_path / "grub.cfg-01-02-00-00-00-00-01").read_text()
