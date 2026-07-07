"""
core/redis_client.py — single Redis connection + key-namespace conventions.

Redis plays three separate roles in this app (see the architecture diagram):
    1. Celery broker/result backend  — Celery manages its own keys, not our concern here
    2. Shared Yahoo-facing rate limiter/cooldown — replaces yf_ratelimit.py's
       in-process threading.Lock, which only works within a single process
    3. Short-TTL stock-data cache, shared across users — if two users scan
       overlapping stocks, the second one is (almost) free

This module only covers (2) and (3) — the "app-level" uses of Redis, as
opposed to Celery's own broker traffic which Celery configures separately
via CELERY_BROKER_URL.

Key namespace (all keys prefixed so `redis-cli --scan` stays readable):
    ratelimit:cooldown_until   — float unix timestamp; string
    ratelimit:last_request     — float unix timestamp; string
    cache:stock:{symbol}       — JSON-encoded fetch_stock_data() result, short TTL
"""
import logging
import os
import time

import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# socket_connect_timeout/socket_timeout: without these, redis-py's default
# is no timeout at all -- a stalled TCP connection or a slow/unreachable
# Redis (Render's free Key Value tier can have brief connectivity hiccups)
# blocks the calling thread FOREVER, with no exception raised. This is the
# exact same failure mode yf_ratelimit.py's REQUEST_TIMEOUT_S was added to
# fix for Yahoo's HTTP calls -- every worker thread ends up wedged inside
# throttle_wait() waiting on Redis, the scan's coordinator loop keeps
# ticking (it doesn't depend on any worker finishing), so last_heartbeat
# keeps updating and the job never looks "stale" -- it just sits at
# scanned_count=0 forever, looking alive.
# decode_responses=True so callers get str, not bytes, out of GET/etc.
_redis_pool = redis.ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)


def get_redis() -> redis.Redis:
    """Return a Redis client backed by the shared connection pool.

    Safe to call often — this doesn't open a new connection each time,
    it borrows one from the pool.
    """
    return redis.Redis(connection_pool=_redis_pool)


# ── Shared rate-limiter primitives (used by core/scanner.py in step 5) ──────
# These replace yf_ratelimit.py's threading.Lock-based _throttle/_trigger_cooldown,
# which only coordinates threads within ONE process. Once there's a pool of
# Celery worker processes all hitting Yahoo, the gate has to live somewhere
# every process can see — hence Redis instead of an in-memory lock.

_COOLDOWN_KEY = "ratelimit:cooldown_until"
_LAST_REQUEST_KEY = "ratelimit:last_request"

MIN_DELAY_S = 0.8   # mirrors yf_ratelimit.MIN_DELAY_S
COOLDOWN_S = 20.0   # mirrors yf_ratelimit.COOLDOWN_S


def throttle_wait():
    """Block the calling worker until it's safe to make the next Yahoo request.

    Every worker process calls this before hitting Yahoo. It's a thin
    Redis-backed version of yf_ratelimit._throttle() — same idea (global
    minimum delay + shared cooldown), but coordinated across processes
    instead of just threads.

    Fails OPEN: if Redis itself is unreachable/slow/erroring, this gives up
    on cross-process throttling for this one call rather than blocking the
    worker thread indefinitely (with socket timeouts now set on the pool,
    "indefinitely" would otherwise still mean minutes, not forever, but a
    scan with 4 workers all randomly stalling for a few seconds each on a
    flaky Redis is still worse than just proceeding -- Yahoo's own 429s are
    the backstop either way).
    """
    deadline = time.time() + 30  # absolute ceiling regardless of how many loop iterations
    while time.time() < deadline:
        try:
            r = get_redis()
            cooldown_until = float(r.get(_COOLDOWN_KEY) or 0)
            now = time.time()
            if now < cooldown_until:
                time.sleep(min(cooldown_until - now, 5))  # re-check in slices, don't oversleep past a cleared cooldown
                continue

            last = float(r.get(_LAST_REQUEST_KEY) or 0)
            wait = MIN_DELAY_S - (now - last)
            if wait > 0:
                time.sleep(wait)
                continue

            r.set(_LAST_REQUEST_KEY, time.time())
            return
        except redis.RedisError as exc:
            logger.warning("throttle_wait: Redis error (%s) — proceeding without throttle", exc)
            return
    logger.warning("throttle_wait: hit 30s safety ceiling — proceeding without throttle")


def trigger_cooldown(seconds: float = COOLDOWN_S):
    """Called by any worker that hits a real Yahoo 429.

    Pushes the shared cooldown deadline forward in Redis so every other
    worker process's next throttle_wait() call also pauses — the same
    "everyone backs off together, once" behaviour yf_ratelimit.py already
    has for threads, extended to a full pool of worker processes.
    """
    try:
        r = get_redis()
        target = time.time() + seconds
        # Only move the deadline forward, never backward (a late-arriving
        # cooldown from an older, smaller `seconds` shouldn't cut a longer one short).
        r.eval(
            """
            local current = tonumber(redis.call('GET', KEYS[1]) or '0')
            local target = tonumber(ARGV[1])
            if target > current then
                redis.call('SET', KEYS[1], target)
            end
            return redis.status_reply('OK')
            """,
            1,
            _COOLDOWN_KEY,
            target,
        )
    except redis.RedisError as exc:
        # A 429 already happened; failing to record the shared cooldown just
        # means other workers won't hear about it -- not a reason to also
        # crash the worker that hit the 429 in the first place.
        logger.warning("trigger_cooldown: Redis error (%s) — cooldown not shared", exc)


# ── Shared stock-data cache (used by core/scanner.py in step 5) ────────────
# Replaces the in-process _DATA_CACHE dict in core/scanner.py, which only
# helps the process that populated it. With Redis, if User A scans RELIANCE
# and User B scans it five minutes later, B's fetch is a cache hit instead
# of a fresh Yahoo call — this is the main lever against Yahoo throttling
# as usage grows, per the architecture notes.

_CACHE_PREFIX = "cache:stock:"
_CACHE_TTL_S = 300  # matches core/scanner.py's existing 300s TTL


def cache_get_stock(symbol: str):
    import json
    try:
        r = get_redis()
        raw = r.get(_CACHE_PREFIX + symbol)
        return json.loads(raw) if raw else None
    except redis.RedisError as exc:
        logger.warning("cache_get_stock(%s): Redis error (%s) — treating as cache miss", symbol, exc)
        return None


def cache_set_stock(symbol: str, data: dict, ttl: int = _CACHE_TTL_S):
    import json
    try:
        r = get_redis()
        r.set(_CACHE_PREFIX + symbol, json.dumps(data, default=str), ex=ttl)
    except redis.RedisError as exc:
        logger.warning("cache_set_stock(%s): Redis error (%s) — result not cached", symbol, exc)
