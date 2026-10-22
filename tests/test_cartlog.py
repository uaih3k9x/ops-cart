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
