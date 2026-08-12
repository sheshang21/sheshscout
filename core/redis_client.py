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

# These used to be separate hardcoded constants ("mirrors yf_ratelimit.*")
# that could silently drift out of sync with core/yf_ratelimit.py's own
# values -- and did: throttle_wait() below is what actually paces requests
# across worker processes via Redis, so a tuning change in yf_ratelimit.py
# alone never reached the real cross-process throttle. Reading the same
# env vars directly here removes the duplication instead of just
# re-syncing the numbers once.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("redis_client: bad value for %s, using default %s", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("redis_client: bad value for %s, using default %s", name, default)
        return default


MIN_DELAY_S = _env_float("YF_MIN_DELAY_S", 1.1)
COOLDOWN_S = _env_float("YF_COOLDOWN_S", 35.0)
# How long throttle_wait() will keep waiting out an ACTIVE cooldown before
# giving up and proceeding anyway. This used to be a bare hardcoded 30 --
# completely disconnected from COOLDOWN_S. The moment COOLDOWN_S was raised
# to 35 (see core/yf_ratelimit.py), that 30s ceiling started firing a few
# seconds BEFORE the cooldown it was supposed to be waiting out actually
# expired, so a worker would hit Yahoo again while still inside the
# cooldown window, immediately draw another 429, which reset the cooldown,
# which the ceiling cut short again -- a real loop, not a hypothetical one
# (see the repeated "hit 30s safety ceiling" / 429 pairs ~30s apart in the
# Render logs). Always sized comfortably longer than COOLDOWN_S now, so it
# can only ever cut off a genuinely stuck Redis loop, never an active,
# legitimate cooldown.
THROTTLE_MAX_WAIT_S = _env_float("YF_THROTTLE_MAX_WAIT_S", max(120.0, COOLDOWN_S * 2))
if THROTTLE_MAX_WAIT_S < COOLDOWN_S:
    logger.warning(
        "redis_client: YF_THROTTLE_MAX_WAIT_S=%.0f is shorter than YF_COOLDOWN_S=%.0f -- "
        "this will cut cooldowns short and cause repeated 429s. Raise "
        "YF_THROTTLE_MAX_WAIT_S above YF_COOLDOWN_S.",
        THROTTLE_MAX_WAIT_S, COOLDOWN_S,
    )


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
    deadline = time.time() + THROTTLE_MAX_WAIT_S  # absolute ceiling regardless of how
                                                    # many loop iterations -- see the
                                                    # note on THROTTLE_MAX_WAIT_S above
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
    logger.warning("throttle_wait: hit %.0fs safety ceiling — proceeding without throttle", THROTTLE_MAX_WAIT_S)


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


_EMPTY_STREAK_KEY = "ratelimit:empty_streak"
EMPTY_STREAK_THRESHOLD = _env_int("YF_EMPTY_STREAK_THRESHOLD", 4)  # how many EMPTY
    # responses in a row, across the WHOLE worker pool, before treating it as a
    # real silent block and triggering the shared cooldown -- as opposed to
    # triggering on every single empty response, which used to fire a full
    # COOLDOWN_S pause for one legitimately-delisted or no-trades-today
    # symbol. That's common and expected, especially for intraday scans
    # (period=1d/5d on illiquid small-caps genuinely returns empty on a
    # quiet day, with no rate-limiting involved at all) -- triggering a
    # pool-wide 35s+ pause on every one of those made intraday scans slower
    # than before this whole cooldown mechanism existed. A real Yahoo block
    # shows up as MANY consecutive empties across unrelated symbols, not one.


def note_empty_response(cooldown_seconds: float = COOLDOWN_S) -> bool:
    """Record one empty/possibly-blocked response. Returns True if this
    pushed the streak over EMPTY_STREAK_THRESHOLD and triggered the shared
    cooldown, False otherwise (including on Redis errors -- fails open,
    same reasoning as everywhere else in this module: a scan that can't
    coordinate cross-process throttling should degrade to "no shared
    signal" rather than block or crash)."""
    try:
        r = get_redis()
        streak = r.incr(_EMPTY_STREAK_KEY)
        r.expire(_EMPTY_STREAK_KEY, 120)  # a stale streak from a much earlier,
                                           # already-resolved block shouldn't
                                           # linger forever if nothing resets it
        if streak >= EMPTY_STREAK_THRESHOLD:
            logger.warning(
                "note_empty_response: %d empty responses in a row across the pool -- "
                "treating as a real block, cooling down ALL workers for %.0fs",
                streak, cooldown_seconds,
            )
            trigger_cooldown(cooldown_seconds)
            r.set(_EMPTY_STREAK_KEY, 0)
            return True
        return False
    except redis.RedisError as exc:
        logger.warning("note_empty_response: Redis error (%s) — streak not tracked", exc)
        return False


def note_success() -> None:
    """Any real (non-empty) response breaks the empty streak -- one
    delisted stock in the middle of a run of good responses is noise, not
    a block, and shouldn't be allowed to accumulate toward the threshold
    over the course of a long scan."""
    try:
        r = get_redis()
        r.set(_EMPTY_STREAK_KEY, 0)
    except redis.RedisError as exc:
        logger.warning("note_success: Redis error (%s) — streak not reset", exc)


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
