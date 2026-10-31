"""核心不变量 (#24): 没有人在车机本机照抄确认码, 任何 MCP 调用都不能让任何 MAC 拿到装机 / 克隆菜单.

一台完整的 MCP 服务 (只读工具 + 写串口 + 标签 + PXE 申请, 和 cart_mcp.real_tools 同一套), tftp / 克隆配置 / 状态文件
都在 tmp, 清单和上锁用真的 kiosk_core; kiosk_core.pxe_arm 换成记账的包装 (照样真写 tmp 里的菜单).
每个场景下把所有工具 x 参数组合 (含伪造 / 偷到的确认码和摘要、多余参数、篡改 mac / batch) 都调一遍,
然后看: pxe_arm 一次没被调、tftp 里没有只给解锁机器的菜单、pxe-mode.json 不是 install.
最后一组反过来证明这套检查不是摆设: 车机上人照抄确认码就真的解锁了.
"""

import itertools
import json
import os

import pytest

import cart_mcp as cm
import kiosk_core as kc
import mcp_core as mc
import mcp_label as ml
import mcp_pxe as mp
import mcp_serial as ms
import pxe_req_core as pr
import pxeserver as px
import serial_write_core as sw

T0 = 1_700_000_000.0
MAC, MAC2, MAC3 = "02:00:00:00:00:01", "02:00:00:00:00:02", "52:54:00:aa:bb:03"
DISK1 = {"name": "nvme0n1", "model": "EXAMPLE NVME 512GB", "serial": "SN-EXAMPLE-1", "size": 512110190592,
         "type": "disk", "tran": "nvme"}
BY = "human:kiosk@local"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Cart:
    """tmp 里的一台推车: tftp (Clonezilla / WinPE 文件齐)、克隆配置和清单、装机口状态、串口和 PXE 申请状态."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.root = tmp_path / "tftp"
        for sub, names in (("clonezilla", px.CLONE_FILES), ("win", px.WINPE_FILES)):
            (self.root / sub).mkdir(parents=True)
            for n in names:
                (self.root / sub / n).write_bytes(b"x")
        (tmp_path / "repo" / "lab-img").mkdir(parents=True)
        self.conf = tmp_path / "clone.json"
        self.conf.write_text(json.dumps({"proto": "smb", "user": "clonesave", "password": "Example-pass1",
                                         "save_share": "clone-rw", "restore_share": "clone-ro",
                                         "local_dir": str(tmp_path / "repo")}))
        self.batch = tmp_path / "clone-batch.json"
        self.write_batch([MAC, MAC2])
        self.mode_path = str(tmp_path / "pxe-mode.json")
        self.clock = Clock()
        monkeypatch.setattr(kc, "_pxe_event", lambda msg: None)
        kc.pxe_disarm(root=self.root, path=self.mode_path)        # 开机复位: 只有救援菜单

        # pxe_arm 换成记账的包装, 照样真写 tmp 里的菜单
        self.arms, real_arm = [], kc.pxe_arm

        def spy(mac, **kw):
            self.arms.append((mac, kw.get("purpose")))
            kw.update(root=self.root, path=self.mode_path, conf_path=self.conf, batch_path=self.batch)
            return real_arm(mac, **kw)
        monkeypatch.setattr(kc, "pxe_arm", spy)

        self.serial = ms.Store(str(tmp_path / "run" / "serial-write.json"), str(tmp_path / "slog"),
                               clock=self.clock, event=None, on_estop_all=self._drop)
        self.pxe = mp.Store(str(tmp_path / "run" / "pxe-req.json"), str(tmp_path / "log" / "pxe-req.log"),
                            clock=self.clock, event=None, stopped=lambda: mp.serial_stopped(self.serial.path))
        self.server = ms.Server(self._tools())

    def _drop(self, by):
        self.pxe.run(lambda b, now, _st: b.drop("system", now, f"急停 (by {by})"))

    def write_batch(self, macs):
        self.batch.write_text(json.dumps({"image": "lab-img", "rule": {"model": DISK1["model"]}, "machines": [
            {"mac": m, "disks": [dict(DISK1, serial=f"SN-{i}")]} for i, m in enumerate(macs)]}))

    def plan(self, purpose, mac):
        if purpose in ("clone-save", "clone-restore"):
            return kc.clone_plan(purpose, mac=mac, root=self.root, conf_path=self.conf, batch_path=self.batch)
        return pr.simple_plan(purpose, mac, kc.winpe_missing(self.root) if purpose == "winpe" else ())

    def mode(self):
        return kc.pxe_mode_read(self.mode_path, self.clock())

    def _tools(self):
        slots = [{"dev": f"/dev/con-{n}", "present": True, "baud": "9600", "count": 0} for n in sw.SLOTS]
        ro = cm.build_tools(slots=lambda: slots, tail=lambda dev, n: "<SW1>", snap=lambda dev: (b"png", "video0"),
                            state=lambda: {"ts": self.clock()},
                            pxe=lambda: cm.read_pxe(self.mode_path, str(self.root), str(self.tmp / "guard.json"),
                                                    self.clock()), now=self.clock)
        serial = ms.build_tools(self.serial, slots=lambda: slots, tail=lambda dev, n: "<SW1>",
                                send=lambda dev, text, timeout, raw: ("sent", "<SW1>", None))
        label = ml.build_label_tools(write=lambda data: None, status=lambda: 0, abbrev=lambda: ({}, "x"))
        pxe = mp.build_tools(self.pxe, plan=self.plan, mode=self.mode,
                             disarm=lambda: kc.pxe_disarm(root=self.root, path=self.mode_path),
                             retire=lambda macs: kc.pxe_retire(macs, root=self.root))
        return ro + serial + label + pxe

    # ---- 看

    def unlocked(self):
        """装机口有没有给任何 MAC 开: pxe-mode.json 是 install, 或 tftp 树里有只给解锁机器的菜单."""
        menus = []
        for d, _dirs, files in os.walk(self.root):
            for name in files:
                if name == "grub.cfg" or name.startswith("grub.cfg-"):
                    with open(os.path.join(d, name), errors="replace") as f:
                        if px.unlock_only_menu(f.read()):
                            menus.append(name)
        return self.mode()["mode"] == "install" or bool(menus)

    def secret(self):
        return mp.peek(self.pxe.path, clock=self.clock, stopped=lambda: False)["pending"]

    def call(self, name, args, who="stdio:agent@198.51.100.7"):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}}, who)

    def human_confirm(self, iid, typed, shown):
        return mp.human_confirm(self.pxe, iid, typed, shown, BY, plan=self.plan, mode=self.mode,
                                arm=lambda it, p: kc.pxe_arm(it["mac"], purpose=it["purpose"], confirm=p["digest"]))


@pytest.fixture
def cart(tmp_path, monkeypatch):
    return Cart(tmp_path, monkeypatch)


# ------------------------------------------------------------ 参数组合

def _values(spec, loot):
    """一个参数能试的值: 合法的、边界的、类型错的, 再加偷来的确认码 / 摘要 / 编号."""
    t = spec.get("type")
    if "enum" in spec:
        vals = list(spec["enum"]) + ["install", ""]
    elif t == "integer":
        vals = [spec.get("minimum", 0), spec.get("maximum", 1), 0, -1, 10 ** 9, "1", True]
    elif t == "boolean":
        vals = [True, False, "true", 1]
    else:
        vals = [MAC, MAC2.upper().replace(":", "-"), MAC3, "", "x" * 300, "not-a-mac"] + loot
    return vals


EXTRA = ("code", "confirm", "digest", "id", "typed", "arm", "by", "force")


def _combos(tool, loot):
    """全部参数的笛卡尔积 (参数少的工具) 或逐个替换 (参数多的), 再加每个多余参数带偷来的值."""
    params = tool["inputSchema"]["properties"]
    req = tool["inputSchema"].get("required", [])
    keys = sorted(params)
    choices = {k: _values(params[k], loot) + ([] if k in req else [None]) for k in keys}
    out = []
    if len(keys) <= 3:
        for vals in itertools.product(*(choices[k] for k in keys)):
            out.append({k: v for k, v in zip(keys, vals) if v is not None})
    else:
        base = {k: choices[k][0] for k in keys}
        for k in keys:
            for v in choices[k]:
                a = dict(base)
                if v is None:
                    a.pop(k)
                else:
                    a[k] = v
                out.append(a)
    base = out[0] if out else {}
    for k in EXTRA:
        for v in loot or ["000000"]:
            out.append(dict(base, **{k: v}))
    return out


def _loot(cart):
    """Agent 最好的情况: 偷到了车机屏幕上的确认码、清单摘要和编号 (正常拿不到)."""
    out = []
    for p in cart.secret():
        out += [p["code"], p["digest"], " ".join(p["code"]), str(p["id"])]
    return out or ["000000", "0" * 16]


def _hammer(cart, rounds=1):
    """所有工具 x 所有组合打一遍 (stdio 和 HTTP 两种身份), 返回调了多少次."""
    tools = cart.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    # 申请放最前、急停放最后: 不然一上来全部急停, 后面的申请全被拒, 打不到"有申请挂着"的路径
    tools.sort(key=lambda t: (t["name"] != "pxe_request", t["name"] == "estop"))
    names = [t["name"] for t in tools]
    assert {"pxe_request", "pxe_requests", "pxe_retire", "pxe_status", "estop", "send_command"} <= set(names)
    n = 0
    for _ in range(rounds):
        loot = _loot(cart)
        for tool in tools:
            for args in _combos(tool, loot):
                for who in ("stdio:agent@198.51.100.7", "tok@100.64.0.5"):
                    cart.call(tool["name"], args, who)
                    n += 1
        for name in ("pxe_arm", "pxe_confirm", "confirm", "resume", "human_confirm", "arm"):
            cart.call(name, {"id": 1, "code": loot[0]})
            n += 1
    return n


def _assert_locked(cart):
    assert cart.arms == [], "MCP 调用让 pxe_arm 被调了"
    assert not cart.unlocked(), "MCP 调用让某台机器拿到了装机 / 克隆菜单"


# ------------------------------------------------------------ 场景

def test_fresh_cart(cart):
    assert _hammer(cart) > 1000
    _assert_locked(cart)
    with open(cart.pxe.log_path) as f:              # 打的组合里确实有申请成功挂起的, 不是全被参数校验挡掉
        evs = [json.loads(x)["ev"] for x in f]
    assert "request" in evs and "retire" in evs


def test_with_pending_requests_and_stolen_codes(cart):
    for args in ({"purpose": "rocky", "mac": MAC3}, {"purpose": "clone-restore", "batch": True},
                 {"purpose": "winpe", "mac": MAC2}):
        res = cart.call("pxe_request", args)["result"]
        assert not res["isError"], res
        text = res["content"][0]["text"]
        assert all(p["code"] not in text and p["digest"] not in text for p in cart.secret())
    assert len(cart.secret()) == 3
    _hammer(cart, rounds=2)
    _assert_locked(cart)


def test_after_expiry(cart):
    cart.call("pxe_request", {"purpose": "rocky", "mac": MAC3})
    loot = _loot(cart)
    cart.clock.t += pr.REQ_TTL_S + 1
    _hammer(cart)
    _assert_locked(cart)
    with pytest.raises(pr.Refused):                  # 过期后人照抄也不行
        cart.human_confirm(1, loot[0], loot[1])
    _assert_locked(cart)


def test_after_estop(cart):
    cart.call("pxe_request", {"purpose": "clone-restore", "batch": True})
    loot = _loot(cart)
    cart.call("estop", {})                            # Agent 自己全部急停 -> 申请作废
    assert cart.secret() == []
    hist = mp.peek(cart.pxe.path, clock=cart.clock, stopped=lambda: False)["history"]
    assert hist[-1]["status"] == "dropped"
    assert cart.call("pxe_request", {"purpose": "rocky", "mac": MAC3})["result"]["isError"]
    _hammer(cart)
    _assert_locked(cart)
    with pytest.raises(pr.Refused):                  # 急停后人照抄旧码也不行
        cart.human_confirm(1, loot[0], loot[1])
    _assert_locked(cart)


def test_replay_after_legit_confirm(cart):
    cart.call("pxe_request", {"purpose": "rocky", "mac": MAC3})
    p = cart.secret()[0]
    cart.human_confirm(p["id"], p["code"], p["digest"])     # 人确认过一次
    assert cart.arms == [(MAC3, "rocky")] and cart.unlocked()
    cart.call("pxe_retire", {})                       # Agent 提前上锁
    assert not cart.unlocked()
    cart.arms.clear()
    _hammer(cart)                                     # 拿着旧码 / 旧摘要 / 旧编号重放
    _assert_locked(cart)
    with pytest.raises(pr.Refused):
        cart.human_confirm(p["id"], p["code"], p["digest"])
    _assert_locked(cart)


def test_tampered_batch_refused_even_with_right_code(cart):
    cart.call("pxe_request", {"purpose": "clone-restore", "batch": True})
    p = cart.secret()[0]
    cart.write_batch([MAC, MAC2, MAC3])               # 申请之后清单被改 (多塞一台)
    with pytest.raises(pr.Refused) as e:
        cart.human_confirm(p["id"], p["code"], p["digest"])
    assert e.value.code == "changed"
    _hammer(cart)
    _assert_locked(cart)


def test_tampered_mac_refused(cart, tmp_path):
    os.rmdir(tmp_path / "repo" / "lab-img")          # 抓镜像: 镜像名还不在仓库里
    assert not cart.call("pxe_request", {"purpose": "clone-save", "mac": MAC})["result"]["isError"]
    p = cart.secret()[0]
    cart.write_batch([MAC2])                          # 那台从清单里拿掉了
    with pytest.raises(pr.Refused):
        cart.human_confirm(p["id"], p["code"], p["digest"])
    other = cart.call("pxe_request", {"purpose": "rocky", "mac": MAC2})["result"]
    assert not other["isError"]
    q = cart.secret()[0]
    with pytest.raises(pr.Refused):                   # 拿别的申请的摘要来确认这一条
        cart.human_confirm(q["id"], q["code"], p["digest"])
    _assert_locked(cart)


def test_wrong_and_forged_codes_on_kiosk(cart):
    cart.call("pxe_request", {"purpose": "rocky", "mac": MAC3})
    p = cart.secret()[0]
    for typed in ("", "000000", p["digest"], p["code"] + "0", p["code"][:-1], None):
        if typed == p["code"]:
            continue
        with pytest.raises(pr.Refused):
            cart.human_confirm(p["id"], typed, p["digest"])
    with pytest.raises(pr.Refused):                   # 码换过了, 旧码作废
        cart.human_confirm(p["id"], p["code"], p["digest"])
    _assert_locked(cart)


def test_retire_only_tightens(cart):
    cart.call("pxe_request", {"purpose": "clone-restore", "batch": True})
    p = cart.secret()[0]
    cart.human_confirm(p["id"], p["code"], p["digest"])
    assert set(cart.mode()["macs"]) == {MAC, MAC2}
    cart.arms.clear()
    cart.call("pxe_retire", {"mac": MAC2})            # 一批里撤一台: 只删它的菜单
    assert not (cart.root / kc.mac_cfg_name(MAC2)).exists() and (cart.root / kc.mac_cfg_name(MAC)).exists()
    cart.call("pxe_retire", {"mac": MAC3})            # 没解锁的: 什么都不动
    assert (cart.root / kc.mac_cfg_name(MAC)).exists()
    cart.call("pxe_retire", {})
    assert cart.arms == [] and not cart.unlocked()


# ------------------------------------------------------------ 反证: 检查不是摆设

@pytest.mark.parametrize("args", [{"purpose": "rocky", "mac": MAC3}, {"purpose": "winpe", "mac": MAC3},
                                  {"purpose": "clone-save", "mac": MAC}, {"purpose": "clone-restore", "batch": True}])
def test_human_with_right_code_does_unlock(tmp_path, monkeypatch, args):
    cart = Cart(tmp_path, monkeypatch)
    if args["purpose"] == "clone-save":
        os.rmdir(tmp_path / "repo" / "lab-img")      # 抓镜像: 镜像名还不在仓库里 (不覆盖)
    assert not cart.call("pxe_request", args)["result"]["isError"]
    p = cart.secret()[0]
    msg = cart.human_confirm(p["id"], p["code"], p["digest"])
    assert "已解锁" in msg and len(cart.arms) == 1 and cart.unlocked()
