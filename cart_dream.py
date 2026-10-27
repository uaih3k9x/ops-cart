#!/usr/bin/env python3
"""推车"做梦": 每晚把 CART-LOG.md 自上次以来的新条目交给推车上的 Claude Code 整理成小结.

- 只喂新增部分 (状态文件记字节位置); 模型失败不前进, 下次重试同一段
- 模型不给任何工具 (--tools ""), 不加载 MCP, 不存会话, 有花费上限: 只读入、只写出文字
- 结果: /opt/pxe/dreams/<日期>.md + latest.json (锁屏显示一句标题, 见 kiosk_core.dream_line)
由 cart-dream.timer 每天 02:00 以 root 跑, 模型以 serial.conf 里的 user 身份调 (用它的 ~/.claude 配置).
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import atomicfile
from serial_panel import serial_conf_read

CART_LOG = "/opt/pxe/CART-LOG.md"
STATE = "/var/lib/ops-cart/dream.json"
DREAMS = "/opt/pxe/dreams"
HEADLINE_MAX = 40
MAX_INPUT = 60000          # 字符; 积压太多只取最后这么多
BUDGET_USD = "1"
TIMEOUT_S = 600

PROMPT = """标准输入里是运维推车维护手册 CART-LOG 自上次整理以来新增的条目 (Markdown 表格行: 日期 | 谁 | 做了什么 | 回滚).
请用中文写一份简短整理, 严格按这个格式:

第一行: 一句话标题, 不超过 30 个字, 概括最值得注意的事 (会显示在锁屏上). 不要加任何前缀、引号或符号.
空一行, 然后:
## 做了什么
(按主题合并, 每条一行, 用 - 开头)
## 没收尾的
(明确提到的待办、未验证、未推送、重启会丢的东西; 没有就写 "无")
## 风险提醒
(可能出问题的地方; 没有就写 "无")

只根据给出的内容写, 不要编造, 不要给操作建议之外的寒暄."""


def new_part(path, offset):
    """(新增文本, 新的结束位置). 文件比记录的位置短 (被换掉/截断) 就从头读."""
    data = open(path, "rb").read()
    if offset > len(data):
        offset = 0
    return data[offset:].decode("utf-8", errors="replace"), len(data)


def load_offset(path=STATE):
    try:
        return int(json.load(open(path))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def save_offset(path, offset):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomicfile.write_json(path, {"offset": offset, "at": time.strftime("%Y-%m-%d %H:%M:%S")})


def split_output(text):
    """模型输出 -> (锁屏标题, 正文). 标题去掉 Markdown 记号并限长."""
    lines = text.strip().splitlines()
    if not lines:
        return "", ""
    head = lines[0].strip().lstrip("#").strip().strip("*").strip()
    return head[:HEADLINE_MAX], "\n".join(lines[1:]).strip()


def claude_cmd(claude=None, user=None):
    user = user or serial_conf_read()["user"]
    claude = claude or f"/home/{user}/.local/bin/claude"
    return ["runuser", "-u", user, "--", "env", f"HOME=/home/{user}",
            f"PATH=/home/{user}/.local/bin:/usr/bin:/bin",
            claude, "-p", PROMPT, "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--max-budget-usd", BUDGET_USD]


def claude_llm(prompt, text):
    """真调模型: 在空的临时目录里跑, 输入走 stdin."""
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run(claude_cmd(), input=text, capture_output=True, text=True,
                           timeout=TIMEOUT_S, cwd=cwd)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def run(log=CART_LOG, state=STATE, out_dir=DREAMS, llm=claude_llm, today=None):
    today = today or time.strftime("%Y-%m-%d")
    offset = load_offset(state)
    text, end = new_part(log, offset)
    if not text.strip():
        return "nothing new"
    try:
        head, body = split_output(llm(PROMPT, text[-MAX_INPUT:]))
    except Exception as ex:          # noqa: BLE001  失败不前进, 下次重试
        return f"failed: {ex}"
    os.makedirs(out_dir, exist_ok=True)
    name = f"{today}.md"
    atomicfile.write_text(os.path.join(out_dir, name),
                          f"# 推车整理 {today}\n\n**{head}**\n\n{body}\n\n"
                          f"<!-- CART-LOG 字节 {offset}-{end} -->\n")
    atomicfile.write_json(os.path.join(out_dir, "latest.json"),
                          {"date": today, "headline": head, "file": name})
    save_offset(state, end)
    return "ok"


if __name__ == "__main__":
    res = run()
    print(res)
    sys.exit(0 if res in ("ok", "nothing new") else 1)
