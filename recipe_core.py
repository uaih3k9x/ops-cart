"""屏幕自动化配方的纯逻辑 (#17): OCR 容错匹配、步骤执行器、配方校验、内置配方.

截图 / OCR / 发键都由调用方注入 (recipe.py 接 cart_snap + cart_ocr + ch9329), 这里不碰硬件, 可离线测.
"""

import functools
import re
import unicodedata

# ---------------------------------------------------------------- OCR 容错匹配
# 只收 BIOS 点阵字里实际见过的误识: 1/I/l/| 互认, 0/O 互认. 别的 (S/5, B/8) 不并,
# 并多了就成了"随便都能对上".
_CONFUSE = str.maketrans({"i": "1", "l": "1", "|": "1", "o": "0"})
_ASCII_WORD = "a-z0-9"


def norm(s):
    """全角转半角、小写、误识字归一、空白并成一个空格."""
    s = unicodedata.normalize("NFKC", s or "").lower().translate(_CONFUSE)
    return " ".join(s.split())


@functools.lru_cache(maxsize=512)
def _regex(pattern):
    """模式 -> 正则 (作用在 norm 过的文字上). 字与字之间允许 OCR 插进来的空格;
    两头是英文字母/数字时要求整词 (F1 不能撞 F12, ESC 不能撞 Description)."""
    chars = [c for c in norm(pattern) if not c.isspace()]
    if not chars:
        return None
    body = r"\s*".join(re.escape(c) for c in chars)
    head = f"(?<![{_ASCII_WORD}])" if re.match(f"[{_ASCII_WORD}]", chars[0]) else ""
    tail = f"(?![{_ASCII_WORD}])" if re.match(f"[{_ASCII_WORD}]", chars[-1]) else ""
    return re.compile(head + body + tail)


def match(text, pattern):
    rx = _regex(pattern)
    return bool(rx and rx.search(norm(text)))


def find(lines, patterns, avoid=()):
    """按模式优先级找第一行 -> (行号, 命中的模式); 含 avoid 任一模式的行跳过. 没有返回 None."""
    for p in patterns:
        for i, line in enumerate(lines):
            text = line.get("text", "")
            if match(text, p) and not any(match(text, a) for a in avoid):
                return i, p
    return None


def pattern_ok(pattern):
    """模式至少两个有效字 (字母/数字/汉字), 太短的模式什么都能撞上."""
    return sum(1 for c in norm(pattern) if c.isalnum()) >= 2
