import json

import cart_dream as cd
import kiosk_core as kc


def test_new_part_from_offset_and_reset_on_shrink(tmp_path):
    log = tmp_path / "CART-LOG.md"
    log.write_text("| a |\n| b |\n")
    text, end = cd.new_part(str(log), 0)
    assert text == "| a |\n| b |\n" and end == len(log.read_bytes())
    with open(log, "a") as f:
        f.write("| c |\n")
    assert cd.new_part(str(log), end) == ("| c |\n", end + 6)
    log.write_text("| x |\n")                       # 被换成短文件: 从头读
    assert cd.new_part(str(log), end + 6)[0] == "| x |\n"


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "s" / "dream.json")
    assert cd.load_offset(p) == 0
    cd.save_offset(p, 123)
    assert cd.load_offset(p) == 123


def test_split_output_headline_and_body():
    head, body = cd.split_output("\n## **推车上了 1.07, 风扇静音**\n\n## 做了什么\n- x\n")
    assert head == "推车上了 1.07, 风扇静音"
    assert body.startswith("## 做了什么")
    assert cd.split_output("")[0] == ""
    assert len(cd.split_output("长" * 100)[0]) == cd.HEADLINE_MAX


def test_claude_cmd_has_no_tools_and_budget():
    cmd = cd.claude_cmd("/opt/claude/bin/claude", "u")
    assert cmd[:4] == ["runuser", "-u", "u", "--"]
    i = cmd.index("--tools")
    assert cmd[i + 1] == ""
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert "--max-budget-usd" in cmd


def test_claude_cmd_custom_prompt_budget_extra():
    """闸门第 2 层 (#22) 复用: 换提示词、预算, 追加参数; 不给工具这几条不变."""
    cmd = cd.claude_cmd("/c", "u", prompt="判一下", budget="0.2", extra=["--output-format", "json"])
    assert cmd[cmd.index("-p") + 1] == "判一下"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.2"
    assert cmd[-2:] == ["--output-format", "json"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert cd.claude_cmd("/c", "u")[cd.claude_cmd("/c", "u").index("-p") + 1] == cd.PROMPT


def test_run_writes_dream_and_latest(tmp_path):
    log = tmp_path / "CART-LOG.md"
    log.write_text("| 2026-10-27 | Claude | 修了冻屏 | 回滚 |\n")
    out_dir, state = tmp_path / "dreams", str(tmp_path / "dream.json")
    seen = {}

    def fake_llm(prompt, text):
        seen["text"] = text
        return "修了冻屏\n\n## 做了什么\n- 修了冻屏\n"
    r = cd.run(str(log), state, str(out_dir), llm=fake_llm, today="2026-10-28")
    assert r == "ok" and "修了冻屏" in seen["text"]
    assert (out_dir / "2026-10-28.md").read_text().startswith("# 推车整理 2026-10-28")
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest == {"date": "2026-10-28", "headline": "修了冻屏", "file": "2026-10-28.md"}
    assert cd.run(str(log), state, str(out_dir), llm=fake_llm, today="2026-10-29") == "nothing new"


def test_run_keeps_offset_when_llm_fails(tmp_path):
    log = tmp_path / "CART-LOG.md"
    log.write_text("| x |\n")
    state = str(tmp_path / "dream.json")

    def boom(prompt, text):
        raise RuntimeError("relay down")
    assert cd.run(str(log), state, str(tmp_path / "d"), llm=boom, today="2026-10-28").startswith("failed")
    assert cd.load_offset(state) == 0                # 下次重试同一段


def test_dream_line_for_lock_screen(tmp_path):
    p = tmp_path / "latest.json"
    assert kc.dream_line(str(p)) == ""
    p.write_text(json.dumps({"date": "2026-10-28", "headline": "修了冻屏", "file": "x.md"}))
    assert kc.dream_line(str(p)) == "昨夜整理 10-28 · 修了冻屏"
    p.write_text("garbage")
    assert kc.dream_line(str(p)) == ""
