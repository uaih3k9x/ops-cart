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


def test_cart_notify_units_read_only_oneshot_timer():
    s, t = unit("cart-notify.service"), unit("cart-notify.timer")
    assert "Type=oneshot" in s and "ExecStart=/usr/bin/python3 /opt/pxe/notify_core.py" in s
    assert "NoNewPrivileges=yes" in s
    body = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
    assert "cart-dash.service" not in body                 # 只读状态文件, 不依赖也不拉起 cart-dash
    assert "StateDirectory" not in body                    # /var/lib/ops-cart 的权限做梦在用, 别改
    assert "OnUnitActiveSec=30" in t and "WantedBy=timers.target" in t


def test_cart_mcp_unit_http_without_bind_override():
    u = unit("cart-mcp.service")
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ExecStart=/usr/bin/python3 /opt/pxe/cart_mcp.py http" in body
    assert "--bind" not in body and "kiosk.service" not in body     # 监听地址由程序自己筛 (本机 + tailnet)


def test_mcp_example_conf_token_is_not_usable():
    import json
    import mcp_core as mc
    conf = json.loads((UNITS.parent.parent / "examples" / "mcp.example.json").read_text())
    assert conf["port"] == 8765 and conf["tokens"]
    for tok in conf["tokens"].values():                 # 照抄样例上线也进不来
        assert mc.token_who(f"Bearer {tok}", conf["tokens"]) is None
