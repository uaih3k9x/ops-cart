"""pxe_log_core (#24 后续): 读 pxe-req.log + events.log 的 [pxe] 行, 按 MAC 分组, 给网页只读显示."""

import json

import cartlog
import mcp_pxe as mp
import pxe_log_core as pl

M1 = "02:00:00:00:00:01"
M2 = "02:00:00:00:00:02"
M3 = "02:00:00:00:00:03"


def _jsonl(path, *recs):
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def _events(path, *lines):
    with open(path, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def _paths(tmp_path):
    return str(tmp_path / "pxe-req.log"), str(tmp_path / "events.log")


# ------------------------------------------------------------ 读尾部

def test_req_log_path_matches_mcp_pxe():
    assert pl.REQ_LOG == mp.LOG_PATH and pl.EVENTS_LOG == cartlog.EVENTS


def test_missing_files_give_empty(tmp_path):
    out = pl.load(*_paths(tmp_path))
    assert out == {"groups": [], "unlinked": []}


def test_tail_only_reads_last_bytes_and_drops_partial_line(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line{i:04d}\n" for i in range(100)))
    lines = pl.tail_lines(str(p), max_bytes=35)
    assert lines == ["line0097", "line0098", "line0099"]
    assert pl.tail_lines(str(tmp_path / "nope")) == []


def test_tail_survives_bad_bytes(tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes(b"ok\n\xff\xfe\n")
    assert pl.tail_lines(str(p))[0] == "ok"


# ------------------------------------------------------------ MAC

def test_macs_in_text():
    assert pl.macs_in(f"装机模式解锁 mac={M1.upper()} 30 分钟") == [M1]
    assert pl.macs_in("删掉 grub.cfg-01-02-00-00-00-00-02, grub.cfg") == [M2]
    assert pl.macs_in(f"{M1} img -> dev, {M2} img -> dev; 不放行 {M1} (x)") == [M1, M2]
    assert pl.macs_in("from 192.0.2.50 端口 eno1") == []
    assert pl.macs_in("02:00:00:00:00:01:02:03") == []        # 太长, 不是 MAC  public-safe-ignore


# ------------------------------------------------------------ 分组

def test_groups_by_mac_newest_first(tmp_path):
    req, ev = _paths(tmp_path)
    _jsonl(req,
           {"t": "2026-10-30T10:00:00", "ev": "request", "id": 1, "who": "stdio:ops", "purpose": "rocky",
            "macs": [M1.upper()]},
           {"t": "2026-10-30T10:01:00", "ev": "confirm", "id": 1, "who": "stdio:ops", "by": "human:kiosk",
            "purpose": "rocky", "macs": [M1]},
           {"t": "2026-10-30T11:00:00", "ev": "request", "id": 2, "who": "stdio:ops", "purpose": "clone-restore",
            "macs": [M2, M3]})
    _events(ev,
            f"2026-10-30 10:01:00 [pxe] 装机模式解锁 mac={M1} 30 分钟; 用途 rocky (自动装机)",
            "2026-10-30 10:02:00 [kvm] 跟 PXE 无关",
            f"2026-10-30 10:20:00 [pxe] 装机菜单被取走 mac={M1} from 192.0.2.50",
            "2026-10-30 10:30:00 [pxe] 装机模式上锁, 只剩救援菜单",
            f"2026-10-30 10:31:00 [pxe-req] ev=request id=9 macs={M3}")    # [pxe-req] 是 JSONL 的复写, 不重复收
    out = pl.load(req, ev)
    assert [g["mac"] for g in out["groups"]] == [M2, M3, M1]
    g1 = out["groups"][2]
    assert g1["last"] == "2026-10-30 10:20:00"
    assert [(e["src"], e["ev"]) for e in g1["events"]] == [("pxe", "pxe"), ("req", "confirm"), ("pxe", "pxe"),
                                                           ("req", "request")]
    assert g1["events"][1]["by"] == "human:kiosk" and g1["events"][1]["what"] == pl.EV_LABEL["confirm"]
    assert out["groups"][0]["events"][0]["macs"] == [M2, M3]
    assert [e["msg"] for e in out["unlinked"]] == ["装机模式上锁, 只剩救援菜单"]


def test_bad_code_without_macs_joins_its_request(tmp_path):
    req, ev = _paths(tmp_path)
    _jsonl(req,
           {"t": "2026-10-30T10:00:00", "ev": "request", "id": 4, "who": "a", "purpose": "winpe", "macs": [M1]},
           {"t": "2026-10-30T10:00:30", "ev": "bad_code", "id": 4, "who": "a"},
           {"t": "2026-10-30T10:00:40", "ev": "bad_code", "id": 77, "who": "a"},
           {"t": "2026-10-30T10:00:50", "ev": "corrupt", "why": "状态文件读不懂"})
    out = pl.load(req, ev)
    assert [e["ev"] for e in out["groups"][0]["events"]] == ["bad_code", "request"]
    assert [e["ev"] for e in out["unlinked"]] == ["corrupt", "bad_code"]


def test_bad_lines_skipped_fields_whitelisted(tmp_path):
    req, ev = _paths(tmp_path)
    _jsonl(req, "{not json", "[1, 2]", json.dumps({"ev": "request"}),
           {"t": "2026-10-30T10:00:00", "ev": "request", "id": 1, "who": "a", "purpose": "rocky", "macs": [M1, "zz"],
            "code": "424242", "digest": "deadbeefdeadbeef", "lines": ["x"], "extra": {"code": "1"}},
           {"t": "2026-10-30T10:00:01", "ev": "drop", "id": True, "macs": "not-a-list", "why": ["x"]})
    _events(ev, "垃圾行", "2026-10-30 10:00:00 [pxe]")
    out = pl.load(req, ev)
    text = json.dumps(out, ensure_ascii=False)
    assert "424242" not in text and "deadbeef" not in text and "code" not in text and "digest" not in text
    e = out["groups"][0]["events"][0]
    assert set(e) <= set(pl.FIELDS) and e["macs"] == [M1]
    drop = out["unlinked"][0]
    assert drop["ev"] == "drop" and "id" not in drop and "why" not in drop


def test_long_values_clipped_and_counts_capped(tmp_path):
    req, ev = _paths(tmp_path)
    _jsonl(req, *({"t": f"2026-10-30T10:{i // 60:02d}:{i % 60:02d}", "ev": "request", "id": i, "who": "x" * (pl.TEXT_MAX + 50),
                   "purpose": "rocky", "macs": [M1]} for i in range(pl.MAX_EVENTS + 20)))
    g = pl.load(req, ev)["groups"][0]
    assert len(g["events"]) == pl.MAX_EVENTS and g["events"][0]["id"] == pl.MAX_EVENTS + 19
    assert len(g["events"][0]["who"]) == pl.TEXT_MAX
