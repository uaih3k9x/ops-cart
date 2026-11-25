"""ARM64 UEFI (option 93 = 0x000b) 网络引导: 按架构精确分发引导文件, 不认识的架构一个包都不回;
ARM64 的 GRUB 菜单 (aa64/grub.cfg) 由推车生成, 默认本地盘, 诊断 Linux 只能手选.

回归: 实机 (一块 ARM64 UEFI 板子, 固件按 F12 网络启动) 的 DISCOVER 带
vendor 'PXEClient:Arch:00011:UNDI:003000' + option 93 = 000b, 老代码在 67 口 OFFER 和 4011 口 ACK 都给了 shimx64.efi.
报文字段照抄实机, MAC / 地址是假的."""

import socket
import struct
import threading
import urllib.error
import urllib.request

import pytest

import httpserve
import pxeserver as ps
from pxeserver import DhcpServer, TftpServer, arch_bootfile, encode_opt, parse_dhcp

IP = "192.0.2.1"
ARM_MAC = b"\x02\x00\x00\x00\x0b\x01"
ARM_VENDOR = b"PXEClient:Arch:00011:UNDI:003000"
ARM_FILE = "aa64/grubaa64.efi"


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *a, **k): self.lines.append(fmt % a if a else fmt)
    def warning(self, fmt, *a, **k): self.lines.append(fmt % a if a else fmt)
    def exception(self, *a, **k): pass
    def error(self, *a, **k): pass


class ProxyGuard:
    """守卫在 proxy 状态 (网络里有别人的 DHCP): 67 口只回代理 OFFER, 不发地址."""
    def may_reply(self): return False
    def may_proxy(self): return True
    def may_renew(self): return False
    def foreign(self, *a, **k): pass


def dhcp(**kw):
    return DhcpServer(IP, "192.0.2.10", "192.0.2.12", "shimx64.efi", "pxelinux.0", FakeLog(), port=1167, **kw)


def packet(msg_type=1, arch=11, vendor=ARM_VENDOR, mac=ARM_MAC, ciaddr=None, extra=b""):
    msg = bytearray(240)
    msg[0], msg[1], msg[2] = 1, 1, 6
    msg[4:8] = b"\x12\x34\x56\x78"
    if ciaddr:
        msg[12:16] = socket.inet_aton(ciaddr)
    msg[28:34] = mac
    msg[236:240] = ps.MAGIC
    opts = encode_opt(53, bytes([msg_type]))
    if vendor is not None:
        opts += encode_opt(60, vendor)
    if arch is not None:
        opts += encode_opt(93, struct.pack("!H", arch))
    return bytes(msg) + opts + extra + b"\xff"


def bootfile_of(reply):
    _, _, opts = parse_dhcp(reply)
    return opts[67].decode(), reply[108:236].split(b"\x00")[0].decode()


# ---- 架构映射

def test_arch_map_is_exact():
    f = lambda a: arch_bootfile({93: struct.pack("!H", a)}, "shim.efi", "pxelinux.0", arm64_file=ARM_FILE)
    assert f(0) == "pxelinux.0"
    assert f(7) == "shim.efi" and f(9) == "shim.efi"
    assert f(11) == ARM_FILE
    for unknown in (6, 10, 0x10, 0x13, 0x19, 0xFFFF):      # IA32 / ARM32 / x86 HTTP / ARM64 HTTP / RISC-V ...
        assert f(unknown) is None, hex(unknown)


def test_arch_map_without_option_93_unchanged():
    assert arch_bootfile({}, "shim.efi", "pxelinux.0", arm64_file=ARM_FILE) == "shim.efi"


def test_arch_map_bad_length_is_unknown():
    assert arch_bootfile({93: b"\x00"}, "shim.efi", "pxelinux.0", arm64_file=ARM_FILE) is None
    # RFC 4578 允许列多个架构: 按第一个
    assert arch_bootfile({93: b"\x00\x0b\x00\x07"}, "shim.efi", "pxelinux.0", arm64_file=ARM_FILE) == ARM_FILE


def test_arm64_disabled_when_no_file():
    assert arch_bootfile({93: struct.pack("!H", 11)}, "shim.efi", "pxelinux.0", arm64_file=None) is None


# ---- 67 口 (自己发地址)

def test_arm64_discover_gets_grubaa64_not_shim():
    reply = dhcp().handle(packet(), ("0.0.0.0", 68))
    assert reply is not None
    assert parse_dhcp(reply)[2][53] == b"\x02"
    assert bootfile_of(reply) == (ARM_FILE, ARM_FILE)


def test_arm64_request_ack_gets_grubaa64():
    d = dhcp()
    d.handle(packet(), None)
    req = packet(msg_type=3, extra=encode_opt(50, ("192.0.2.10",)) + encode_opt(54, (IP,)))
    reply = d.handle(req, None)
    assert parse_dhcp(reply)[2][53] == b"\x05"
    assert bootfile_of(reply)[0] == ARM_FILE


@pytest.mark.parametrize("arch,vendor", [
    (0x13, b"HTTPClient:Arch:00019:UNDI:003000"),       # ARM64 UEFI HTTP boot: 不支持
    (0x0a, b"PXEClient:Arch:00010:UNDI:003000"),        # ARM32 UEFI
    (0x06, b"PXEClient:Arch:00006:UNDI:003000"),        # x86 32 位 UEFI
])
def test_unknown_arch_gets_nothing_and_holds_no_address(arch, vendor):
    d = dhcp()
    assert d.handle(packet(arch=arch, vendor=vendor), ("0.0.0.0", 68)) is None
    assert d.leases == {}
    assert any("不支持" in line for line in d.log.lines)


def test_unknown_arch_request_gets_nothing():
    d = dhcp()
    req = packet(msg_type=3, arch=0x13, extra=encode_opt(50, ("192.0.2.10",)) + encode_opt(54, (IP,)))
    assert d.handle(req, None) is None


def test_x86_and_bios_unchanged():
    d = dhcp()
    assert bootfile_of(d.handle(packet(arch=7, vendor=b"PXEClient", mac=b"\x02\0\0\0\0\x07"), None))[0] == "shimx64.efi"
    assert bootfile_of(d.handle(packet(arch=0, vendor=b"PXEClient", mac=b"\x02\0\0\0\0\x00"), None))[0] == "pxelinux.0"


def test_ipxe_x86_still_gets_script_and_arm_ipxe_does_not():
    d = dhcp()
    ipxe = encode_opt(77, b"iPXE")
    x86 = d.handle(packet(arch=7, vendor=b"PXEClient", mac=b"\x02\0\0\0\0\x08", extra=ipxe), None)
    assert bootfile_of(x86)[0] == f"http://{IP}/boot.ipxe"
    arm = d.handle(packet(arch=11, extra=ipxe), None)           # boot.ipxe 是 x86 的 wimboot, ARM 起不来
    assert bootfile_of(arm)[0] == ARM_FILE


# ---- 67 口 (代理模式) 和 4011

def test_proxy_mode_answers_arm64():
    assert 11 in DhcpServer.PROXY_ARCHES
    d = dhcp()
    d.guard = ProxyGuard()
    reply = d.handle(packet(), ("0.0.0.0", 68))
    assert socket.inet_ntoa(reply[16:20]) == "0.0.0.0"
    assert bootfile_of(reply)[0] == ARM_FILE


def test_proxy_mode_ignores_unknown_arch():
    d = dhcp()
    d.guard = ProxyGuard()
    assert d.handle(packet(arch=0x13, vendor=b"PXEClient:Arch:00019:UNDI:003000"), None) is None


def test_4011_ack_for_arm64_gets_grubaa64():
    """实机: 拿到地址后单播到 4011 要引导文件, 老代码回 shimx64.efi."""
    req = packet(msg_type=3, ciaddr="192.0.2.10")
    reply = dhcp().handle_proxy(req, ("192.0.2.10", 4011))
    assert parse_dhcp(reply)[2][53] == b"\x05"
    assert bootfile_of(reply) == (ARM_FILE, ARM_FILE)


@pytest.mark.parametrize("msg_type", [1, 3])
def test_4011_ignores_unknown_arch(msg_type):
    req = packet(msg_type=msg_type, arch=0x0a, vendor=b"PXEClient:Arch:00010:UNDI:003000", ciaddr="192.0.2.10")
    assert dhcp().handle_proxy(req, ("192.0.2.10", 4011)) is None


def test_4011_bios_still_answered():
    """自己发地址时 BIOS 的 PXE ROM 也可能来 4011: 照旧回 pxelinux.0 (不按 PROXY_ARCHES 卡)."""
    req = packet(msg_type=3, arch=0, vendor=b"PXEClient:Arch:00000:UNDI:002001", ciaddr="192.0.2.10")
    assert bootfile_of(dhcp().handle_proxy(req, ("192.0.2.10", 4011)))[0] == "pxelinux.0"


def test_arm64_file_configurable():
    reply = dhcp(arm64_file="arm/BOOTAA64.EFI").handle(packet(), None)
    assert bootfile_of(reply)[0] == "arm/BOOTAA64.EFI"


def test_cli_arm64_file_default_and_env(monkeypatch):
    base = ["serve", "--ip", IP, "--pool", "192.0.2.10,192.0.2.12", "--tftp-root", "/x"]
    monkeypatch.delenv("PXE_ARM64_FILE", raising=False)
    assert ps.parse_args(base).arm64_file == ARM_FILE
    monkeypatch.setenv("PXE_ARM64_FILE", "arm/BOOTAA64.EFI")
    assert ps.parse_args(base).arm64_file == "arm/BOOTAA64.EFI"
    assert ps.parse_args(base + ["--arm64-file", "y.efi"]).arm64_file == "y.efi"


# ---- ARM64 GRUB 菜单

def test_arm64_menu_default_is_local_disk():
    menu = ps.grub_menu_arm64(IP)
    assert menu.startswith("set default=0\nset timeout=10\n")
    first = menu.split("menuentry ")[1]
    assert first.startswith(f"'{ps.LOCAL_TITLE}'") and "exit" in first
    assert "inst.ks" not in menu and "inst.rescue" not in menu and "inst.sshd" not in menu


def test_arm64_menu_diag_entries():
    menu = ps.grub_menu_arm64(IP)
    assert f"linux (http,{IP})/aa64/alpine/vmlinuz-lts " in menu
    assert f"initrd (http,{IP})/aa64/alpine/initramfs-lts" in menu
    assert "linux /aa64/alpine/vmlinuz-lts " in menu            # TFTP 慢路
    assert f"modloop=http://{IP}/aa64/alpine/modloop-lts" in menu
    assert f"alpine_repo=http://{IP}/aa64/alpine/repo/main" in menu
    assert f",http://{IP}/aa64/alpine/repo/community " in menu
    assert " pkgs=python3," in menu and "ethtool" in menu and "dmidecode" in menu
    assert f"apkovl=http://{IP}/aa64/alpine/ops-diag.apkovl.tar.gz" in menu   # 收集脚本 + 开机自启
    assert f"ops_diag=http://{IP}:8081/diag/" in menu                          # 收集完传回推车
    assert "ssh_key" not in menu                                # 不开 sshd: 只有屏幕前 / 串口上的人能登
    assert "earlycon console=tty0 console=ttyAMA0,115200" in menu
    assert menu.count("menuentry ") == 6                        # + 两个 ARM64 WinPE 手动项 (test_arm64_winpe.py)


def test_arm64_menu_diag_loads_ftgm_initrd():
    """#119: 两个 Alpine 诊断项都在 initramfs-lts 后面级联 ftgm-initrd (飞腾板载网卡驱动, initramfs 阶段取网前加载);
    顺序要紧 (后解开的覆盖先解开的: 附加 cpio 里的 modules.dep 要盖住 initramfs-lts 自带的)."""
    menu = ps.grub_menu_arm64(IP)
    lines = [l.strip() for l in menu.splitlines() if l.strip().startswith("initrd ")]
    assert lines == [f"initrd (http,{IP})/aa64/alpine/initramfs-lts (http,{IP})/aa64/alpine/ftgm-initrd.cpio.gz",
                     "initrd /aa64/alpine/initramfs-lts /aa64/alpine/ftgm-initrd.cpio.gz"]
    assert ps.ARM64_NIC_INITRD == "aa64/alpine/ftgm-initrd.cpio.gz"
    assert "modules=" not in menu                               # 加载清单在 cpio 的 /etc/modules 里, 菜单不重复写


def test_arm64_menu_passes_gates():
    menu = ps.grub_menu_arm64(IP)
    assert not ps.unlock_only_menu(menu)
    assert not ps.rescue_default_menu(menu)


def test_arm64_diag_is_manual_only():
    """诊断 Linux 当救援一样只能手选: 默认项是它的菜单, 门禁换成默认菜单."""
    menu = ps.grub_menu_arm64(IP).replace("set default=0", "set default=1")
    assert ps.rescue_default_menu(menu)


# ---- TFTP: aa64/grub.cfg 推车生成, aa64/ 下别的菜单一律找不到

@pytest.fixture
def tftp(tmp_path):
    (tmp_path / "aa64").mkdir()
    (tmp_path / "aa64" / "grubaa64.efi").write_bytes(b"MZ-arm64")
    (tmp_path / "aa64" / "grub.cfg").write_text("set default=0\nmenuentry 'x' {\n linux /v inst.ks=http://x/ks\n}\n")
    (tmp_path / "aa64" / "grub.cfg-01-02-00-00-00-0b-01").write_text("set default=0\n")
    (tmp_path / "grub.cfg").write_text("set default=0\n")
    return TftpServer(IP, str(tmp_path), FakeLog(), pxe_mode_path=str(tmp_path / "mode.json"))


def test_tftp_arm64_menu_is_generated_not_read(tftp):
    f, size, what = tftp.open_file("aa64/grub.cfg", ("192.0.2.10", 1))
    data = f.read()
    assert data == ps.grub_menu_arm64(IP).encode() and size == len(data)
    assert "推车生成" in what


@pytest.mark.parametrize("name", ["aa64/grub.cfg-01-02-00-00-00-0b-01", "aa64/grub.cfg-C000020A", "AA64/GRUB.CFG-01-x",
                                  "aa64/sub/grub.cfg", "/aa64/grub.cfg.bak"])
def test_tftp_other_arm64_menus_not_found(tftp, name):
    with pytest.raises(FileNotFoundError):
        tftp.open_file(name, ("192.0.2.10", 1))


def test_tftp_arm64_case_alias_still_generated(tftp):
    f, _, _ = tftp.open_file("AA64/Grub.Cfg", ("192.0.2.10", 1))
    assert f.read() == ps.grub_menu_arm64(IP).encode()


def test_tftp_arm64_bootloader_served(tftp):
    f, size, _ = tftp.open_file("aa64/grubaa64.efi", ("192.0.2.10", 1))
    assert f.read() == b"MZ-arm64"
    f.close()


def test_tftp_x86_menu_unaffected(tftp):
    f, _, what = tftp.open_file("grub.cfg", ("192.0.2.10", 1))
    assert f.read() == b"set default=0\n" and "推车生成" not in what


def test_http_arm64_menus_404(tmp_path):
    """GRUB 只经 TFTP 取菜单; 80 口 HTTP 对 aa64/ 下的 grub.cfg* 一律 404 (同 pxelinux.cfg)."""
    (tmp_path / "aa64").mkdir()
    (tmp_path / "aa64" / "grub.cfg").write_text("set default=0\n")
    (tmp_path / "aa64" / "grub.cfg-01-02-00-00-00-0b-01").write_text("set default=0\n")
    (tmp_path / "aa64" / "grubaa64.efi").write_bytes(b"MZ")
    srv = httpserve.make_server("127.0.0.1", 0, str(tmp_path), pxe_mode=str(tmp_path / "m.json"), event=lambda m: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def code(path):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/{path}", timeout=3).status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert code("aa64/grub.cfg") == 404
        assert code("aa64/grub.cfg-01-02-00-00-00-0b-01") == 404
        assert code("aa64/grubaa64.efi") == 200
    finally:
        srv.shutdown()
        srv.server_close()
