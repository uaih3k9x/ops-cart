"""mcp_label: 推车 MCP 的标签工具 (issue #20). 模型提议 -> 代码校验 -> 预览 -> 打一张 -> 人确认 -> 批量.

- label_preview: 查 ASCII / cart_label.LIMITS 长度 / tspl_preview.check 出界和对折, 回检查结果 + SVG (文本)
- label_print: 校验同上, 不过就拒; 打之前读一次状态字节, 不正常就拒.
  默认只许 1 张; 批量 (>1) 要带上一次单张打印回的 proof (一次性, PROOF_TTL 秒内, 绑版面) + 人确认的原话
- label_printer_status: ESC !? 读一个状态字节, 拆成 开盖/缺纸/缺碳带...
- label_abbrev_propose: 缩写对照表只提议、回 diff, 不写回 (写回留给人)
打印机写入和状态读取都是注入的函数, 测试不碰 /dev/usb/lp0.
"""

import copy
import difflib
import json
import os
import secrets
import select
import threading
import time

import cart_label as cl
import mcp_core as mc
import tspl_preview as tp

PRINTER = "/dev/usb/lp0"
ABBREV_PATHS = ("/etc/ops-cart/label_abbrev.json",          # 推车本地 (可含真实资产名), 有就用它
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "label_abbrev.json"))
MAX_LABELS = 30                 # 一次最多这么多张; 再多分几次打
PREVIEW_SVG_MAX = 3             # 预览只画前几张, 回包别太大
PROOF_TTL = 600                 # 单张打完 10 分钟内人看过就能批量
STATUS_TIMEOUT = 2.0
SHORT_ID_MAX = 99999            # E-00000 ~ E-99999

# TSPL 手册 <ESC>!? 状态字节: 一位一种情况, 可能多位同时置
STATUS_BITS = ((0x01, "开盖"), (0x02, "卡纸"), (0x04, "缺纸"), (0x08, "缺碳带"),
               (0x10, "暂停"), (0x20, "打印中"), (0x40, "开盖"), (0x80, "其它故障"))
STATUS_BUSY_OK = 0x20           # 只有"打印中"不算故障: 打印机自己排队

# 每种版面的字段 -> cart_label.check_text 的 kind
FIELDS = {"cable": (("here", "cable_here"), ("there", "cable_there")),
          "text": (("big", "tag_big"), ("small", "tag_small"))}


# ------------------------------------------------------------ 状态字节

def decode_status(b):
    flags = []
    for bit, name in STATUS_BITS:
        if b & bit and name not in flags:
            flags.append(name)
    return {"byte": f"0x{b:02X}", "ok": not (b & ~STATUS_BUSY_OK), "flags": flags}


# ------------------------------------------------------------ 参数 / 校验

def _labels(kind, raw):
    """labels 参数 -> 干净的 [dict]; 结构不对抛 ArgError (内容长度/ASCII 另查)."""
    if kind not in FIELDS:
        raise mc.ArgError(f"kind 只能是 {sorted(FIELDS)}, 给的是 {kind!r}")
    if not isinstance(raw, list):
        raise mc.ArgError("labels 要是数组")
    if not raw:
        raise mc.ArgError("labels 至少一张")
    if len(raw) > MAX_LABELS:
        raise mc.ArgError(f"一次最多 {MAX_LABELS} 张, 给了 {len(raw)} 张; 分几次打")
    keys = [f for f, _ in FIELDS[kind]] + (["shortId"] if kind == "cable" else [])
    out = []
    for k, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise mc.ArgError(f"第 {k} 张要是对象")
        extra = sorted(set(r) - set(keys))
        if extra:
            raise mc.ArgError(f"第 {k} 张有不认识的字段: {', '.join(extra)} ({kind} 只要 {', '.join(keys)})")
        for f in keys:
            if f not in r:
                raise mc.ArgError(f"第 {k} 张缺 {f}")
        if kind == "cable":
            sid = r["shortId"]
            if isinstance(sid, bool) or not isinstance(sid, int) or not 0 <= sid <= SHORT_ID_MAX:
                raise mc.ArgError(f"第 {k} 张 shortId 要 0~{SHORT_ID_MAX} 的整数, 给的是 {sid!r}")
        for f, _ in FIELDS[kind]:
            if not isinstance(r[f], str):
                raise mc.ArgError(f"第 {k} 张 {f} 要字符串, 给的是 {r[f]!r}")
        out.append({f: r[f] for f in keys})
    return out


def _field_problems(kind, labels):
    probs = []
    for k, r in enumerate(labels, 1):
        for f, ck in FIELDS[kind]:
            try:
                cl.check_text(r[f], ck)
            except ValueError as e:
                probs.append(f"第 {k} 张 {f}: {e}")
    return probs


def _tspl(kind, labels):
    if kind == "cable":
        return cl.to_tspl(cl.cable_labels(labels)).encode()
    return cl.to_tspl(cl.text_labels([(r["big"], r["small"]) for r in labels])).encode()


def render(kind, labels, guides):
    """-> (问题清单, TSPL 字节或 None, 解析后的张). 字段不过不渲染; 渲染了再查出界和对折."""
    probs = _field_problems(kind, labels)
    if probs:
        return probs, None, []
    try:
        data = _tspl(kind, labels)
    except ValueError as e:                     # cart_label 自己的出界判断
        return [str(e)], None, []
    parsed = tp.parse(data)
    return [f"第 {k} 张 {m}" for k, m in tp.check(parsed, guides)], data, parsed


def _abbrev_labels(kind, labels, table):
    return [dict(r, **{f: cl.abbreviate(r[f], table) for f, _ in FIELDS[kind]}) for r in labels]


def _layout(kind):
    """proof 绑的版面: 种类 + 校准常量 (X0/Y0 改了就得重新打样)."""
    return f"{kind}/X0={cl.X0}/Y0={cl.Y0}"


# ------------------------------------------------------------ 缩写提议

def _diff(old, new):
    a = json.dumps(old, ensure_ascii=False, indent=2).splitlines()
    b = json.dumps(new, ensure_ascii=False, indent=2).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "现在", "提议", lineterm="", n=2))


def propose_abbrev(table, path, category, entries):
    new = copy.deepcopy(table)
    cat = new.setdefault(category, {})
    added, changed, same = {}, {}, []
    for k, v in entries.items():
        if k in cat and cat[k] == v:
            same.append(k)
        elif k in cat:
            changed[k] = [cat[k], v]
        else:
            added[k] = v
        cat[k] = v
    return {"ok": not cl.validate_abbrev(new), "problems": cl.validate_abbrev(new), "category": category,
            "new_category": category not in table, "added": added, "changed": changed, "unchanged": same,
            "diff": _diff(table, new), "path": path, "written": False,
            "note": "只是提议, 没写回. 请人看过 diff 后自己改对照表."}


# ------------------------------------------------------------ 工具

class LabelTool(mc.Tool):
    """mc.Tool 默认标只读; 打印要标成会出纸. labels 的 items 结构补进 inputSchema."""

    def __init__(self, name, description, params, fn, read_only=True):
        super().__init__(name, description, params, fn)
        self.read_only = read_only

    def spec(self):
        s = super().spec()
        s["annotations"] = {"readOnlyHint": self.read_only, "destructiveHint": False,
                            "idempotentHint": self.read_only, "openWorldHint": False}
        props = s["inputSchema"]["properties"]
        if "labels" in props:
            props["labels"]["items"] = LABEL_ITEM
            props["labels"]["minItems"], props["labels"]["maxItems"] = 1, MAX_LABELS
        if "entries" in props:
            props["entries"]["additionalProperties"] = {"type": "string"}
        return s


LABEL_ITEM = {"type": "object", "additionalProperties": False, "properties": {
    "shortId": {"type": "integer", "minimum": 0, "maximum": SHORT_ID_MAX, "description": "cable: 线缆短号, 印成 E-00042"},
    "here": {"type": "string", "description": f"cable: 本端 (小字 '@ ...'), 最多 {cl.LIMITS['cable_here_max']} 字符"},
    "there": {"type": "string", "description": f"cable: 对端 (大字), 最多 {cl.LIMITS['cable_there_max']} 字符"},
    "big": {"type": "string", "description": f"text: 大字, 最多 {cl.LIMITS['tag_big_max']} 字符"},
    "small": {"type": "string", "description": f"text: 小字, 最多 {cl.LIMITS['tag_small_max']} 字符"}}}

KIND = {"type": "string", "enum": ["cable", "text"], "required": True,
        "description": "cable = 旗形线缆标签 (shortId/here/there); text = 推车线标 (big/small)"}
LABELS = {"type": "array", "required": True,
          "description": f"每张一个对象, 1~{MAX_LABELS} 张. 只认 ASCII (打印机内置字体没有中文)"}


def build_label_tools(write, status, abbrev, now=time.time, token=lambda: secrets.token_urlsafe(12), guides=None):
    """write(bytes) 发给打印机; status() -> 状态字节 int; abbrev() -> (对照表 dict, 路径).
    都可以抛 mc.ToolError. guides 默认 tspl_preview.cable_guides(). 测试里全换成假的."""
    guides = guides or tp.cable_guides()
    proofs, lock = {}, threading.Lock()         # proof -> (版面, 发出时刻); 进程内, 重启就没了

    def _table():
        table, _ = abbrev()
        return table

    def preview(a):
        kind = a["kind"]
        labels = _labels(kind, a["labels"])
        if not isinstance(a["abbrev"], bool):
            raise mc.ArgError("abbrev 要 true/false")
        if a["abbrev"]:
            labels = _abbrev_labels(kind, labels, _table())
        probs, _, parsed = render(kind, labels, guides)
        shown = parsed[:PREVIEW_SVG_MAX]
        head = {"ok": not probs, "kind": kind, "count": len(labels), "labels": labels, "problems": probs,
                "svg_shown": len(shown),
                "note": "SVG 字形是等宽近似、Data Matrix 是示意; 黄框 = 旗面, 红虚线 = 折痕, 红框 = 出界. "
                        "ok 了再用同样的 labels 调 label_print, 先打 1 张给人看."}
        bad = lambda lb: tp.outside(lb, tp.check_area(lb, guides))     # noqa: E731
        return [mc.json_content(head)] + [mc.text_content(tp.to_svg(lb, guides, bad(lb))) for lb in shown]

    def _take_proof(kind, proof, confirmed, t):
        """批量放行检查. 过了返回 None, 不过返回原因. 不在这里消耗 (打成了才消耗)."""
        if not proof:
            return (f"批量 (>1 张) 要先打一张同版面的给人看: 先只打 1 张, 人确认位置对了, "
                    f"再带上那次回的 proof 和 confirmed (人确认的原话) 来打; proof {PROOF_TTL // 60} 分钟内有效, 只能用一次")
        if not isinstance(confirmed, str) or not confirmed.strip():
            return "批量要 confirmed: 人看过样张后确认的原话 (会记进审计日志)"
        got = proofs.get(proof)
        if got is None:
            return "proof 不认识或已经用过; 先打一张同版面的样张拿新的 proof"
        layout, t0 = got
        if t - t0 > PROOF_TTL:
            return f"proof 过期了 ({int(t - t0)} 秒前发的, 有效 {PROOF_TTL} 秒); 重新打一张样张"
        if layout != _layout(kind):
            return f"proof 是 {layout} 版面打样拿的, 这次是 {_layout(kind)}; 版面不同要重新打样"
        return None

    def do_print(a):
        kind = a["kind"]
        labels = _labels(kind, a["labels"])
        batch = len(labels) > 1
        probs, data, _ = render(kind, labels, guides)
        if probs:
            raise mc.ToolError("不打, 校验没过: " + "; ".join(probs))
        with lock:                              # 一台打印机: 查令牌、查状态、写、发令牌整段串行
            t = now()
            why = _take_proof(kind, a.get("proof"), a.get("confirmed"), t) if batch else None
            for p in [p for p, (_, t0) in proofs.items() if t - t0 > PROOF_TTL]:
                proofs.pop(p, None)             # 先查后清: 过期的令牌要报"过期", 不是"不认识"
            if why:
                raise mc.ToolError(why)
            st = decode_status(status())
            if not st["ok"]:
                raise mc.ToolError(f"打印机没准备好 ({st['byte']}: {'/'.join(st['flags'])}), 没打; 请人处理后再试")
            if batch:
                proofs.pop(a["proof"], None)    # 先消耗再写: 写一半失败也不能拿同一个令牌再来一批
            write(data)
            out = {"printed": len(labels), "kind": kind, "labels": labels}
            if not batch:
                p = token()
                proofs[p] = (_layout(kind), t)
                out.update(proof=p, proof_ttl_s=PROOF_TTL,
                           next="请人看这张: 位置、字对不对. 人确认后要批量, 带上 proof 和 confirmed (人的原话).")
        return [mc.json_content(out)]

    def printer_status(_a):
        return [mc.json_content(decode_status(status()))]

    def abbrev_propose(a):
        cat, entries = a["category"], a["entries"]
        if not isinstance(entries, dict) or not entries or len(entries) > 20 or not all(
                isinstance(k, str) and k and isinstance(v, str) for k, v in entries.items()):
            raise mc.ArgError("entries 要是 1~20 条 {原文: 缩写} (都是字符串)")
        table, path = abbrev()
        return [mc.json_content(propose_abbrev(table, path, cat, entries))]

    return [
        LabelTool("label_preview",
                  "标签预览 (不打印): 查仅 ASCII、长度上限 (cable 对端<=12/本端<=23, text 大字<=11/小字<=23)、"
                  "出界和对折对称, 回检查结果 JSON + 前几张的 SVG (文本). abbrev=true 先按缩写对照表把中文/长名缩短, "
                  "回包里的 labels 就是要打的内容.",
                  {"kind": KIND, "labels": LABELS,
                   "abbrev": {"type": "boolean", "default": False, "description": "先套缩写对照表"}}, preview),
        LabelTool("label_print",
                  "打标签 (TSC 标签机). 校验同 label_preview, 不过就拒; 打印机开盖/缺纸/缺碳带也拒. "
                  "默认只许 1 张, 回一个 proof; 批量 (>1 张) 必须带同版面单张打样回的 proof (10 分钟内, 一次性) "
                  "和 confirmed (人看过样张后确认的原话). 不会自动缩写: 传入什么就印什么.",
                  {"kind": KIND, "labels": LABELS,
                   "proof": {"type": "string", "maxLength": 64, "description": "批量才要: 单张打样回的 proof"},
                   "confirmed": {"type": "string", "maxLength": 200,
                                 "description": "批量才要: 人看过样张后确认的原话, 记进审计"}},
                  do_print, read_only=False),
        LabelTool("label_printer_status",
                  "读标签机状态字节 (ESC !?): ok / 开盖 / 卡纸 / 缺纸 / 缺碳带 / 暂停 / 打印中, 可能多项同时.",
                  {}, printer_status),
        LabelTool("label_abbrev_propose",
                  "提议往缩写对照表加/改条目 (原文 -> ASCII 缩写): 回校验问题和 diff. 只提议, 不写回; 写回要人做.",
                  {"category": {"type": "string", "pattern": r"[a-z][a-z0-9_]{0,31}", "required": True,
                                "description": "类别, 如 vendor / device_type / role / port / medium / location"},
                   "entries": {"type": "object", "required": True, "description": "{原文: 缩写}, 1~20 条"}},
                  abbrev_propose),
    ]


# ------------------------------------------------------------ 真设备 (推车上)

def _open(dev, flags):
    try:
        return os.open(dev, flags)              # 不带 O_CREAT: 打印机没接时绝不在 /dev/usb 下造个普通文件
    except FileNotFoundError:
        raise mc.ToolError(f"打印机没接 ({dev} 不在)") from None
    except OSError as e:
        raise mc.ToolError(f"打不开 {dev}: {e}") from None


def real_write(data, dev=PRINTER):
    fd = _open(dev, os.O_WRONLY)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    except OSError as e:
        raise mc.ToolError(f"写 {dev} 失败: {e}") from None
    finally:
        os.close(fd)


def real_status(dev=PRINTER, timeout=STATUS_TIMEOUT):
    fd = _open(dev, os.O_RDWR)
    try:
        os.write(fd, b"\x1b!?")
        r, _, _ = select.select([fd], [], [], timeout)
        got = os.read(fd, 1) if r else b""
    except OSError as e:
        raise mc.ToolError(f"读 {dev} 状态失败: {e}") from None
    finally:
        os.close(fd)
    if not got:
        raise mc.ToolError(f"打印机没回状态字节 ({dev}, {timeout:g} 秒); 没开机或不支持双向?")
    return got[0]


def real_abbrev(paths=ABBREV_PATHS):
    """推车本地表优先, 没有用仓库里的通用表. 表本身有错也报 (cart_label.load_abbrev)."""
    for p in paths:
        if os.path.exists(p):
            try:
                return cl.load_abbrev(p), p
            except (OSError, ValueError) as e:
                raise mc.ToolError(f"缩写对照表 {p} 读不了: {e}") from None
    raise mc.ToolError(f"没有缩写对照表 (找过 {', '.join(paths)})")


def real_label_tools():
    return build_label_tools(write=real_write, status=real_status, abbrev=real_abbrev)
