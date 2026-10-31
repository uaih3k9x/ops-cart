"""mcp_label (#20) 的离线测试: 假打印机 (写入/状态都换掉), 不碰 /dev/usb/lp0."""

import json
import os
import xml.etree.ElementTree as ET

import pytest

import cart_label as cl
import cart_mcp as cm
import mcp_core as mc
import mcp_label as ml
import tspl_preview as tp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CABLE = {"shortId": 42, "here": "RK01-U20 SRV-1 MGMT", "there": "RK01 SW-C G9"}
TEXT = {"big": "LAN1", "small": "CART END"}


class Printer:
    def __init__(self, status=0x00):
        self.jobs, self.status_byte, self.status_calls = [], status, 0
        self.write_error = None

    def write(self, data):
        if self.write_error:
            raise mc.ToolError(self.write_error)
        self.jobs.append(data)

    def status(self):
        self.status_calls += 1
        if isinstance(self.status_byte, Exception):
            raise self.status_byte
        return self.status_byte


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _table():
    with open(os.path.join(ROOT, "examples", "label_abbrev.json"), encoding="utf-8") as f:
        return json.load(f)


def _server(p=None, clock=None, guides=None, table=None, audit=None):
    p = p or Printer()
    seq = iter(f"proof{n:04d}" for n in range(1000))
    tools = ml.build_label_tools(write=p.write, status=p.status,
                                 abbrev=table or (lambda: (_table(), "examples/label_abbrev.json")),
                                 now=clock or Clock(), token=lambda: next(seq), guides=guides)
    return mc.Server(tools, audit=audit, version="t"), p


def _call(server, name, args=None):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args or {}}})
    return r["result"]


def _json(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def _err(res):
    assert res["isError"] is True, res
    return res["content"][0]["text"]


# ---- 注册 / 规格 ----

def test_tool_names_and_annotations():
    s, _ = _server()
    specs = {t["name"]: t for t in s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert set(specs) == {"label_preview", "label_print", "label_printer_status", "label_abbrev_propose"}
    assert specs["label_print"]["annotations"]["readOnlyHint"] is False      # 会出纸
    assert specs["label_print"]["annotations"]["destructiveHint"] is False   # 最坏废一张, 不碰设备配置
    for n in ("label_preview", "label_printer_status", "label_abbrev_propose"):
        assert specs[n]["annotations"]["readOnlyHint"] is True
    items = specs["label_print"]["inputSchema"]["properties"]["labels"]["items"]
    assert items["type"] == "object" and {"shortId", "here", "there", "big", "small"} <= set(items["properties"])


def test_real_tools_registers_label_tools():
    names = {t.name for t in cm.real_tools()}
    assert {"label_preview", "label_print", "label_printer_status", "label_abbrev_propose"} <= names
    assert {"list_threads", "read_output"} <= names                         # 原来的只读工具还在


# ---- 状态字节 ----

@pytest.mark.parametrize("b, ok, flags", [
    (0x00, True, []),
    (0x01, False, ["开盖"]),
    (0x04, False, ["缺纸"]),
    (0x08, False, ["缺碳带"]),
    (0x0D, False, ["开盖", "缺纸", "缺碳带"]),         # 多位同时置
    (0x20, True, ["打印中"]),                          # 正在打, 不算故障
])
def test_decode_status(b, ok, flags):
    d = ml.decode_status(b)
    assert d["ok"] is ok and d["flags"] == flags and d["byte"] == f"0x{b:02X}"


def test_printer_status_tool():
    s, p = _server(Printer(0x04))
    d = _json(_call(s, "label_printer_status"))
    assert d["ok"] is False and d["flags"] == ["缺纸"]
    p.status_byte = mc.ToolError("打印机没接 (/dev/usb/lp0 不在)")
    assert "没接" in _err(_call(s, "label_printer_status"))


# ---- 预览 ----

def test_preview_cable_ok_with_svg():
    s, p = _server()
    res = _call(s, "label_preview", {"kind": "cable", "labels": [CABLE]})
    d = _json(res)
    assert d["ok"] is True and d["problems"] == [] and d["count"] == 1
    assert d["labels"] == [CABLE]
    svg = res["content"][1]["text"]
    root = ET.fromstring(svg)                                              # 合法 XML
    assert root.tag.endswith("svg") and "RK01 SW-C G9" in svg
    assert p.jobs == [] and p.status_calls == 0                            # 预览不碰打印机


def test_preview_text_ok():
    s, _ = _server()
    d = _json(_call(s, "label_preview", {"kind": "text", "labels": [TEXT]}))
    assert d["ok"] is True and d["labels"] == [TEXT]


def test_preview_svg_capped():
    s, _ = _server()
    rows = [dict(CABLE, shortId=n) for n in range(1, 8)]
    res = _call(s, "label_preview", {"kind": "cable", "labels": rows})
    d = _json(res)
    assert d["count"] == 7 and d["svg_shown"] == ml.PREVIEW_SVG_MAX
    assert len(res["content"]) == 1 + ml.PREVIEW_SVG_MAX


@pytest.mark.parametrize("label, frag", [
    (dict(CABLE, there="X" * 13), "超长"),                    # 对端最多 12
    (dict(CABLE, here="Y" * 24), "超长"),                     # 本端最多 23
    (dict(CABLE, there="交换机"), "ASCII"),
    (dict(CABLE, there="SW\tA"), "ASCII"),
])
def test_preview_reports_field_problems(label, frag):
    s, _ = _server()
    res = _call(s, "label_preview", {"kind": "cable", "labels": [CABLE, label]})
    d = _json(res)
    assert d["ok"] is False and len(res["content"]) == 1                  # 字段不过就不出图
    assert any(frag in m and "第 2 张" in m for m in d["problems"])


def test_preview_text_limits():
    s, _ = _server()
    d = _json(_call(s, "label_preview", {"kind": "text", "labels": [{"big": "B" * 12, "small": "ok"}]}))
    assert d["ok"] is False and "超长" in d["problems"][0]


def test_preview_reports_out_of_bounds():
    g = tp.cable_guides()
    x0, y0, x1, y1 = g["flag"]
    narrow = dict(g, flag=(x0 + 80, y0, x1, y1))                           # 旗面左边收进来 10mm: Data Matrix 出界
    s, _ = _server(guides=narrow)
    res = _call(s, "label_preview", {"kind": "cable", "labels": [CABLE]})
    d = _json(res)
    assert d["ok"] is False and any("出界" in m for m in d["problems"])
    assert len(res["content"]) == 2                                        # 出界照样出图, 红框标出来


def test_preview_abbrev_applies_table():
    s, _ = _server()
    d = _json(_call(s, "label_preview", {"kind": "cable", "abbrev": True,
                                         "labels": [{"shortId": 3, "here": "华为 交换机 上联口", "there": "核心 防火墙"}]}))
    assert d["ok"] is True
    assert d["labels"] == [{"shortId": 3, "here": "HW SW UP", "there": "CORE FW"}]


@pytest.mark.parametrize("args, frag", [
    ({"kind": "cable", "labels": []}, "至少"),
    ({"kind": "cable", "labels": "x"}, "labels"),
    ({"kind": "cable", "labels": [{"here": "a", "there": "b"}]}, "shortId"),
    ({"kind": "cable", "labels": [dict(CABLE, extra=1)]}, "extra"),
    ({"kind": "cable", "labels": [dict(CABLE, shortId=-1)]}, "shortId"),
    ({"kind": "cable", "labels": [dict(CABLE, shortId=True)]}, "shortId"),
    ({"kind": "cable", "labels": [dict(CABLE, here=5)]}, "here"),
    ({"kind": "text", "labels": [CABLE]}, "big"),
    ({"kind": "asset", "labels": [CABLE]}, "kind"),
    ({"kind": "cable", "labels": [CABLE], "abbrev": "yes"}, "abbrev"),
])
def test_preview_bad_args(args, frag):
    s, _ = _server()
    assert frag in _err(_call(s, "label_preview", args))


def test_too_many_labels():
    s, _ = _server()
    rows = [dict(CABLE, shortId=n) for n in range(ml.MAX_LABELS + 1)]
    assert "最多" in _err(_call(s, "label_preview", {"kind": "cable", "labels": rows}))


# ---- 打印: 单张 ----

def test_print_one_cable_writes_tspl_and_returns_proof():
    s, p = _server()
    d = _json(_call(s, "label_print", {"kind": "cable", "labels": [CABLE]}))
    assert d["printed"] == 1 and d["proof"] == "proof0000" and d["proof_ttl_s"] == ml.PROOF_TTL
    assert p.jobs == [cl.to_tspl(cl.cable_labels([CABLE])).encode()]
    assert p.status_calls == 1                                             # 打之前先查状态


def test_print_one_text():
    s, p = _server()
    _json(_call(s, "label_print", {"kind": "text", "labels": [TEXT]}))
    assert p.jobs == [cl.to_tspl(cl.text_labels([("LAN1", "CART END")])).encode()]


@pytest.mark.parametrize("label, frag", [
    (dict(CABLE, there="X" * 13), "超长"),
    (dict(CABLE, here="机柜"), "ASCII"),
])
def test_print_refuses_bad_fields(label, frag):
    s, p = _server()
    assert frag in _err(_call(s, "label_print", {"kind": "cable", "labels": [label]}))
    assert p.jobs == [] and p.status_calls == 0


def test_print_refuses_out_of_bounds():
    g = tp.cable_guides()
    x0, y0, x1, y1 = g["flag"]
    s, p = _server(guides=dict(g, flag=(x0 + 80, y0, x1, y1)))
    assert "出界" in _err(_call(s, "label_print", {"kind": "cable", "labels": [CABLE]}))
    assert p.jobs == []


@pytest.mark.parametrize("b, frag", [(0x04, "缺纸"), (0x01, "开盖"), (0x08, "缺碳带")])
def test_print_refuses_when_printer_not_ready(b, frag):
    s, p = _server(Printer(b))
    assert frag in _err(_call(s, "label_print", {"kind": "cable", "labels": [CABLE]}))
    assert p.jobs == []


def test_print_status_unreadable_is_refused():
    s, p = _server(Printer(mc.ToolError("打印机没回状态字节")))
    assert "没回状态" in _err(_call(s, "label_print", {"kind": "cable", "labels": [CABLE]}))
    assert p.jobs == []


def test_print_write_failure_gives_no_proof():
    p = Printer()
    p.write_error = "写 /dev/usb/lp0 失败"
    s, _ = _server(p)
    assert "失败" in _err(_call(s, "label_print", {"kind": "cable", "labels": [CABLE]}))
    p.write_error = None
    r = _call(s, "label_print", {"kind": "cable", "labels": [CABLE, CABLE], "proof": "proof0000",
                                 "confirmed": "看过了"})
    assert "proof" in _err(r)                                              # 没打成的那次不发令牌


# ---- 打印: 批量要先打一张、人确认 ----

def _one(s, kind="cable"):
    lab = CABLE if kind == "cable" else TEXT
    return _json(_call(s, "label_print", {"kind": kind, "labels": [lab]}))["proof"]


def _rows(n):
    return [dict(CABLE, shortId=k) for k in range(1, n + 1)]


def test_batch_without_proof_refused():
    s, p = _server()
    msg = _err(_call(s, "label_print", {"kind": "cable", "labels": _rows(3)}))
    assert "先打一张" in msg and p.jobs == []


def test_batch_with_proof_and_confirmation():
    s, p = _server()
    proof = _one(s)
    d = _json(_call(s, "label_print", {"kind": "cable", "labels": _rows(3), "proof": proof,
                                       "confirmed": "现场: 第一张位置对了"}))
    assert d["printed"] == 3 and "proof" not in d
    assert p.jobs[-1] == cl.to_tspl(cl.cable_labels(_rows(3))).encode()


def test_batch_needs_human_confirmation_text():
    s, p = _server()
    proof = _one(s)
    for c in (None, "", "   "):
        args = {"kind": "cable", "labels": _rows(2), "proof": proof}
        if c is not None:
            args["confirmed"] = c
        assert "确认" in _err(_call(s, "label_print", args))
    assert len(p.jobs) == 1
    _json(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": proof, "confirmed": "ok"}))


def test_proof_is_single_use():
    s, p = _server()
    proof = _one(s)
    args = {"kind": "cable", "labels": _rows(2), "proof": proof, "confirmed": "ok"}
    _json(_call(s, "label_print", args))
    assert "proof" in _err(_call(s, "label_print", args))
    assert len(p.jobs) == 2


def test_proof_expires():
    clock = Clock()
    s, p = _server(clock=clock)
    proof = _one(s)
    clock.t += ml.PROOF_TTL + 1
    assert "过期" in _err(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": proof,
                                                   "confirmed": "ok"}))
    assert len(p.jobs) == 1


def test_proof_bound_to_layout():
    s, p = _server()
    proof = _one(s, "text")
    msg = _err(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": proof, "confirmed": "ok"}))
    assert "版面" in msg and len(p.jobs) == 1


def test_unknown_proof_refused():
    s, _ = _server()
    assert "proof" in _err(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": "nope1234",
                                                    "confirmed": "ok"}))


def test_bad_batch_does_not_burn_proof():
    s, p = _server()
    proof = _one(s)
    bad = _rows(2) + [dict(CABLE, there="X" * 13)]
    assert "超长" in _err(_call(s, "label_print", {"kind": "cable", "labels": bad, "proof": proof,
                                                   "confirmed": "ok"}))
    _json(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": proof, "confirmed": "ok"}))


def test_batch_printer_not_ready_does_not_burn_proof():
    s, p = _server()
    proof = _one(s)
    p.status_byte = 0x04
    args = {"kind": "cable", "labels": _rows(2), "proof": proof, "confirmed": "ok"}
    assert "缺纸" in _err(_call(s, "label_print", args))
    p.status_byte = 0x00
    _json(_call(s, "label_print", args))


def test_batch_is_audited_with_proof_and_confirmation(tmp_path):
    a = mc.Audit(str(tmp_path / "a.log"))
    s, _ = _server(audit=a)
    proof = _one(s)
    _json(_call(s, "label_print", {"kind": "cable", "labels": _rows(2), "proof": proof,
                                   "confirmed": "现场: 第一张齐了"}))
    lines = [json.loads(x) for x in open(a.path, encoding="utf-8")]
    last = lines[-1]
    assert last["tool"] == "label_print" and last["ok"] is True
    assert last["args"]["proof"] == proof and last["args"]["confirmed"] == "现场: 第一张齐了"


# ---- 缩写对照表: 只提议, 不写回 ----

def test_abbrev_propose_returns_diff_without_writing(tmp_path):
    path = tmp_path / "abbrev.json"
    path.write_text(json.dumps(_table(), ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    def load():
        return json.loads(path.read_text(encoding="utf-8")), str(path)

    s, _ = _server(table=load)
    d = _json(_call(s, "label_abbrev_propose", {"category": "device_type",
                                                "entries": {"负载均衡": "LB", "交换机": "SW"}}))
    assert d["ok"] is True and d["written"] is False and d["path"] == str(path)
    assert d["added"] == {"负载均衡": "LB"} and d["unchanged"] == ["交换机"] and d["changed"] == {}
    assert '+    "负载均衡": "LB"' in d["diff"]
    assert path.read_bytes() == before                                     # 绝不写回


@pytest.mark.parametrize("entries, frag", [
    ({"负载均衡": "负载"}, "ASCII"),
    ({"负载均衡": "X" * 12}, "超过"),
    ({"华为": "HUAWEI"}, "不一致"),                       # vendor 里已有 HW
])
def test_abbrev_propose_reports_problems(entries, frag):
    s, _ = _server()
    d = _json(_call(s, "label_abbrev_propose", {"category": "device_type", "entries": entries}))
    assert d["ok"] is False and any(frag in m for m in d["problems"])


def test_abbrev_propose_changed_value():
    s, _ = _server()
    d = _json(_call(s, "label_abbrev_propose", {"category": "device_type", "entries": {"交换机": "SWT"}}))
    assert d["changed"] == {"交换机": ["SW", "SWT"]}


@pytest.mark.parametrize("args, frag", [
    ({"category": "_limits", "entries": {"a": "b"}}, "category"),
    ({"category": "device_type", "entries": {}}, "entries"),
    ({"category": "device_type", "entries": ["a"]}, "entries"),
    ({"category": "device_type", "entries": {"a": 1}}, "entries"),
])
def test_abbrev_propose_bad_args(args, frag):
    s, _ = _server()
    assert frag in _err(_call(s, "label_abbrev_propose", args))


def test_abbrev_table_missing_is_tool_error():
    def load():
        raise mc.ToolError("没有缩写对照表")
    s, _ = _server(table=load)
    assert "对照表" in _err(_call(s, "label_preview", {"kind": "cable", "abbrev": True, "labels": [CABLE]}))
    assert "对照表" in _err(_call(s, "label_abbrev_propose", {"category": "role", "entries": {"x": "y"}}))


# ---- 真设备函数 (拿普通文件冒充, 不碰 /dev/usb/lp0) ----

def test_real_write_to_file(tmp_path):
    f = tmp_path / "lp0"
    f.write_bytes(b"")
    ml.real_write(b"CLS\r\n", str(f))
    assert f.read_bytes() == b"CLS\r\n"


def test_real_write_missing_device_never_creates_file(tmp_path):
    f = tmp_path / "lp0"
    with pytest.raises(mc.ToolError, match="不在"):
        ml.real_write(b"x", str(f))
    assert not f.exists()


def test_real_status_missing_and_silent(tmp_path):
    with pytest.raises(mc.ToolError, match="不在"):
        ml.real_status(str(tmp_path / "lp0"))
    f = tmp_path / "lp0"
    f.write_bytes(b"")
    with pytest.raises(mc.ToolError, match="没回"):
        ml.real_status(str(f), timeout=0.1)                               # 普通文件: 写了 ESC !? 读不回东西


def test_real_abbrev_prefers_local_table(tmp_path):
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"vendor": {"Acme": "AC"}}), encoding="utf-8")
    t, path = ml.real_abbrev(paths=(str(local), os.path.join(ROOT, "examples", "label_abbrev.json")))
    assert path == str(local) and t["vendor"] == {"Acme": "AC"}
    t, path = ml.real_abbrev(paths=(str(tmp_path / "none.json"), os.path.join(ROOT, "examples", "label_abbrev.json")))
    assert path.endswith("label_abbrev.json") and "vendor" in t
    with pytest.raises(mc.ToolError, match="对照表"):
        ml.real_abbrev(paths=(str(tmp_path / "none.json"),))
