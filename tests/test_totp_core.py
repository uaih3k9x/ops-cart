"""TOTP (RFC 6238) 校验和防重放 (#50 远程审批第一阶段). 种子全是测试用假种子."""

import os

import pytest

import totp_core as tc

RFC_SHA1 = b"12345678901234567890"
RFC_SHA256 = b"12345678901234567890123456789012"
RFC_SHA512 = (b"1234567890" * 7)[:64]

FAKE = b"fake-seed-for-tests!"            # 20 字节, 只给测试用
T0 = 1_700_000_010.0                       # 某个步的中间, 免得卡在步边界上


# ------------------------------------------------------------ RFC 6238 附录 B

@pytest.mark.parametrize("t,algo,key,want", [
    (59, "sha1", RFC_SHA1, "94287082"),
    (1111111109, "sha1", RFC_SHA1, "07081804"),
    (1111111111, "sha1", RFC_SHA1, "14050471"),
    (1234567890, "sha1", RFC_SHA1, "89005924"),
    (2000000000, "sha1", RFC_SHA1, "69279037"),
    (20000000000, "sha1", RFC_SHA1, "65353130"),
    (59, "sha256", RFC_SHA256, "46119246"),
    (1111111109, "sha256", RFC_SHA256, "68084774"),
    (1234567890, "sha256", RFC_SHA256, "91819424"),
    (59, "sha512", RFC_SHA512, "90693936"),
    (1111111109, "sha512", RFC_SHA512, "25091201"),
    (1234567890, "sha512", RFC_SHA512, "93441116"),
])
def test_rfc6238_vectors(t, algo, key, want):
    assert tc.totp(key, t, digits=8, algo=algo) == want


def test_default_is_six_digits_sha1_30s():
    assert tc.totp(RFC_SHA1, 59) == "287082"


def test_unknown_algo_refused():
    with pytest.raises(ValueError):
        tc.totp(RFC_SHA1, 59, algo="md5")


# ------------------------------------------------------------ 种子

def test_seed_b32_roundtrip_tolerates_spaces_and_case():
    b32 = tc.seed_to_b32(FAKE)
    assert tc.seed_from_b32(b32) == FAKE
    spaced = " ".join(b32[i:i + 4] for i in range(0, len(b32), 4)).lower()
    assert tc.seed_from_b32(spaced) == FAKE


@pytest.mark.parametrize("bad", ["", "!!!!", "AAAA", None, 123])
def test_seed_bad_or_too_short_refused(bad):
    with pytest.raises(ValueError):
        tc.seed_from_b32(bad)


def test_new_seed_is_random_and_long_enough():
    a, b = tc.new_seed(), tc.new_seed()
    assert a != b and len(tc.seed_from_b32(a)) >= 20


def test_otpauth_uri_has_issuer_and_secret():
    uri = tc.otpauth_uri("alice", tc.seed_to_b32(FAKE))
    assert uri.startswith("otpauth://totp/ops-cart:alice?")
    assert "secret=" + tc.seed_to_b32(FAKE) in uri and "issuer=ops-cart" in uri


def test_otpauth_uri_refuses_bad_account():
    with pytest.raises(ValueError):
        tc.otpauth_uri("Alice Smith", tc.seed_to_b32(FAKE))


# ------------------------------------------------------------ 校验

def _code(t, key=FAKE):
    return tc.totp(key, t)


def test_correct_code_ok_and_advances_last_step():
    res, st = tc.verify(tc.fresh_account(), FAKE, _code(T0), T0)
    assert res == "ok" and st["last_step"] == int(T0 // 30)


def test_wrong_code_bad_and_counts():
    wrong = "000000" if _code(T0) != "000000" else "111111"
    res, st = tc.verify(tc.fresh_account(), FAKE, wrong, T0)
    assert res == "bad" and st["fails"] == 1


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "", None, 123456, " 12345"])
def test_malformed_code_is_bad(code):
    res, st = tc.verify(tc.fresh_account(), FAKE, code, T0)
    assert res == "bad" and st["fails"] == 1


def test_plus_minus_one_step_ok_two_steps_expired():
    assert tc.verify(tc.fresh_account(), FAKE, _code(T0 - 30), T0)[0] == "ok"
    assert tc.verify(tc.fresh_account(), FAKE, _code(T0 + 30), T0)[0] == "ok"
    assert tc.verify(tc.fresh_account(), FAKE, _code(T0 - 60), T0)[0] == "bad"     # 过期
    assert tc.verify(tc.fresh_account(), FAKE, _code(T0 + 60), T0)[0] == "bad"


def test_same_code_twice_is_replay():
    code = _code(T0)
    _, st = tc.verify(tc.fresh_account(), FAKE, code, T0)
    res, st2 = tc.verify(st, FAKE, code, T0 + 5)
    assert res == "replay" and st2["fails"] == 0


def test_older_step_after_newer_is_replay():
    _, st = tc.verify(tc.fresh_account(), FAKE, _code(T0), T0)
    assert tc.verify(st, FAKE, _code(T0 - 30), T0 + 1)[0] == "replay"


def test_next_step_after_used_ok():
    _, st = tc.verify(tc.fresh_account(), FAKE, _code(T0), T0)
    assert tc.verify(st, FAKE, _code(T0 + 30), T0 + 30)[0] == "ok"


def test_clock_back_rejected_even_with_matching_code():
    _, st = tc.verify(tc.fresh_account(), FAKE, _code(T0), T0)
    back = T0 - 300                                      # 推车时钟往回跳 5 分钟
    assert tc.verify(st, FAKE, _code(back), back)[0] == "clock_back"


def test_no_seed_refused_without_counting():
    res, st = tc.verify(tc.fresh_account(), None, _code(T0), T0)
    assert res == "no_seed" and st["fails"] == 0


def test_verify_does_not_mutate_input():
    st = tc.fresh_account()
    tc.verify(st, FAKE, _code(T0), T0)
    assert st == tc.fresh_account()


@pytest.mark.parametrize("bad", [{"last_step": "x"}, {"last_step": -1}, [], None, {"fails": 1.5}])
def test_bad_account_state_refused_not_reset(bad):
    """读不懂的账号状态不当成新账号 (否则 last_step 归零, 旧码能重放)."""
    assert tc.verify(bad, FAKE, _code(T0), T0)[0] == "bad_state"


# ------------------------------------------------------------ 输错退避 (照 PinGate)

def _wrong(t):
    return "000000" if _code(t) != "000000" else "111111"


def _fail_n(st, n, now):
    for _ in range(n):
        _, st = tc.verify(st, FAKE, _wrong(now), now)
    return st


def test_lock_after_max_fails_even_correct_code():
    st = _fail_n(tc.fresh_account(), tc.MAX_FAILS, T0)
    res, _ = tc.verify(st, FAKE, _code(T0 + 1), T0 + 1)
    assert res == "locked"
    assert st["locked_until"] == T0 + tc.LOCK_S


def test_lock_doubles_and_caps():
    st, now = tc.fresh_account(), T0
    spans = []
    for _ in range(10):
        st = _fail_n(st, tc.MAX_FAILS, now)
        spans.append(st["locked_until"] - now)
        now = st["locked_until"] + 1
    assert spans[0] == tc.LOCK_S and spans[1] == 2 * tc.LOCK_S
    assert max(spans) == tc.MAX_LOCK_S == 3600


def test_success_after_lock_resets():
    st = _fail_n(tc.fresh_account(), tc.MAX_FAILS, T0)
    later = st["locked_until"] + 31
    res, st = tc.verify(st, FAKE, _code(later), later)
    assert res == "ok" and st["fails"] == 0 and st["lockouts"] == 0


# ------------------------------------------------------------ 种子文件 (LoadCredential 交过来的)

def _put(d, name, text, mode=0o600):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)
    return p


def test_load_seed_ok(tmp_path):
    _put(str(tmp_path), "totp-alice", tc.seed_to_b32(FAKE) + "\n")
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid()) == FAKE


def test_load_seed_per_person(tmp_path):
    other = b"another-fake-seed-xx"
    _put(str(tmp_path), "totp-alice", tc.seed_to_b32(FAKE))
    _put(str(tmp_path), "totp-bob", tc.seed_to_b32(other))
    assert tc.load_seed(str(tmp_path), "bob", expect_uid=os.getuid()) == other


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_load_seed_refuses_group_or_world_readable(tmp_path, mode):
    _put(str(tmp_path), "totp-alice", tc.seed_to_b32(FAKE), mode)
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid()) is None


def test_load_seed_refuses_wrong_owner(tmp_path):
    _put(str(tmp_path), "totp-alice", tc.seed_to_b32(FAKE))
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid() + 1) is None


def test_load_seed_refuses_symlink(tmp_path):
    real = _put(str(tmp_path), "real", tc.seed_to_b32(FAKE))
    os.symlink(real, os.path.join(str(tmp_path), "totp-alice"))
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid()) is None


@pytest.mark.parametrize("acct", ["../etc", "Alice", "", "a" * 40, None, "a/b"])
def test_load_seed_refuses_bad_account(tmp_path, acct):
    assert tc.load_seed(str(tmp_path), acct, expect_uid=os.getuid()) is None


def test_load_seed_missing_or_garbage(tmp_path):
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid()) is None
    _put(str(tmp_path), "totp-alice", "not base32 !!!")
    assert tc.load_seed(str(tmp_path), "alice", expect_uid=os.getuid()) is None

