"""屏幕自动化配方的纯逻辑 (#17): OCR 容错匹配、步骤执行器、配方校验、内置配方.

截图 / OCR / 发键都由调用方注入 (recipe.py 接 cart_snap + cart_ocr + ch9329), 这里不碰硬件, 可离线测.
"""

import functools
import re
import time
import unicodedata

from ch9329 import Ch9329Busy

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


# ---------------------------------------------------------------- 执行器
# 配方 = {"name", "timeout": 总秒数, "steps": [步骤...]}; 步骤是 dict, "do" 选原语:
#   wait_text    patterns 或 prompts=[[提示, 键], ...] (按优先级), 看到就过; prompts 记下对应的键给 "$prompt"
#   press        key 按一下
#   press_until  key (可写 "$prompt") 每轮连按 burst 下 (间隔 interval), 再看一眼, 直到 patterns 出现
#   select_menu  patterns (按优先级) / avoid / skip_if, 在菜单里移到那一项再回车, 见 _select_menu
# 每步都有 timeout, 也受总超时管; 每次截图、命中、发键都经 log(dict) 记下来.

PROMPT_KEY = "$prompt"
POLL_S = 0.5                    # 看一眼没看到, 歇多久再看 (OCR 本身就要秒级)
STEP_TIMEOUT = 30
BUSY_WAIT_S = 1.0               # 键鼠被人占着: 隔多久再试
BUSY_MAX_S = 30.0               # 最多等这么久, 等不到就停, 不抢
MENU_GAP = 2.5                  # 菜单相邻两项的 y 间距上限 (倍行高), 再大就算不是同一块菜单
MENU_X_TOL = 4                  # 同一列菜单项 x 的容差 (倍行高), 高亮标记 "> " 会把行首往左推
# 菜单的标题/帮助行, 不算菜单项 (也挡住往上往下数); 以冒号结尾的行同样算标题 (Dell 的 "UEFI BOOT:")
MENU_CHROME = ["Menu", "Please select", "Select boot", "to move", "to select", "to exit", "to boot",
               "to change"]
_MARKER = re.compile(r"^\s*(?:->|>|»|►|▶|→|\*)")


class StepFailed(Exception):
    pass


class Runner:
    """snap() -> 截图路径; ocr(路径) -> [{text, x, y, w, h, conf}] (cart_ocr 的格式);
    send_key(键名) 发一个键, 被占用抛 Ch9329Busy, 别的 OSError 当键鼠坏了."""

    def __init__(self, snap, ocr, send_key, log=lambda ev: None, clock=time.monotonic, sleep=time.sleep,
                 busy_wait=BUSY_WAIT_S, busy_max=BUSY_MAX_S, dry_run=False):
        self.snap, self.ocr, self.send_key, self.log = snap, ocr, send_key, log
        self.clock, self.sleep = clock, sleep
        self.busy_wait, self.busy_max, self.dry_run = busy_wait, busy_max, dry_run
        self.step = None
        self.ctx = {}
        self.seen = []
        self.limit, self.limit_kind, self.deadline = 0.0, "", 0.0

    # ---- 基本动作
    def _ev(self, ev, **kw):
        self.log(dict(ev=ev, step=self.step, t=round(self.clock(), 2), **kw))

    def _look(self):
        try:
            shot = self.snap()
            lines = self.ocr(shot) or []
        except Exception as e:                          # 截图/OCR 出错: 看不见就不动
            raise StepFailed(f"截图/识别失败: {e}") from None
        self.seen = [l.get("text", "") for l in lines]
        self._ev("look", shot=shot, lines=len(lines))
        return shot, lines

    def _hit(self, shot, lines, patterns, avoid=()):
        h = find(lines, patterns, avoid)
        if h:
            self._ev("hit", shot=shot, text=lines[h[0]].get("text", ""), pattern=h[1])
        return h

    def _press(self, key):
        if self.dry_run:
            self._ev("key", key=key, dry_run=True)
            return
        waited = 0.0
        while True:
            try:
                self.send_key(key)
                self._ev("key", key=key)
                return
            except Ch9329Busy:
                if waited >= self.busy_max or self.clock() >= self.deadline:
                    raise StepFailed(f"键鼠被占用, 等了 {waited:.0f} 秒还没空出来, 不抢") from None
                self._ev("busy", key=key)
                self.sleep(self.busy_wait)
                waited += self.busy_wait
            except (OSError, ValueError) as e:          # 拔线 / 不认识的键
                raise StepFailed(f"键鼠出错: {e}") from None

    def _timeout(self, what):
        return StepFailed(f"{self.limit:.0f} 秒内{what} ({self.limit_kind})")

    def _expired(self):
        return self.clock() >= self.deadline

    # ---- 原语
    def _wait_text(self, step):
        prompts = step.get("prompts")
        patterns = [p for p, _k in prompts] if prompts else step["patterns"]
        while True:
            shot, lines = self._look()
            h = self._hit(shot, lines, patterns, step.get("avoid", ()))
            if h:
                if prompts:
                    key = dict((p, k) for p, k in prompts)[h[1]]
                    self.ctx["key"] = key
                    self._ev("prompt", key=key, text=lines[h[0]].get("text", ""))
                return
            if self._expired():
                raise self._timeout(f"没看到 {patterns}")
            self.sleep(step.get("poll", POLL_S))

    def _press_once(self, step):
        self._press(step["key"])

    def _press_until(self, step):
        key = step["key"]
        if key == PROMPT_KEY:
            key = self.ctx.get("key")
            if not key:
                raise StepFailed("前面没认出开机提示, 不知道该按哪个键")
        burst = 1 if key == "esc" else max(1, int(step.get("burst", 1)))    # ESC 连按会把菜单又关掉
        while True:
            for _ in range(burst):
                self._press(key)
                self.sleep(step.get("interval", POLL_S))
            shot, lines = self._look()
            if self._hit(shot, lines, step["patterns"], step.get("avoid", ())):
                return
            if self._expired():
                raise self._timeout(f"连按 {key} 也没看到 {step['patterns']}")

    def _select_menu(self, step):
        """找到目标项后算出它在菜单里是第几项, 再决定怎么移过去.
        高亮行怎么判断 (OCR 拿不到颜色):
          - OCR 读出了行首标记 (> » ► * ->) 且只有一行有: 从标记那行相对移动;
          - 读不出标记: 假设 Home 能回到第一项 (多数启动菜单如此; 不认 Home 的, 菜单刚打开时
            通常也停在第一项), 再按 N 次下;
          - 回车前再看一眼: 目标还在同一位置才回车; 读得出标记却不在目标上就停."""
        patterns, avoid = step["patterns"], step.get("avoid", ())
        while True:
            shot, lines = self._look()
            if step.get("skip_if") and self._hit(shot, lines, step["skip_if"]):
                self._ev("skip", reason="已经在走网络启动")
                return
            h = self._hit(shot, lines, patterns, avoid)
            if h:
                break
            if self._expired():
                raise self._timeout(f"菜单里没看到 {patterns}")
            self.sleep(step.get("poll", POLL_S))
        items, idx = menu_items(lines, h[0])
        mark = marker_index(items)
        self._ev("menu", shot=shot, index=idx, items=len(items), text=items[idx].get("text", ""),
                 how="home" if mark is None else "marker")
        if mark is None:
            moves = ["home"] + ["down"] * idx
        else:
            moves = ["down" if idx > mark else "up"] * abs(idx - mark)
        for k in moves:
            self._press(k)
            self.sleep(step.get("interval", 0.2))
        shot, lines = self._look()                      # 回车前核对
        h2 = find(lines, [h[1]], avoid)
        if not h2:
            raise StepFailed("移动后目标项不见了, 不回车")
        items2, idx2 = menu_items(lines, h2[0])
        mark2 = marker_index(items2)
        if (idx2, len(items2)) != (idx, len(items)):
            raise StepFailed(f"移动后菜单变了 (第 {idx2 + 1}/{len(items2)} 项, 原来 {idx + 1}/{len(items)}), 不回车")
        if mark2 is not None and mark2 != idx2:
            raise StepFailed(f"高亮停在第 {mark2 + 1} 项, 目标是第 {idx2 + 1} 项, 不回车")
        self._press("enter")

    STEPS = {"wait_text": _wait_text, "press": _press_once, "press_until": _press_until,
             "select_menu": _select_menu}

    def run(self, recipe):
        """-> {ok, step (失败在第几步, 从 0 数), reason}. 配方不合规直接抛 RecipeError, 一个键都不发."""
        validate(recipe)
        start = self.clock()
        total = float(recipe["timeout"])
        self._ev("start", name=recipe.get("name"), timeout=total, dry_run=self.dry_run)
        for i, step in enumerate(recipe["steps"]):
            self.step = i
            st = float(step.get("timeout", STEP_TIMEOUT))
            left = start + total - self.clock()
            if st < left:
                self.limit, self.limit_kind, self.deadline = st, "单步超时", self.clock() + st
            else:
                self.limit, self.limit_kind, self.deadline = total, "总超时", start + total
            self._ev("step", do=step["do"])
            try:
                if left <= 0:
                    raise StepFailed(f"总超时 {total:.0f} 秒, 第 {i + 1} 步没开始")
                self.STEPS[step["do"]](self, step)
            except StepFailed as e:
                self._ev("fail", reason=str(e), seen=self.seen[:20])
                return {"ok": False, "step": i, "reason": str(e)}
        self.step = None
        self._ev("done")
        return {"ok": True, "step": None, "reason": ""}


def _is_chrome(line):
    text = line.get("text", "").strip()
    return (sum(1 for c in norm(text) if c.isalnum()) < 2 or text.endswith(":")
            or any(match(text, p) for p in MENU_CHROME))


def menu_items(lines, t):
    """目标行 t 所在的那一列菜单 -> (菜单项列表, 目标是第几项).
    同一列 = x 接近; 从目标往上往下连着数, 碰到标题/分隔线/帮助行或空出一大段就停."""
    target = lines[t]
    h = max(1, target.get("h", 16))
    col = sorted((l for l in lines if abs(l.get("x", 0) - target.get("x", 0)) <= MENU_X_TOL * h),
                 key=lambda l: l.get("y", 0))
    k = next(i for i, l in enumerate(col) if l is target)

    def walk(rng):
        out, prev = [], target
        for i in rng:
            l = col[i]
            if abs(l.get("y", 0) - prev.get("y", 0)) > MENU_GAP * h or _is_chrome(l):
                break
            out.append(l)
            prev = l
        return out
    above = walk(range(k - 1, -1, -1))[::-1]
    below = walk(range(k + 1, len(col)))
    return above + [target] + below, len(above)


def marker_index(items):
    """恰好一项行首带高亮标记 -> 它的序号; 没有或不止一项 -> None (读不出来)."""
    marked = [i for i, l in enumerate(items) if _MARKER.match(l.get("text", ""))]
    return marked[0] if len(marked) == 1 else None


# ---------------------------------------------------------------- 配方校验
# 第一版只进启动菜单, 不改 BIOS 设置: 配方里任何地方出现保存类字眼一律拒绝 (看的也不行),
# 键只收白名单 (导航 + 各厂商的启动菜单键). F10 是保存键, Intel NUC 的启动菜单恰好也是 F10, 不支持.

ALLOWED_KEYS = frozenset({"enter", "esc", "up", "down", "home", "end", "pgup", "pgdn",
                          "f8", "f9", "f11", "f12"})
FORBIDDEN = ["save", "f10", "保存", "load default", "恢复默认"]
MAX_TOTAL_S = 900
MAX_STEPS = 30
_FIELDS = {
    "wait_text": {"patterns", "prompts", "avoid", "timeout", "poll"},
    "press": {"key", "timeout"},
    "press_until": {"key", "patterns", "avoid", "interval", "burst", "timeout"},
    "select_menu": {"patterns", "avoid", "skip_if", "interval", "poll", "timeout"},
}
_REQUIRED = {"press": {"key"}, "press_until": {"key", "patterns"}, "select_menu": {"patterns"}}


class RecipeError(ValueError):
    pass


def _flat(s):
    return norm(s).replace(" ", "")


_FORBIDDEN_FLAT = [_flat(w) for w in FORBIDDEN]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise RecipeError(f"{what} 要在 {lo}~{hi} 之间, 现在是 {v!r}")


def _patterns(v, what):
    if not isinstance(v, list) or not v:
        raise RecipeError(f"{what} 要是非空列表")
    for p in v:
        if not isinstance(p, str) or not pattern_ok(p):
            raise RecipeError(f"{what} 里的 {p!r} 太短或不是文字, 会什么都撞上")


def _key(k, what):
    if not isinstance(k, str) or k not in ALLOWED_KEYS:
        raise RecipeError(f"{what}: 不认识/不许发的键 {k!r} (只许 {', '.join(sorted(ALLOWED_KEYS))})")


def validate(recipe):
    """不合规抛 RecipeError, 合规原样返回. 宁可错拒, 不可放过."""
    if not isinstance(recipe, dict):
        raise RecipeError("配方要是一个对象")
    for s in _strings(recipe):
        f = _flat(s)
        for w, fw in zip(FORBIDDEN, _FORBIDDEN_FLAT):
            if fw in f:
                raise RecipeError(f"配方里出现保存类字眼 {w!r} ({s!r}), 第一版不做改设置的配方")
    extra = set(recipe) - {"name", "timeout", "steps"}
    if extra:
        raise RecipeError(f"配方里有不认识的字段 {sorted(extra)}")
    if not isinstance(recipe.get("name", ""), str):
        raise RecipeError("name 要是文字")
    _num(recipe.get("timeout"), 1, MAX_TOTAL_S, "总超时 timeout")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not 0 < len(steps) <= MAX_STEPS:
        raise RecipeError(f"steps 要是 1~{MAX_STEPS} 步的列表")
    have_prompt = False
    for i, st in enumerate(steps, 1):
        where = f"第 {i} 步"
        if not isinstance(st, dict) or st.get("do") not in _FIELDS:
            raise RecipeError(f"{where}: 不认识的步骤 {st!r}")
        do = st["do"]
        extra = set(st) - _FIELDS[do] - {"do"}
        missing = _REQUIRED.get(do, set()) - set(st)
        if extra or missing:
            raise RecipeError(f"{where} {do}: 多了字段 {sorted(extra)} / 缺字段 {sorted(missing)}")
        if "timeout" in st:
            _num(st["timeout"], 0.1, MAX_TOTAL_S, f"{where} timeout")
        for f in ("interval", "poll"):
            if f in st:
                _num(st[f], 0.1, 10, f"{where} {f}")
        if "burst" in st:
            _num(st["burst"], 1, 10, f"{where} burst")
        for f in ("patterns", "avoid", "skip_if"):
            if f in st:
                _patterns(st[f], f"{where} {f}")
        if do == "wait_text":
            if ("patterns" in st) == ("prompts" in st):
                raise RecipeError(f"{where} wait_text: patterns 和 prompts 二选一")
            if "prompts" in st:
                pr = st["prompts"]
                if not isinstance(pr, list) or not pr or not all(
                        isinstance(x, (list, tuple)) and len(x) == 2 for x in pr):
                    raise RecipeError(f"{where} prompts 要是 [[提示, 键], ...]")
                _patterns([p for p, _k in pr], f"{where} prompts")
                for _p, k in pr:
                    _key(k, f"{where} prompts")
                have_prompt = True
        if "key" in st:
            if st["key"] == PROMPT_KEY and do == "press_until":
                if not have_prompt:
                    raise RecipeError(f"{where}: 用 $prompt 之前要有带 prompts 的 wait_text")
            else:
                _key(st["key"], where)
    return recipe


def load_json(text):
    import json
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise RecipeError(f"配方不是合法 JSON: {e}") from None
    return validate(obj)


# ---------------------------------------------------------------- 内置配方
# 开机提示 -> 启动菜单键, 按优先级 (一屏里 F12 / F11 都有时先认 F12). 都是网上和手册里的常见说法,
# 推车实拍校准过的再在这里注明.
BOOT_PROMPTS = [
    ("F12", "f12", "Dell / Lenovo / Acer / Gigabyte / VMware; Supermicro 的 F12 直接网络启动"),
    ("F11", "f11", "MSI / ASRock / Supermicro, AMI 的 BBS Popup"),
    ("F8", "f8", "ASUS 台式主板"),
    ("F9", "f9", "HP Boot Device Options"),
    ("ESC", "esc", "HP Startup Menu / ASUS 笔记本; 有的 AMI 是 DEL/ESC 进 Setup, 所以排最后"),
]
# 启动菜单的标题; 带 Press / 键名的行 (开机提示本身, 如 "ESC for Boot Menu") 不算
MENU_TITLES = ["Please select boot device", "Select boot device", "Boot Menu", "Boot Option Menu",
               "Boot Options", "Boot Device Options", "Boot Manager", "UEFI BOOT", "启动菜单"]
PROMPT_LINE = ["Press", "F12", "F11", "F8", "F9", "ESC", "DEL"]
# 已经在走网络启动的迹象 (有的机器按 F12 直接网络启动, 不出菜单)
PXE_PROGRESS = ["Start PXE over", "PXE-E", "DHCP", "Checking Media", "NBP", "Station IP", "CLIENT MAC",
                "iPXE", "GNU GRUB"]
NET_ITEMS = ["IPv4", "PXE", "Network", "NIC"]       # 菜单里找网络启动项, 按优先级
NET_AVOID = ["IPv6", "HTTP"]


def boot_pxe(timeout=120, key=None):
    """盯开机提示 -> 狂按对应的启动菜单键 -> 菜单里选网络启动 -> 等 PXE 动起来.
    key 给了就不认提示, 上电就按这个键 (提示太快、OCR 来不及看时用)."""
    steps = []
    if key is None:
        steps.append({"do": "wait_text", "prompts": [[p, k] for p, k, _who in BOOT_PROMPTS], "timeout": 90})
    steps += [
        {"do": "press_until", "key": key or PROMPT_KEY, "patterns": MENU_TITLES + PXE_PROGRESS,
         "avoid": PROMPT_LINE, "interval": 0.3, "burst": 3, "timeout": 45},
        {"do": "select_menu", "patterns": NET_ITEMS, "avoid": NET_AVOID, "skip_if": PXE_PROGRESS,
         "timeout": 20},
        {"do": "wait_text", "patterns": PXE_PROGRESS, "timeout": 60},
    ]
    return validate({"name": "boot-pxe", "timeout": timeout, "steps": steps})


BUILTIN = {"boot-pxe": boot_pxe}


def builtin(name, **kw):
    if name not in BUILTIN:
        raise RecipeError(f"没有内置配方 {name!r} (有: {', '.join(BUILTIN)})")
    return BUILTIN[name](**kw)
