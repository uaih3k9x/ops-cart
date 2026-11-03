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
        self.ocr_lines = [{"text": "Media Present", "x": 1, "y": 2, "w": 3, "h": 4, "conf": 90.0}]
        self.log = tmp_path / "kvm-thread.log"
        self.events = []
        self.store = mk.LeaseStore(str(tmp_path / "kvm-lease.json"), clock=lambda: self.t[0],
                                   stopped=lambda: self.stopped, log_path=str(self.log),
                                   event=lambda tag, msg: self.events.append((tag, msg)))
        tools = mk.build_tools(self.store, ports=lambda: self.ports, opener=self.opener,
                               ocr=lambda dev, lang: (self.ocr_lines, "卡2 (/dev/video3) 当前画面", self.engine),
                               wol=lambda mac, iface: self.wols.append((mac, iface)),
                               broker_up=lambda port: True)
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

    def lease(self, **a):
        err, r = self.call("kvm_lease_acquire", **a)
        assert not err, r
        return r["lease"]


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
    tok = rig.lease()
    rig.ports = {}
    err, msg = rig.call("kvm_key", keys="a", lease=tok)
    assert err and "键鼠" in msg
    rig.ports = {"S1": "/p1", "S2": "/p2"}
    err, msg = rig.call("kvm_key", keys="a", lease=tok)
    assert err and "S1" in msg
    assert not rig.call("kvm_key", keys="a", lease=tok, serial="S2")[0]
    assert rig.opened[-1][0] == "/p2"


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
