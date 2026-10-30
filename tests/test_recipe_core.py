"""recipe_core 测试: OCR 容错匹配 / 步骤执行器 / 配方校验. 全离线, 用假画面序列驱动, 不碰硬件."""

import pytest

import recipe_core as rc


# ------------------------------------------------------------ OCR 归一化匹配

@pytest.mark.parametrize("text", [
    "Press F12 for Boot Menu",
    "Press FI2 for Boot Menu",          # 1 认成 I
    "Press Fl2 for Boot Menu",          # 1 认成 l
    "Press f12 for boot menu",
    "Press F 12 for Boot Menu",         # 中间多了空格
    "<F12> Boot Menu",
    "F12=Boot Menu",
    "Press Ｆ１２ for Boot Menu",        # 全角
    "F|2 Boot",
])
def test_match_f12_tolerates_ocr_noise(text):
    assert rc.match(text, "F12")


@pytest.mark.parametrize("text", [
    "Press F1 to enter setup",          # F1 不是 F12
    "Press F11 for BBS Popup",
    "Press F2 to enter setup",
    "F120",                             # 后面还跟着数字, 不是 F12
    "",
])
def test_match_f12_rejects_other_keys(text):
    assert not rc.match(text, "F12")


def test_match_f1_does_not_hit_f12_or_f11():
    assert not rc.match("Press F12 for Boot Menu", "F1")
    assert not rc.match("F11 BBS Popup", "F1")
    assert rc.match("Press F1 to continue", "F1")


@pytest.mark.parametrize("text", [
    "UEFI: PXE IPv4 Intel(R) Ethernet",
    "UEFI: PX E IPv4",                  # PXE 中间断开
    "UEFI:pxe ipv4",
    "UEFI: PXE IPv4",
    "UEFI: PXE lPv4",                   # I 认成 l
    "UEFI: PXE 1Pv4",
])
def test_match_pxe_ipv4_variants(text):
    assert rc.match(text, "PXE")
    assert rc.match(text, "IPv4")


def test_match_o_and_zero_confusable():
    assert rc.match("B00t Menu", "Boot Menu")
    assert rc.match("BootMenu", "Boot Menu")
    assert rc.match("boot  menu", "Boot Menu")


def test_match_is_whole_word_not_anything():
    # ESC 不能在 Description 里撞上
    assert not rc.match("Description", "ESC")
    assert rc.match("Press ESC for boot options", "ESC")
    assert rc.match("<ESC> Boot Menu", "ESC")
    # PXE 不能在别的词中间
    assert not rc.match("APXEB", "PXE")
    # 完全不相干的不能对上
    assert not rc.match("Windows Boot Manager", "PXE")
    assert not rc.match("SATA: Samsung SSD", "Network")
    assert not rc.match("Network", "Networks")


def test_match_ignores_confusables_only_where_listed():
    # S/5, B/8 不算同一个字 (不做成什么都能对上)
    assert not rc.match("PXE 8OOT", "PXE BOOT")
    assert not rc.match("Boot", "8oot")
    assert not rc.match("Setup", "5etup")


def test_match_chinese():
    assert rc.match("请选择启动设备", "启动设备")
    assert not rc.match("请选择启动设备", "网络启动")


def test_norm_maps_confusables():
    assert rc.norm("FI2") == rc.norm("F12") == rc.norm("fl2") == rc.norm("f|2")
    assert rc.norm("B0OT") == rc.norm("boot")
    assert rc.norm("Ｆ１２") == rc.norm("f12")


def test_find_returns_first_line_by_pattern_priority():
    lines = [{"text": "UEFI: PXE IPv6 Intel", "y": 10},
             {"text": "UEFI: PXE IPv4 Intel", "y": 30}]
    # 模式按优先级: 先找 IPv4, 再退到 PXE
    hit = rc.find(lines, ["IPv4", "PXE"])
    assert hit == (1, "IPv4")
    assert rc.find(lines, ["Network"]) is None
    assert rc.find([], ["PXE"]) is None


def test_find_skips_lines_matching_avoid():
    lines = [{"text": "UEFI: PXE IPv6", "y": 10}, {"text": "UEFI: PXE IPv4", "y": 30}]
    assert rc.find(lines, ["PXE"], avoid=["IPv6"]) == (1, "PXE")
    assert rc.find(lines[:1], ["PXE"], avoid=["IPv6"]) is None


def test_pattern_too_short_rejected():
    assert not rc.pattern_ok("F")
    assert not rc.pattern_ok("  ")
    assert not rc.pattern_ok("--")
    assert rc.pattern_ok("F8")
    assert rc.pattern_ok("启动")


def test_match_ipv4_not_ipv6_and_punctuation_boundaries():
    assert not rc.match("UEFI: PXE IPv6", "IPv4")
    assert rc.match("PXE-E18: Server response timeout", "PXE")
    assert rc.match("(F12)", "F12")
    assert rc.match("F12:Boot", "F12")


# ------------------------------------------------------------ 执行器 (假画面序列驱动)

from ch9329 import Ch9329Busy, Ch9329Gone      # noqa: E402


def L(text, y=0, x=100, h=16):
    return {"text": text, "x": x, "y": y, "w": 8 * len(text), "h": h, "conf": 90.0}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Screen:
    """一串假画面 (每帧是 OCR 出的行). 每截一次图往后走一帧, 走到头停在最后一帧; 每次 OCR 花 1 秒."""

    def __init__(self, frames, clock, ocr_s=1.0):
        self.frames, self.clock, self.ocr_s = frames, clock, ocr_s
        self.n = 0

    def snap(self):
        path = f"/run/shot-{self.n:03d}.png"
        self.n += 1
        return path

    def ocr(self, path):
        self.clock.t += self.ocr_s
        i = int(path[-7:-4])
        return self.frames[min(i, len(self.frames) - 1)]


class Keys:
    def __init__(self, busy_times=0, gone=False):
        self.sent, self.busy_times, self.gone = [], busy_times, gone

    def __call__(self, key):
        if self.gone:
            raise Ch9329Gone("拔线了")
        if self.busy_times > 0:
            self.busy_times -= 1
            raise Ch9329Busy("键鼠被占用")
        self.sent.append(key)


def runner(frames, keys=None, **kw):
    clock = FakeClock()
    screen = Screen(frames, clock)
    keys = keys or Keys()
    events = []
    r = rc.Runner(screen.snap, screen.ocr, keys, log=events.append, clock=clock, sleep=clock.sleep, **kw)
    return r, keys, events, clock


BLANK = []
POST = [L("American Megatrends", 10), L("Press DEL to enter setup, F11 for BBS Popup", 400)]
MENU = [
    L("Please select boot device:", 100, x=200),
    L("------------------------------", 120, x=200),
    L("Windows Boot Manager (Samsung SSD)", 140, x=200),
    L("UEFI: SanDisk USB, Partition 1", 160, x=200),
    L("UEFI: PXE IPv6 Intel(R) Ethernet", 180, x=200),
    L("UEFI: PXE IPv4 Intel(R) Ethernet", 200, x=200),
    L("Enter Setup", 220, x=200),
    L("------------------------------", 240, x=200),
    L("↑ and ↓ to move selection", 260, x=200),
    L("ENTER to select boot device", 280, x=200),
]
PXE_PROGRESS = [L(">>Start PXE over IPv4.", 50, x=20), L("Station IP address is 192.0.2.50", 70, x=20)]


def rec(*steps, timeout=120):
    return {"name": "t", "timeout": timeout, "steps": list(steps)}


def test_wait_text_returns_when_seen():
    r, keys, events, clock = runner([BLANK, BLANK, POST])
    res = r.run(rec({"do": "wait_text", "patterns": ["BBS Popup"], "timeout": 30}))
    assert res["ok"], res
    hits = [e for e in events if e["ev"] == "hit"]
    assert hits and hits[0]["text"].endswith("BBS Popup") and hits[0]["shot"] == "/run/shot-002.png"
    assert keys.sent == []


def test_wait_text_times_out_honestly():
    r, keys, events, clock = runner([BLANK])
    res = r.run(rec({"do": "wait_text", "patterns": ["Boot Menu"], "timeout": 10}))
    assert not res["ok"] and res["step"] == 0
    assert "Boot Menu" in res["reason"] and "10" in res["reason"]
    assert 10 <= clock.t < 13
    assert keys.sent == []


def test_total_timeout_caps_step_timeout():
    r, keys, events, clock = runner([BLANK])
    res = r.run(rec({"do": "wait_text", "patterns": ["Boot Menu"], "timeout": 60}, timeout=5))
    assert not res["ok"] and "总超时" in res["reason"]
    assert clock.t < 8


def test_press_logs_key():
    r, keys, events, clock = runner([BLANK])
    assert r.run(rec({"do": "press", "key": "enter"}))["ok"]
    assert keys.sent == ["enter"]
    assert any(e["ev"] == "key" and e["key"] == "enter" for e in events)


def test_press_until_spams_until_text_appears():
    r, keys, events, clock = runner([BLANK, BLANK, BLANK, MENU])
    res = r.run(rec({"do": "press_until", "key": "f11", "patterns": ["select boot device"],
                     "interval": 0.3, "burst": 2, "timeout": 30}))
    assert res["ok"], res
    assert keys.sent == ["f11"] * 8          # 每看一眼前连按 2 下, 第 4 眼看到菜单


def test_press_until_times_out():
    r, keys, events, clock = runner([BLANK])
    res = r.run(rec({"do": "press_until", "key": "f12", "patterns": ["Boot Menu"],
                     "interval": 0.5, "timeout": 10}))
    assert not res["ok"] and "Boot Menu" in res["reason"]
    assert 5 <= len(keys.sent) <= 10


def test_prompt_picks_key_from_table():
    # 提示一闪而过: 只有一帧有 F11, 之后黑屏, 按 F11 后菜单出来
    frames = [BLANK, POST, BLANK, BLANK, MENU]
    r, keys, events, clock = runner(frames)
    res = r.run(rec(
        {"do": "wait_text", "prompts": [["F12", "f12"], ["F11", "f11"], ["ESC", "esc"]], "timeout": 30},
        {"do": "press_until", "key": "$prompt", "patterns": ["select boot device"], "interval": 0.3,
         "timeout": 30}))
    assert res["ok"], res
    assert set(keys.sent) == {"f11"}
    assert any(e["ev"] == "prompt" and e["key"] == "f11" for e in events)


def test_prompt_key_without_prompt_step_fails():
    r, keys, events, clock = runner([BLANK])
    res = r.run(rec({"do": "press_until", "key": "$prompt", "patterns": ["Boot Menu"], "timeout": 5}))
    assert not res["ok"] and keys.sent == []


def test_select_menu_moves_to_nth_item():
    # 没有高亮标记: Home 回到第一项, 再按 3 下到 PXE IPv4 (第 4 项, 跳过标题/分隔线)
    r, keys, events, clock = runner([MENU])
    res = r.run(rec({"do": "select_menu", "patterns": ["IPv4", "PXE", "Network"], "avoid": ["IPv6"],
                     "timeout": 20}))
    assert res["ok"], res
    assert keys.sent == ["home", "down", "down", "down", "enter"]
    sel = [e for e in events if e["ev"] == "menu"][0]
    assert sel["index"] == 3 and sel["items"] == 5 and "IPv4" in sel["text"]


def test_select_menu_first_item_only_enter():
    menu = [L("Boot Menu", 80, x=200),
            L("Network Boot: IBA GE Slot 0100", 140, x=200),
            L("SATA HDD", 160, x=200)]
    r, keys, events, clock = runner([menu])
    assert r.run(rec({"do": "select_menu", "patterns": ["Network"], "timeout": 20}))["ok"]
    assert keys.sent == ["home", "enter"]


def test_select_menu_uses_marker_when_visible():
    # OCR 读出了 ">" 高亮标记: 从标记那行相对移动, 不按 Home
    menu = [L("Boot Option Menu", 80, x=200),
            L("> Windows Boot Manager", 120, x=190),
            L("UEFI: PXE IPv4 Realtek", 140, x=200),
            L("UEFI: USB Disk", 160, x=200)]
    after = [L("Boot Option Menu", 80, x=200),
             L("Windows Boot Manager", 120, x=200),
             L("> UEFI: PXE IPv4 Realtek", 140, x=190),
             L("UEFI: USB Disk", 160, x=200)]
    r, keys, events, clock = runner([menu, after])
    assert r.run(rec({"do": "select_menu", "patterns": ["PXE"], "timeout": 20}))["ok"]
    assert keys.sent == ["down", "enter"]


def test_select_menu_stops_when_marker_lands_elsewhere():
    menu = [L("> Windows Boot Manager", 120, x=190),
            L("UEFI: PXE IPv4 Realtek", 140, x=200)]
    wrong = [L("Windows Boot Manager", 120, x=200),
             L("UEFI: PXE IPv4 Realtek", 140, x=200),
             L("> UEFI: USB", 160, x=190)]
    r, keys, events, clock = runner([menu, wrong])
    res = r.run(rec({"do": "select_menu", "patterns": ["PXE"], "timeout": 20}))
    assert not res["ok"] and "enter" not in keys.sent


def test_select_menu_stops_if_menu_vanishes_before_enter():
    r, keys, events, clock = runner([MENU, BLANK])
    res = r.run(rec({"do": "select_menu", "patterns": ["IPv4"], "timeout": 20}))
    assert not res["ok"] and "enter" not in keys.sent


def test_select_menu_waits_for_menu_then_times_out():
    r, keys, events, clock = runner([BLANK])
    res = r.run(rec({"do": "select_menu", "patterns": ["PXE"], "timeout": 8}))
    assert not res["ok"] and keys.sent == []


def test_select_menu_skip_if_already_booting():
    r, keys, events, clock = runner([PXE_PROGRESS])
    res = r.run(rec({"do": "select_menu", "patterns": ["PXE"], "skip_if": ["Start PXE over"],
                     "timeout": 8}))
    assert res["ok"] and keys.sent == []


def test_busy_waits_then_sends():
    r, keys, events, clock = runner([BLANK], keys=Keys(busy_times=3), busy_wait=1.0, busy_max=10)
    assert r.run(rec({"do": "press", "key": "enter"}))["ok"]
    assert keys.sent == ["enter"]
    assert sum(1 for e in events if e["ev"] == "busy") == 3


def test_busy_gives_up_after_limit():
    r, keys, events, clock = runner([BLANK], keys=Keys(busy_times=999), busy_wait=1.0, busy_max=5)
    res = r.run(rec({"do": "press", "key": "enter"}))
    assert not res["ok"] and "占用" in res["reason"] and keys.sent == []
    assert clock.t <= 7


def test_keyboard_gone_stops_immediately():
    r, keys, events, clock = runner([BLANK], keys=Keys(gone=True))
    res = r.run(rec({"do": "press", "key": "enter"}))
    assert not res["ok"] and "拔线" in res["reason"]


def test_snap_failure_stops():
    clock = FakeClock()

    def bad_snap():
        raise RuntimeError("采集卡没出帧")
    r = rc.Runner(bad_snap, lambda p: [], Keys(), clock=clock, sleep=clock.sleep)
    res = r.run(rec({"do": "wait_text", "patterns": ["PXE"], "timeout": 5}))
    assert not res["ok"] and "采集卡没出帧" in res["reason"]


def test_dry_run_never_sends():
    r, keys, events, clock = runner([MENU], dry_run=True)
    r.run(rec({"do": "press", "key": "enter"}))
    assert keys.sent == []
    assert any(e["ev"] == "key" and e.get("dry_run") for e in events)
