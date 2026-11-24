"""ARM64 Windows PE 冒烟测试 (docs/ARM64_WINPE.md): 只验"Windows ARM64 内核能不能在这块板子上起来".

两条路:
  A. GRUB 菜单手选 -> chainloader aa64/winpe/<build>/bootaa64.efi (能不能走通要实机验).
  B. 按 MAC 一次性开关: 这台 ARM64 下一次 PXE 时固件直接拿 Windows 启动管理器 (WDS 的标准做法),
     DHCP 带 option 252 指 BCD; 启动管理器经 TFTP 传完一次就自动关掉, 过期也关.
默认项永远是本地盘; WinPE 项只能手选. 微软的文件不进仓库, 测试里都是假文件."""

import json
import socket

import pytest

import pxeserver as ps
from pxeserver import DhcpServer, TftpServer, encode_opt, parse_dhcp

from test_arm64_pxe import ARM_FILE, ARM_MAC, IP, FakeLog, ProxyGuard, bootfile_of, packet

OTHER_MAC = b"\x02\x00\x00\x00\x0b\x02"
NOW = 1_800_000_000.0


# ---- GRUB 菜单: 两个手动项, 默认还是本地盘

def test_menu_has_two_winpe_entries():
    menu = ps.grub_menu_arm64(IP)
    assert "menuentry 'Windows PE ARM64 23H2 (smoke test)' {\n    chainloader /aa64/winpe/22621/bootaa64.efi\n}" in menu
    assert ("menuentry 'Windows PE ARM64 24H2 (expect fail: no LSE)' {\n"
            "    chainloader /aa64/winpe/26100/bootaa64.efi\n}") in menu
    assert menu.count("menuentry ") == 6


def test_menu_default_still_local_disk():
    menu = ps.grub_menu_arm64(IP)
    assert menu.startswith("set default=0\nset timeout=10\n")
    assert menu.split("menuentry ")[1].startswith(f"'{ps.LOCAL_TITLE}'")
    assert not ps.rescue_default_menu(menu) and not ps.unlock_only_menu(menu)


@pytest.mark.parametrize("idx", [4, 5])
def test_winpe_entries_cannot_be_default(idx):
    """门禁: 默认项指到 WinPE 的菜单 (改坏了的生成器 / 手写的) 一律当危险菜单, 换成默认菜单."""
    menu = ps.grub_menu_arm64(IP).replace("set default=0", f"set default={idx}")
    assert ps.rescue_default_menu(menu)


@pytest.mark.parametrize("text", ["chainloader /aa64/winpe/22621/bootaa64.efi",
                                  "chainloader (http,192.0.2.1)/AA64/WinPE/26100/BOOTAA64.EFI"])
def test_winpe_is_manual_only(text):
    assert ps._manual_only(text)


def test_handwritten_menu_defaulting_to_winpe_replaced(tmp_path):
    """TFTP 兜底: 根下手写的 grub.cfg 默认项是 ARM64 WinPE, 发出去的是默认菜单."""
    (tmp_path / "grub.cfg").write_text("set default=0\nmenuentry 'pe' {\n    chainloader /aa64/winpe/22621/bootaa64.efi\n}\n")
    t = TftpServer(IP, str(tmp_path), FakeLog(), pxe_mode_path=str(tmp_path / "mode.json"))
    f, _, what = t.open_file("grub.cfg", ("192.0.2.10", 1))
    assert f.read() == ps.grub_menu(IP).encode() and "回默认菜单" in what


# ---- 一次性开关的状态文件

@pytest.fixture
def state(tmp_path):
    return str(tmp_path / "arm64-winpe.json")


def test_arm_then_armed(state):
    ps.arm64_winpe_arm(state, "02:00:00:00:0B:01", "22621", minutes=15, now=NOW)
    doc = json.load(open(state))
    assert doc == {"mac": "02:00:00:00:0b:01", "build": "22621", "since": NOW, "until": NOW + 900}
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", NOW + 899) == "22621"


def test_not_armed_for_other_mac_or_after_expiry(state):
    ps.arm64_winpe_arm(state, "02:00:00:00:0b:01", "26100", minutes=15, now=NOW)
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:02", NOW) is None
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", NOW + 901) is None


@pytest.mark.parametrize("content", [None, "", "{", "[]", '{"mac": "x", "build": "22621", "until": 9e12}',
                                     '{"mac": "02:00:00:00:0b:01", "build": "../x", "until": 9e12}',
                                     '{"mac": "02:00:00:00:0b:01", "build": "22621"}'])
def test_bad_state_is_not_armed(state, content):
    if content is not None:
        open(state, "w").write(content)
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", NOW) is None


@pytest.mark.parametrize("build,minutes", [("19041", 15), ("2262", 15), ("22621", 0), ("22621", 61)])
def test_arm_rejects_unknown_build_or_bad_minutes(state, build, minutes):
    with pytest.raises(ValueError):
        ps.arm64_winpe_arm(state, "02:00:00:00:0b:01", build, minutes=minutes, now=NOW)


def test_disarm(state):
    ps.arm64_winpe_arm(state, "02:00:00:00:0b:01", "22621", minutes=15, now=NOW)
    assert ps.arm64_winpe_disarm(state) is True
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", NOW) is None
    assert ps.arm64_winpe_disarm(state) is False


# ---- DHCP: 开关打开时, 只有那台 ARM64 拿 Windows 启动管理器 + option 252

def armed_dhcp(state, build="22621", **kw):
    ps.arm64_winpe_arm(state, ARM_MAC.hex(":"), build, minutes=15, now=NOW)
    d = DhcpServer(IP, "192.0.2.10", "192.0.2.12", "shimx64.efi", "pxelinux.0", FakeLog(), port=1167,
                   arm64_winpe_path=state, now=lambda: NOW, **kw)
    return d


def opt252(reply):
    return parse_dhcp(reply)[2].get(252)


def test_armed_mac_gets_bootmgr_and_bcd_path(state):
    d = armed_dhcp(state)
    offer = d.handle(packet(), None)
    assert bootfile_of(offer) == ("aa64/winpe/22621/bootaa64.efi",) * 2
    assert opt252(offer) == b"\\aa64\\winpe\\22621\\boot\\bcd"
    req = packet(msg_type=3, extra=encode_opt(50, ("192.0.2.10",)) + encode_opt(54, (IP,)))
    ack = d.handle(req, None)
    assert parse_dhcp(ack)[2][53] == b"\x05"
    assert bootfile_of(ack)[0] == "aa64/winpe/22621/bootaa64.efi" and opt252(ack) is not None


def test_armed_26100(state):
    offer = armed_dhcp(state, build="26100").handle(packet(), None)
    assert bootfile_of(offer)[0] == "aa64/winpe/26100/bootaa64.efi"
    assert opt252(offer) == b"\\aa64\\winpe\\26100\\boot\\bcd"


def test_other_machines_unchanged_while_armed(state):
    d = armed_dhcp(state)
    other = d.handle(packet(mac=OTHER_MAC), None)
    assert bootfile_of(other)[0] == ARM_FILE and opt252(other) is None
    x86 = d.handle(packet(arch=7, vendor=b"PXEClient", mac=ARM_MAC), None)     # 同一 MAC 报 x86: 不给 ARM 的文件
    assert bootfile_of(x86)[0] == "shimx64.efi" and opt252(x86) is None


def test_not_armed_unchanged(state):
    d = DhcpServer(IP, "192.0.2.10", "192.0.2.12", "shimx64.efi", "pxelinux.0", FakeLog(), port=1167,
                   arm64_winpe_path=state, now=lambda: NOW)
    reply = d.handle(packet(), None)
    assert bootfile_of(reply)[0] == ARM_FILE and opt252(reply) is None


def test_expired_unchanged(state):
    d = armed_dhcp(state)
    d.now = lambda: NOW + 3600
    assert bootfile_of(d.handle(packet(), None))[0] == ARM_FILE


def test_armed_proxy_and_4011(state):
    d = armed_dhcp(state)
    d.guard = ProxyGuard()
    offer = d.handle(packet(), ("0.0.0.0", 68))
    assert socket.inet_ntoa(offer[16:20]) == "0.0.0.0"
    assert bootfile_of(offer)[0] == "aa64/winpe/22621/bootaa64.efi" and opt252(offer) is not None
    ack = d.handle_proxy(packet(msg_type=3, ciaddr="192.0.2.10"), ("192.0.2.10", 4011))
    assert bootfile_of(ack)[0] == "aa64/winpe/22621/bootaa64.efi" and opt252(ack) is not None


def test_default_state_path_is_under_opt_pxe():
    assert ps.ARM64_WINPE_STATE == "/opt/pxe/arm64-winpe.json"
    d = DhcpServer(IP, "192.0.2.10", "192.0.2.12", "shimx64.efi", "pxelinux.0", FakeLog(), port=1167)
    assert d.arm64_winpe_path == ps.ARM64_WINPE_STATE


# ---- TFTP: 启动管理器传完一次就关掉 (一次性)

class FakeSock:
    """TFTP 会话用的假 socket: 每发一个包就回对应的 ACK."""
    def __init__(self):
        self.sent = []

    def settimeout(self, t): pass
    def close(self): pass

    def sendto(self, pkt, addr):
        self.sent.append(pkt)
        self._last = pkt

    def recvfrom(self, n):
        block = self._last[2:4] if self._last[0:2] == b"\x00\x03" else b"\x00\x00"
        return b"\x00\x04" + block, ("192.0.2.10", 2000)


@pytest.fixture
def winpe_tree(tmp_path):
    d = tmp_path / "aa64" / "winpe" / "22621"
    d.mkdir(parents=True)
    (d / "bootaa64.efi").write_bytes(b"MZ" + b"\0" * 3000)
    (d / "boot").mkdir()
    (d / "boot" / "bcd").write_bytes(b"regf")
    return tmp_path


def rrq(name, **opts):
    return b"\x00\x01" + name.encode() + b"\x00octet\x00" + b"".join(
        k.encode() + b"\x00" + str(v).encode() + b"\x00" for k, v in opts.items())


def tftp_for(root, state, events):
    return TftpServer(IP, str(root), FakeLog(), pxe_mode_path=str(root / "mode.json"),
                      arm64_winpe_path=state, event=events.append)


def test_bootmgr_transfer_disarms(winpe_tree, state):
    ps.arm64_winpe_arm(state, ARM_MAC.hex(":"), "22621", minutes=15, now=NOW)
    events = []
    t = tftp_for(winpe_tree, state, events)
    t._session(FakeSock(), rrq("aa64/winpe/22621/bootaa64.efi", blksize=1428), ("192.0.2.10", 2000))
    assert ps.arm64_winpe_armed(state, ARM_MAC.hex(":"), NOW) is None
    assert any("WinPE" in e for e in events)


def test_backslash_bootmgr_path_also_disarms(winpe_tree, state):
    ps.arm64_winpe_arm(state, ARM_MAC.hex(":"), "22621", minutes=15, now=NOW)
    t = tftp_for(winpe_tree, state, [])
    t._session(FakeSock(), rrq("\\aa64\\winpe\\22621\\bootaa64.efi"), ("192.0.2.10", 2000))
    assert not (winpe_tree / "arm64-winpe.json").exists()


def test_other_files_do_not_disarm(winpe_tree, state):
    ps.arm64_winpe_arm(state, ARM_MAC.hex(":"), "22621", minutes=15, now=NOW)
    t = tftp_for(winpe_tree, state, [])
    t._session(FakeSock(), rrq("\\aa64\\winpe\\22621\\boot\\bcd"), ("192.0.2.10", 2000))
    assert ps.arm64_winpe_armed(state, ARM_MAC.hex(":"), NOW) == "22621"


def test_tsize_probe_does_not_disarm(winpe_tree, state):
    """固件先发带 tsize 的 RRQ 问大小, 收到 OACK 就报错断开; 这次不算传完."""
    ps.arm64_winpe_arm(state, ARM_MAC.hex(":"), "22621", minutes=15, now=NOW)

    class Probe(FakeSock):
        def recvfrom(self, n):
            return b"\x00\x05\x00\x08tsize only\x00", ("192.0.2.10", 2000)
    t = tftp_for(winpe_tree, state, [])
    t._session(Probe(), rrq("aa64/winpe/22621/bootaa64.efi", tsize=0), ("192.0.2.10", 2000))
    assert ps.arm64_winpe_armed(state, ARM_MAC.hex(":"), NOW) == "22621"


# ---- 命令行: 开 / 关

def test_cli_arm_and_off(state, capsys):
    assert ps.main(["arm64-winpe", "--mac", "02-00-00-00-0B-01", "--build", "26100", "--state", state]) is None
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", ps.time.time()) == "26100"
    assert "26100" in capsys.readouterr().out
    ps.main(["arm64-winpe", "--off", "--state", state])
    assert ps.arm64_winpe_armed(state, "02:00:00:00:0b:01", ps.time.time()) is None


def test_cli_rejects_bad_build(state):
    with pytest.raises(SystemExit):
        ps.main(["arm64-winpe", "--mac", "02:00:00:00:0b:01", "--build", "19041", "--state", state])
