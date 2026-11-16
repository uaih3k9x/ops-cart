"""serial_allowlist (#75 影子期): 串口写闸门第 1 层按厂商的命令前缀白名单. 纯逻辑, 不碰串口.

影子期只记录: 白名单判定写进 gate.shadow, 放行结果仍按 survey_core.is_readonly. 这里测白名单本身."""

import itertools
import random

import pytest

import serial_allowlist as sa
import serial_write_core as sw
import survey_core as sv

# ------------------------------------------------------------ 参数样例 (正例展开用)

SAMPLES = {
    "vlan": ["1", "100", "4094"],
    "ipv4": ["192.0.2.1", "198.51.100.254", "203.0.113.0"],
    "filter": ["sysname", "GigabitEthernet0/0/1"],
}
MAC = {"huawei": "0200-0000-0001", "h3c": "0200-0000-0001", "cisco": "0200.0000.0001", "ruijie": "0200.0000.0001"}
IFNAMES = {
    "huawei": ["GigabitEthernet0/0/1", "XGigabitEthernet0/0/1", "25GE1/0/1", "40GE1/0/1", "100GE1/0/1",
               "Eth-Trunk1", "Vlanif100", "LoopBack0", "MEth0/0/0",
               # H3C 在写闸门里走华为清单, 接口名一起收
               "Ten-GigabitEthernet1/0/1", "Twenty-FiveGigE1/0/1", "FortyGigE1/0/1", "HundredGigE1/0/1",
               "Bridge-Aggregation1", "Vlan-interface100", "M-GigabitEthernet0/0/0", "GigabitEthernet1/0/1.100"],
    "h3c": ["GigabitEthernet1/0/1", "Ten-GigabitEthernet1/0/1", "Twenty-FiveGigE1/0/1", "FortyGigE1/0/1",
            "HundredGigE1/0/1", "Bridge-Aggregation1", "Vlan-interface100", "LoopBack0", "M-GigabitEthernet0/0/0"],
    "cisco": ["GigabitEthernet0/1", "TenGigabitEthernet1/0/1", "TwentyFiveGigE1/0/1", "FortyGigabitEthernet1/0/1",
              "HundredGigE1/0/1", "FastEthernet0/1", "Ethernet1/1", "Port-channel1", "Vlan100", "Loopback0"],
    "ruijie": ["GigabitEthernet0/1", "TenGigabitEthernet0/1", "AggregatePort1", "VLAN100", "Loopback0"],
}
VENDORS = ("huawei", "h3c", "cisco", "ruijie")


def _samples(vendor, typ):
    if typ == "ifname":
        return IFNAMES[vendor]
    if typ == "mac":
        return [MAC[vendor]]
    return SAMPLES[typ]


def _expand(vendor, e):
    """条目 -> 主段的具体命令 (每个参数槽轮流取样例, 不做全笛卡尔积)."""
    words = e.text.split(" ")
    slots = [w[1:-1] for w in words if w.startswith("{")]
    if not slots:
        return [e.text]
    pools = [_samples(vendor, t) for t in slots]
    n = max(len(p) for p in pools)
    out = []
    for i in range(n):
        vals = iter([p[i % len(p)] for p in pools])
        out.append(" ".join(next(vals) if w.startswith("{") else w for w in words))
    return out


def _pipe_samples(e):
    out = []
    for word, arg in (e.pipes or {}).items():
        out.append(f" | {word} sysname" if arg else f" | {word}")
    return out


def _scope(e):
    return "survey" if e.big else "gate"


def _all():
    for v in VENDORS:
        for e in sa.ALLOW[v]:
            yield v, e


# ------------------------------------------------------------ 正例: 每条条目的规范样例恰好命中它

@pytest.mark.parametrize("vendor,e", list(_all()), ids=lambda x: x if isinstance(x, str) else x.text)
def test_canonical_samples_hit_exactly_that_entry(vendor, e):
    mode = "exec"
    for cmd in _expand(vendor, e):
        for full in [cmd] + [cmd + p for p in _pipe_samples(e)]:
            r = sa.match(full, vendor, mode=mode, scope=_scope(e))
            assert r["entry"] == sa.entry_id(vendor, e), (full, r)
            assert r["src"] == e.src


def test_entry_id_format():
    e = next(e for e in sa.ALLOW["huawei"] if e.text == "display interface {ifname}")
    assert sa.entry_id("huawei", e) == "huawei:display interface {ifname}"
    assert "huawei:display interface {ifname}" in sa.all_ids()


# ------------------------------------------------------------ 各厂商命中 / 不命中

@pytest.mark.parametrize("vendor,cmd", [
    ("huawei", "display version"), ("huawei", "display interface GigabitEthernet0/0/1"),
    ("huawei", "display vlan 100"), ("huawei", "display mac-address 0200-0000-0001"),
    ("huawei", "display ip routing-table 192.0.2.1"),
    ("huawei", "display transceiver interface XGigabitEthernet0/0/1 verbose"),
    ("cisco", "show version"), ("cisco", "show interfaces GigabitEthernet0/1"),
    ("cisco", "show ip route 198.51.100.1"), ("cisco", "show interfaces TenGigabitEthernet1/0/1 transceiver"),
    ("ruijie", "show version"), ("ruijie", "show interfaces AggregatePort1"),
    ("h3c", "display lldp neighbor-information list"), ("h3c", "display link-aggregation summary"),
])
def test_vendor_hits(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec")["entry"], cmd


@pytest.mark.parametrize("vendor,cmd", [
    ("huawei", "display logbuffer"), ("huawei", "display diagnostic-information"),
    ("huawei", "display ntp-service trace"), ("huawei", "display ip routing-table"),
    ("huawei", "display memory"), ("huawei", "display vlan 0"), ("huawei", "display vlan 4095"),
    ("huawei", "display vlan 0100"), ("huawei", "display ip routing-table 256.0.0.1"),
    ("huawei", "display ip routing-table 192.0.2.01"), ("huawei", "display ip routing-table 192.0.2"),
    ("huawei", "display ip routing-table core-sw"), ("huawei", "display mac-address 0200.0000.0001"),
    ("huawei", "display mac-address 0200-0000-000A"),
    ("cisco", "show tech-support"), ("cisco", "show logging"), ("cisco", "show memory"),
    ("cisco", "show running-config all"), ("cisco", "show processes cpu"), ("cisco", "do show version"),
    ("cisco", "show ip route"), ("cisco", "show mac-address-table"), ("cisco", "show mac address-table 1"),
    ("ruijie", "show mac address-table"), ("ruijie", "show interfaces GigabitEthernet 0/1"),
    ("h3c", "display memory"), ("h3c", "display lldp neighbor brief"),
    ("linux", "uname -a"), ("linux", "cat /etc/os-release"), ("nosuch", "show version"), (None, "show version"),
])
def test_vendor_misses(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec", scope="survey")["entry"] is None, cmd


def test_cross_vendor_no_hits_huawei_cisco_ruijie():
    """跨厂商只在思科 / 锐捷 / 华为之间做; 思科和锐捷文本相同的条目除外 (show version 这类)."""
    same = {e.text for e in sa.ALLOW["cisco"]} & {e.text for e in sa.ALLOW["ruijie"]}
    for a, b in itertools.permutations(("huawei", "cisco", "ruijie"), 2):
        for e in sa.ALLOW[a]:
            if e.text in same and {a, b} == {"cisco", "ruijie"}:
                continue
            for cmd in _expand(a, e):
                assert sa.match(cmd, b, mode="exec", scope="survey")["entry"] is None, (a, b, cmd)


def test_survey_h3c_and_huawei_lists_are_separate():
    """巡检层面: H3C 特有的条目在华为下不命中, 华为特有的在 H3C 下不命中."""
    hw = {e.text for e in sa.ALLOW["huawei"]}
    h3 = {e.text for e in sa.ALLOW["h3c"]}
    assert sa.match("display lldp neighbor-information list", "huawei", mode="exec")["entry"] is None
    assert sa.match("display lldp neighbor brief", "h3c", mode="exec")["entry"] is None
    assert sa.match("screen-length disable", "huawei", mode="exec")["entry"] is None
    assert sa.match("screen-length 0 temporary", "h3c", mode="exec")["entry"] is None
    assert hw - h3 and h3 - hw


def test_write_gate_has_no_h3c_vendor():
    """写闸门登记不了 h3c (提示符和华为同形), auto_huawei 把它认成华为, 用华为清单."""
    assert "h3c" not in sw.load_rules(None)["vendors"]
    v, at = sw.slot_vendor(1, sw.load_rules(None), "<H3C-SW>")
    assert v == "huawei" and at


# ------------------------------------------------------------ 拍板 1: 缩写 (含接口名缩写) 不收

@pytest.mark.parametrize("cmd", ["dis version", "disp version", "display ver", "display int brief",
                                 "display interface Gi0/0/1", "display interface GE0/0/1",
                                 "display interface gigabitethernet0/0/1", "display interface XGE0/0/1",
                                 "display interface Eth-Trunk", "display interface Vlanif"])
def test_huawei_abbreviations_refused(cmd):
    assert sa.match(cmd, "huawei", mode="exec")["entry"] is None


@pytest.mark.parametrize("cmd", ["sh version", "show ver", "show int status", "show interfaces Gi0/1",
                                 "show interfaces Te1/0/1", "show interfaces Po1", "show interfaces Fa0/1"])
def test_cisco_abbreviations_refused(cmd):
    assert sa.match(cmd, "cisco", mode="exec")["entry"] is None


def test_every_literal_prefix_refused():
    """每条条目的每个字面量: 所有真前缀、大写、首字母大写、加引号都拒."""
    for v, e in _all():
        words = e.text.split(" ")
        base = _expand(v, e)[0].split(" ")
        for i, w in enumerate(words):
            if w.startswith("{"):
                continue
            variants = {w[:k] for k in range(1, len(w))} | {w.upper(), w.capitalize(), f'"{w}"', f"'{w}'"}
            for x in variants - {w}:
                cmd = " ".join(base[:i] + [x] + base[i + 1:])
                assert sa.match(cmd, v, mode="exec", scope="survey")["entry"] is None, (v, cmd)


# ------------------------------------------------------------ 拍板 2: IPv6 不收

@pytest.mark.parametrize("vendor,cmd", [("huawei", "display ip routing-table 2001:db8::1"),
                                        ("huawei", "display ipv6 routing-table 2001:db8::1"),
                                        ("cisco", "show ip route 2001:db8::1"),
                                        ("cisco", "show ipv6 route 2001:db8::1")])
def test_ipv6_refused(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec")["entry"] is None
    assert "ipv6" not in sa.TYPES


# ------------------------------------------------------------ 拍板 3 / 4: ping / traceroute / 大输出不进第 1 层

@pytest.mark.parametrize("vendor,cmd", [("huawei", "ping 192.0.2.1"), ("huawei", "tracert 192.0.2.1"),
                                        ("cisco", "ping 192.0.2.1"), ("cisco", "traceroute 192.0.2.1"),
                                        ("ruijie", "ping 192.0.2.1"), ("ruijie", "traceroute 192.0.2.1")])
def test_ping_traceroute_not_in_layer1(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec", scope="survey")["entry"] is None
    for v, e in _all():
        assert e.text.split(" ")[0] not in ("ping", "tracert", "traceroute"), e.text


@pytest.mark.parametrize("vendor,cmd", [("huawei", "display current-configuration"), ("huawei", "display arp"),
                                        ("huawei", "display mac-address"),
                                        ("huawei", "display current-configuration | include sysname"),
                                        ("cisco", "show running-config"), ("cisco", "show arp"),
                                        ("cisco", "show mac address-table"), ("ruijie", "show mac-address-table")])
def test_big_output_not_in_write_gate_layer1_but_ok_for_survey(vendor, cmd):
    """大输出命令不进写闸门第 1 层; 巡检默认清单要用, 只在巡检范围内命中."""
    g = sa.match(cmd, vendor, mode="exec", scope="gate")
    assert g["entry"] is None and "大输出" in g["why"]
    assert sa.match(cmd, vendor, mode="exec", scope="survey")["entry"]


# ------------------------------------------------------------ 拍板 11: 管道参数限一个安全词

def test_pipes_huawei():
    c = "display current-configuration"
    ok = [f"{c} | include sysname", f"{c} | exclude x", f"{c} | begin interface", f"{c} | section bgp",
          f"{c} | count", f"{c} | no-more", f"{c} | include GigabitEthernet0/0/1"]
    bad = [f"{c} | count x", f"{c} | no-more x", f"{c} | include", f"{c} | section",
           f"{c} | include a b", f'{c} | include "a b"', f"{c} | include 'a'", f"{c} | include a*",
           f"{c} | include a+", f"{c} | include (a)", f"{c} | include a:b", f"{c} | include to-file",
           f"{c} | include TFTP", f"{c} | include reset", f"{c} | include clear",
           f"{c} | include a | count", f"{c} |include a", f"{c} | include a|b", f"{c}|count",
           f"{c} | save x", f"{c} | redirect flash:x", f"{c} | tee x", f"{c} | append x",
           f"{c} | include " + "a" * 65]
    for cmd in ok:
        assert sa.match(cmd, "huawei", mode="exec", scope="survey")["entry"], cmd
    for cmd in bad:
        assert sa.match(cmd, "huawei", mode="exec", scope="survey")["entry"] is None, cmd


@pytest.mark.parametrize("vendor,cmd", [("cisco", "show running-config | no-more"),
                                        ("ruijie", "show running-config | no-more"),
                                        ("h3c", "display current-configuration | no-more"),
                                        ("ruijie", "show running-config | section x"),
                                        ("huawei", "display version | include x"),
                                        ("cisco", "show version | include x")])
def test_pipes_only_where_declared(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec", scope="survey")["entry"] is None


def test_filter_refuses_file_words_and_colon():
    for w in sorted(sa.FILE_WORDS):
        for x in (w, w.upper()):
            assert sa.match(f"show running-config | include {x}", "cisco", mode="exec",
                            scope="survey")["entry"] is None, x
    assert sa.match("show running-config | include a:b", "cisco", mode="exec", scope="survey")["entry"] is None


# ------------------------------------------------------------ 通用检查 (第二道防线)

@pytest.mark.parametrize("cmd", ["display version ", " display version", "display  version",
                                 "display\tversion", "display version\n", "display version?",
                                 "display version ?", "display version; reboot", "display version & x",
                                 "display version [x]", "display version {x}", "display version $x",
                                 "display version `x`", "display version # x", "display version ! x",
                                 "display versión", "", None, 42])
def test_common_checks(cmd):
    assert sa.match(cmd, "huawei", mode="exec")["entry"] is None


def test_too_long_refused():
    assert sa.match("display current-configuration | include " + "a" * 300, "huawei", mode="exec",
                    scope="survey")["entry"] is None


# ------------------------------------------------------------ 提示符上下文 (session 条目只在 exec)

@pytest.mark.parametrize("vendor,line,mode", [
    ("huawei", "<SW1>", "exec"), ("huawei", "[SW1]", "config"), ("huawei", "[~SW1]", "config"),
    ("huawei", "[*SW1]", "config"), ("huawei", "[SW1-GigabitEthernet0/0/1]", "config"),
    ("cisco", "SW1#", "exec"), ("cisco", "SW1>", "exec"), ("cisco", "SW1(config)#", "config"),
    ("cisco", "SW1(config-if)#", "config"), ("ruijie", "SW1#", "exec"), ("ruijie", "SW1(config-if-range)#", "config"),
    ("huawei", "", "config"), ("huawei", "Password:", "config"), ("cisco", "  ---- More ----", "config"),
    (None, "<SW1>", "config"), ("linux", "root@host:~#", "config"),
])
def test_prompt_mode(vendor, line, mode):
    assert sa.prompt_mode(vendor, line) == mode


@pytest.mark.parametrize("vendor,cmd", [("huawei", "screen-length 0 temporary"), ("cisco", "terminal length 0"),
                                        ("ruijie", "terminal length 0"), ("h3c", "screen-length disable")])
def test_session_entries_only_in_exec(vendor, cmd):
    assert sa.match(cmd, vendor, mode="exec")["entry"]
    r = sa.match(cmd, vendor, mode="config")
    assert r["entry"] is None and "exec" in r["why"]
    assert sa.match(cmd, vendor)["entry"] is None               # mode 不给 = 按 config (更严)


def test_plain_entries_any_mode():
    for mode in ("exec", "config"):
        assert sa.match("display version", "huawei", mode=mode)["entry"]
        assert sa.match("show version", "cisco", mode=mode)["entry"]


def test_screen_length_variants_refused():
    for cmd in ("screen-length 0", "screen-length 0 temporary x", "screen-length 512 temporary",
                "terminal length 0 x", "terminal length 512", "terminal width 0"):
        for v in VENDORS:
            assert sa.match(cmd, v, mode="exec")["entry"] is None, (v, cmd)


# ------------------------------------------------------------ 拍板 8: Linux 槽位不走白名单

def test_no_linux_entries():
    assert "linux" not in sa.ALLOW
    assert sa.catalog("linux", "exec") == []
    r = sa.match("ls", "linux", mode="exec")
    assert r["entry"] is None and "linux" in r["why"].lower()


# ------------------------------------------------------------ 拍板 5: 规则文件只能禁用

def test_parse_disable_known_unknown():
    ids, unknown = sa.parse_disable(["huawei:display interface {ifname}", "huawei:display logbuffer", 3,
                                     "cisco:show version", "cisco:show version"])
    assert ids == ["cisco:show version", "huawei:display interface {ifname}"]
    assert unknown == ["huawei:display logbuffer", "3"]
    assert sa.parse_disable(None) == ([], [])
    assert sa.parse_disable("huawei:display version") == ([], ["huawei:display version"])   # 不是列表


def test_disabled_entry_refuses_all_values():
    dis = ["huawei:display interface {ifname}"]
    for name in IFNAMES["huawei"]:
        r = sa.match(f"display interface {name}", "huawei", mode="exec", disabled=dis)
        assert r["entry"] is None and "禁用" in r["why"]
    assert sa.match("display interface brief", "huawei", mode="exec", disabled=dis)["entry"]


def test_disabled_session_refused():
    r = sa.match("screen-length 0 temporary", "huawei", mode="exec", disabled=["huawei:screen-length 0 temporary"])
    assert r["entry"] is None


# ------------------------------------------------------------ 结构不变量

def test_no_overlap_between_entries():
    for v in VENDORS:
        for e in sa.ALLOW[v]:
            for cmd in _expand(v, e):
                hits = sa.hits(cmd, v)
                assert [sa.entry_id(v, x) for x in hits] == [sa.entry_id(v, e)], (v, cmd)


def test_entries_have_no_danger_forbidden_or_file_words():
    for v, e in _all():
        for cmd in _expand(v, e):
            assert not sw.danger_words(cmd), (v, cmd)
            assert not set(cmd) & sa.FORBIDDEN, cmd
            assert not {w.lower() for w in cmd.split(" ")} & sa.FILE_WORDS, cmd


KNOWN_ABBR = {"dis", "disp", "sh", "int", "ver", "conf", "br", "nei", "cur", "run", "ip-int", "term", "len"}


def test_literals_lowercase_full_words():
    for v, e in _all():
        for w in e.text.split(" "):
            if w.startswith("{"):
                assert w[1:-1] in sa.TYPES, (v, e.text)
                continue
            assert w not in KNOWN_ABBR, (v, e.text)
            if (v, e.text) not in sa.CASE_EXCEPTIONS:
                assert w == w.lower(), (v, e.text)


def test_every_type_used():
    used = {w[1:-1] for _v, e in _all() for w in e.text.split(" ") if w.startswith("{")}
    used |= {"filter"} if any(e.pipes for _v, e in _all()) else set()
    assert used == set(sa.TYPES)


def test_src_values_and_pending_entries_marked():
    """待核条目标出来, 留到纳管现场随巡检核实 (拍板 12); 不猜."""
    srcs = {e.src for _v, e in _all()}
    assert srcs <= {"survey", "待核"}
    pending = sa.pending_review()
    assert "huawei:display interface {ifname}" in pending
    assert "huawei:display version" not in pending
    assert all(i in sa.all_ids() for i in pending)


def test_survey_defaults_in_allowlist():
    """巡检默认清单都在白名单里 (巡检范围, exec)."""
    for v, prof in sv.load_profiles(path="/nonexistent").items():
        for c in prof["commands"]:
            assert sa.match(c["cmd"], v, mode="exec", scope="survey")["entry"], (v, c["cmd"])


def test_consistent_with_is_readonly():
    """白名单命中的样例, is_readonly 也要放行 (只比 is_readonly 有规则的厂商). 差异表外不许有例外."""
    for v in sv._RULES:
        for e in sa.ALLOW[v]:
            if sa.entry_id(v, e) in sa.KNOWN_DIFF:
                continue
            for cmd in _expand(v, e):
                for full in [cmd] + [cmd + p for p in _pipe_samples(e)]:
                    assert sv.is_readonly(full, v), (v, full)
    assert all(i in sa.all_ids() for i in sa.KNOWN_DIFF)


# ------------------------------------------------------------ 随机组合 (固定种子)

def test_random_combinations_never_pass_bad_words():
    """字面量、参数样例、危险词随机拼: 命中的组合里不许出现危险词 / 缩写 / 写文件参数 / 多余的词."""
    rnd = random.Random(75)
    words = sorted({w for _v, e in _all() for w in e.text.split(" ") if not w.startswith("{")})
    bad = ["reload", "save", ";", "?", "dis", "sh", "to-file", "flash:x", "Gi0/1", "all", "core-sw", "0100"]
    pool = words + bad + ["|", "GigabitEthernet0/0/1", "192.0.2.1", "100", "include", "count", "sysname"]
    hits = 0
    for _ in range(30000):
        v = rnd.choice(VENDORS)
        cmd = " ".join(rnd.choice(pool) for _ in range(rnd.randint(1, 5)))
        r = sa.match(cmd, v, mode="exec", scope="survey")
        if r["entry"]:
            hits += 1
            main = cmd.split(" | ")[0].split(" ")
            assert not set(main) & set(bad), (v, cmd)
            assert len(main) == len(r["entry"].split(":", 1)[1].split(" ")), (v, cmd)
    assert hits                                     # 组合里确实有命中的, 不是空跑


# ------------------------------------------------------------ 影子结论 (gate.shadow)

def test_shadow_fields():
    s = sa.shadow("display version", "huawei", "exec", readonly=True)
    assert s == {"allowlist": "huawei:display version", "src": "survey", "readonly": True, "pass": True}
    s = sa.shadow("display interface GigabitEthernet0/0/1", "huawei", "config", readonly=True)
    assert s["allowlist"] == "huawei:display interface {ifname}" and s["src"] == "待核" and s["pass"] is True
    s = sa.shadow("display logbuffer", "huawei", "exec", readonly=True)
    assert s["allowlist"] is None and s["pass"] is False and s["why"] and s["readonly"] is True
    s = sa.shadow("screen-length 0 temporary", "huawei", "exec", readonly=False)
    assert s["allowlist"] == "huawei:screen-length 0 temporary" and s["pass"] is False     # 组合: 两个都过才算
    s = sa.shadow("display version", "huawei", "exec", readonly=True, disabled=["huawei:display version"])
    assert s["allowlist"] is None and s["pass"] is False


# ------------------------------------------------------------ gate_allowlist 的数据

def test_catalog_by_mode_and_disable():
    ex = {c["id"] for c in sa.catalog("huawei", "exec")}
    cf = {c["id"] for c in sa.catalog("huawei", "config")}
    assert "huawei:screen-length 0 temporary" in ex and "huawei:screen-length 0 temporary" not in cf
    assert "huawei:display current-configuration" not in ex                 # 大输出不进写闸门第 1 层
    assert "huawei:display version" not in {c["id"] for c in sa.catalog("huawei", "exec",
                                                                         disabled=["huawei:display version"])}
    c = next(c for c in sa.catalog("huawei", "exec") if c["id"] == "huawei:display interface {ifname}")
    assert c["cmd"] == "display interface {ifname}" and c["src"] == "待核"
    assert sa.catalog(None, "exec") == []
    assert set(sa.type_help("huawei")) == set(sa.TYPES)
