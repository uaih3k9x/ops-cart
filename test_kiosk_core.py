"""kiosk_core 测试: 纯函数与配置读写, 不碰真机服务."""

import json

import pytest

import kiosk_core as kc


def test_default_conf_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "PXE_CONF", str(tmp_path / "pxe.conf"))
    cfg = kc.pxe_conf_read()          # 不存在 -> 默认
    assert cfg == kc.DEFAULT_PXE_CONF
    cfg["pool_end"] = "192.168.77.99"
    kc.pxe_conf_write(cfg)
    assert kc.pxe_conf_read()["pool_end"] == "192.168.77.99"


def test_conf_read_merges_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "PXE_CONF", str(tmp_path / "pxe.conf"))
    (tmp_path / "pxe.conf").write_text(json.dumps({"ip": "10.1.1.1/24"}))
    cfg = kc.pxe_conf_read()
    assert cfg["ip"] == "10.1.1.1/24"
    assert cfg["pool_start"] == kc.DEFAULT_PXE_CONF["pool_start"]


def test_render_pxe_service():
    cfg = {"ip": "172.16.0.1", "pool_start": "172.16.0.50", "pool_end": "172.16.0.60"}
    line = kc.render_pxe_service(cfg)
    assert line.startswith("/usr/bin/python3 /opt/pxe/pxeserver.py serve")
    assert "--ip 172.16.0.1" in line
    assert "--pool 172.16.0.50,172.16.0.60" in line
    assert "--tftp-root /opt/pxe/tftp" in line


def test_apply_pxe_conf_validates_cidr(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "PXE_CONF", str(tmp_path / "pxe.conf"))
    ok, msg = kc.apply_pxe_conf({"ip": "192.168.77.1", "pool_start": "a", "pool_end": "b"})
    assert not ok and "CIDR" in msg


def test_apply_pxe_conf_writes_conf_and_unit(tmp_path, monkeypatch):
    conf_path = tmp_path / "pxe.conf"
    unit_path = tmp_path / "pxe-server.service"
    monkeypatch.setattr(kc, "PXE_CONF", str(conf_path))
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (0, ""))
    cfg = {"ip": "192.168.78.1/24", "prefix": 24,
           "pool_start": "192.168.78.10", "pool_end": "192.168.78.20"}
    ok, msg = kc.apply_pxe_conf(cfg, unit_path=str(unit_path))
    assert ok
    assert json.loads(conf_path.read_text())["ip"] == "192.168.78.1/24"
    assert "ExecStart=" in unit_path.read_text()


def test_nmcli_set_static_builds_correct_cmd():
    cmds = kc.nmcli_set_static("Lan1-pxe", "192.168.9.5/24", "192.168.9.254", "116.57.62.41")
    assert cmds[0][:6] == ["nmcli", "con", "mod", "Lan1-pxe", "ipv4.method", "manual"]
    assert "192.168.9.5/24" in cmds[0]
    assert cmds[0][-1] == "116.57.62.41"      # dns
    assert cmds[0][cmds[0].index("ipv4.gateway") + 1] == "192.168.9.254"
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


def test_services_status_shape(monkeypatch):
    monkeypatch.setattr(kc, "run", lambda cmd, timeout=5: (0, "enabled" if cmd[1] == "is-enabled" else "active"))
    st = kc.services_status(("pxe-server",))
    assert st["pxe-server"] == (True, True)


def test_nics_status_marks_no_carrier(monkeypatch):
    def fake_run(cmd, timeout=5):
        if cmd[:2] == ["ip", "-4"]:
            return 0, "eno1            UP      192.168.77.1/24"
        if cmd[0] == "nmcli":
            return 0, "manual"
        return 1, ""   # carrier 文件读不到 = 没插线
    monkeypatch.setattr(kc, "run", fake_run)
    nics = kc.nics_status()
    assert nics[0]["ip"] == "192.168.77.1/24"
    assert nics[0]["link"] is False
    assert nics[0]["method"] == "manual"


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
    p.write_text("\n445C 机房 → 左转\n\n" + "\n".join(f"l{i}" for i in range(30)) + "\n")
    text = kc.notice_read(str(p), max_lines=5)
    assert text.splitlines()[0] == "445C 机房 → 左转"
    assert len(text.splitlines()) == 5


def test_notice_read_limits_chars(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("长" * 1000)
    assert len(kc.notice_read(str(p), max_chars=300)) == 300


# ---------------------------------------------------------------- 开机自检 / 品牌

NICS_OK = [{"name": "eno1", "role": "PXE 装机口", "ip": "-", "link": False},
           {"name": "enp3s0", "role": "管理口", "ip": "192.168.9.228/24", "link": True}]
MISC_OK = {"tailscale_ip": "100.64.0.10", "ch9329": False, "uptime": "up 1 min"}


def _rows(**kw):
    args = dict(nics=NICS_OK, misc=MISC_OK, svcs={"pxe-server": (True, False)},
                temps=[("CPU", 45.0)], fans=[("fan2", 2689), ("fan3", 2500)], serial_ok=True)
    args.update(kw)
    return {r["label"]: r for r in kc.selftest_rows(**args)}


def test_selftest_all_good():
    rows = _rows()
    assert rows["LAN2"]["state"] == "OK" and rows["LAN2"]["detail"] == "192.168.9.228"
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
    nics = [dict(NICS_OK[0], link=True, ip="192.168.77.1/24"), NICS_OK[1]]
    rows = _rows(nics=nics, svcs={"pxe-server": (True, True)}, temps=[], fans=[])
    assert rows["LAN1 / PXE"]["state"] == "OK" and "RUNNING" in rows["LAN1 / PXE"]["detail"]
    assert rows["CPU"]["state"] == "--" and rows["FAN"]["state"] == "--"


def test_selftest_line_format():
    line = kc.selftest_line({"label": "LAN2", "state": "OK", "detail": "1.2.3.4"}, width=20)
    assert line.startswith("LAN2 ") and ". OK" in line and line.endswith("1.2.3.4")


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
