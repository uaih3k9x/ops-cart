"""TOTP (RFC 6238) 校验, 只用标准库 (#50 远程审批, 设计稿 3.3).

TOTP 只证明"这 30 秒里手机在人手上", 不绑定具体挂起项.
- 30 秒一步, 6 位, SHA-1 (验证器 App 的默认); 按校验时刻容许 ±1 步.
- 防重放: 每个账号记 last_step, 只增不减. 步数 <= last_step 的码一律拒 (同一个码跨通道也只能用一次).
  时钟回拨到 last_step 之前时记 clock_back, 宁可暂时用不了.
- 输错照 kiosk_core.PinGate 指数退避: 连续 MAX_FAILS 次冷却 LOCK_S 秒, 每次翻倍, 封顶 MAX_LOCK_S.
  按账号算, 跨通道共享 (状态由 approval_core 存在同一个文件里). 车机确认不走这里, 不受冷却影响.
- 种子每人一个 (拍板 12): /etc/ops-cart/approve-secrets/totp-<账号> (root 0700 目录 / 0600 文件),
  只经 LoadCredential= 交给取件单元, 运行时从 $CREDENTIALS_DIRECTORY 读. 日志里永远不出现种子和码.

verify 是纯函数: 输入账号状态, 返回 (结果, 新状态), 不改输入."""

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import stat
import struct
import urllib.parse

STEP_S = 30
DIGITS = 6
WINDOW = 1                  # ±1 步
MAX_FAILS = 5
LOCK_S = 60
MAX_LOCK_S = 3600           # 封顶 1 小时 (设计稿 M4)
SEED_MIN_BYTES = 10         # 80 位; 新签发的是 20 字节
SEED_FILE_MAX = 256
ISSUER = "ops-cart"
ACCOUNT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CODE_RE = re.compile(r"[0-9]{6}")
_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def valid_account(acct):
    return isinstance(acct, str) and bool(ACCOUNT_RE.fullmatch(acct))


# ------------------------------------------------------------ 算码

def hotp(key, counter, digits=DIGITS, algo="sha1"):
    if algo not in _ALGOS:
        raise ValueError(f"不支持的算法 {algo!r}")
    mac = hmac.new(key, struct.pack(">Q", counter), _ALGOS[algo]).digest()
    off = mac[-1] & 0x0F
    val = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(val % 10 ** digits).zfill(digits)


def totp(key, t, step=STEP_S, digits=DIGITS, algo="sha1"):
    return hotp(key, int(t // step), digits, algo)


# ------------------------------------------------------------ 种子

def seed_from_b32(text):
    if not isinstance(text, str):
        raise ValueError("种子要是 base32 字符串")
    s = re.sub(r"\s+", "", text).upper().rstrip("=")
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except (binascii.Error, ValueError):
        raise ValueError("种子不是合法的 base32") from None
    if len(key) < SEED_MIN_BYTES:
        raise ValueError(f"种子太短 (至少 {SEED_MIN_BYTES} 字节)")
    return key


def seed_to_b32(key):
    return base64.b32encode(key).decode("ascii").rstrip("=")


def new_seed():
    return seed_to_b32(secrets.token_bytes(20))


def otpauth_uri(account, seed_b32, issuer=ISSUER):
    """给验证器 App 扫的 otpauth:// 链接 (只在签发时显示一次, 不进日志)."""
    if not valid_account(account):
        raise ValueError("账号名只能是小写字母开头的 [a-z0-9_-], 最长 32")
    q = urllib.parse.urlencode({"secret": seed_b32, "issuer": issuer, "algorithm": "SHA1",
                                "digits": DIGITS, "period": STEP_S})
    return f"otpauth://totp/{issuer}:{account}?{q}"


def load_seed(sdir, account, expect_uid=0):
    """读 <sdir>/totp-<账号>. 不是普通文件、是链接、属主不对、组或其他人有任何权限、太大、内容不对,
    一律返回 None (调用方按 no_seed 拒). 不抛异常, 不把原因带出种子内容."""
    if not valid_account(account):
        return None
    path = os.path.join(sdir, f"totp-{account}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != expect_uid \
                or st.st_size > SEED_FILE_MAX:
            return None
        data = os.read(fd, SEED_FILE_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return seed_from_b32(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


# ------------------------------------------------------------ 校验

def fresh_account():
    return {"last_step": 0, "fails": 0, "locked_until": 0.0, "lockouts": 0}


def valid_state(st):
    if not isinstance(st, dict):
        return False
    for k, typ in (("last_step", int), ("fails", int), ("lockouts", int), ("locked_until", (int, float))):
        v = st.get(k, fresh_account()[k])
        if isinstance(v, bool) or not isinstance(v, typ) or v < 0:
            return False
    return True


def verify(acct_state, key, code, now):
    """-> (结果, 新状态). 结果: ok / bad / replay / clock_back / locked / no_seed / bad_state.
    只有 bad 计入输错次数; 读不懂的账号状态不当成新账号 (否则 last_step 归零, 旧码能重放)."""
    if not valid_state(acct_state):
        return "bad_state", acct_state
    st = dict(fresh_account(), **acct_state)
    if now < st["locked_until"]:
        return "locked", st
    if key is None:
        return "no_seed", st
    now_step = int(now // STEP_S)
    hit = None
    if isinstance(code, str) and _CODE_RE.fullmatch(code):
        for s in range(now_step - WINDOW, now_step + WINDOW + 1):
            if hmac.compare_digest(hotp(key, s), code):
                hit = s
                break
    if hit is None:
        st["fails"] += 1
        if st["fails"] >= MAX_FAILS:
            span = min(LOCK_S * 2 ** st["lockouts"], MAX_LOCK_S)
            st["fails"], st["locked_until"], st["lockouts"] = 0, now + span, st["lockouts"] + 1
        return "bad", st
    if hit <= st["last_step"]:
        return ("clock_back" if now_step + WINDOW <= st["last_step"] else "replay"), st
    st.update(last_step=hit, fails=0, lockouts=0)
    return "ok", st
