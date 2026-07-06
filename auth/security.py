"""
auth/security.py — password hashing.

Uses argon2 (via argon2-cffi) rather than bcrypt: it's the current
recommended default (winner of the Password Hashing Competition, tunable
for both time and memory cost, which bcrypt isn't) and is what the
migration plan called for ("argon2/bcrypt").
"""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

_hasher = PasswordHasher()  # sane defaults; tune time_cost/memory_cost later if profiling says to


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with older/weaker parameters than current
    defaults — call after a successful verify and rehash+save if True.
    Not wired up anywhere yet; here so it's not forgotten later."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHash:
        return False
