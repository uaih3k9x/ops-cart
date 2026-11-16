"""cart_accept 的 #38 接线: 假 sh 喂 firewall-cmd / nmcli 输出, 按 uuid 读配置 (复用 netmode_core), 判定走 accept_core."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def ca(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(ROOT, "scripts"))
    spec = importlib.util.spec_from_file_location("cart_accept_t38", os.path.join(ROOT, "scripts", "cart_accept.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OPT", ROOT)             # netmode_core 从仓库根导
    yield mod
    sys.modules.pop("cart_accept_t38", None)


DUMPS = {
    "u-static": "connection.id:cart-eno1-static\nconnection.uuid:u-static\nconnection.interface-name:eno1\nconnection.zone:pxe\n",
    "u-old": "connection.id:Wired connection 1\nconnection.uuid:u-old\nconnection.interface-name:eno1\nconnection.zone:\n",
    "u-lan2": "connection.id:cart-enp3s0-dhcp\nconnection.uuid:u-lan2\nconnection.interface-name:enp3s0\nconnection.zone:\n",
}


def _fake_sh(uuids, link="unavailable", zone=""):
    def sh(cmd, timeout=10, inp=None):
        if cmd[:2] == ["firewall-cmd", "--state"]:
            return 0, "running\n", ""
        if cmd[0] == "firewall-cmd" and cmd[1].startswith("--get-zone-of-interface"):
            return (0, zone + "\n", "") if zone else (2, "", "no zone\n")            # 真 firewall-cmd 把 no zone 打到 stderr
        if cmd[0] == "firewall-cmd":
            return 0, "", ""
        if cmd == ["nmcli", "-t", "-f", "DEVICE,STATE", "device"]:
            return 0, f"enp3s0:connected\neno1:{link}\n", ""
        if cmd == ["nmcli", "-t", "-f", "UUID,TYPE", "con", "show"]:
            return 0, "".join(f"{u}:802-3-ethernet\n" for u in uuids) + "w-1:802-11-wireless\n", ""
        if cmd[:5] == ["nmcli", "-t", "con", "show", "uuid"]:
            return 0, DUMPS[cmd[5]], ""
        raise AssertionError(f"没想到的命令 {cmd}")
    return sh


def _run38(ca):
    chk = next(c for c in ca.config_checks() if c.issue == "#38")
    return chk.fn()


def test_no_link_profile_pxe_is_skip(ca, monkeypatch):
    monkeypatch.setattr(ca, "sh", _fake_sh(["u-static", "u-lan2"]))
    st, why = _run38(ca)
    assert st == ca.ac.SKIP and "插线后生效" in why


def test_no_link_other_profile_on_eno1_fails_with_name(ca, monkeypatch):
    monkeypatch.setattr(ca, "sh", _fake_sh(["u-static", "u-old", "u-lan2"]))
    st, why = _run38(ca)
    assert st == ca.ac.FAIL and "Wired connection 1" in why and "cart-enp3s0-dhcp" not in why


def test_link_up_does_not_read_profiles(ca, monkeypatch):
    monkeypatch.setattr(ca, "sh", _fake_sh([], link="connected", zone="pxe"))
    st, _why = _run38(ca)
    assert st == ca.ac.FAIL                              # 假 firewall-cmd 没列端口: 照原来的判法
