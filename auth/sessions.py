"""
auth/sessions.py — server-side sessions stored in Redis.

Why sessions-in-Redis over a JWT: revocation. A logout, a password change,
or an admin action ("kick this user") just deletes a key — no waiting for
a token to expire, no denylist to maintain. Redis is already part of the
stack for the rate limiter/cache, so this doesn't add a new moving part.

Key shape:  session:{session_id} -> user_id (string), with a TTL.
"""
import os
import secrets

from core.redis_client import get_redis

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(14 * 24 * 3600)))  # 14 days
_SESSION_PREFIX = "session:"


def create_session(user_id: str) -> str:
    """Create a new session for user_id, return the opaque session token."""
    session_id = secrets.token_urlsafe(32)
    get_redis().set(_SESSION_PREFIX + session_id, str(user_id), ex=SESSION_TTL_SECONDS)
    return session_id


def get_user_id(session_id: str) -> str | None:
    """Look up the user_id for a session token, refreshing its TTL (sliding
    expiry — active users stay logged in; idle sessions still expire)."""
    if not session_id:
        return None
    r = get_redis()
    key = _SESSION_PREFIX + session_id
    user_id = r.get(key)
    if user_id:
        r.expire(key, SESSION_TTL_SECONDS)
    return user_id


def delete_session(session_id: str) -> None:
    if session_id:
        get_redis().delete(_SESSION_PREFIX + session_id)
