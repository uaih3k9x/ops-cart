"""mcp_kvm: 键鼠 (租约 + 只经代理 + 急停) / OCR / WOL 工具, 全用假依赖 (#41)."""

import json

import pytest

import mcp_core as mc
import mcp_kvm as mk
import mcp_serial as ms

PORT = "/dev/serial/by-id/usb-1a86_X-if00"


class Ch:
    def __init__(self, log, fail=None):
        self.log, self.fail = log, fail

    def _go(self, *item):
        if self.fail:
            raise self.fail
        self.log.append(item)

    def send_keys(self, keys):
        self._go("keys", tuple(keys))

    def type_text(self, text, secret=False):
        self._go("type", text, secret) if secret else self._go("type", text)

    def close(self):
        self.log.append(("close",))


class Rig:
    def __init__(self, tmp_path):
        self.t = [1000.0]
        self.stopped = False
        self.opened, self.sent, self.fail, self.open_fail = [], [], None, None
        self.ports = {"SER1": PORT}
        self.wols = []
        self.engine = "rapidocr"
        self.human_n = 0                # 代理记的人工输入次数 (车机 / 网页口)
        self.boot = "boot1"             # 代理启动 id
        self.ocr_lines = [{"text": "Media Present", "x": 1, "y": 2, "w": 3, "h": 4, "conf": 90.0}]
        self.log = tmp_path / "kvm-thread.log"
        self.events = []
        self.store = mk.LeaseStore(str(tmp_path / "kvm-lease.json"), clock=lambda: self.t[0],
                                   stopped=lambda: self.stopped, log_path=str(self.log),
                                   event=lambda tag, msg: self.events.append((tag, msg)))
        tools = mk.build_tools(self.store, ports=lambda: self.ports, opener=self.opener,
                               ocr=lambda dev, lang: (self.ocr_lines, "卡2 (/dev/video3) 当前画面", self.engine),
                               wol=lambda mac, iface: self.wols.append((mac, iface)),
                               broker_up=lambda port: True, human_seq=lambda port: [self.boot, self.human_n])
        self.server = ms.Server(tools)

    def opener(self, port, who):
        if self.open_fail:
            raise self.open_fail
        self.opened.append((port, who))
        return Ch(self.sent, self.fail)

    def call(self, name, **a):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": a}}, "agent-a")["result"]
        text = r["content"][0]["text"]
        try:
            text = json.loads(text)
        except ValueError:
            pass
        return r["isError"], text

    def lease(self, known=True, **a):
        """known: 假装人已在车机上确认过一次回车 (新租约的当前行本来是"看不清", #64)."""
        err, r = self.call("kvm_lease_acquire", **a)
        assert not err, r
        if known:
            self.known()
        return r["lease"]

    def known(self):
        with self.store._tx() as (st, _now, _evs):
            st["line"], st["blind"] = "", ""


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---- 租约

def test_keys_need_lease(rig):
    err, msg = rig.call("kvm_key", keys="enter", lease="x" * 16)
    assert err and "租约" in msg and rig.sent == []


def test_lease_then_keys_go_through_broker_as_agent(rig):
    tok = rig.lease()
    err, _ = rig.call("kvm_key", keys="ctrl+shift+esc", lease=tok)
    assert not err
    assert rig.sent[0] == ("keys", ("ctrl", "shift", "esc")) and rig.opened == [(PORT, "agent-a")]
    assert rig.sent[-1] == ("close",)


def test_single_writer(rig):
    rig.lease()
    err, msg = rig.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "kvm_lease_acquire", "arguments": {}}}, "agent-b")["result"]["isError"], None
    assert err


def test_lease_expires_and_renew(rig):
    tok = rig.lease(ttl_s=60)
    rig.t[0] += 50
    assert not rig.call("kvm_lease_renew", lease=tok)[0]
    rig.t[0] += 50
    assert not rig.call("kvm_key", keys="a", lease=tok)[0]      # 续过了还在
    rig.t[0] += 61
    assert rig.call("kvm_key", keys="a", lease=tok)[0]
    assert rig.call("kvm_lease_renew", lease=tok)[0]            # 过期不能续


def test_release(rig):
    tok = rig.lease()
    assert not rig.call("kvm_lease_release", lease=tok)[0]
    assert rig.call("kvm_key", keys="a", lease=tok)[0]
    rig.lease()                                                 # 别人能拿


def test_lease_file_is_private_and_token_not_in_status(rig):
    import os
    import stat
    tok = rig.lease()
    assert stat.S_IMODE(os.stat(rig.store.path).st_mode) == 0o600
    err, st = rig.call("kvm_status")
    assert not err and tok not in json.dumps(st) and st["writer"] == "agent-a" and st["all_stopped"] is False


def test_corrupt_lease_file_means_no_lease(rig):
    tok = rig.lease()
    with open(rig.store.path, "w") as f:
        f.write("{bad")
    assert rig.call("kvm_key", keys="a", lease=tok)[0]


# ---- 急停: 全部急停收回租约, 解除后要重新申请

def test_all_stop_refuses_and_revokes_lease(rig):
    tok = rig.lease()
    rig.stopped = True
    err, msg = rig.call("kvm_key", keys="a", lease=tok)
    assert err and "急停" in msg and rig.sent == []
    assert rig.call("kvm_lease_acquire")[0]
    rig.stopped = False
    assert rig.call("kvm_key", keys="a", lease=tok)[0]          # 解除后旧令牌也不算


# ---- 发键

def test_type_with_enter(rig):
    tok = rig.lease()
    assert not rig.call("kvm_type", text="uname -r", enter=True, lease=tok)[0]
    assert rig.sent[:2] == [("type", "uname -r"), ("keys", ("enter",))]


def test_no_broker_is_error_never_direct(rig):
    tok = rig.lease()
    rig.open_fail = FileNotFoundError("no sock")
    err, msg = rig.call("kvm_type", text="x", lease=tok)
    assert err and "代理" in msg


@pytest.mark.parametrize("exc", [OSError("busy"), ValueError("无法输入")])
def test_send_errors_become_tool_errors(rig, exc):
    tok = rig.lease()
    rig.fail = exc
    assert rig.call("kvm_type", text="x", lease=tok)[0]


def test_port_choice(rig):
    rig.ports = {"S1": "/p1", "S2": "/p2"}
    tok = rig.lease()
    rig.ports = {}
    err, msg = rig.call("kvm_key", keys="a", lease=tok)
    assert err and "键鼠" in msg
    rig.ports = {"S1": "/p1", "S2": "/p2"}      # 挑不出口时一个键都不动, 闸门状态也不动
    err, msg = rig.call("kvm_key", keys="a", lease=tok)
    assert err and "S1" in msg
    assert rig.call("kvm_key", keys="a", lease=tok, serial="S2")[1]["sent"]
    assert rig.opened[-1][0] == "/p2"


def test_keyboard_plugged_after_lease_is_unknown(rig):
    """申请租约时没看到这块键鼠的人工输入次数: 这一行算看不清."""
    tok = rig.lease()
    rig.ports = {"SER1": PORT, "S2": "/p2"}
    assert rig.call("kvm_key", keys="a", lease=tok, serial="S2")[1]["status"] == "pending"


# ---- OCR

def test_ocr_readable(rig):
    err, r = rig.call("screen_ocr")
    assert not err and r["readable"] is True and r["text"] == "Media Present"
    assert r["lines"][0]["x"] == 1 and "/dev/video3" in r["screen"]


def test_ocr_low_confidence_says_use_screenshot(rig):
    rig.ocr_lines = [{"text": "Sete gd aS", "x": 0, "y": 0, "w": 1, "h": 1, "conf": 33.0}]
    err, r = rig.call("screen_ocr")
    assert not err and r["readable"] is False and "screenshot" in r["hint"]


def test_ocr_empty(rig):
    rig.ocr_lines = []
    err, r = rig.call("screen_ocr")
    assert not err and r["readable"] is False and r["text"] == ""


def test_ocr_view_drops_noise_from_good_screen():
    lines = [{"text": "Good line", "x": 0, "y": 0, "w": 1, "h": 1, "conf": 90.0},
             {"text": "~", "x": 0, "y": 1, "w": 1, "h": 1, "conf": 5.0}]
    assert mk.ocr_view(lines)["text"] == "Good line"


# ---- WOL

def test_wol_only_lan1(rig):
    err, r = rig.call("wol_send", mac="AA-BB-CC-00-11-22")
    assert not err and rig.wols == [("aa:bb:cc:00:11:22", "eno1")] and "不保证" in r["note"]


def test_wol_bad_mac_is_tool_error(rig):
    assert rig.call("wol_send", mac="ff:ff:ff:ff:ff:ff")[0] and rig.wols == []


def test_new_tools_cannot_arm_pxe():
    names = {t.name for t in mk.real_tools()}
    assert names == {"kvm_status", "kvm_lease_acquire", "kvm_lease_renew", "kvm_lease_release",
                     "kvm_key", "kvm_type", "screen_ocr", "wol_send"}


def test_same_who_cannot_steal_live_lease(rig):
    """stdio 下两个 Agent 可能是同一个用户: 认令牌不认名字."""
    tok = rig.lease()
    assert rig.call("kvm_lease_acquire")[0]
    assert not rig.call("kvm_key", keys="a", lease=tok)[0]


# ---- 闸门 (#42)

def logs(rig):
    return [json.loads(ln) for ln in rig.log.read_text().splitlines()]


@pytest.mark.parametrize("text", ["format c:", "diskpart", "mkfs.ext4 /dev/sda", "Format-Volume -DriveLetter C",
                                  "rm -rf /", "dd if=/dev/zero of=/dev/sda"])
def test_dangerous_line_is_held_not_sent(rig, text):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text=text, enter=True, lease=tok)
    assert not err and r["status"] == "pending" and rig.sent == []


@pytest.mark.parametrize("text", ["ipconfig /all", "uname -r", "lsblk"])
def test_harmless_line_goes_straight(rig, text):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text=text, lease=tok)
    assert not err and r["sent"] is True


def test_splitting_across_calls_is_still_caught(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="disk", lease=tok)[1]["sent"]
    err, r = rig.call("kvm_type", text="part", lease=tok)
    assert r["status"] == "pending"
    assert rig.store.view()["pending"][0]["line"] == "diskpart"


def test_enter_resets_line_buffer(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="disk", lease=tok)
    rig.call("kvm_key", keys="enter", lease=tok)
    assert rig.call("kvm_type", text="part", lease=tok)[1]["sent"]


def test_danger_combo_held(rig):
    tok = rig.lease()
    err, r = rig.call("kvm_key", keys="ctrl+alt+delete", lease=tok)
    assert r["status"] == "pending" and rig.sent == []
    mk.human_action(rig.store, "reject", r["id"], "human:kiosk")
    assert rig.call("kvm_key", keys="esc", lease=tok)[1]["sent"]


def test_human_confirm_sends_as_agent_holder(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="format c:", enter=True, lease=tok)
    pid = rig.store.view()["pending"][0]["id"]
    res = mk.human_action(rig.store, "confirm", pid, "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)
    assert "format c:" in res and rig.sent[:2] == [("type", "format c:"), ("keys", ("enter",))]
    assert rig.opened == [(PORT, "agent-a")] and rig.store.view()["pending"] == []
    assert rig.store.view()["history"][-1]["result"] == "confirmed"


def test_human_reject_sends_nothing(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="format c:", lease=tok)
    pid = rig.store.view()["pending"][0]["id"]
    mk.human_action(rig.store, "reject", pid, "human:kiosk")
    assert rig.sent == [] and rig.store.view()["history"][-1]["result"] == "rejected"


def test_agent_has_no_confirm_tool():
    assert not [t.name for t in mk.real_tools() if "confirm" in t.name or "reject" in t.name]


@pytest.mark.parametrize("how", ["expire", "release", "estop"])
def test_pending_dies_with_lease(rig, how):
    tok = rig.lease(ttl_s=60)
    rig.call("kvm_type", text="format c:", lease=tok)
    pid = rig.store.view()["pending"][0]["id"]
    if how == "expire":
        rig.t[0] += 61
    elif how == "release":
        rig.call("kvm_lease_release", lease=tok)
    else:
        rig.stopped = True
    with pytest.raises(ValueError):
        mk.human_action(rig.store, "confirm", pid, "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)
    assert rig.sent == []
    rig.stopped = False
    assert rig.store.view()["pending"] == []


def test_confirm_during_estop_refused(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="format c:", lease=tok)
    rig.stopped = True
    with pytest.raises(ValueError):
        mk.human_action(rig.store, "confirm", 1, "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)


# ---- secret

def test_secret_not_in_audit_or_log(rig, tmp_path):
    audit = []

    class A:
        def write(self, line):
            audit.append(line)
    rig.server.audit = A()
    tok = rig.lease()
    assert rig.call("kvm_type", text="hunter2xyz", secret=True, lease=tok)[1]["sent"]
    assert rig.sent[0] == ("type", "hunter2xyz", True)
    assert "hunter2xyz" not in "".join(audit) and "hunter2xyz" not in rig.log.read_text()
    assert "hunter2xyz" not in rig.store.path and "hunter2xyz" not in open(rig.store.path).read()


def test_secret_with_danger_word_refused_not_held(rig):
    tok = rig.lease()
    err, msg = rig.call("kvm_type", text="format", secret=True, lease=tok)
    assert err and "secret" in msg and rig.sent == [] and rig.store.view()["pending"] == []
    assert "format" not in rig.log.read_text().replace("ev", "")


def test_plain_text_is_audited(rig):
    audit = []

    class A:
        def write(self, line):
            audit.append(line)
    rig.server.audit = A()
    rig.call("kvm_type", text="uname -r", lease=rig.lease())
    assert "uname -r" in "".join(audit)


# ---- 日志 (#43)

def test_thread_log_records_lifecycle(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="uname", enter=True, lease=tok)
    rig.call("kvm_type", text="format c:", lease=tok)
    mk.human_action(rig.store, "reject", 1, "human:kiosk")
    rig.call("kvm_lease_release", lease=tok)
    evs = [r["ev"] for r in logs(rig)]
    assert evs == ["lease_acquire", "send", "pending", "reject", "lease_release"]
    send = logs(rig)[1]
    assert send["text"] == "uname + 回车" and send["src"] == "agent" and send["who"] == "agent-a" and send["ok"] is True
    assert logs(rig)[3]["src"] == "human"
    assert rig.events and rig.events[0][0] == "kvm"


def test_failed_send_logged(rig):
    tok = rig.lease()
    rig.fail = OSError("gone")
    rig.call("kvm_type", text="x", lease=tok)
    assert logs(rig)[-1]["ok"] is False


def test_estop_revocation_logged_once(rig):
    tok = rig.lease()
    rig.stopped = True
    rig.call("kvm_key", keys="a", lease=tok)
    rig.call("kvm_key", keys="a", lease=tok)
    assert [r["ev"] for r in logs(rig)].count("revoked") == 1


# ---- peek

def test_peek_readonly_and_no_token(rig):
    tok = rig.lease()
    rig.call("kvm_type", text="format c:", lease=tok)
    before = open(rig.store.path).read()
    v = mk.peek(rig.store.path, clock=lambda: rig.t[0], stopped=lambda: False)
    assert open(rig.store.path).read() == before and tok not in json.dumps(v)
    assert v["writer"] == "agent-a" and v["pending"][0]["id"] == 1


def test_peek_when_stopped_or_missing(tmp_path, rig):
    tok = rig.lease()
    rig.call("kvm_type", text="format c:", lease=tok)
    v = mk.peek(rig.store.path, clock=lambda: rig.t[0], stopped=lambda: True)
    assert v["stopped"] and v["writer"] is None and v["pending"] == []
    assert mk.peek(str(tmp_path / "none.json"), stopped=lambda: False)["writer"] is None


@pytest.mark.parametrize("tool,args", [("kvm_type", {"text": "mat c:"}), ("kvm_key", {"keys": "enter"})])
def test_nothing_goes_out_while_something_is_held(rig, tool, args):
    """键盘是一条流: 挂起时接着发, 人确认后顺序就乱了 (试用时 for 挂起, mat c: 真发出去了)."""
    tok = rig.lease()
    _, r = rig.call("kvm_type", text="for", lease=tok)
    assert r["status"] == "pending"
    err, msg = rig.call(tool, lease=tok, **args)
    assert err and "等确认" in msg and rig.sent == []
    mk.human_action(rig.store, "reject", r["id"], "human:kiosk")
    assert not rig.call(tool, lease=tok, **args)[0]


def test_ocr_says_which_engine(rig):
    assert rig.call("screen_ocr")[1]["engine"] == "rapidocr"
    rig.engine = "tesseract (RapidOCR 不可用: x)"
    assert "不可用" in rig.call("screen_ocr")[1]["engine"]


# ---- #64 闸门绕法: 只查最后一行 / 逐键不计入 / secret 前半截不计入

def _keys(rig, tok, *names):
    out = []
    for k in names:
        err, r = rig.call("kvm_key", keys=k, lease=tok)
        assert not err, r
        out.append(r)
        if r.get("status") == "pending":
            break
    return out


@pytest.mark.parametrize("text", ["echo ok\nformat c: /q /y\nok", "format c: /q /y\nok", "ok\n\ndiskpart\n"])
def test_every_line_of_text_is_checked(rig, text):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text=text, lease=tok)
    assert not err and r["status"] == "pending" and rig.sent == []


def test_first_line_joined_with_buffer_is_checked(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="for", lease=tok)[1]["status"] == "pending"   # "fo" 起就算 format 缩写
    mk.human_action(rig.store, "reject", 1, "human:kiosk")
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    err, r = rig.call("kvm_type", text="kpart\nls", lease=tok)
    assert r["status"] == "pending" and "diskpart" in r["danger"]


def test_multiline_harmless_goes_and_buffer_is_last_line(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="uname -r\nls\ndis", lease=tok)[1]["sent"]
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["status"] == "pending"


def test_key_by_key_spelling_is_caught(rig):
    tok = rig.lease()
    out = _keys(rig, tok, *"diskpart")
    assert out[-1]["status"] == "pending" and "diskpart" in out[-1]["danger"]
    assert ("keys", ("t",)) not in rig.sent
    assert rig.store.view()["pending"][0]["line"] == "diskpart"


def test_key_space_counts_as_char(rig):
    tok = rig.lease()
    _keys(rig, tok, "l", "s", "space")
    assert rig.call("kvm_type", text="-l", lease=tok)[1]["sent"]
    out = _keys(rig, tok, "enter", *"dis", "k", "p", "a", "r", "t")
    assert out[-1]["status"] == "pending"


def test_key_enter_resets_buffer(rig):
    tok = rig.lease()
    _keys(rig, tok, *"dis", "enter")
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["sent"]


def test_confirmed_key_keeps_line(rig):
    tok = rig.lease()
    out = _keys(rig, tok, *"diskpart")
    mk.human_action(rig.store, "confirm", out[-1]["id"], "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)
    assert rig.sent[-2] == ("keys", ("t",))
    # 人确认过的行还在缓冲里, 接着打的照样跟它一起查
    assert rig.call("kvm_type", text=" x", lease=tok)[1]["status"] == "pending"


def test_backspace_removes_one_char(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="diskx", lease=tok)[1]["sent"]
    _keys(rig, tok, "backspace")
    assert rig.call("kvm_type", text="part", lease=tok)[1]["status"] == "pending"


def test_backspace_on_empty_line_makes_line_unknown(rig):
    tok = rig.lease()
    _keys(rig, tok, "backspace")
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["status"] == "pending"


def test_secret_prefix_counts_toward_line(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", secret=True, lease=tok)[1]["sent"]
    err, r = rig.call("kvm_type", text="kpart c:", lease=tok)
    assert not err and r["status"] == "pending" and [s for s in rig.sent if s[0] != "close"] == [("type", "dis", True)]
    assert rig.store.view()["pending"][0]["line"] == "<secret>kpart c:"
    assert "dis" not in open(rig.store.path).read().replace("diskpart", "")


def test_secret_then_enter_is_fine_and_resets(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="hunter2", secret=True, lease=tok)[1]["sent"]
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["sent"]
    assert rig.call("kvm_type", text="uname -r", enter=True, lease=tok)[1]["sent"]


def test_secret_after_secret_on_same_line_goes_but_enter_needs_human(rig):
    """第三轮中2: 不带回车的 secret 打在看不全的行上放行, 这一行变成看不清, 回车要人确认 (人看得见屏幕)."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", secret=True, lease=tok)[1]["sent"]
    assert rig.call("kvm_type", text="kpart", secret=True, lease=tok)[1]["sent"]
    assert "kpart" not in rig.log.read_text()
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_secret_completing_plain_prefix_refused(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    err, _ = rig.call("kvm_type", text="kpart", secret=True, lease=tok)
    assert err and rig.store.view()["pending"] == []


def test_secret_with_danger_on_later_line_refused(rig):
    tok = rig.lease()
    err, _ = rig.call("kvm_type", text="ok\nformat c:", secret=True, lease=tok)
    assert err and rig.sent == []


@pytest.mark.parametrize("nav", ["home", "left", "right", "end", "tab", "delete", "ctrl+a", "alt+b", "shift+a"])
def test_cursor_or_edit_keys_make_line_unknown(rig, nav):
    """光标挪了 / 补全 / 组合键: 推车不知道这一行变成了什么, 接着打的字和回车都要人确认."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="rmat c:", lease=tok)[1]["sent"]
    _keys(rig, tok, nav)
    err, r = rig.call("kvm_type", text="fo", lease=tok)
    assert r["status"] == "pending" and "看不全" in r["reason"]
    mk.human_action(rig.store, "reject", r["id"], "human:kiosk")
    err, r = rig.call("kvm_key", keys="enter", lease=tok)
    assert r["status"] == "pending"
    mk.human_action(rig.store, "confirm", r["id"], "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["sent"]          # 人确认过回车, 新的一行重新可知


@pytest.mark.parametrize("text,enter", [("", True), ("\nls", False)])
def test_newline_on_unknown_line_needs_human(rig, text, enter):
    tok = rig.lease()
    _keys(rig, tok, "x", "left")
    err, r = rig.call("kvm_type", text=text, enter=enter, lease=tok)
    assert r["status"] == "pending"




def test_recall_then_typing_needs_human(rig):
    tok = rig.lease()
    _keys(rig, tok, "up")
    err, r = rig.call("kvm_type", text=" /q", lease=tok)
    assert r["status"] == "pending"


def test_recall_on_nonempty_line_makes_it_unknown(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["sent"]
    _keys(rig, tok, "up")
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_neutral_keys_keep_line(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    _keys(rig, tok, "capslock")
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["status"] == "pending"




# ---- #64 追加: 组合键超集也算, SysRq 是 alt+printscreen

@pytest.mark.parametrize("keys", [["ctrl", "alt", "delete"], ["ctrl", "alt", "del"], ["CTRL", "Alt", "Del"],
                                  ["ctrl", "shift", "alt", "del"], ["alt", "f4"], ["ctrl", "alt", "f4"],
                                  ["shift", "alt", "f4"], ["alt", "printscreen"], ["alt", "printscreen", "b"],
                                  ["ctrl", "alt", "printscreen", "o"]])
def test_danger_combo_superset_matches(keys):
    assert mk.danger_keys(keys)


@pytest.mark.parametrize("keys", [["ctrl", "alt"], ["alt"], ["f4"], ["printscreen"], ["ctrl", "delete"],
                                  ["alt", "f5"], ["ctrl", "c"]])
def test_harmless_combos_not_danger(keys):
    assert not mk.danger_keys(keys)


def test_no_sysrq_entry_that_cannot_be_sent():
    import ch9329
    for combo in mk.DANGER_KEYS:
        assert all(k in ch9329.MOD or k in ch9329.KEYS for k in combo), combo


@pytest.mark.parametrize("keys", ["ctrl+shift+alt+del", "alt+printscreen+b"])
def test_superset_combo_held(rig, keys):
    tok = rig.lease()
    err, r = rig.call("kvm_key", keys=keys, lease=tok)
    assert r["status"] == "pending" and rig.sent == []


# ---- #64 第二轮: 闸门看不清的一律"看不清", 下一个回车要人确认

def _confirm(rig, r):
    mk.human_action(rig.store, "confirm", r["id"], "human:kiosk", opener=rig.opener, ports=lambda: rig.ports)


@pytest.mark.parametrize("how", ["fresh", "release", "expire", "stop"])
def test_new_lease_line_is_unknown(rig, how):
    """租约之间人可能在屏幕上留了半截命令: 新租约的第一个回车要人确认, 确认后才重新可知."""
    if how != "fresh":
        tok = rig.lease()
        assert rig.call("kvm_type", text="ls", lease=tok)[1]["sent"]
        if how == "release":
            rig.call("kvm_lease_release", lease=tok)
        elif how == "expire":
            rig.t[0] += 3600
        else:
            rig.stopped = True
            rig.call("kvm_status")
            rig.stopped = False
            rig.t[0] += mk.ESTOP_SETTLE_S
    tok = rig.lease(known=False)
    err, r = rig.call("kvm_key", keys="enter", lease=tok)
    assert not err and r["status"] == "pending" and rig.sent[-1:] != [("keys", ("enter",))]
    _confirm(rig, r)
    assert rig.call("kvm_type", text="ls", enter=True, lease=tok)[1]["sent"]


def test_new_lease_typing_needs_human(rig):
    tok = rig.lease(known=False)
    err, r = rig.call("kvm_type", text=" /q", lease=tok)
    assert r["status"] == "pending" and "看不全" in r["reason"]


@pytest.mark.parametrize("keys", ["Enter", "CTRL+c", "foo", "a+b+c+d+e+f+g"])
def test_unsendable_keys_refused_before_state_changes(rig, keys):
    """键名跟芯片键表对不上 (大小写也算) 就当场拒绝, 行缓冲不动."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    err, msg = rig.call("kvm_key", keys=keys, lease=tok)
    assert err and "键" in msg
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["status"] == "pending"


def test_untypable_text_refused_before_state_changes(rig):
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    err, _ = rig.call("kvm_type", text="x\u00e9\nok", lease=tok)
    assert err
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["status"] == "pending"


def test_untypable_secret_message_has_no_content(rig):
    tok = rig.lease()
    err, msg = rig.call("kvm_type", text="pw\u00e9", secret=True, lease=tok)
    assert err and "\u00e9" not in msg and rig.sent == []


@pytest.mark.parametrize("how", ["agent", "human"])
def test_failed_send_makes_line_unknown(rig, how):
    """发键失败可能发了一半: 推车不知道屏幕上剩了什么."""
    tok = rig.lease()
    if how == "agent":
        rig.fail = OSError("gone")
        err, _ = rig.call("kvm_type", text="ls", lease=tok)
        assert err
    else:
        err, r = rig.call("kvm_type", text="diskpart", lease=tok)
        rig.fail = OSError("gone")
        with pytest.raises(ValueError):
            _confirm(rig, r)
    rig.fail = None
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_tab_in_text_refused(rig):
    tok = rig.lease()
    err, msg = rig.call("kvm_type", text="rm -rf /ho\t", lease=tok)
    assert err and "tab" in msg.lower() and rig.sent == []


@pytest.mark.parametrize("keys", ["up", "down", "pgup", "pgdn", "esc", "f3", "f7", "f9"])
def test_recall_on_empty_line_then_enter_needs_human(rig, keys):
    """空行调出历史再回车, 可能重放人确认过的危险命令."""
    tok = rig.lease()
    _keys(rig, tok, keys)
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


@pytest.mark.parametrize("keys", ["f10", "f11", "f12", "f13"])
def test_high_f_keys_keep_line(rig, keys):
    """cmd.exe 的历史键是 F1–F9; F10 以上 (BIOS 启动菜单常用) 不动这一行."""
    tok = rig.lease()
    out = _keys(rig, tok, keys, "enter")
    assert all(r.get("sent") for r in out)
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    _keys(rig, tok, keys)
    assert rig.call("kvm_type", text="kpart", lease=tok)[1]["status"] == "pending"


@pytest.mark.parametrize("text", ["!!", "sudo !!", "!-2", "!rm", "echo !$", "^ls^rm", " ^a^b"])
def test_history_expansion_enter_needs_human(rig, text):
    """bash 历史展开: 回车后真正执行的不是推车看到的这一行."""
    tok = rig.lease()
    err, r = rig.call("kvm_type", text=text, enter=True, lease=tok)
    assert r["status"] == "pending" and rig.sent == []
    mk.human_action(rig.store, "reject", r["id"], "human:kiosk")
    assert rig.call("kvm_type", text=text, lease=tok)[1]["sent"]
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_history_expansion_in_middle_line_of_text(rig):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text="ls\n!!\nls", lease=tok)
    assert r["status"] == "pending" and rig.sent == []


def test_secret_with_bang_makes_enter_need_human(rig):
    """密码里常有 !: 推车看不见内容, 不知道是不是历史展开, 回车要人确认 (人看得见屏幕是不是密码提示)."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="pw!1", secret=True, lease=tok)[1]["sent"]
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_secret_with_bang_and_enter_refused(rig):
    tok = rig.lease()
    err, msg = rig.call("kvm_type", text="pw!1", secret=True, enter=True, lease=tok)
    assert err and "kvm_key" in msg and rig.sent == []


@pytest.mark.parametrize("text", ["pw\nreboot", "pw\n", "\npw"])
def test_secret_newline_refused(rig, text):
    """secret 带换行就能在审计里无痕执行多行命令."""
    tok = rig.lease()
    err, msg = rig.call("kvm_type", text=text, secret=True, lease=tok)
    assert err and "换行" in msg and rig.sent == []
    assert "reboot" not in rig.log.read_text()


@pytest.mark.parametrize("text", ["/sbin/reboot", "/usr/sbin/shutdown -h now", "sudo /bin/rm -rf /x",
                                  "C:\\Windows\\System32\\shutdown.exe /s /t 0", '"shutdown.exe" -r',
                                  "format.com c:", "& 'C:\\x\\diskpart.exe'", "reboot.exe"])
def test_command_name_with_path_ext_quotes(text):
    assert mk.danger_of(text)


@pytest.mark.parametrize("text", ["ls -l", "uname -r", "cat /var/log/syslog", "cd C:\\Users\\x", "ping 192.0.2.1"])
def test_plain_commands_not_danger(text):
    assert not mk.danger_of(text)


def test_ctrl_alt_backspace_is_danger():
    assert mk.danger_keys(["ctrl", "alt", "backspace"])


def test_secret_failure_reason_not_logged(rig):
    tok = rig.lease()
    rig.fail = OSError("boom-hunter2")
    err, _ = rig.call("kvm_type", text="hunter2", secret=True, lease=tok)
    assert err and "hunter2" not in rig.log.read_text()


def test_same_token_concurrent_send_refused(rig):
    """同一令牌两路并发: 上一条还在发就不放第二条, 免得键交错."""
    tok = rig.lease()
    sid = rig.store.gate(tok, "type", text="ls")
    assert isinstance(sid, str)
    with pytest.raises(mc.ToolError):
        rig.store.gate(tok, "type", text="pwd")
    rig.store.finish("agent-a", "type", "ls", True, send=sid)
    assert isinstance(rig.store.gate(tok, "type", text="pwd"), str)


# ---- #64 第三轮

@pytest.mark.parametrize("how", ["text", "keys"])
def test_truncated_history_expansion_still_needs_human(rig, how):
    """!! 之后用超长填充把 ! 挤出行缓冲: 回车照样要人确认."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="!!", lease=tok)[1]["sent"]
    if how == "text":
        assert rig.call("kvm_type", text=" " * mk.TEXT_MAX, lease=tok)[1]["sent"]
    else:
        line, blind = "!!" + " " * (mk.TEXT_MAX - 1), ""
        _h, _b, line, blind = mk.press(line, blind, ["space"])
        assert "!" not in line and blind == mk.UNKNOWN
        return
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_truncated_plain_line_is_partial():
    _h, _b, line, blind = mk.advance("x" * mk.TEXT_MAX, "", "y")
    assert len(line) == mk.TEXT_MAX and blind == mk.PARTIAL


def test_secret_on_new_lease_without_enter_goes(rig):
    """新租约时屏幕停在 Password:, 密码要能打进去; 回车要人确认."""
    tok = rig.lease(known=False)
    err, r = rig.call("kvm_type", text="hunter2", secret=True, lease=tok)
    assert not err and r["sent"] and rig.sent[0] == ("type", "hunter2", True)
    err, r = rig.call("kvm_key", keys="enter", lease=tok)
    assert r["status"] == "pending" and "hunter2" not in json.dumps(r)


def test_secret_after_tab_in_form_goes(rig):
    """用户名 -> tab -> 密码的表单."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="admin", lease=tok)[1]["sent"]
    _keys(rig, tok, "tab")
    assert rig.call("kvm_type", text="hunter2", secret=True, lease=tok)[1]["sent"]
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_secret_with_enter_on_unknown_line_still_refused(rig):
    tok = rig.lease(known=False)
    err, _ = rig.call("kvm_type", text="hunter2", secret=True, enter=True, lease=tok)
    assert err and rig.sent == []


def test_secret_with_danger_on_unknown_line_still_refused(rig):
    tok = rig.lease(known=False)
    err, _ = rig.call("kvm_type", text="diskpart", secret=True, lease=tok)
    assert err and rig.sent == []


def test_human_typed_during_lease_makes_line_unknown(rig):
    """租约期间人经车机 / 网页口敲过键: 推车不知道这一行变成了什么."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["sent"]
    rig.human_n += 1
    err, r = rig.call("kvm_key", keys="enter", lease=tok)
    assert r["status"] == "pending"
    _confirm(rig, r)
    assert rig.call("kvm_type", text="ls", enter=True, lease=tok)[1]["sent"]


def test_human_typed_before_first_gate_counts(rig):
    """人在申请租约之后、Agent 第一次发键之前敲的也算."""
    tok = rig.lease()
    rig.human_n += 1
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["status"] == "pending"


def test_confirmed_danger_combo_makes_line_unknown(rig):
    """alt+f4 之类确认发出后焦点可能换了窗口."""
    tok = rig.lease()
    err, r = rig.call("kvm_key", keys="alt+f4", lease=tok)
    _confirm(rig, r)
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_sending_flag_cleared_by_finish_not_by_estimate(rig):
    """发送中的标志由 finish 清, 不靠按长度估的时间: 估短了也不会放第二条进来."""
    tok = rig.lease()
    sid = rig.store.gate(tok, "type", text="ls")
    rig.t[0] += 10                               # 远超原来 len/20 的估算
    with pytest.raises(mc.ToolError):
        rig.store.gate(tok, "type", text="pwd")
    rig.store.finish("agent-a", "type", "ls", True, send=sid)
    assert isinstance(rig.store.gate(tok, "type", text="pwd"), str)


def test_take_refused_while_agent_sending(rig):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text="diskpart", lease=tok)
    with rig.store._tx() as (st, now, _evs):
        st["sending_until"] = now + 100          # 假装 Agent 那边有一条还在发
    with pytest.raises(ValueError):
        rig.store.take(r["id"], "human:kiosk")
    assert rig.store.view()["pending"]


def test_agent_blocked_while_confirmed_item_sending(rig):
    tok = rig.lease()
    err, r = rig.call("kvm_type", text="diskpart", lease=tok)
    rig.store.take(r["id"], "human:kiosk")       # 人确认了, 还没 finish = 正在发
    rig.t[0] += 10
    err, msg = rig.call("kvm_type", text="ls", lease=tok)
    assert err and "在发" in msg


def test_unexpected_exception_still_finishes(rig):
    tok = rig.lease()
    rig.fail = RuntimeError("bug")
    try:
        assert rig.call("kvm_type", text="ls", lease=tok)[0]
    except RuntimeError:
        pass
    rig.fail = None
    assert not rig.store.view()["sending"]
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_failure_before_any_key_keeps_line(rig):
    """代理没在跑、一个键都没发出去: 这一行还是原样, 不算看不清."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok)[1]["sent"]
    rig.open_fail = FileNotFoundError("no sock")
    assert rig.call("kvm_type", text="x", lease=tok)[0]
    rig.open_fail = None
    r = rig.call("kvm_type", text="kpart", lease=tok)[1]          # 缓冲还是 dis, 不是 disx, 也没变看不清
    assert r["status"] == "pending" and "diskpart" in r["danger"] and "看不全" not in r["reason"]


def test_state_without_blind_field_and_lease_is_unknown(rig):
    tok = rig.lease()
    doc = json.loads(open(rig.store.path).read())
    doc.pop("blind")
    open(rig.store.path, "w").write(json.dumps(doc))
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["status"] == "pending"


def test_new_lease_ls_enter_needs_one_confirmation(rig):
    """钉住: 新租约后 ls + enter=true 只要人确认一次, 之后的行照常放行."""
    tok = rig.lease(known=False)
    err, r = rig.call("kvm_type", text="ls", enter=True, lease=tok)
    assert r["status"] == "pending"
    _confirm(rig, r)
    assert rig.call("kvm_type", text="pwd", enter=True, lease=tok)[1]["sent"]
    assert len(rig.store.view()["history"]) == 1


def test_real_human_seq_reads_broker_status(monkeypatch):
    class Fake:
        def __init__(self, port, src, who):
            if port == "/gone":
                raise FileNotFoundError(port)

        def boot_status(self):
            return "b1", {"/dev/ttyX": {"human_n": 3}}

        def close(self):
            pass
    monkeypatch.setattr(mk.kvm_broker, "BrokerCh", Fake)
    monkeypatch.setattr(mk.os.path, "realpath", lambda p: "/dev/ttyX" if p == "/by-id/x" else p)
    assert mk.human_seq("/by-id/x") == ["b1", 3]
    assert mk.human_seq("/by-id/y") == ["b1", 0]
    assert mk.human_seq("/gone") is None



# ---- #64 第四轮

def _store(tmp_path, t):
    return mk.LeaseStore(str(tmp_path / "st.json"), clock=lambda: t[0], stopped=lambda: False)


def _known(store):
    with store._tx() as (st, _now, _evs):
        st["line"], st["blind"] = "", ""


def test_stale_rollback_across_leases_does_not_clear_new_lease(tmp_path):
    """A 过闸门后卡住, 租约换成 B; A 的 finish(started=False) 不能把旧行写回 B."""
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 60, {"/p": ["b", 0]})["lease"]
    _known(s)
    sid = s.gate(a, "type", text="ls", port="/p", human=["b", 0])
    s.release(a)
    t[0] += mk.SEND_GRACE_MAX + 10                 # 兜底时长过了, B 才申请得到
    b = s.acquire("B", 60, {"/p": ["b", 0]})["lease"]
    s.finish("A", "type", "ls", False, "no sock", started=False, send=sid)
    r = s.gate(b, "type", text="", enter=True, port="/p", human=["b", 0])
    assert isinstance(r, dict) and r["status"] == "pending"


def test_acquire_refused_while_sending(tmp_path):
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 60, {"/p": ["b", 0]})["lease"]
    _known(s)
    assert isinstance(s.gate(a, "type", text="ls", port="/p", human=["b", 0]), str)
    s.release(a)
    with pytest.raises(mc.ToolError):
        s.acquire("B", 60, {})


def _after_expiry_send(s, a, text):
    """兜底过期后这一行看不清 (第六轮): 先挂起; 人拒掉、假装确认过回车, 再放一条新的."""
    r = s.gate(a, "type", text=text, port="/p", human=["b", 0])
    assert isinstance(r, dict) and r["status"] == "pending"
    s.reject(r["id"], "human:kiosk")
    _known(s)
    return s.gate(a, "type", text=text, port="/p", human=["b", 0])


def test_stale_rollback_across_sends_does_not_clobber(tmp_path):
    """发送卡过兜底时长, 同一租约放了新的一条; 老的那条 finish 回滚不能盖掉新的状态."""
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 300, {"/p": ["b", 0]})["lease"]
    _known(s)
    old = s.gate(a, "type", text="ls", port="/p", human=["b", 0])
    t[0] += mk.send_grace("ls", False) + 10
    new = _after_expiry_send(s, a, "dis")
    assert isinstance(new, str) and new != old
    s.finish("A", "type", "ls", False, "no sock", started=False, send=old)
    s.finish("A", "type", "dis", True, send=new)
    r = s.gate(a, "type", text="", enter=True, port="/p", human=["b", 0])     # 不一致按看不清
    assert isinstance(r, dict) and r["status"] == "pending"


def test_stale_finish_does_not_clear_new_sending_flag(tmp_path):
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 300, {"/p": ["b", 0]})["lease"]
    _known(s)
    old = s.gate(a, "type", text="ls", port="/p", human=["b", 0])
    t[0] += mk.send_grace("ls", False) + 10
    assert isinstance(_after_expiry_send(s, a, "pwd"), str)
    s.finish("A", "type", "ls", True, send=old)
    assert s.view()["sending"]


@pytest.mark.parametrize("field,val", [("blind", []), ("blind", {}), ("blind", 3), ("before", {"line": [], "blind": {}}),
                                       ("before", [1, []]), ("human_seen", []), ("sending_id", [])])
def test_corrupt_field_types_do_not_crash(rig, field, val):
    tok = rig.lease()
    doc = json.loads(open(rig.store.path).read())
    doc[field] = val
    open(rig.store.path, "w").write(json.dumps(doc))
    rig.store.view()
    err, r = rig.call("kvm_type", text="ls", lease=tok)
    assert not err
    if field == "blind":
        assert r["status"] == "pending"


def test_broker_restart_counter_collision_is_change(rig):
    """代理重启后次数从 0 重数, 可能撞上旧值: 比 (启动 id, 次数)."""
    tok = rig.lease()
    assert rig.call("kvm_type", text="ls", lease=tok)[1]["sent"]
    rig.boot = "boot2"
    assert rig.call("kvm_key", keys="enter", lease=tok)[1]["status"] == "pending"


def test_human_seq_unknown_both_sides_is_change(tmp_path):
    """代理拿不到 (None) 两头都 None: 按变了算."""
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 60, {"/p": None})["lease"]
    _known(s)
    r = s.gate(a, "type", text="ls", port="/p", human=None)
    assert isinstance(r, dict) and r["status"] == "pending"


def test_who_failure_leaves_no_sending_flag(rig):
    """who() 出错也不能留下"在发"标志 (挪到过闸门之前)."""
    n = [0]

    def who():
        n[0] += 1
        if n[0] > 1:
            raise RuntimeError("bug")
        return "agent-a"
    tools = mk.build_tools(rig.store, ports=lambda: rig.ports, opener=rig.opener, ocr=None, wol=None,
                           broker_up=lambda p: True, who=who, human_seq=lambda p: [rig.boot, rig.human_n])
    srv = ms.Server(tools)

    def call(name, **a):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": a}}, "agent-a")
    tok = json.loads(call("kvm_lease_acquire", ttl_s=60)["result"]["content"][0]["text"])["lease"]
    rig.known()
    try:
        call("kvm_type", text="ls", lease=tok)
    except RuntimeError:
        pass
    assert not rig.store.view()["sending"]


def test_switching_keyboard_makes_line_unknown(rig):
    """行缓冲不分键鼠: 换一块键鼠发, 这一行看不清."""
    rig.ports = {"S1": "/p1", "S2": "/p2"}
    tok = rig.lease()
    assert rig.call("kvm_type", text="dis", lease=tok, serial="S1")[1]["sent"]
    r = rig.call("kvm_type", text="ls", lease=tok, serial="S2")[1]
    assert r["status"] == "pending" and "看不全" in r["reason"]


# ---- #64 第五轮

def test_stale_half_sent_then_new_rollback(tmp_path):
    """老的一条发了一半才回报, 新的一条一个没发: 不能用新的 before 把行洗干净."""
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 300, {"/p": ["b", 0]})["lease"]
    _known(s)
    old = s.gate(a, "type", text="ls", port="/p", human=["b", 0])
    t[0] += mk.send_grace("ls", False) + 10
    new = _after_expiry_send(s, a, "x")
    s.finish("A", "type", "ls", False, "timeout", started=True, send=old)
    s.finish("A", "type", "x", False, "no sock", started=False, send=new)
    r = s.gate(a, "type", text="", enter=True, port="/p", human=["b", 0])
    assert isinstance(r, dict) and r["status"] == "pending"


@pytest.mark.parametrize("text,enter,n", [("", False, 1), ("ls", False, 1), ("ls", True, 2), ("x" * 1000, True, 2)],
                         ids=["空", "打字", "打字+回车", "满长+回车"])
def test_send_grace_covers_broker_client_timeout(text, enter, n):
    """兜底不能短于代理正常最长耗时 (人在操作时 agent 请求排队), 每个请求一份客户端超时."""
    import kvm_broker
    assert mk.send_grace(text, enter) >= n * kvm_broker.CLIENT_TIMEOUT_S + len(text) * 0.2
    assert mk.send_grace(text, enter) <= mk.SEND_GRACE_MAX


def test_gate_sending_lasts_past_broker_queue(tmp_path):
    import kvm_broker
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 300, {"/p": ["b", 0]})["lease"]
    _known(s)
    s.gate(a, "type", text="ls", port="/p", human=["b", 0])
    t[0] += kvm_broker.CLIENT_TIMEOUT_S - 1
    assert s.view()["sending"]


@pytest.mark.parametrize("val", [1e300, float("inf"), "nan"])
def test_absurd_sending_until_is_clamped(tmp_path, val):
    """极大值 / Infinity / 墙钟回拨: 截到 now + 最大兜底, 不会永远拒绝."""
    t = [1000.0]
    s = _store(tmp_path, t)
    doc = {"sending_until": val}
    raw = json.dumps(doc).replace('"nan"', "NaN")
    open(s.path, "w").write(raw)
    s.view()
    with pytest.raises(mc.ToolError):                # 截到 now + 最大兜底: 先拒
        s.acquire("A", 60, {})
    t[0] += mk.SEND_GRACE_MAX + 1
    s.acquire("A", 60, {})


def test_estop_clears_sending(rig):
    """人在车上按全部急停: "在发"标志清掉, 解除后能重新申请."""
    tok = rig.lease()
    sid = rig.store.gate(tok, "type", text="ls", port=PORT, human=[rig.boot, rig.human_n])
    assert isinstance(sid, str)
    rig.stopped = True
    rig.store.view()
    rig.stopped = False
    assert not rig.store.view()["sending"]
    rig.t[0] += mk.ESTOP_SETTLE_S
    rig.lease()


def test_pending_remembers_port_and_confirm_fails_if_gone(rig):
    """挂起时的口存进条目; 确认时那块键鼠不在了就算失败, 不重新挑口."""
    rig.ports = {"S1": "/p1"}
    tok = rig.lease()
    err, r = rig.call("kvm_type", text="diskpart", lease=tok)
    rig.ports = {"S2": "/p2"}
    with pytest.raises(ValueError):
        _confirm(rig, r)
    assert not [o for o in rig.opened if o[0] == "/p2"]


def test_confirm_uses_remembered_port(rig):
    rig.ports = {"S1": "/p1"}
    tok = rig.lease()
    err, r = rig.call("kvm_type", text="diskpart", lease=tok)
    rig.ports = {"S2": "/p2", "S1": "/p1"}
    _confirm(rig, r)
    assert rig.opened[-1][0] == "/p1"


def test_store_switching_port_is_unknown(tmp_path):
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 60, {"/p1": ["b", 0], "/p2": ["b", 0]})["lease"]
    _known(s)
    sid = s.gate(a, "type", text="ls", port="/p1", human=["b", 0])
    s.finish("A", "type", "ls", True, send=sid)
    r = s.gate(a, "type", text="x", port="/p2", human=["b", 0])
    assert isinstance(r, dict) and "看不全" in r["reason"]



# ---- #64 第六轮

@pytest.mark.parametrize("how", ["gate", "take"])
def test_expired_send_without_report_makes_line_unknown(tmp_path, how):
    """进程在打字和回车之间死了, 没回报: 兜底过期后这一行看不清, 下一个回车要人确认."""
    t = [0.0]
    s = _store(tmp_path, t)
    a = s.acquire("A", 300, {"/p": ["b", 0]})["lease"]
    _known(s)
    if how == "gate":
        assert isinstance(s.gate(a, "type", text="ls", enter=True, port="/p", human=["b", 0]), str)
    else:
        r = s.gate(a, "type", text="diskpart", enter=True, port="/p", human=["b", 0])
        s.take(r["id"], "human:kiosk")
    t[0] += mk.send_grace("ls", True) + 1 if how == "gate" else mk.send_grace("diskpart", True) + 1
    r = s.gate(a, "type", text="", enter=True, port="/p", human=["b", 0])
    assert isinstance(r, dict) and r["status"] == "pending"


def test_no_lease_right_after_estop_release(rig):
    """急停清了"在发", 代理那边排着的请求要人停手 QUIET_S 才会丢完: 解除后这段时间不给租约."""
    import kvm_broker_core as kbc
    rig.stopped = True
    rig.store.view()
    rig.stopped = False
    err, msg = rig.call("kvm_lease_acquire", ttl_s=60)
    assert err and "急停" in msg
    rig.t[0] += kbc.QUIET_S + 1.5
    rig.lease()
