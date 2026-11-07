"""远程审批第一阶段的纯逻辑 (#50, 设计稿 #59): 分级、因素要求、digest、摘要/卡片、请求解析、决定、审计.
不接真飞书, 不碰原生 Book. 种子全是测试用假种子."""

import json
import os

import pytest

import approval_core as ac
import totp_core as tc

FAKE = b"fake-seed-for-tests!"
NOW = 1_700_000_010.0
EPOCH = "1a2b3c4d"
ACCOUNTS = {"alice": {"feishu": "ou_example"}, "bob": {"feishu": "ou_other"}}
WEB_TS = ("alice@example.com", "laptop-1")


def seeds(acct):
    return FAKE if acct in ACCOUNTS else None


def serial(cmd="display version", kind="line", danger=(), **kw):
    return ac.make_item("serial", 17, EPOCH, target="slot:2", created=NOW - 60,
                        action={"type": kind, "text": cmd}, danger=list(danger), ctx=5, agent="claude-laptop", **kw)


def kvm(keys=(), text="", secret=False, prefix="", danger=("format",)):
    if keys:
        action = {"type": "key", "keys": list(keys)}
    else:
        action = {"type": "type", "text": text, "enter": False, "secret": secret, "prefix": prefix}
    return ac.make_item("kvm", 3, EPOCH, target="kvm:DOC-SERIAL-01", created=NOW - 60, action=action,
                        danger=list(danger), ctx=0, agent="claude-laptop")


def pxe(purpose="winpe"):
    return ac.make_item("pxe", 4, EPOCH, target="02:00:00:00:00:01", created=NOW - 60,
                        action={"purpose": purpose, "macs": ["02:00:00:00:00:01"]}, agent="claude-laptop")


def estop_release(scope="all", nid=1):
    return ac.make_item("estop_release", nid, EPOCH, target=scope, created=NOW - 10, action={"scope": scope})


def label_batch(count=12):
    return ac.make_item("label_batch", 9, EPOCH, target="tsc", created=NOW - 10,
                        action={"count": count, "kind": "cable"}, agent="claude-laptop")


# ------------------------------------------------------------ 分级

def test_serial_without_danger_is_L2():
    assert ac.tier_of(serial("vlan 20")) == ac.L2


def test_serial_with_danger_is_L3():
    assert ac.tier_of(serial("save", danger=["save"])) == ac.L3


def test_serial_key_ctrl_c_is_L2():
    assert ac.tier_of(serial("ctrl_c", kind="key")) == ac.L2


def test_kvm_pending_default_L3():
    assert ac.tier_of(kvm(text="format c:")) == ac.L3
    assert ac.tier_of(kvm(keys=("ctrl", "alt", "delete"), danger=())) == ac.L3


def test_kvm_alt_f4_whitelisted_L2():
    assert ac.tier_of(kvm(keys=("ALT", "f4"), danger=())) == ac.L2


@pytest.mark.parametrize("purpose", ["winpe", "rocky", "clone-save", "clone-restore"])
def test_pxe_all_purposes_L4(purpose):
    assert ac.tier_of(pxe(purpose)) == ac.L4


def test_estop_release_is_L3():
    assert ac.tier_of(estop_release()) == ac.L3
    assert ac.tier_of(estop_release("slot:3")) == ac.L3


def test_label_batch_is_L2():
    assert ac.tier_of(label_batch()) == ac.L2


def test_unknown_kind_refused_at_construction():
    with pytest.raises(ValueError):
        ac.make_item("acp", 1, EPOCH, target="x", created=NOW, action={})


def test_item_id_format():
    it = serial()
    assert it["id"] == f"serial:{EPOCH}:17" and it["tier"] == ac.L2


@pytest.mark.parametrize("epoch", ["", "XYZ", "1a2b3c4", "1a2b3c4d5"])
def test_bad_epoch_refused(epoch):
    with pytest.raises(ValueError):
        ac.make_item("serial", 1, epoch, target="slot:1", created=NOW, action={"type": "line", "text": "x"})


def test_empty_target_refused():
    with pytest.raises(ValueError):
        ac.make_item("kvm", 1, EPOCH, target="", created=NOW, action={"type": "type", "text": "x"})


# ------------------------------------------------------------ 各通道要什么因素

def test_feishu_requirements_follow_maintainer_decision():
    assert ac.requirement(ac.L2, "feishu", "serial") == "none"       # 拍板 2: L2 直接点批准
    assert ac.requirement(ac.L3, "feishu", "serial") == "totp"
    assert ac.requirement(ac.L3, "feishu", "estop_release") == "totp"
    assert ac.requirement(ac.L4, "feishu", "pxe") == "forbid"


def test_feishu_kvm_L3_only_reject():
    assert ac.requirement(ac.L3, "feishu", "kvm") == "forbid"        # 要看实时画面, 飞书上给不了


def test_L4_forbidden_on_every_remote_channel():
    for ch in ac.CHANNELS:
        assert ac.requirement(ac.L4, ch, "pxe") == "forbid"


def test_web_L2_needs_totp_L3_waits_for_oob():
    assert ac.requirement(ac.L2, "web", "serial") == "totp"
    assert ac.requirement(ac.L3, "web", "serial") == "forbid"


def test_unknown_channel_or_tier_forbid():
    assert ac.requirement(ac.L2, "kiosk", "serial") == "forbid"     # 车机不走这里
    assert ac.requirement(ac.L2, "sms", "serial") == "forbid"
    assert ac.requirement(7, "feishu", "serial") == "forbid"


# ------------------------------------------------------------ digest v1 (公式回归, 以后不许改)

def test_digest_v1_regression():
    it = serial("display version")
    assert it["digest"] == ac.digest(it)
    assert ac.digest(it) == "4e73fd954a7a7524"


def test_digest_changes_with_action_ctx_target_but_not_evidence():
    base = serial("vlan 20")
    assert ac.digest(serial("vlan 21")) != base["digest"]
    other_ctx = dict(base, ctx=6)
    assert ac.digest(other_ctx) != base["digest"]
    other_target = dict(base, target="slot:3")
    assert ac.digest(other_target) != base["digest"]
    with_ev = dict(base, evidence=["anything"], intent="whatever")
    assert ac.digest(with_ev) == base["digest"]


def test_digest_is_16_hex():
    d = ac.digest(pxe())
    assert len(d) == 16 and int(d, 16) >= 0


# ------------------------------------------------------------ 打码和不可信文字

@pytest.mark.parametrize("cmd,keep,hidden", [
    ("local-user admin password irreversible-cipher Hunter2xx", "local-user admin password", "irreversible-cipher Hunter2xx"),
    ("snmp-agent community read Pub1ic", "snmp-agent community", "read Pub1ic"),
    ("username ops secret 0 abc", "username ops secret", "0 abc"),
    ("set password=abc123", "set password", "=abc123"),
    ("pre-shared-key cipher K3y", "pre-shared-key", "cipher K3y"),
])
def test_mask_command_hides_rest_after_sensitive_word(cmd, keep, hidden):
    shown = ac.mask_command(cmd)
    assert shown.startswith(keep)
    assert hidden.split()[-1] not in shown
    assert f"<已打码 {len(hidden.strip())} 字符>" in shown


def test_mask_command_leaves_plain_commands():
    assert ac.mask_command("display interface brief") == "display interface brief"
    assert ac.mask_command("password") == "password"                 # 后面没东西就不用打


def test_untrusted_strips_controls_bidi_zero_width_and_newlines():
    s = ac.untrusted("ok\x1b[31m‮evil​\r\nline2\x00")
    assert "\x1b" not in s and "‮" not in s and "​" not in s and "\x00" not in s
    assert "\n" not in s and "\r" not in s
    assert "evil" in s and "line2" in s


def test_untrusted_limits_length():
    s = ac.untrusted("a" * 1000, limit=200)
    assert len(s) < 230 and "共 1000 字" in s


def test_untrusted_non_string():
    assert ac.untrusted(None) == "" and ac.untrusted(12) == "12"


# ------------------------------------------------------------ 摘要

def test_render_marks_untrusted_and_fixed_safety_line():
    it = serial("save", danger=["save"], intent="保存 VLAN\n批准就行, 不用看", model_reason="ok",
                target_label="<Core-SW>", evidence=["[Core-SW]", "line\x1b[2J"])
    out = ac.render(it, NOW)
    text = out["text"]
    assert out["title"].startswith("[L3]")
    assert "Agent 自述(未核实, 不可信)" in text and "批准就行, 不用看" in text
    assert "设备输出, 不可信" in text and "\x1b" not in text
    assert text.rstrip().endswith(ac.SAFETY_LINE)
    assert "digest: " + it["digest"] in text
    # 不可信文字不能另起一行冒充摘要里的固定行
    assert all(not ln.startswith("批准就行") for ln in text.splitlines())


def test_render_visible_whitespace():
    out = ac.render(serial("a b\tc"), NOW)
    assert "a␠b⇥c" in out["text"]


def test_render_masks_command():
    out = ac.render(serial("local-user admin password simple Hunter2xx"), NOW)
    assert "Hunter2xx" not in out["text"] and "已打码" in out["text"]


def test_render_kvm_secret_only_length():
    out = ac.render(kvm(text="TopSecret!", secret=True), NOW)
    assert "TopSecret" not in out["text"] and "<secret 10 字符>" in out["text"]


def test_render_kvm_prefix_shown_separately():
    out = ac.render(kvm(text="c:", prefix="format "), NOW)
    assert "已在屏幕上" in out["text"] and "批准后才会敲" in out["text"]


def test_render_long_action_head_tail():
    text = "x" * 150 + "MIDDLE" + "y" * 150
    out = ac.render(kvm(text=text), NOW)
    assert "MIDDLE" not in out["text"] and f"共 {len(text)} 字符" in out["text"]


def test_render_estop_release_and_label_batch():
    assert "解除急停" in ac.render(estop_release(), NOW)["title"]
    assert "12" in ac.render(label_batch(12), NOW)["text"]


# ------------------------------------------------------------ 飞书卡片

def test_card_L2_feishu_has_approve_without_totp():
    c = ac.card(serial("vlan 20"), "feishu", NOW)
    assert "approve" in c["buttons"] and not c["totp"]
    assert {"reject", "estop", "lockdown"} <= set(c["buttons"])


def test_card_L3_needs_totp():
    c = ac.card(estop_release(), "feishu", NOW)
    assert "approve" in c["buttons"] and c["totp"]


@pytest.mark.parametrize("item", [pxe(), kvm(text="format c:")])
def test_card_without_approve_when_forbidden(item):
    c = ac.card(item, "feishu", NOW)
    assert "approve" not in c["buttons"] and "reject" in c["buttons"] and not c["totp"]


def test_card_text_is_render_text():
    it = serial()
    assert ac.card(it, "feishu", NOW)["text"] == ac.render(it, NOW)["text"]


# ------------------------------------------------------------ 请求解析

def req(**kw):
    r = {"op": "approve", "id": f"serial:{EPOCH}:17", "digest": serial()["digest"], "account": "alice",
         "by": "feishu:ou_example", "totp": "", "oob": "", "retype": "", "nonce": "9c2e" * 4, "t": NOW}
    r.update(kw)
    return r


def raw(**kw):
    return json.dumps(req(**kw))


def test_parse_ok():
    assert ac.parse_request(raw(), NOW)["op"] == "approve"


@pytest.mark.parametrize("bad", [
    lambda r: r.pop("oob"),                     # 少字段
    lambda r: r.update(extra=1),                # 多字段
    lambda r: r.update(op="confirm"),
    lambda r: r.update(op="challenge"),         # 带外码是下一阶段
    lambda r: r.update(id="serial:1a2b:17"),
    lambda r: r.update(digest="xyz"),
    lambda r: r.update(digest=""),              # reject 也要带 digest
    lambda r: r.update(account="Alice"),
    lambda r: r.update(totp=123456),
    lambda r: r.update(totp="12345"),
    lambda r: r.update(nonce="short"),
    lambda r: r.update(t=NOW - 61),             # 只认 60 秒内
    lambda r: r.update(t=NOW + 30),             # 未来时间
    lambda r: r.update(t=True),
    lambda r: r.update(by="see https://evil.example/approve"),
    lambda r: r.update(by="x" * 200),
])
def test_parse_refuses(bad):
    r = req()
    bad(r)
    with pytest.raises(ac.BadRequest):
        ac.parse_request(json.dumps(r), NOW)


def test_parse_refuses_oversize_and_non_object():
    with pytest.raises(ac.BadRequest):
        ac.parse_request("x" * 5000, NOW)
    with pytest.raises(ac.BadRequest):
        ac.parse_request("[1, 2]", NOW)
    with pytest.raises(ac.BadRequest):
        ac.parse_request("{bad json", NOW)


def test_parse_reject_with_digest_ok():
    assert ac.parse_request(raw(op="reject"), NOW)["op"] == "reject"


# ------------------------------------------------------------ tailscale 身份

@pytest.mark.parametrize("login,node", [("alice@example.com", "laptop-1"), ("alice@github", None), (None, None)])
def test_ts_identity_ok(login, node):
    assert ac.ts_identity(login, node) == {"ts_login": login, "ts_node": node}


@pytest.mark.parametrize("login,node", [
    ("alice", None), ("a b@example.com", None), ("alice@example.com", "Bad Node"),
    ("alice@example.com\n", None), ("x" * 300 + "@example.com", None), (None, "laptop"),
])
def test_ts_identity_bad(login, node):
    with pytest.raises(ValueError):
        ac.ts_identity(login, node)


# ------------------------------------------------------------ 决定

def decide(item, r, channel="feishu", state=None, now=NOW, seed_of=seeds, ts=None):
    if ts is None:
        ts = WEB_TS if channel == "web" else (None, None)
    return ac.decide(state if state is not None else ac.fresh_state(), item, ac.parse_request(json.dumps(r), now),
                     channel, seed_of, ACCOUNTS, now, ts_login=ts[0], ts_node=ts[1])


def L3_estop_req(**kw):
    it = estop_release()
    return it, req(id=it["id"], digest=it["digest"], **kw)


def test_feishu_L2_approve_without_totp():
    it = serial("vlan 20")
    out, st, rec = decide(it, req(digest=it["digest"]))
    assert out == "authorized" and rec["factor"] == "none"
    assert st["decisions"][f"serial:{EPOCH}:17"]["op"] == "approve"


def test_L3_estop_release_needs_totp():
    it, r = L3_estop_req()
    assert decide(it, r)[0] == "totp_bad"
    it, r = L3_estop_req(totp=tc.totp(FAKE, NOW))
    out, _, rec = decide(it, r)
    assert out == "authorized" and rec["factor"] == "totp:ok"


def test_L3_wrong_totp_counts_fail():
    it, r = L3_estop_req(totp="000000" if tc.totp(FAKE, NOW) != "000000" else "111111")
    out, st, rec = decide(it, r)
    assert out == "totp_bad" and st["accounts"]["alice"]["fails"] == 1 and rec["factor"] == "totp:bad"


def test_L3_expired_totp_refused():
    it, r = L3_estop_req(totp=tc.totp(FAKE, NOW - 90))
    assert decide(it, r)[0] == "totp_bad"


def test_totp_replay_across_requests_and_channels():
    code = tc.totp(FAKE, NOW)
    it, r = L3_estop_req(totp=code)
    out, st, _ = decide(it, r)
    assert out == "authorized"
    it2 = estop_release("slot:1", nid=2)
    r2 = req(id=it2["id"], digest=it2["digest"], totp=code, nonce="ab" * 8, t=NOW + 31)
    out2, _, _ = decide(it2, r2, state=st, now=NOW + 31, channel="feishu")
    assert out2 == "replay"                     # 过了 30 秒限频, 同一个码仍然不能用


def test_nonce_single_use():
    out, st, _ = decide(serial(), req())
    assert out == "authorized"
    out2, _, _ = decide(serial(), req(), state=st)
    assert out2 == "replay"


def test_L4_forbidden_even_with_totp():
    it = pxe()
    out, _, rec = decide(it, req(id=it["id"], digest=it["digest"], totp=tc.totp(FAKE, NOW)))
    assert out == "forbidden" and rec["result"] == "tier"


def test_L4_reject_allowed():
    it = pxe()
    out, _, _ = decide(it, req(op="reject", id=it["id"], digest=it["digest"]))
    assert out == "rejected"


def test_kvm_L3_feishu_forbidden():
    it = kvm(text="format c:")
    assert decide(it, req(id=it["id"], digest=it["digest"], totp=tc.totp(FAKE, NOW)))[0] == "forbidden"


def test_web_L2_needs_totp():
    assert decide(serial(), req(by="web:192.0.2.20/ab12cd"), channel="web")[0] == "totp_bad"
    out, _, _ = decide(serial(), req(by="web:192.0.2.20/ab12cd", totp=tc.totp(FAKE, NOW)), channel="web")
    assert out == "authorized"


def test_digest_mismatch_dropped():
    out, _, rec = decide(serial("vlan 21"), req())         # 请求里是 "display version" 的 digest
    assert out == "stale" and rec["ev"] == "drop"


def test_item_gone():
    out, _, rec = decide(None, req())
    assert out == "gone"


def test_item_id_mismatch_refused():
    other = ac.make_item("serial", 18, EPOCH, target="slot:2", created=NOW - 60,
                         action={"type": "line", "text": "display version"}, ctx=5)
    assert decide(other, req())[0] == "stale"


def test_expired_item():
    it = ac.make_item("serial", 17, EPOCH, target="slot:2", created=NOW - ac.EXPIRES_S[ac.L2] - 1,
                      action={"type": "line", "text": "display version"}, ctx=5)
    assert decide(it, req(digest=it["digest"]))[0] == "expired"


def test_unknown_account_forbidden():
    out, _, rec = decide(serial(), req(account="mallory"))
    assert out == "forbidden" and rec["result"] == "account"


def test_no_seed_refused():
    it, r = L3_estop_req(totp=tc.totp(FAKE, NOW))
    assert decide(it, r, seed_of=lambda a: None)[0] == "no_seed"


def test_already_and_conflict():
    out, st, _ = decide(serial(), req())
    assert decide(serial(), req(nonce="ab" * 8), state=st)[0] == "already"
    assert decide(serial(), req(op="reject", nonce="cd" * 8), state=st)[0] == "conflict"


def test_L3_rate_limit_30s_per_account():
    it, r = L3_estop_req(totp=tc.totp(FAKE, NOW))
    _, st, _ = decide(it, r)
    it2 = estop_release("slot:1", nid=2)
    r2 = req(id=it2["id"], digest=it2["digest"], totp=tc.totp(FAKE, NOW + 30), nonce="ab" * 8, t=NOW + 20)
    assert decide(it2, r2, state=st, now=NOW + 20)[0] == "too_soon"
    r3 = dict(r2, t=NOW + 31, nonce="cd" * 8)
    assert decide(it2, r3, state=st, now=NOW + 31)[0] == "authorized"


def test_totp_lock_shared_across_channels():
    st = ac.fresh_state()
    bad = "000000" if tc.totp(FAKE, NOW) != "000000" else "111111"
    for i in range(tc.MAX_FAILS):
        it, r = L3_estop_req(totp=bad, nonce=f"{i:016x}")
        _, st, _ = decide(it, r, state=st)
    out, _, rec = decide(serial(), req(totp=tc.totp(FAKE, NOW), nonce="ee" * 8, by="web:192.0.2.20/ab12cd"), channel="web", state=st)
    assert out == "locked" and rec["ev"] == "totp_lock"


def test_decide_does_not_mutate_input_state():
    st = ac.fresh_state()
    decide(serial(), req(), state=st)
    assert st == ac.fresh_state()


def test_bad_ts_identity_forbidden():
    out, _, rec = decide(serial(), req(), ts=("not-a-login", None))
    assert out == "forbidden" and rec["result"] == "identity"


def test_nonces_pruned_after_24h():
    _, st, _ = decide(serial(), req())
    later = NOW + 25 * 3600
    st2 = ac.prune(st, later)
    assert not st2["nonces"] and not st2["decisions"]


# ------------------------------------------------------------ 审计

AUDIT_KEYS = {"t", "ev", "id", "digest", "kind", "tier", "channel", "account", "by", "factor", "summary",
              "result", "ts_login", "ts_node", "nonce"}


def test_audit_fields_and_identity():
    it, r = L3_estop_req(totp=tc.totp(FAKE, NOW))
    _, _, rec = decide(it, r, ts=("alice@example.com", "phone-1"))
    assert set(rec) == AUDIT_KEYS
    assert rec["ts_login"] == "alice@example.com" and rec["ts_node"] == "phone-1"
    assert rec["channel"] == "feishu" and rec["account"] == "alice" and rec["tier"] == ac.L3


def test_audit_never_has_codes_or_full_nonce():
    code = tc.totp(FAKE, NOW)
    it, r = L3_estop_req(totp=code)
    _, _, rec = decide(it, r)
    blob = json.dumps(rec, ensure_ascii=False)
    assert code not in blob and r["nonce"] not in blob and rec["nonce"] == r["nonce"][:4]


def test_audit_summary_masked():
    it = serial("local-user admin password simple Hunter2xx")
    _, _, rec = decide(it, req(digest=it["digest"]))
    assert "Hunter2xx" not in rec["summary"]


def test_audit_bad_request_record():
    rec = ac.audit_bad_request("feishu", "缺字段", NOW)
    assert rec["ev"] == "bad_request" and set(rec) == AUDIT_KEYS


def test_append_audit_jsonl(tmp_path):
    p = str(tmp_path / "approval.log")
    _, _, rec = decide(serial(), req())
    ac.append_audit(p, rec)
    ac.append_audit(p, rec)
    lines = open(p).read().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["ev"] == "authorized"


# ------------------------------------------------------------ 决定记录落盘

def test_store_roundtrip_and_mode(tmp_path):
    p = str(tmp_path / "approvals.json")
    store = ac.ApprovalStore(p, audit_path=str(tmp_path / "approval.log"))
    r = ac.parse_request(raw(), NOW)
    out = store.decide(serial(), r, "feishu", seeds, ACCOUNTS, NOW)
    assert out == "authorized"
    assert os.stat(p).st_mode & 0o777 == 0o600
    # 重新加载 ("重启后"): nonce 还在
    store2 = ac.ApprovalStore(p, audit_path=str(tmp_path / "approval.log"))
    assert store2.decide(serial(), r, "feishu", seeds, ACCOUNTS, NOW) == "replay"
    assert len(open(str(tmp_path / "approval.log")).read().splitlines()) == 2


def test_store_unreadable_fails_closed(tmp_path):
    p = tmp_path / "approvals.json"
    p.write_text("{broken")
    store = ac.ApprovalStore(str(p), audit_path=str(tmp_path / "approval.log"))
    r = ac.parse_request(raw(), NOW)
    assert store.decide(serial(), r, "feishu", seeds, ACCOUNTS, NOW) == "state_unreadable"
    assert p.read_text() == "{broken"                      # 不擅自重置 (否则 last_step 归零)


def test_store_totp_last_step_survives_reload(tmp_path):
    p = str(tmp_path / "approvals.json")
    code = tc.totp(FAKE, NOW)
    it, r = L3_estop_req(totp=code)
    ac.ApprovalStore(p).decide(it, ac.parse_request(json.dumps(r), NOW), "feishu", seeds, ACCOUNTS, NOW)
    it2 = estop_release("slot:1", nid=2)
    r2 = req(id=it2["id"], digest=it2["digest"], totp=code, nonce="ab" * 8, t=NOW + 45)
    out = ac.ApprovalStore(p).decide(it2, ac.parse_request(json.dumps(r2), NOW + 45), "feishu", seeds,
                                     ACCOUNTS, NOW + 45)
    assert out == "replay"


def test_totp_code_used_on_feishu_cannot_be_reused_on_web():
    code = tc.totp(FAKE, NOW)
    it, r = L3_estop_req(totp=code)
    out, st, _ = decide(it, r)
    assert out == "authorized"
    web_item = ac.make_item("serial", 30, EPOCH, target="slot:4", created=NOW - 5,
                            action={"type": "line", "text": "vlan 30"}, ctx=1)
    r2 = req(id=web_item["id"], digest=web_item["digest"], totp=code, nonce="ab" * 8, t=NOW + 5,
             by="web:192.0.2.20/ab12cd")
    assert decide(web_item, r2, state=st, now=NOW + 5, channel="web")[0] == "replay"



# ------------------------------------------------------------ 审查第一轮 (中 1-3, 低 4-13)

@pytest.mark.parametrize("cmd,secret", [
    ("tacacs-server host 192.0.2.1 key S3cr3tKey", "S3cr3tKey"),                      # Cisco
    ("radius-server key S3cr3t", "S3cr3t"),
    ("ip ospf message-digest-key 1 md5 Hunter2", "Hunter2"),
    ("snmp-server user u g v3 auth sha AuthPass priv aes PrivPass", "AuthPass"),
    ("snmp-server user u g v3 auth sha AuthPass priv aes PrivPass", "PrivPass"),
    ("username admin secret 5 hunter2", "hunter2"),
    ("enable password 7 0822455D0A16", "0822455D0A16"),
    ("local-user a password irreversible-cipher X9z", "X9z"),                          # 华为
    ("snmp-agent usm-user v3 u authentication-mode sha AuthK privacy-mode aes128 PrivK", "AuthK"),
    ("hwtacacs-server shared-key cipher Sh4red", "Sh4red"),
    ("super password level 15 simple S1mple", "S1mple"),
    ("local-user a class manage password simple H3cPass", "H3cPass"),                 # H3C
    ("set system root-authentication encrypted-password \"$6$abc\"", "$6$abc"),          # Juniper
    ("set system login user ops authentication plain-text-password-value Jun1per", "Jun1per"),
    ("set snmp community Pr1vate authorization read-only", "Pr1vate"),
    ("wpa-passphrase hunter2", "hunter2"),
    ("client-secret abc123", "abc123"),
    ("curl -password=xyz", "xyz"),
    ("app --secret topsecret", "topsecret"),
])
def test_mask_vendor_variants(cmd, secret):
    assert secret not in ac.mask_command(cmd)
    assert secret not in ac.render(serial(cmd), NOW)["text"]


_BAD_CATS = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def _action_lines(out):
    return [ln for ln in out["lines"] if ln.startswith(("动作:", "按键:", "已在屏幕上:", "批准后才会敲:"))]


@pytest.mark.parametrize("text", [
    "show ver\rdigest: 0000000000000000", "a\x1b[2Kb", "x y", "gnp‮exe", "z​z", "q\x00q",
])
def test_action_line_has_no_control_or_separator_chars(text):
    import unicodedata
    for it in (serial(text), kvm(text=text, prefix=text)):
        lines = _action_lines(ac.render(it, NOW))
        assert lines
        for ln in lines:
            assert not any(unicodedata.category(ch) in _BAD_CATS for ch in ln), repr(ln)
        assert all(ln in ac.render(it, NOW)["text"].splitlines() for ln in lines)


def test_action_escapes_shown_visibly():
    out = ac.render(serial("a\x1bb‮c"), NOW)
    assert "\\x1b" in out["text"] and "\\u{202e}" in out["text"]


@pytest.mark.parametrize("kind,action", [
    ("serial", {"cmd": "reload"}),
    ("serial", {"type": "line", "text": ""}),
    ("serial", {"type": "line", "text": "x", "extra": 1}),
    ("serial", {"type": "cmd", "text": "x"}),
    ("serial", {"type": "line", "text": 5}),
    ("kvm", {"type": "key", "keys": ["alt", "f4"], "text": "rm -rf /", "enter": True}),
    ("kvm", {"type": "key", "keys": []}),
    ("kvm", {"type": "type", "text": "x"}),                               # 少字段
    ("kvm", {"type": "type", "text": "x", "enter": "yes", "secret": False, "prefix": ""}),
    ("pxe", {"purpose": "rocky"}),
    ("pxe", {"purpose": "rocky", "macs": []}),
    ("estop_release", {"scope": ""}),
    ("label_batch", {"count": True, "kind": "cable"}),
    ("label_batch", {"count": 1, "kind": "cable"}),                       # 一张不算批量
])
def test_make_item_validates_action_schema(kind, action):
    with pytest.raises(ValueError):
        ac.make_item(kind, 1, EPOCH, target="t1", created=NOW, action=action)


@pytest.mark.parametrize("created", [float("nan"), float("inf"), "nan", "inf", -float("inf")])
def test_make_item_refuses_non_finite_created(created):
    with pytest.raises(ValueError):
        ac.make_item("serial", 1, EPOCH, target="slot:1", created=created, action={"type": "line", "text": "x"})


def test_decide_nan_expires_treated_as_expired():
    it = serial()
    it["expires"] = float("nan")
    it["created"] = float("nan")
    assert decide(it, req(digest=ac.digest(it)))[0] in ("expired", "stale")


def test_decide_recomputes_tier_and_expiry():
    it = pxe()
    it["tier"] = ac.L2                                  # 被篡改的 tier 不算数
    assert decide(it, req(id=it["id"], digest=it["digest"]))[0] == "forbidden"
    it2 = serial()
    it2["expires"] = NOW + 10 ** 6                      # 被篡改的期限不算数
    late = it2["created"] + ac.EXPIRES_S[ac.L2] + 1
    r = req(t=late)
    assert decide(it2, r, now=late)[0] == "expired"


def test_parse_refuses_duplicate_keys():
    r = raw(op="reject")
    dup = r[:-1] + ', "op": "approve"}'
    with pytest.raises(ac.BadRequest):
        ac.parse_request(dup, NOW)


@pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
def test_parse_refuses_nan_constants(const):
    r = raw().replace(f'"t": {NOW}', f'"t": {const}')
    assert const in r
    with pytest.raises(ac.BadRequest):
        ac.parse_request(r, NOW)


def test_evidence_masked():
    it = serial(evidence=["snmp-server community Pr1vate RW", "username a password Hunter2"])
    text = ac.render(it, NOW)["text"]
    assert "Pr1vate" not in text and "Hunter2" not in text


def test_target_label_masked_and_clean():
    it = serial(target_label="sw password Hunter2\x1b")
    text = ac.render(it, NOW)["text"]
    assert "Hunter2" not in text and "\x1b" not in text


def test_store_audit_failure_does_not_commit(tmp_path):
    p = str(tmp_path / "approvals.json")
    bad_audit = str(tmp_path / "no-such-dir-file")
    os.makedirs(bad_audit)                              # 审计路径是目录: 写不进去
    store = ac.ApprovalStore(p, audit_path=bad_audit)
    r = ac.parse_request(raw(), NOW)
    assert store.decide(serial(), r, "feishu", seeds, ACCOUNTS, NOW) == "audit_fail"
    assert not os.path.exists(p)


@pytest.mark.parametrize("doc", [
    {"v": 1, "accounts": {"alice": "x"}, "nonces": {}, "decisions": {}},
    {"v": 1, "accounts": {"Bad Name": {}}, "nonces": {}, "decisions": {}},
    {"v": 1, "accounts": {"alice": {"last_step": -3}}, "nonces": {}, "decisions": {}},
    {"v": 1, "accounts": {"alice": {"last_hi": "x"}}, "nonces": {}, "decisions": {}},
    {"v": 1, "accounts": {}, "nonces": {"zz": 1.0}, "decisions": {}},
    {"v": 1, "accounts": {}, "nonces": {"ab" * 8: "x"}, "decisions": {}},
    {"v": 1, "accounts": {}, "nonces": {}, "decisions": {"serial:1a2b3c4d:1": {"op": "boom", "t": 1.0}}},
    {"v": 1, "accounts": {}, "nonces": {}, "decisions": {"nope": {"op": "approve", "t": 1.0}}},
])
def test_store_inner_state_validated(tmp_path, doc):
    p = tmp_path / "approvals.json"
    p.write_text(json.dumps(doc))
    log = tmp_path / "approval.log"
    store = ac.ApprovalStore(str(p), audit_path=str(log))
    assert store.decide(serial(), ac.parse_request(raw(), NOW), "feishu", seeds, ACCOUNTS, NOW) \
        == "state_unreadable"
    assert json.loads(log.read_text().splitlines()[-1])["ev"] == "state_unreadable"


def test_web_requires_tailscale_login():
    r = req(by="web:192.0.2.20/ab12cd", totp=tc.totp(FAKE, NOW))
    out, _, rec = decide(serial(), r, channel="web", ts=(None, None))
    assert out == "forbidden" and rec["result"] == "identity"


def test_web_by_must_be_web_form():
    r = req(totp=tc.totp(FAKE, NOW))                    # by 是 feishu:...
    assert decide(serial(), r, channel="web")[0] == "forbidden"


def test_feishu_requires_bound_open_id():
    out, _, rec = decide(serial(), req(by="feishu:ou_other"))      # bob 的 open_id 冒充 alice
    assert out == "forbidden" and rec["result"] == "identity"
    out, _, rec = decide(serial(), req(by="web:192.0.2.20/ab12cd"))
    assert out == "forbidden" and rec["result"] == "identity"


def test_feishu_account_without_binding_forbidden():
    st = ac.fresh_state()
    out, _, _ = ac.decide(st, serial(), ac.parse_request(raw(), NOW), "feishu", seeds, {"alice": {}}, NOW)
    assert out == "forbidden"


def test_truncated_action_cannot_be_approved():
    it = kvm(text="a" * 300)
    assert ac.truncated(it)
    assert "approve" not in ac.card(it, "web", NOW)["buttons"]
    out, _, rec = decide(it, req(id=it["id"], digest=it["digest"]))
    assert out == "forbidden"
    assert decide(it, req(op="reject", id=it["id"], digest=it["digest"], nonce="ab" * 8))[0] == "rejected"


def test_serial_action_never_truncated():
    import serial_write_core as sw
    assert ac.ACTION_MAX >= sw.LINE_MAX
    assert not ac.truncated(serial("x" * sw.LINE_MAX))


_CONCURRENT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import approval_core as ac
p, nid, nonce, code, now = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6])
it = ac.make_item("serial", int(nid), "1a2b3c4d", target="slot:2", created=now - 5,
                  action={"type": "line", "text": "vlan 20"}, ctx=1)
r = {"op": "approve", "id": it["id"], "digest": it["digest"], "account": "alice", "by": "web:192.0.2.20/ab12cd",
     "totp": code, "oob": "", "retype": "", "nonce": nonce, "t": now}
seed = b"fake-seed-for-tests!"
print(ac.ApprovalStore(p).decide(it, ac.parse_request(json.dumps(r), now), "web", lambda a: seed,
                                 {"alice": {}}, now, ts_login="alice@example.com"))
"""


def test_concurrent_processes_same_totp_only_one_ok(tmp_path):
    import subprocess
    import sys
    import time as _t
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    now = _t.time()
    code = tc.totp(FAKE, now)
    p = str(tmp_path / "approvals.json")
    procs = [subprocess.Popen([sys.executable, "-c", _CONCURRENT, root, p, str(40 + i), f"{i:016x}", code,
                               str(now)], stdout=subprocess.PIPE, text=True) for i in range(6)]
    outs = sorted(pr.communicate(timeout=30)[0].strip() for pr in procs)
    assert outs.count("authorized") == 1 and outs.count("replay") == 5, outs
