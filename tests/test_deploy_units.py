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


def test_kvm_broker_unit_socket_and_hardening():
    """#9 键鼠代理: socket 路径和代码默认一致, 日志进 /var/log/ops-cart (logrotate 已覆盖), 去掉全部能力."""
    import kvm_broker
    u = unit("kvm-broker.service")
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ExecStart=/usr/bin/python3 /opt/pxe/kvm_broker.py" in body
    assert kvm_broker.SOCK == "/run/ops-kvm/kvm.sock" and "RuntimeDirectory=ops-kvm" in body
    assert kvm_broker.LOG.startswith("/var/log/ops-cart/") and "ReadWritePaths=/var/log/ops-cart" in body
    for k in ("NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet=\n",
              "SupplementaryGroups=dialout", "RestrictAddressFamilies=AF_UNIX\n", "IPAddressDeny=any",
              "Restart=on-failure"):
        assert k in body + "\n", k
    assert "kiosk.service" not in body and "webkiosk.service" not in body     # 谁都不依赖谁, 代理不在客户端直开
    rot = (UNITS.parent / "logrotate.d" / "ops-cart").read_text()
    assert "/var/log/ops-cart/*.log" in rot and "copytruncate" in rot


def test_serial_req_units_only_stop_direction_from_web():
    """#23 网页急停 / 拒绝: ops-web 只能往 serial-req 目录投请求, root 的 oneshot 执行 (只认停的方向)."""
    import mcp_serial
    p, s = unit("cart-serial-req.path"), unit("cart-serial-req.service")
    assert f"DirectoryNotEmpty={mcp_serial.REQ_DIR}" in p and "WantedBy=multi-user.target" in p
    body = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
    assert "Type=oneshot" in body and "ExecStart=/usr/bin/python3 /opt/pxe/mcp_serial.py requests" in body
    assert "User=" not in body                              # root: 要改 0600 的状态文件
    tmp = (UNITS.parent / "tmpfiles.d" / "ops-cart.conf").read_text()
    assert f"d {mcp_serial.REQ_DIR} 0700 ops-web ops-web -" in tmp
    web = unit("webkiosk.service")
    assert f"ReadWritePaths=-{mcp_serial.REQ_DIR}" in web


def test_revtunnel_unit_off_by_default_and_pinned():
    """tailnet 被墙时的备用通道: 推车主动 ssh -R 到跳板机. 没配置文件就不起; 只在跳板机本地口听; 认死跳板机指纹."""
    u = unit("cart-revtunnel.service")
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ConditionPathExists=/etc/ops-cart/revtunnel.env" in body
    assert "EnvironmentFile=/etc/ops-cart/revtunnel.env" in body
    for k in ("-R localhost:${REMOTE_PORT}:localhost:22", "ExitOnForwardFailure=yes", "BatchMode=yes",
              "StrictHostKeyChecking=yes", "UserKnownHostsFile=/etc/ops-cart/revtunnel_known_hosts",
              "-i /etc/ops-cart/revtunnel_ed25519", "ServerAliveInterval=", "Restart=always",
              "NoNewPrivileges=yes", "ProtectSystem=strict"):
        assert k in body, k
    assert "0.0.0.0" not in body and "GatewayPorts" not in body
    # SELinux: systemd (init_t) 不能直接 exec ssh_exec_t (静默拒, 203/EXEC); 经 /bin/sh 再 exec 才行 (推车实测)
    assert "ExecStart=/bin/sh -c 'exec /usr/bin/ssh " in body


def test_revtunnel_example_env_has_no_real_host():
    """样例只放占位: 文档用的保留地址 / 名字, 不放真跳板机."""
    p = UNITS.parent.parent / "examples" / "revtunnel.example.env"
    kv = dict(l.split("=", 1) for l in p.read_text().splitlines() if l and not l.startswith("#"))
    assert set(kv) == {"JUMP_HOST", "JUMP_PORT", "JUMP_USER", "REMOTE_PORT"}
    assert kv["JUMP_HOST"].endswith(".example") or kv["JUMP_HOST"].startswith("192.0.2.")
    assert 1024 < int(kv["REMOTE_PORT"]) < 65536


def test_pxe_firewall_zone_in_repo_opens_what_pxe_needs():
    """#38: pxe 区以前只在车上手工建过."""
    xml = (UNITS.parent / "firewalld" / "pxe.xml").read_text()
    for port, proto in ((67, "udp"), (68, "udp"), (69, "udp"), (4011, "udp"), (80, "tcp"), (8000, "tcp")):
        assert f'port="{port}" protocol="{proto}"' in xml


def test_cart_mcp_readonly_unit():
    """#49: 只读档 HTTP 单独一个单元, 加固同 cart-mcp, 默认不开 (验收表里按需, 见 test_accept_core)."""
    u = unit("cart-mcp-ro.service")
    body = "\n".join(l for l in u.splitlines() if not l.lstrip().startswith("#"))
    assert "ExecStart=/usr/bin/python3 /opt/pxe/cart_mcp.py http --profile readonly" in body
    for k in ("UMask=0077", "PrivateTmp=yes", "Restart=on-failure", "ProtectKernelTunables=yes",
              "WantedBy=multi-user.target"):
        assert k in body, k
    for k in ("ProtectSystem=strict", "ReadWritePaths=/run/ops-cart /var/log/ops-cart -/run/sudo",
              "ProtectHome=read-only"):                       # #60 第三轮: 文件系统只读, 只放开这三处
        assert k in body, k
    assert "NoNewPrivileges" not in body                       # 读串口要 sudo -u 跑 conbroker tail (setuid), 不能加
    assert "--bind" not in body and "kiosk.service" not in body


def test_mcp_http_units_cap_tasks():
    """#60: 慢连接一个占一个线程, 单元把线程数封顶."""
    for name in ("cart-mcp.service", "cart-mcp-ro.service"):
        assert "TasksMax=64" in unit(name), name
