"""notify_core: 当前状态 → 和上次比 → 只在变化时出事件; 防抖、冷却合并、持久化."""

import json
import pathlib

import pytest

import notify_core as nc

T0 = 1_800_000_000.0
OL = {"percent": 100, "state": "charging", "status": "OL"}
OB = {"percent": 92, "state": "discharging", "status": "OB DISCHRG"}
LOW = {"percent": 12, "state": "discharging", "status": "OB DISCHRG"}


def snap(ups=OL, guard="serving", auth="online", svc=None, boot=("b1", True), ts=T0, **over):
    """拼一份 collect() 的输入: 每个读取函数都是假的."""
    services = {"pxe-server": "active", "kiosk": "active", "cart-dash": "active"}
    services.update(svc or {})
    read = {
        "state": lambda: {"ts": ts, "ups": ups},
        "guard": lambda: None if guard is None else {"state": guard, "detail": "x", "iface": "eno1"},
        "auth": lambda: None if auth is None else {"state": auth, "ok": auth in ("online", "login ok", "skip"),
                                                   "message": "密码不对" if auth == "login fail" else ""},
        "services": lambda: services,
        "boot": lambda: {"id": boot[0], "ok": boot[1]},
        "now": lambda: ts,
    }
    read.update(over)
    return nc.collect(read)


def run(prev, cur, now):
    events, state = nc.diff(prev, cur, now)
    return [e["key"] for e in events], events, state


# ---------------------------------------------------------------- 采集

def test_collect_maps_sources_to_values():
    cur = snap(ups=OB, guard="proxy", auth="login fail", svc={"kiosk": "failed"})
    v = cur["values"]
    assert v["ups.power"] == "OB" and v["ups.low"] is False and cur["info"]["pct"] == 92
    assert v["guard"] == "proxy"
    assert v["auth"] == "fail" and cur["info"]["auth_state"] == "login fail"
    assert v["svc.kiosk"] == "down" and v["svc.pxe-server"] == "up" and v["svc.cart-dash"] == "up"
    assert cur["boot"] == {"id": "b1", "ok": True}


def test_collect_low_battery_uses_ups_core_threshold():
    assert snap(ups=LOW)["values"]["ups.low"] is True


def test_collect_unknowns_are_none_and_never_raise():
    def boom():
        raise OSError("no")
    cur = nc.collect({"state": boom, "guard": boom, "auth": boom, "services": boom, "boot": boom,
                      "now": lambda: T0})
    assert set(cur["values"].values()) == {None}
    assert cur["boot"] == {"id": None, "ok": None}


def test_collect_stale_dash_snapshot_means_ups_unknown():
    cur = snap(ups=OB, ts=T0, now=lambda: T0 + nc.STATE_STALE_S + 1)
    assert cur["values"]["ups.power"] is None and cur["values"]["ups.low"] is None


def test_collect_guard_transient_states_are_unknown_and_stop_states_merge():
    assert snap(guard="probing")["values"]["guard"] is None
    assert snap(guard="starting")["values"]["guard"] is None
    assert snap(guard="no-link")["values"]["guard"] == "stop"
    assert snap(guard="no-ip")["values"]["guard"] == "stop"


def test_collect_guard_ignored_while_pxe_server_down():
    cur = snap(guard="serving", svc={"pxe-server": "inactive"})
    assert cur["values"]["guard"] is None and cur["values"]["svc.pxe-server"] == "down"


def test_collect_service_activating_is_unknown():
    assert snap(svc={"kiosk": "activating"})["values"]["svc.kiosk"] is None


# ---------------------------------------------------------------- diff

def test_first_run_normal_is_silent_and_same_state_is_silent():
    keys, _, st = run(None, snap(), T0)
    assert keys == []
    keys, _, st = run(st, snap(), T0 + 30)
    assert keys == []


def test_first_run_abnormal_reports():
    keys, ev, _ = run(None, snap(ups=OB), T0)
    assert keys == ["ups.power"]


def test_ups_on_battery_then_back():
    _, _, st = run(None, snap(), T0)
    keys, ev, st = run(st, snap(ups=OB), T0 + 30)
    assert keys == ["ups.power"]
    assert ev[0]["text"] == "推车转电池供电 92%，市电断了"
    assert ev[0]["prio"] >= 4
    keys, ev, st = run(st, snap(ups=OB), T0 + 60)
    assert keys == []                                   # 状态不变不推
    t = T0 + 30 + nc.RULES["ups.power"][1]
    keys, ev, st = run(st, snap(ups=OL), t)
    assert keys == ["ups.power"] and ev[0]["text"].startswith("推车市电恢复")


def test_low_battery_reports_once_and_its_recovery_is_quiet():
    _, _, st = run(None, snap(ups=OB), T0)
    keys, ev, st = run(st, snap(ups=LOW), T0 + 30)
    assert keys == ["ups.low"] and "12%" in ev[0]["text"] and ev[0]["prio"] == 5
    keys, _, st = run(st, snap(ups=LOW), T0 + 60)
    assert keys == []
    keys, ev, st = run(st, snap(ups=OL), T0 + 4000)
    assert keys == ["ups.power"]                        # 低电量解除不单独推, 市电恢复那条就够


def test_cooldown_merges_repeats_then_reports_final_state():
    _, _, st = run(None, snap(), T0)
    keys, _, st = run(st, snap(ups=OB), T0 + 30)
    assert keys == ["ups.power"]
    keys, _, st = run(st, snap(ups=OL), T0 + 60)        # 冷却期内: 不推
    assert keys == []
    keys, _, st = run(st, snap(ups=OB), T0 + 90)
    keys2, _, st = run(st, snap(ups=OL), T0 + 120)
    assert keys == keys2 == []
    keys, ev, st = run(st, snap(ups=OL), T0 + 30 + nc.RULES["ups.power"][1])
    assert keys == ["ups.power"] and "市电恢复" in ev[0]["text"]
    assert "冷却期内还变过" in ev[0]["text"]


def test_cooldown_ends_on_same_state_is_silent():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(ups=OB), T0 + 30)
    _, _, st = run(st, snap(ups=OL), T0 + 60)
    _, _, st = run(st, snap(ups=OB), T0 + 90)          # 又回到已经推过的状态
    keys, _, _ = run(st, snap(ups=OB), T0 + 30 + nc.RULES["ups.power"][1] + 1)
    assert keys == []


def test_short_flap_reports_once():
    """守卫 serving → proxy 只闪了一下就回来: 只推一条"短暂…已回到…", 不推两条."""
    _, _, st = run(None, snap(), T0)
    keys, _, st = run(st, snap(guard="proxy"), T0 + 30)
    assert keys == []                                   # 还没稳住 (settle)
    keys, ev, st = run(st, snap(guard="serving"), T0 + 35)
    assert keys == ["guard"] and "短暂" in ev[0]["text"] and ev[0]["prio"] <= 3
    keys, _, _ = run(st, snap(guard="serving"), T0 + 65)
    assert keys == []


def test_settled_change_reports_after_settle():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(guard="proxy"), T0 + 30)
    keys, ev, st = run(st, snap(guard="proxy"), T0 + 30 + nc.RULES["guard"][0])
    assert keys == ["guard"] and ev[0]["text"].startswith("装机口转代理模式")


def test_guard_first_value_is_baseline():
    keys, _, st = run(None, snap(guard="proxy"), T0)
    assert keys == []
    keys, _, _ = run(st, snap(guard=None), T0 + 30)     # 读不到: 不算变化
    assert keys == []


def test_unknown_does_not_clear_state():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(ups=OB), T0 + 30)
    _, _, st = run(st, snap(ups=None), T0 + 400)
    keys, _, _ = run(st, snap(ups=OB), T0 + 430)
    assert keys == []


def test_service_down_and_up():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(svc={"kiosk": "failed"}), T0 + 30)
    keys, ev, st = run(st, snap(svc={"kiosk": "failed"}), T0 + 60)
    assert keys == ["svc.kiosk"]
    assert ev[0]["text"] == "推车服务 kiosk 挂了（failed），看 journalctl -u kiosk"
    t = T0 + 60 + nc.RULES["svc"][1]
    _, _, st = run(st, snap(), t)
    keys, ev, _ = run(st, snap(), t + 30)
    assert keys == ["svc.kiosk"] and ev[0]["text"] == "推车服务 kiosk 恢复运行"


def test_service_restart_blip_reports_once():
    _, _, st = run(None, snap(), T0)
    _, _, st = run(st, snap(svc={"cart-dash": "failed"}), T0 + 30)
    keys, ev, _ = run(st, snap(), T0 + 60)
    assert keys == ["svc.cart-dash"] and "短暂" in ev[0]["text"]


def test_auth_fail_and_recover_without_secrets():
    _, _, st = run(None, snap(), T0)
    keys, ev, st = run(st, snap(auth="login fail"), T0 + 120)
    keys, ev, st = run(st, snap(auth="login fail"), T0 + 240)
    assert keys == ["auth"]
    assert ev[0]["text"].startswith("校园网认证失败（login fail）")
    t = T0 + 240 + nc.RULES["auth"][1]
    _, _, st = run(st, snap(), t)
    keys, ev, _ = run(st, snap(), t + 200)
    assert keys == ["auth"] and ev[0]["text"].startswith("校园网认证恢复")


def test_auth_untrusted_message_omits_host():
    _, _, st = run(None, snap(), T0)
    cur = snap(auth="untrusted")
    cur["info"]["auth_msg"] = "portal.example.invalid"
    _, _, st = run(st, cur, T0 + 120)
    _, ev, _ = run(st, cur, T0 + 300)
    assert "无线面板" in ev[0]["text"] and "example" not in ev[0]["text"]


def test_auth_skip_counts_ok():
    keys, _, _ = run(None, snap(auth="skip"), T0)
    assert keys == []


def test_boot_bad_previous_boot_reports_once():
    _, _, st = run(None, snap(boot=("b1", False)), T0)       # 这次启动还没走到 boot-success
    keys, ev, st = run(st, snap(boot=("b2", False)), T0 + 100)  # 换了 boot id, 上次一直没成功
    assert keys == ["boot"] and "非正常重启" in ev[0]["text"]
    keys, _, st = run(st, snap(boot=("b2", True)), T0 + 200)
    assert keys == []
    keys, _, _ = run(st, snap(boot=("b3", False)), T0 + 300)  # 上一次成功过
    assert keys == []


def test_boot_ok_is_sticky_within_boot():
    _, _, st = run(None, snap(boot=("b1", True)), T0)
    _, _, st = run(st, snap(boot=("b1", None)), T0 + 30)     # 读不到不覆盖
    keys, _, _ = run(st, snap(boot=("b2", None)), T0 + 60)
    assert keys == []


def test_boot_unknown_source_never_reports():
    _, _, st = run(None, snap(boot=(None, None)), T0)
    keys, _, _ = run(st, snap(boot=(None, None)), T0 + 30)
    assert keys == []


def test_boot_unknown_success_never_reports():
    _, _, st = run(None, snap(boot=("b1", None)), T0)        # 判断不了成没成功
    keys, _, _ = run(st, snap(boot=("b2", None)), T0 + 30)
    assert keys == []


def test_first_ever_boot_record_does_not_report():
    keys, _, _ = run(None, snap(boot=("b9", None)), T0)
    assert keys == []


def test_diff_is_pure():
    prev = run(None, snap(), T0)[2]
    frozen = json.dumps(prev, sort_keys=True)
    nc.diff(prev, snap(ups=OB), T0 + 30)
    assert json.dumps(prev, sort_keys=True) == frozen


def test_messages_are_one_line():
    _, _, st = run(None, snap(), T0)
    cur = snap(auth="login fail")
    cur["info"]["auth_msg"] = "多行\n消息"
    _, _, st = run(st, cur, T0 + 120)
    _, ev, _ = run(st, cur, T0 + 300)
    assert "\n" not in ev[0]["text"]


# ---------------------------------------------------------------- 持久化

def test_state_roundtrip_and_bad_file(tmp_path):
    p = tmp_path / "lib" / "notify.json"
    assert nc.load_state(str(p)) is None
    _, _, st = run(None, snap(), T0)
    nc.save_state(str(p), st)
    assert nc.load_state(str(p)) == st
    p.write_text("{半个")
    assert nc.load_state(str(p)) is None


# ---------------------------------------------------------------- 配置 / 发送器

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "notify.example.json"


class FakeResp:
    def __init__(self, status=200, body=b"{}"):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class FakeOpener:
    """假 urlopen: 记下请求, 按 fail 决定抛错或返回状态码. 测试里不发真请求."""

    def __init__(self, fail=None, status=200, body=b"{}"):
        self.reqs, self.fail, self.status, self.body = [], fail, status, body

    def __call__(self, req, timeout=None):
        self.reqs.append((req, timeout))
        if self.fail:
            raise self.fail
        return FakeResp(self.status, self.body)


def test_example_conf_is_valid_and_placeholder():
    conf = nc.load_conf(str(EXAMPLE))
    assert conf["kind"] == "ntfy"
    assert conf["url"].startswith("https://push.example.invalid/")


def test_load_conf_missing_is_none_and_bad_raises(tmp_path):
    assert nc.load_conf(str(tmp_path / "nope.json")) is None
    p = tmp_path / "n.json"
    for bad in ('{"kind": "slack", "url": "https://push.example.invalid/x"}',
                '{"kind": "ntfy", "url": "ftp://x"}', "[1]", "{半"):
        p.write_text(bad)
        with pytest.raises(ValueError):
            nc.load_conf(str(p))


def test_load_conf_disabled_is_none(tmp_path):
    p = tmp_path / "n.json"
    p.write_text('{"kind": "ntfy", "url": "https://push.example.invalid/x", "enabled": false}')
    assert nc.load_conf(str(p)) is None


def test_ntfy_sender_posts_body_and_headers():
    op = FakeOpener()
    send = nc.ntfy_sender({"kind": "ntfy", "url": "https://push.example.invalid/cart", "title": "推车",
                           "token": "tk_secret", "timeout": 7}, opener=op)
    send({"key": "ups.power", "text": "推车转电池供电 92%，市电断了", "prio": 4, "ts": T0})
    req, timeout = op.reqs[0]
    assert req.get_method() == "POST" and req.full_url == "https://push.example.invalid/cart"
    assert req.data == "推车转电池供电 92%，市电断了".encode()
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["priority"] == "4" and h["authorization"] == "Bearer tk_secret"
    h["title"].encode("ascii")                          # 中文标题按 RFC 2047 编码, 头里只有 ASCII
    assert h["title"].lower().startswith("=?utf-8?")
    assert timeout == 7


def test_ntfy_sender_optional_headers():
    op = FakeOpener()
    nc.ntfy_sender({"kind": "ntfy", "url": "https://push.example.invalid/c", "priority": False},
                   opener=op)({"key": "k", "text": "x", "prio": 4, "ts": T0})
    h = {k.lower() for k, _ in op.reqs[0][0].header_items()}
    assert "priority" not in h and "title" not in h and "authorization" not in h


def test_ntfy_sender_raises_on_http_error_status():
    send = nc.ntfy_sender({"kind": "ntfy", "url": "https://push.example.invalid/c"},
                          opener=FakeOpener(status=500))
    with pytest.raises(OSError):
        send({"key": "k", "text": "x", "prio": 3, "ts": T0})


# ---------------------------------------------------------------- 发件箱

def ev(key, text, ts=T0):
    return {"key": key, "text": text, "prio": 3, "ts": ts}


def test_queue_keeps_last_per_key_and_caps():
    box = nc.queue([ev("a", "1"), ev("b", "1")], [ev("a", "2")])
    assert [(e["key"], e["text"]) for e in box] == [("b", "1"), ("a", "2")]
    box = nc.queue([], [ev(f"k{i}", "x") for i in range(nc.MAX_OUTBOX + 5)])
    assert len(box) == nc.MAX_OUTBOX and box[-1]["key"] == f"k{nc.MAX_OUTBOX + 4}"


def test_deliver_stops_at_first_failure_and_marks_late_messages():
    sent = []

    def send(e):
        if e["key"] == "b":
            raise OSError("网断了")
        sent.append(e["text"])
    left, err = nc.deliver([ev("a", "早的"), ev("b", "x"), ev("c", "y")], send, now=T0 + 600)
    assert [e["key"] for e in left] == ["b", "c"] and "网断了" in err
    assert sent[0].startswith("[补发 ") and sent[0].endswith("早的")
    sent.clear()
    assert nc.deliver([ev("a", "新的")], send, now=T0 + 5) == ([], None)
    assert sent == ["新的"]


# ---------------------------------------------------------------- 跑一轮

def readers(ups=OL, ts=T0):
    return {"state": lambda: {"ts": ts, "ups": ups},
            "guard": lambda: {"state": "serving"},
            "auth": lambda: {"state": "online", "ok": True, "message": ""},
            "services": lambda: {"pxe-server": "active", "kiosk": "active", "cart-dash": "active"},
            "boot": lambda: {"id": "b1", "ok": True},
            "now": lambda: ts}


def test_run_once_without_conf_logs_only(tmp_path):
    st, conf, logs = str(tmp_path / "notify.json"), str(tmp_path / "none.json"), []
    op = FakeOpener()
    nc.run_once(readers(), st, conf, now=T0, opener=op, log=lambda k, m: logs.append((k, m)))
    r = nc.run_once(readers(ups=OB, ts=T0 + 30), st, conf, now=T0 + 30, opener=op,
                    log=lambda k, m: logs.append((k, m)))
    assert op.reqs == []                                # 没配置: 零外发
    assert ("notify", "推车转电池供电 92%，市电断了") in logs
    assert nc.load_state(st)["outbox"] == [] and r["sent"] == 0


def test_run_once_sends_and_retries_last_state_after_failure(tmp_path):
    st, conf = str(tmp_path / "notify.json"), tmp_path / "conf.json"
    conf.write_text('{"kind": "ntfy", "url": "https://push.example.invalid/cart"}')
    logs = []

    def log(_k, m):
        logs.append(m)
    nc.run_once(readers(), st, str(conf), now=T0, opener=FakeOpener(), log=log)
    down = FakeOpener(fail=OSError("网断了"))
    nc.run_once(readers(ups=OB, ts=T0 + 30), st, str(conf), now=T0 + 30, opener=down, log=log)
    assert len(down.reqs) == 1
    assert [e["key"] for e in nc.load_state(st)["outbox"]] == ["ups.power"]    # 发送失败不丢
    assert sum("推送失败" in m for m in logs) == 1
    nc.run_once(readers(ups=OB, ts=T0 + 60), st, str(conf), now=T0 + 60, opener=down, log=log)
    assert sum("推送失败" in m for m in logs) == 1      # 一直失败不刷日志
    up = FakeOpener()
    r = nc.run_once(readers(ups=OB, ts=T0 + 400), st, str(conf), now=T0 + 400, opener=up, log=log)
    assert r["sent"] == 1 and up.reqs[0][0].data.decode().endswith("推车转电池供电 92%，市电断了")
    assert nc.load_state(st)["outbox"] == []
    assert any("推送恢复" in m for m in logs)


def test_run_once_bad_conf_sends_nothing_and_logs(tmp_path):
    st, conf = str(tmp_path / "notify.json"), tmp_path / "conf.json"
    conf.write_text('{"kind": "ntfy"}')
    op, logs = FakeOpener(), []
    nc.run_once(readers(ups=OB), st, str(conf), now=T0, opener=op, log=lambda k, m: logs.append(m))
    assert op.reqs == [] and any("notify.json" in m for m in logs)


def test_main_test_push_needs_conf_and_uses_sender(tmp_path, capsys):
    assert nc.main(["--test", "--conf", str(tmp_path / "none.json")]) == 1
    conf = tmp_path / "conf.json"
    conf.write_text('{"kind": "ntfy", "url": "https://push.example.invalid/cart"}')
    op = FakeOpener()
    assert nc.main(["--test", "--conf", str(conf)], opener=op) == 0
    assert "测试" in op.reqs[0][0].data.decode()
    assert nc.main(["--test", "--conf", str(conf)], opener=FakeOpener(fail=OSError("断"))) == 1


# ---------------------------------------------------------------- 真读取的解析部分

def test_parse_is_active():
    out = "active\nfailed\ninactive\n"
    assert nc.parse_is_active(["a", "b", "c"], out) == {"a": "active", "b": "failed", "c": "inactive"}
    assert nc.parse_is_active(["a", "b"], "active\n") == {"a": "active", "b": None}


def test_parse_boot_success():
    assert nc.parse_boot_success("Result=success\nExecMainExitTimestampMonotonic=61234567\n") is True
    assert nc.parse_boot_success("Result=success\nExecMainExitTimestampMonotonic=0\n") is False
    assert nc.parse_boot_success("Result=exit-code\nExecMainExitTimestampMonotonic=61234567\n") is False
    assert nc.parse_boot_success("") is None


def test_default_read_uses_injected_runner_and_paths(tmp_path):
    g = tmp_path / "guard.json"
    g.write_text('{"state": "proxy"}')
    bid = tmp_path / "boot_id"
    bid.write_text("abc\n")
    calls = []

    def runner(cmd):
        calls.append(cmd)
        if "is-active" in cmd:
            return "active\nactive\nfailed\n"
        return "Result=success\nExecMainExitTimestampMonotonic=5\n"
    read = nc.default_read(runner=runner, dash=str(tmp_path / "no.json"), guard=str(g),
                           auth=str(tmp_path / "no2.json"), boot_id=str(bid))
    cur = nc.collect(read)
    assert cur["values"]["guard"] == "proxy" and cur["values"]["svc.cart-dash"] == "down"
    assert cur["values"]["auth"] is None and cur["boot"] == {"id": "abc", "ok": True}
    assert all(c[0] == "systemctl" for c in calls)


# ---- 飞书群自定义机器人 (webhook + 签名校验) ----

FEISHU = {"kind": "feishu-webhook", "url": "https://open.feishu.cn/open-apis/bot/v2/hook/EXAMPLE",
          "secret": "s3cr3t-example"}


def test_feishu_sign_uses_ts_newline_secret_as_hmac_key():
    import base64
    import hashlib
    import hmac
    want = base64.b64encode(hmac.new(b"1700000000\ns3cr3t-example", b"", hashlib.sha256).digest()).decode()
    assert nc.feishu_sign("s3cr3t-example", 1700000000) == want
    wrong = base64.b64encode(hmac.new(b"s3cr3t-example", b"1700000000\ns3cr3t-example",
                                      hashlib.sha256).digest()).decode()
    assert nc.feishu_sign("s3cr3t-example", 1700000000) != wrong       # 密钥和消息别放反


def test_feishu_sender_posts_signed_text():
    import json
    op = FakeOpener(body=b'{"code":0,"msg":"success"}')
    send = nc.make_sender(FEISHU, opener=op, clock=lambda: 1700000000.7)
    send({"key": "ups.power", "text": "推车转电池供电 92%，市电断了", "prio": 4, "ts": T0})
    req, timeout = op.reqs[0]
    assert req.get_method() == "POST" and req.full_url == FEISHU["url"]
    body = json.loads(req.data.decode("utf-8"))
    assert body["msg_type"] == "text" and body["content"]["text"] == "推车转电池供电 92%，市电断了"
    assert body["timestamp"] == "1700000000" and body["sign"] == nc.feishu_sign(FEISHU["secret"], 1700000000)
    assert {k.lower(): v for k, v in req.header_items()}["content-type"].startswith("application/json")


def test_feishu_sender_title_prefix_and_no_secret():
    import json
    op = FakeOpener(body=b'{"code":0}')
    conf = {"kind": "feishu-webhook", "url": FEISHU["url"], "title": "推车"}
    nc.make_sender(conf, opener=op)({"key": "k", "text": "x", "prio": 3, "ts": T0})
    body = json.loads(op.reqs[0][0].data.decode("utf-8"))
    assert body["content"]["text"] == "【推车】x" and "sign" not in body and "timestamp" not in body


@pytest.mark.parametrize("resp", [b'{"code":19021,"msg":"sign match fail"}', b'{"StatusCode":1}', b"not json"])
def test_feishu_sender_raises_on_api_error_even_http_200(resp):
    send = nc.make_sender(FEISHU, opener=FakeOpener(body=resp))
    with pytest.raises(OSError):
        send({"key": "k", "text": "x", "prio": 3, "ts": T0})


def test_load_conf_feishu_needs_https(tmp_path):
    p = tmp_path / "n.json"
    p.write_text('{"kind": "feishu-webhook", "url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"}')
    assert nc.load_conf(str(p))["kind"] == "feishu-webhook"
    p.write_text('{"kind": "feishu-webhook", "url": "http://open.feishu.cn/open-apis/bot/v2/hook/x"}')
    with pytest.raises(ValueError):
        nc.load_conf(str(p))


def test_make_sender_ntfy_unchanged():
    op = FakeOpener()
    nc.make_sender({"kind": "ntfy", "url": "https://push.example.invalid/c"}, opener=op)(
        {"key": "k", "text": "x", "prio": 3, "ts": T0})
    assert op.reqs[0][0].data == b"x"


def test_feishu_example_conf_is_valid_and_placeholder():
    conf = nc.load_conf(str(EXAMPLE.parent / "notify.feishu.example.json"))
    assert conf["kind"] == "feishu-webhook" and conf["url"].endswith("/00000000-0000-0000-0000-000000000000")



# ---------------------------------------------------------------- 飞书应用机器人 (推车专用应用, 只开收发消息)

APP = {"kind": "feishu-app", "app_id": "cli_example0000000000", "app_secret": "example-secret-not-real",
       "chat_id": "oc_00000000000000000000000000000000", "title": "推车"}


class SeqOpener(FakeOpener):
    """按顺序回不同正文: 先换 token, 再发消息."""

    def __init__(self, bodies):
        super().__init__()
        self.bodies = list(bodies)

    def __call__(self, req, timeout=None):
        self.reqs.append((req, timeout))
        return FakeResp(200, self.bodies.pop(0))


TOK = b'{"code":0,"msg":"ok","tenant_access_token":"t-example","expire":7200}'


def test_feishu_app_gets_token_then_posts_to_chat():
    import json
    op = SeqOpener([TOK, b'{"code":0,"msg":"success"}'])
    nc.make_sender(APP, opener=op)({"key": "ups.power", "text": "转电池供电", "prio": 4, "ts": T0})
    (treq, _), (mreq, _) = op.reqs
    assert treq.full_url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert json.loads(treq.data) == {"app_id": APP["app_id"], "app_secret": APP["app_secret"]}
    assert mreq.full_url.endswith("/open-apis/im/v1/messages?receive_id_type=chat_id")
    assert mreq.full_url.startswith("https://open.feishu.cn/")
    assert {k.lower(): v for k, v in mreq.header_items()}["authorization"] == "Bearer t-example"
    body = json.loads(mreq.data.decode("utf-8"))
    assert body["receive_id"] == APP["chat_id"] and body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "【推车】转电池供电"}


def test_feishu_app_reuses_token_until_near_expiry():
    op = SeqOpener([TOK, b'{"code":0}', b'{"code":0}', TOK, b'{"code":0}'])
    now = [1000.0]
    send = nc.make_sender(APP, opener=op, clock=lambda: now[0])
    ev = {"key": "k", "text": "x", "prio": 3, "ts": T0}
    send(ev)
    now[0] += 3600
    send(ev)                                            # 还在有效期内: 不再换 token
    now[0] += 7200
    send(ev)                                            # 过期了: 重新换
    urls = [r.full_url for r, _ in op.reqs]
    assert sum("tenant_access_token" in u for u in urls) == 2 and len(urls) == 5


@pytest.mark.parametrize("bodies", [
    [b'{"code":10014,"msg":"app secret invalid"}'],
    [TOK, b'{"code":99991672,"msg":"Access denied"}'],
    [b"not json"],
])
def test_feishu_app_raises_on_api_error(bodies):
    send = nc.make_sender(APP, opener=SeqOpener(bodies))
    with pytest.raises(OSError):
        send({"key": "k", "text": "x", "prio": 3, "ts": T0})


def test_feishu_app_error_does_not_leak_secret():
    send = nc.make_sender(APP, opener=SeqOpener([b'{"code":10014,"msg":"app secret invalid"}']))
    with pytest.raises(OSError) as ex:
        send({"key": "k", "text": "x", "prio": 3, "ts": T0})
    assert APP["app_secret"] not in str(ex.value)


def test_load_conf_feishu_app_needs_ids_not_url(tmp_path):
    import json
    p = tmp_path / "n.json"
    p.write_text(json.dumps(APP))
    assert nc.load_conf(str(p))["kind"] == "feishu-app"
    for k in ("app_id", "app_secret", "chat_id"):
        p.write_text(json.dumps({x: v for x, v in APP.items() if x != k}))
        with pytest.raises(ValueError):
            nc.load_conf(str(p))


def test_feishu_app_example_conf_is_valid_and_placeholder():
    conf = nc.load_conf(str(EXAMPLE.parent / "notify.feishu-app.example.json"))
    assert conf["kind"] == "feishu-app" and conf["chat_id"].startswith("oc_000")

# ---------------------------------------------------------------- #11 自己发地址限时

def test_guard_expired_is_pushed_with_renew_hint():
    """自己发地址到时停发: 手机上要知道新机器拿不到地址了, 去车机续期; 续期回来再推一条."""
    _, _, st = run(None, snap(), T0)
    assert snap(guard="expired")["values"]["guard"] == "expired"
    _, _, st = run(st, snap(guard="expired"), T0 + 30)
    keys, ev, st = run(st, snap(guard="expired"), T0 + 30 + nc.RULES["guard"][0])
    assert keys == ["guard"] and "到时" in ev[0]["text"] and "续期" in ev[0]["text"]
    later = T0 + 30 + nc.RULES["guard"][1] + 60
    _, _, st = run(st, snap(guard="serving"), later)
    keys, ev, _ = run(st, snap(guard="serving"), later + nc.RULES["guard"][0])
    assert keys == ["guard"] and "整个网段" in ev[0]["text"]
