"""
auth/lockout.py — slow down brute-force password guessing.

Simple fixed-window counter in Redis, keyed by email (not IP — a public
app behind shared NATs/mobile carriers means IP-based lockout locks out
innocent people sharing an address; email-based means only the actual
target account gets slowed down).

Not a replacement for rate-limiting at the reverse-proxy/WAF level later —
just a cheap first layer so a single leaked-password-list run can't hammer
one account thousands of times a minute.
"""
from core.redis_client import get_redis

_ATTEMPTS_PREFIX = "login_attempts:"
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60  # 15 minutes


def is_locked_out(email: str) -> bool:
    r = get_redis()
    count = r.get(_ATTEMPTS_PREFIX + email.lower())
    return int(count or 0) >= MAX_ATTEMPTS


def record_failed_attempt(email: str) -> None:
    r = get_redis()
    key = _ATTEMPTS_PREFIX + email.lower()
    count = r.incr(key)
    if count == 1:
        r.expire(key, WINDOW_SECONDS)


def clear_attempts(email: str) -> None:
    get_redis().delete(_ATTEMPTS_PREFIX + email.lower())
