"""部署件守规矩: LAN1 铁律、装机网段只来自 pxe.env、网页口单元的加固项."""

import pathlib

import pytest

UNITS = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd"


def unit(name):
    return (UNITS / name).read_text()


def test_pxe_server_pinned_to_lan1():
    u = unit("pxe-server.service")
    assert "--iface eno1" in u
    assert "EnvironmentFile=/etc/ops-cart/pxe.env" in u
    assert "--ip ${PXE_IP} --prefix ${PXE_PREFIX} --pool ${PXE_POOL}" in u


@pytest.mark.parametrize("name", ["pxe-http-boot.service", "pxe-http-repo.service"])
def test_pxe_http_binds_install_address_only(name):
    u = unit(name)
    assert "EnvironmentFile=/etc/ops-cart/pxe.env" in u and "--bind ${PXE_IP}" in u


def test_env_example_parses_and_is_consistent():
    import ipaddress
    import kiosk_core as kc
    env = kc.pxe_env_read(UNITS.parent / "pxe.env.example")
    net = ipaddress.ip_interface(f"{env['PXE_IP']}/{env['PXE_PREFIX']}").network
    lo, hi = (ipaddress.ip_address(x) for x in env["PXE_POOL"].split(","))
    assert lo in net and hi in net and not lo <= ipaddress.ip_address(env["PXE_IP"]) <= hi


def test_no_unit_hardcodes_ipv4():
    import re
    for p in UNITS.iterdir():
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", p.read_text()), p.name


def test_webkiosk_unit_hardened_and_independent():
    u = unit("webkiosk.service")
    for k in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/opt/pxe",
              "Restart=on-failure", "ExecStart=/usr/bin/python3 /opt/pxe/webkiosk.py"):
        assert k in u, k
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "kiosk.service" not in body                    # 车机界面崩了网页口照样在


def test_campus_auth_units_renamed():
    assert (UNITS / "campus-auth.service").exists() and (UNITS / "campus-auth.timer").exists()
    assert "/etc/ops-cart/campus-auth.json" in unit("campus-auth.service")
