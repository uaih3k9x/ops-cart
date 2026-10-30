"""cartlog 测试: 机器事件日志 + CART-LOG 一行摘要."""

import time

import cartlog

NOW = time.mktime((2026, 10, 22, 14, 3, 0, 0, 0, -1))


def test_event_appends_line(tmp_path):
    p = tmp_path / "sub" / "events.log"                    # 目录不存在也能写
    cartlog.event("netprof", "eno1 → 临时", path=str(p), now=NOW)
    cartlog.event("netprof", "第二行\n带换行", path=str(p), now=NOW)
    assert p.read_text().splitlines() == [
        "2026-10-22 14:03:00 [netprof] eno1 → 临时",
        "2026-10-22 14:03:00 [netprof] 第二行 带换行"]


def test_event_never_raises(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("")
    assert cartlog.event("x", "y", path=str(bad / "events.log"), now=NOW) is False


def test_summary_goes_to_cart_log_table(tmp_path):
    p = tmp_path / "CART-LOG.md"
    p.write_text("# 日志\n| 日期 | 谁 | 事 |\n")
    cartlog.summary("机房模式 18 分钟 → /var/log/ops-cart/reports/x.md", who="kiosk", path=str(p), now=NOW)
    assert p.read_text().splitlines()[-1] == \
        "| 2026-10-22 | kiosk | 14:03 机房模式 18 分钟 → /var/log/ops-cart/reports/x.md |"


def test_summary_escapes_pipes(tmp_path):
    p = tmp_path / "CART-LOG.md"
    cartlog.summary("a|b", path=str(p), now=NOW)
    assert "a\\|b" in p.read_text()


def test_fields_formats_key_values_on_one_line():
    line = cartlog.fields(act="login", src="192.0.2.1", ok=True, note="a b\nc", n=3, none=None)
    assert line == 'act=login src=192.0.2.1 ok=true note="a b c" n=3'


# ---- CART-LOG 滚动: 老条目挪进归档, 手册部分不动, 做梦的读取位置跟着挪 ----

HEAD = "# 手册\n\n## 待办\n\n- [ ] x\n\n## 日志\n\n| 日期 | 谁 | 做了什么 | 回滚 |\n| --- | --- | --- | --- |\n"
ROWS = ["| 2026-10-18 | a | 一 | — |", "| 2026-10-20 | a | 二 | — |",
        "| 2026-10-27 | a | 三 | — |", "| 2026-10-30 | a | 四 | — |"]


def _log(rows=ROWS):
    return (HEAD + "\n".join(rows) + "\n").encode()


def test_roll_moves_old_rows_keeps_manual():
    new, old_rows, off = cartlog.roll(_log(), "2026-10-26", offset=len(_log()))
    text = new.decode()
    assert text.startswith("# 手册\n\n## 待办\n\n- [ ] x\n\n## 日志\n")
    assert "| 2026-10-27 |" in text and "| 2026-10-30 |" in text
    assert "| 2026-10-18 |" not in text and "| 2026-10-20 |" not in text
    assert old_rows == ROWS[:2]
    assert cartlog.ARCHIVE_NOTE in text                     # 指路: 更早的在归档里
    assert off == len(new)                                  # 全读过 -> 新文件也全读过


def test_roll_keeps_unread_tail_and_shifts_offset():
    data = _log()
    unread_from = data.index(b"| 2026-10-27")               # 做梦只读到 10-20
    new, old_rows, off = cartlog.roll(data, "2026-10-26", offset=unread_from)
    assert new[off:] == data[unread_from:]                  # 没读过的原样留着, 位置对上


def test_roll_never_archives_unread_rows():
    data = _log()
    unread_from = data.index(b"| 2026-10-20")               # 10-20 还没被整理
    new, old_rows, off = cartlog.roll(data, "2026-10-26", offset=unread_from)
    assert old_rows == ROWS[:1]
    assert b"| 2026-10-20 |" in new and new[off:] == data[unread_from:]


def test_roll_note_added_once():
    new, _, off = cartlog.roll(_log(), "2026-10-19", offset=len(_log()))
    again, rows, _ = cartlog.roll(new, "2026-10-21", offset=off)
    assert again.decode().count(cartlog.ARCHIVE_NOTE) == 1 and rows == [ROWS[1]]


def test_roll_nothing_to_do():
    new, rows, off = cartlog.roll(_log(), "2026-10-01", offset=7)
    assert new == _log() and rows == [] and off == 7


def test_roll_without_log_section_raises():
    import pytest
    with pytest.raises(ValueError):
        cartlog.roll(b"# no table\n", "2026-10-26", offset=0)


def test_archive_text_appends(tmp_path):
    first = cartlog.archive_text("", ROWS[:1])
    assert first.startswith("# CART-LOG 归档") and "| --- |" in first and first.endswith(ROWS[0] + "\n")
    both = cartlog.archive_text(first, ROWS[1:2])
    assert both.count("| 日期 |") == 1 and both.endswith(ROWS[1] + "\n")


def test_roll_files_end_to_end(tmp_path):
    log, state, arch = tmp_path / "CART-LOG.md", tmp_path / "dream.json", tmp_path / "cart-log"
    data = _log()
    log.write_bytes(data)
    unread_from = data.index(b"| 2026-10-30")
    state.write_text('{"offset": %d, "at": "x"}' % unread_from)
    msg = cartlog.roll_files("2026-10-26", log=str(log), state=str(state), archive_dir=str(arch))
    assert "2 条" in msg
    new = log.read_bytes()
    import json
    off = json.loads(state.read_text())["offset"]
    assert new[off:] == data[unread_from:]
    files = list(arch.iterdir())
    assert len(files) == 1 and files[0].name == "CART-LOG-2026-10.md"
    assert "| 2026-10-18 |" in files[0].read_text()
