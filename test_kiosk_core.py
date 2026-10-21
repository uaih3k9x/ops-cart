"""kiosk_core 测试: 纯函数与配置读写, 不碰真机服务."""

import time
import json

import pytest

import kiosk_core as kc


def test_default_conf_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "PXE_CONF", str(tmp_path / "pxe.conf"))
    cfg = kc.pxe_conf_read()          # 不存在 -> 默认
    assert cfg == kc.DEFAULT_PXE_CONF
    cfg["pool_end"] = "192.168.200.99"
    kc.pxe_conf_write(cfg)
    assert kc.pxe_conf_read()["pool_end"] == "192.168.200.99"


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
    ok, msg = kc.apply_pxe_conf({"ip": "192.168.200.1", "pool_start": "a", "pool_end": "b"})
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
    cmds = kc.nmcli_set_static("Lan1-pxe", "192.168.99.5/24", "192.168.99.254", "203.0.113.41")
    assert cmds[0][:6] == ["nmcli", "con", "mod", "Lan1-pxe", "ipv4.method", "manual"]
    assert "192.168.99.5/24" in cmds[0]
    assert cmds[0][-1] == "203.0.113.41"      # dns
    assert cmds[0][cmds[0].index("ipv4.gateway") + 1] == "192.168.99.254"
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
            return 0, "eno1            UP      192.168.200.1/24"
        if cmd[0] == "nmcli":
            return 0, "manual"
        return 1, ""   # carrier 文件读不到 = 没插线
    monkeypatch.setattr(kc, "run", fake_run)
    nics = kc.nics_status()
    assert nics[0]["ip"] == "192.168.200.1/24"
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
    p.write_text("\nR5C 机房 → 左转\n\n" + "\n".join(f"l{i}" for i in range(30)) + "\n")
    text = kc.notice_read(str(p), max_lines=5)
    assert text.splitlines()[0] == "R5C 机房 → 左转"
    assert len(text.splitlines()) == 5


def test_notice_read_limits_chars(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("长" * 1000)
    assert len(kc.notice_read(str(p), max_chars=300)) == 300


# ---------------------------------------------------------------- 开机自检 / 品牌

NICS_OK = [{"name": "eno1", "role": "PXE 装机口", "ip": "-", "link": False},
           {"name": "enp3s0", "role": "管理口", "ip": "192.168.99.228/24", "link": True}]
MISC_OK = {"tailscale_ip": "100.100.100.10", "ch9329": False, "uptime": "up 1 min"}


def _rows(**kw):
    args = dict(nics=NICS_OK, misc=MISC_OK, svcs={"pxe-server": (True, False)},
                temps=[("CPU", 45.0)], fans=[("fan2", 2689), ("fan3", 2500)], serial_ok=True)
    args.update(kw)
    return {r["label"]: r for r in kc.selftest_rows(**args)}


def test_selftest_all_good():
    rows = _rows()
    assert rows["LAN2"]["state"] == "OK" and rows["LAN2"]["detail"] == "192.168.99.228"
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
    nics = [dict(NICS_OK[0], link=True, ip="192.168.200.1/24"), NICS_OK[1]]
    rows = _rows(nics=nics, svcs={"pxe-server": (True, True)}, temps=[], fans=[])
    assert rows["LAN1 / PXE"]["state"] == "OK" and "RUNNING" in rows["LAN1 / PXE"]["detail"]
    assert rows["CPU"]["state"] == "--" and rows["FAN"]["state"] == "--"


def _ctx(**kw):
    ctx = dict(nics=NICS_OK, misc=MISC_OK, svcs={"pxe-server": (True, False)},
               temps=[("CPU", 45.0)], fans=[("fan2", 2689)],
               exists=lambda p: True, ping=lambda h: True, pci=lambda vid, did: [])
    ctx.update(kw)
    return ctx


def test_selftest_run_follows_plan_order_and_labels():
    plan = [{"check": "cpu"}, {"check": "lan", "label": "MGMT", "iface": "enp3s0"}]
    rows = kc.selftest_run(plan, _ctx())
    assert [(r["label"], r["state"]) for r in rows] == [("CPU", "OK"), ("MGMT", "OK")]


def test_selftest_run_isolates_crashing_check():
    def boom(ctx):
        raise RuntimeError("x")
    rows = kc.selftest_run([{"check": "boom", "label": "BOOM"}, {"check": "cpu"}], _ctx(),
                           checks=dict(kc.CHECKS, boom=boom))
    assert rows[0] == {"label": "BOOM", "state": "FAIL", "detail": "ERROR"}
    assert rows[1]["state"] == "OK"


def test_selftest_run_unknown_check():
    rows = kc.selftest_run([{"check": "nope"}], _ctx())
    assert rows == [{"label": "NOPE", "state": "FAIL", "detail": "UNKNOWN CHECK"}]


def test_check_serial_uses_dev_param():
    seen = []
    ctx = _ctx(exists=lambda p: seen.append(p) or False)
    assert kc.check_serial(ctx, dev="/dev/ttyS1") == ("--", "NOT CONNECTED")
    assert seen == ["/dev/ttyS1"]


def test_check_ping():
    assert kc.check_ping(_ctx(), host="100.100.100.8") == ("OK", "100.100.100.8")
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
    px = kc.notice_font_px("请前往R6上课", 1000, 1000)    # 5 中文 + 3 数字 = 6.8 字宽
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


# ---------------------------------------------------------------- CH9329 串口定位

def test_ch9329_port_prefers_stable_by_id(tmp_path):
    byid = tmp_path / "by-id"
    byid.mkdir()
    (byid / "usb-1a86_USB_Single_Serial_CAPSN019212-if00").touch()
    (byid / "usb-0403_FT232R_XYZ-if00-port0").touch()          # FTDI console 线不算
    got = kc.ch9329_port(str(byid / "usb-1a86_*-if00"), fallback="/dev/ttyACM0")
    assert got == str(byid / "usb-1a86_USB_Single_Serial_CAPSN019212-if00")


def test_ch9329_port_fallback_when_absent(tmp_path):
    assert kc.ch9329_port(str(tmp_path / "none-*"), fallback="/dev/ttyACM0") == "/dev/ttyACM0"


# ------------------------------------------------------------ 装机模式 (2026-10-21)
# 默认只有救援菜单; 推车上输屏幕随机码解锁, 只对选定 MAC 下发装机菜单, 30 分钟或被取走即上锁

MAC = "02:00:00:41:b5:a2"


def test_mac_cfg_name_is_grub_per_mac_file():
    assert kc.mac_cfg_name(MAC) == "grub.cfg-01-02-00-00-41-b5-a2"
    assert kc.mac_cfg_name("02-00-00-41-b5-a2") == "grub.cfg-01-02-00-00-41-b5-a2"
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
    assert "inst.ks" in (tmp_path / "grub.cfg-01-02-00-00-41-b5-a2").read_text()
    assert "inst.ks" not in (tmp_path / "grub.cfg").read_text()        # 别的机器照样只能救援
    st = kc.pxe_mode_read(path=state, now=1001)
    assert st == {"mode": "install", "mac": MAC, "since": 1000, "until": 1000 + 1800}


def test_disarm_and_expiry(tmp_path):
    state = tmp_path / "pxe-mode.json"
    kc.pxe_arm(MAC, now=1000, root=tmp_path, path=state)
    assert kc.pxe_mode_read(path=state, now=1000 + 1801)["mode"] == "expired"
    kc.pxe_disarm(root=tmp_path, path=state)
    assert not (tmp_path / "grub.cfg-01-02-00-00-41-b5-a2").exists()
    assert kc.pxe_mode_read(path=state, now=2000) == {"mode": "rescue"}
    assert "inst.ks" not in (tmp_path / "grub.cfg").read_text()


def test_mode_read_missing_or_garbage_is_rescue(tmp_path):
    assert kc.pxe_mode_read(path=tmp_path / "nope", now=0) == {"mode": "rescue"}
    (tmp_path / "bad").write_text("{oops")
    assert kc.pxe_mode_read(path=tmp_path / "bad", now=0) == {"mode": "rescue"}


LOG = """2026-10-21 15:11:01,729 TFTP RRQ /grub.cfg-01-02-00-00-41-b5-a2 失败: [Errno 2] No such file
2026-10-21 15:19:22,118 DHCP 客户端 02:00:00:41:b5:a2 vendor='PXEClient:Arch:00007' arch=7 src=0.0.0.0:68
2026-10-21 15:19:30,001 DHCP 客户端 aa:bb:cc:dd:ee:ff vendor='PXEClient:Arch:00007' arch=7 src=0.0.0.0:68
2026-10-21 15:20:00,000 TFTP RRQ /grub.cfg-01-02-00-00-41-b5-a2 (900 bytes, blksize=1024) from 192.168.200.10
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
    assert "--wid=4196677" in cmd and cmd[-1] == "/dev/video0"
    assert "--input-cursor=no" in cmd and "--input-vo-keyboard=no" in cmd
    assert "--profile=low-latency" in cmd


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
