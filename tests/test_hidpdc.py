"""hidpdc 测试: 用真机 APC BX1200CI-CN 的 758 字节 report descriptor 当 fixture.

描述符来自 2026-10-29 推车上的 UPS (USB 051d:0002, hidraw7)。
解析是纯函数, 不碰设备; 真正的 ioctl 读取在上车时冒烟.
"""

import hidpdc

# 真机描述符 (758 字节, 折行只为人眼好读)
DESC = bytes.fromhex(
    "05840904a1010924a100850109fe790275089501150026ff00b122850209ff7903b1228503058509"
    "897904b1228504098f7901b1228505098bb122850a058409fd750826ff00650055007901b122850b"
    "0585092cb1037508850c096625ffb1a2096681a2850d0983750825646500b122850e0967b122850f"
    "0968751027ffff0000660110b1a285117508058509291501b1a2851309d01500250181a209d0b1a2"
    "8514094281a20942b1a2851505840957751016ffff26ff7f660110b1a285170585092a9501751027"
    "ffff0000660110b1a285180584095a7508150125036500b1a20902a1028516058565007501150025"
    "01094481a20944b1a2094581a20945b1a209d081a209d0b1a209d181a209d1b1a20584096981a209"
    "69b1a20585094381a20943b1a20585094b81a2094bb1a20584096581a20965b1a2058509db81a209"
    "dbb1a295178101b101c0c005840912a10095018520058509857510150027ffff000081220985b1a2"
    "8521058409587508250681a20958b1a285220585096625ffb1a285230968751027ffff0000660110"
    "b1a28524092ab1a28525058409406721d1f0005505b12285260930b1a2c00584091aa10085300584"
    "09407508150026ff006721d1f0005507b122853109307510b1a285320953168c00269600b1a28533"
    "0954161801262201b1a2853575080686ff096115002502b1a285360952250db1a2c00905a1008540"
    "097c25ffb1a2c005840916a1008550093575081500256465005500b1a28552058409447510150027"
    "ffff00006621d15507b1a2c0857f058409fe790575089501150026ff0065005500b122857e0686ff"
    "0942790775089501150026ff00b122857d058409ff7903b122857c09fd7901b122857b0585098575"
    "1027ffff0000b1a205840902a102857a0585094475012501b1a20945b1a209d0b1a209d1b1a20585"
    "094bb1a2058509dbb1a205840965b1a29519b101c085780584095a7508950115012503b1a2857906"
    "86ff097275089501b1a206ffff09ffa10285e309ef7508950726ff001500818209efb182c0c0"
)


def test_descriptor_is_the_real_one():
    assert len(DESC) == 758


def test_has_percent_load_as_byte_0_to_100():
    fields = hidpdc.parse_descriptor(DESC)
    load = [f for f in hidpdc.iter_fields(fields) if (f.usage_page, f.usage) == (0x84, 0x35)]
    assert load, "应当解析出 Power Device / PercentLoad"
    f = load[0]
    assert f.size == 8 and (f.logmin, f.logmax) == (0, 100)
    assert f.variable and not f.constant


def test_has_voltage_but_no_temperature():
    usages = {(f.usage_page, f.usage) for f in hidpdc.iter_fields(hidpdc.parse_descriptor(DESC))}
    assert (0x84, 0x30) in usages          # Voltage
    assert (0x84, 0x36) not in usages      # Temperature: 这块板不报
    assert (0x84, 0x31) not in usages      # Current


def test_battery_reports_present():
    usages = {(f.usage_page, f.usage) for f in hidpdc.iter_fields(hidpdc.parse_descriptor(DESC))}
    assert (0x85, 0x66) in usages          # RemainingCapacity
    assert (0x85, 0x68) in usages          # RunTimeToEmpty
    assert (0x85, 0x83) in usages          # DesignCapacity（CapacityMode 0x2C 这块板没报）


def test_report_ids_are_unique_and_payload_sized():
    fields = hidpdc.parse_descriptor(DESC)
    sizes = hidpdc.report_sizes(fields)
    assert sizes, "至少要有 report"
    for rid, bits in sizes.items():
        assert 0 < bits <= 0xFFFF, rid
    # 真板上确实有 15 bit 这种非整字节报告, 读的时候要向上取整到字节
    assert set(hidpdc.report_bytes(sizes)) == set(sizes)
    assert all(n >= 1 for n in hidpdc.report_bytes(sizes).values())


def test_extract_bits_little_endian():
    # 8bit 字段
    assert hidpdc.extract(b"\x2a", 0, 8) == 0x2A
    # 跨字节的 16bit
    assert hidpdc.extract(b"\x34\x12", 0, 16) == 0x1234
    # 非字节对齐 + LSB first: 0b1010_0011 的 bit3..bit6 = [0,0,1,0] -> 0b0100 = 4
    assert hidpdc.extract(bytes([0b10100011]), 3, 4) == 0b0100


def test_scale_uses_unit_exponent():
    assert hidpdc.scale(100, unit_exp=0) == 100
    assert hidpdc.scale(1234, unit_exp=-2) == 12.34
    assert hidpdc.scale(5, unit_exp=1) == 50


def test_read_reports_decodes_named_values():
    fields = hidpdc.parse_descriptor(DESC)
    # 构造一个只含 PercentLoad 的报告: 用真实字段的偏移写进去
    load = next(f for f in hidpdc.iter_fields(fields) if (f.usage_page, f.usage) == (0x84, 0x35))
    buf = bytearray(hidpdc.report_bytes(hidpdc.report_sizes(fields))[load.report_id])
    hidpdc.put_bits(buf, load.offset, load.size, 42)
    values = hidpdc.decode_report(fields, load.report_id, bytes(buf))
    assert values["ups.load"] == 42


def test_decode_ignores_unknown_usages():
    fields = hidpdc.parse_descriptor(DESC)
    sizes = hidpdc.report_sizes(fields)
    rid = next(iter(sizes))
    values = hidpdc.decode_report(fields, rid, bytes(hidpdc.report_bytes(sizes)[rid]))
    assert "ups.load" not in values or values["ups.load"] == 0


def test_descriptor_path_uses_sysfs(tmp_path):
    # /dev/hidrawN 不是软链, 描述符要从 /sys/class/hidraw/<名>/device/ 找
    fake_sysfs = tmp_path / "hidraw"
    dev_dir = fake_sysfs / "hidraw7" / "device"
    dev_dir.mkdir(parents=True)
    (dev_dir / "report_descriptor").write_bytes(b"\x05\x84")
    got = hidpdc.descriptor_path("/dev/hidraw7", sysfs=str(fake_sysfs))
    assert got == str(dev_dir / "report_descriptor")


def test_descriptor_path_falls_back(tmp_path):
    got = hidpdc.descriptor_path("/dev/hidraw7", sysfs=str(tmp_path / "nope"))
    assert got.endswith("report_descriptor")


def test_scale_ignores_absurd_exponent():
    # 真机 Voltage 的 unit exponent 报 7, 232 * 10^7 伏 显然是笔误
    assert hidpdc.scale(232, unit_exp=7) == 232
    assert abs(hidpdc.scale(232, unit_exp=-1) - 23.2) < 1e-9


def test_status_from_pdc_booleans():
    fields = hidpdc.parse_descriptor(DESC)
    sizes = hidpdc.report_bytes(hidpdc.report_sizes(fields, kind="feature"))
    # 把 ACPresent 的那一位点起来
    ac = next(f for f in hidpdc.iter_fields(fields)
              if (f.usage_page, f.usage) == (hidpdc.PAGE_BATTERY, 0xD0)
              and f.kind == "feature" and f.size == 1)
    buf = bytearray(sizes[ac.report_id])
    hidpdc.put_bits(buf, ac.offset, ac.size, 1)
    assert "OL" in hidpdc.status_flags(fields, {ac.report_id: bytes(buf)}).split()
