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

    def type_text(self, text):
        self._go("type", text)

    def close(self):
        self.log.append(("close",))


class Rig:
    def __init__(self, tmp_path):
        self.t = [1000.0]
        self.stopped = False
        self.opened, self.sent, self.fail, self.open_fail = [], [], None, None
        self.ports = {"SER1": PORT}
        self.wols = []
        self.ocr_lines = [{"text": "Media Present", "x": 1, "y": 2, "w": 3, "h": 4, "conf": 90.0}]
        self.store = mk.LeaseStore(str(tmp_path / "kvm-lease.json"), clock=lambda: self.t[0],
                                   stopped=lambda: self.stopped)
        tools = mk.build_tools(self.store, ports=lambda: self.ports, opener=self.opener,
                               ocr=lambda dev, lang: (self.ocr_lines, "卡2 (/dev/video3) 当前画面"),
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
    err, _ = rig.call("kvm_key", keys="ctrl+alt+delete", lease=tok)
    assert not err
    assert rig.sent[0] == ("keys", ("ctrl", "alt", "delete")) and rig.opened == [(PORT, "agent-a")]
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
    assert not err and tok not in json.dumps(st) and st["writer"] == "agent-a"


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
