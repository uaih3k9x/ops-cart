"""srv_data_core: 数据盘 /srv/data + 装机库 ISO 只读挂载的单元生成 (#11). 纯函数 + 一个写单元目录的 CLI."""

import pathlib
import re
import subprocess
import sys

import pytest

import srv_data_core as sd

ROOT = pathlib.Path(__file__).resolve().parent.parent
UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


# ---------------------------------------------------------------- systemd-escape --path 规则

@pytest.mark.parametrize("path,unit", [
    ("/", "-"),
    ("/srv/data", "srv-data"),
    ("/srv/data/", "srv-data"),
    ("//srv//data", "srv-data"),
    ("/srv/data/win/win10", "srv-data-win-win10"),
    ("/srv/data/win/win11-24h2", r"srv-data-win-win11\x2d24h2"),
    ("/srv/data/win/a b", r"srv-data-win-a\x20b"),
    ("/srv/data/win/.x", "srv-data-win-.x"),       # 只有整串开头的 . 才转义
    ("/.x", r"\x2ex"),
    ("/a\\b", r"a\x5cb"),
    ("/srv/data/win/a.b_c:d", "srv-data-win-a.b_c:d"),
    ("/srv/data/win/中", r"srv-data-win-\xe4\xb8\xad"),
])
def test_escape_path_matches_systemd(path, unit):
    assert sd.escape_path(path) == unit


def test_mount_unit_name():
    assert sd.mount_unit_name("/srv/data") == "srv-data.mount"
    assert sd.mount_unit_name("/srv/data/win/win11-24h2") == r"srv-data-win-win11\x2d24h2.mount"


# ---------------------------------------------------------------- UUID

def test_uuid_normalized_lowercase():
    assert sd.check_uuid(UUID.upper()) == UUID
    assert sd.check_uuid("  " + UUID + "\n") == UUID


@pytest.mark.parametrize("bad", ["", "abc", "/dev/sdb1", UUID + "0", UUID.replace("-", ""),
                                 "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1fg", "LABEL=data", None])
def test_uuid_rejects_non_uuid(bad):
    with pytest.raises(ValueError):
        sd.check_uuid(bad)


# ---------------------------------------------------------------- 数据盘单元

def test_data_mount_unit_by_uuid_xfs_nofail():
    u = sd.data_mount_unit(UUID)
    assert f"What=/dev/disk/by-uuid/{UUID}" in u
    assert "Where=/srv/data" in u and "Type=xfs" in u
    opts = re.search(r"^Options=(.*)$", u, re.M).group(1).split(",")
    assert "nofail" in opts                    # 没装数据盘: 不挡开机, local-fs.target 不等它
    assert "WantedBy=multi-user.target" in u
    assert "RequiredBy" not in u and "local-fs.target" not in u


def test_data_mount_unit_no_capacity_hardcoded():
    """换盘只改 UUID: 单元里不写容量/型号."""
    u = sd.data_mount_unit(UUID)
    assert not re.search(r"\d+\s*[GT]B?\b", u)


def test_data_mount_unit_rejects_bad_uuid():
    with pytest.raises(ValueError):
        sd.data_mount_unit("/dev/sdb1")


# ---------------------------------------------------------------- ISO 名字

@pytest.mark.parametrize("name", ["win10.iso", "win11.iso", "Win11_24H2.iso", "win11-24h2.iso", "a.b.iso"])
def test_iso_name_ok(name):
    assert sd.iso_name_error(name) is None


@pytest.mark.parametrize("name", ["", ".iso", "a b.iso", "中文.iso", "win10.img", "x/y.iso", ".hidden.iso",
                                  "a$b.iso", "-x.iso", "x" * 61 + ".iso"])
def test_iso_name_rejected(name):
    assert sd.iso_name_error(name)


# ---------------------------------------------------------------- ISO 挂载单元

def test_iso_unit_read_only_loop_after_data_disk():
    name, text = sd.iso_unit("win11-24h2.iso")
    assert name == r"srv-data-win-win11\x2d24h2.mount"
    assert "What=/srv/data/iso/win11-24h2.iso" in text
    assert "Where=/srv/data/win/win11-24h2" in text
    assert "Type=udf" in text
    opts = re.search(r"^Options=(.*)$", text, re.M).group(1).split(",")
    assert "ro" in opts and "loop" in opts and "nofail" in opts
    assert "context=system_u:object_r:samba_share_t:s0" in opts      # SELinux enforcing 下 smbd 读得到
    assert "RequiresMountsFor=/srv/data" in text
    assert "ConditionPathExists=/srv/data/iso/win11-24h2.iso" in text
    assert text.startswith(sd.ISO_MARK)       # 生成的才带记号, 清理时只删带记号的


def test_iso_unit_rejects_bad_name():
    with pytest.raises(ValueError):
        sd.iso_unit("a b.iso")


def test_iso_plan_writes_good_skips_bad_removes_stale():
    existing = {r"srv-data-win-old.mount": sd.ISO_MARK + "\n...",
                "srv-data-win-win10.mount": sd.ISO_MARK + "\nold text",
                "srv-data-win-handmade.mount": "# 人手写的\n",       # 没记号: 不归我们管
                "srv-data.mount": "# 数据盘本身\n"}
    p = sd.iso_plan(["win10.iso", "win11.iso", "a b.iso", "readme.txt"], existing)
    assert sorted(p["write"]) == ["srv-data-win-win10.mount", "srv-data-win-win11.mount"]
    assert p["remove"] == ["srv-data-win-old.mount"]
    assert [n for n, _ in p["skipped"]] == ["a b.iso"]          # 不是 .iso 的文件不提


def test_iso_plan_empty_dir_removes_all_generated():
    existing = {"srv-data-win-win10.mount": sd.ISO_MARK + "\n"}
    p = sd.iso_plan([], existing)
    assert p["write"] == {} and p["remove"] == ["srv-data-win-win10.mount"]


# ---------------------------------------------------------------- 目录布局 (deploy/srv-data)

def test_layout_tmpfiles_matches_core():
    conf = (ROOT / "deploy" / "srv-data" / "srv-data.tmpfiles.conf").read_text()
    lines = [l.split() for l in conf.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    paths = {l[1]: l for l in lines}
    for d in sd.LAYOUT.values():
        assert d in paths, d
    assert paths[sd.LAYOUT["drivers"]][2:5] == ["2775", "root", sd.SMB_GROUP]      # 驱动总裁: 可写
    assert paths[sd.LAYOUT["clone"]][2:5] == ["2775", "root", sd.SMB_GROUP]
    for k in ("iso", "win", "tools", "org"):
        assert paths[sd.LAYOUT[k]][2:5] == ["0755", "root", "root"], k


def test_layout_org_holds_unit_data():
    """单位的数据 (克隆镜像) 单独一个顶层目录, 交接时整块拷走/清掉."""
    assert sd.LAYOUT["clone"].startswith(sd.LAYOUT["org"] + "/")
    assert sd.LAYOUT["org"].count("/") == 3            # /srv/data/<顶层>


def test_layout_service_only_after_mount():
    u = (ROOT / "deploy" / "srv-data" / "srv-data-layout.service").read_text()
    assert "Requires=srv-data.mount" in u and "After=srv-data.mount" in u
    assert "ConditionPathIsMountPoint=/srv/data" in u      # 没挂上就不在根分区上建目录
    assert "WantedBy=srv-data.mount" in u


def test_clone_example_local_dir_is_layout_clone():
    import json
    conf = json.loads((ROOT / "examples" / "clone.example.json").read_text())
    assert conf["local_dir"] == sd.LAYOUT["clone"]


def test_scripts_never_format():
    """格式化/分区只写在文档里让人自己确认执行."""
    for name in ("setup-srv-data.sh", "setup-samba.sh"):
        body = "\n".join(l for l in (ROOT / "scripts" / name).read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        for bad in ("mkfs", "parted", "fdisk", "wipefs", "sgdisk", "dd if="):
            assert bad not in body, (name, bad)


@pytest.mark.parametrize("name", ["setup-srv-data.sh", "setup-samba.sh"])
def test_scripts_bash_syntax(name):
    r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- CLI

def _cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "srv_data_core.py"), *args],
                          capture_output=True, text=True)


def test_cli_data_unit():
    r = _cli("data-unit", UUID)
    assert r.returncode == 0 and f"What=/dev/disk/by-uuid/{UUID}" in r.stdout


def test_cli_data_unit_bad_uuid_exit_2():
    r = _cli("data-unit", "/dev/sdb1")
    assert r.returncode == 2 and "UUID" in r.stderr


def test_cli_iso_units_sync(tmp_path):
    iso, units = tmp_path / "iso", tmp_path / "units"
    iso.mkdir()
    units.mkdir()
    (iso / "win10.iso").write_bytes(b"x")
    (iso / "bad name.iso").write_bytes(b"x")
    (units / "srv-data-win-gone.mount").write_text(sd.ISO_MARK + "\n")
    (units / "keep.mount").write_text("# hand\n")
    r = _cli("iso-units", "--iso-dir", str(iso), "--unit-dir", str(units))
    assert r.returncode == 0, r.stderr
    assert (units / "srv-data-win-win10.mount").read_text().startswith(sd.ISO_MARK)
    assert not (units / "srv-data-win-gone.mount").exists() and (units / "keep.mount").exists()
    assert "write srv-data-win-win10.mount" in r.stdout and "remove srv-data-win-gone.mount" in r.stdout
    assert "bad name.iso" in r.stderr


def test_cli_iso_units_missing_iso_dir_removes_generated(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "srv-data-win-win10.mount").write_text(sd.ISO_MARK + "\n")
    r = _cli("iso-units", "--iso-dir", str(tmp_path / "nope"), "--unit-dir", str(units))
    assert r.returncode == 0 and not (units / "srv-data-win-win10.mount").exists()
