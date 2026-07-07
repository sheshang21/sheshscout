"""
yf_ratelimit.py  ·  Universal yfinance Rate-Limit Shield  (Redis-backed variant)
=================================================================================
*** This is the core/ copy, used by core/scanner.py -- NOT the same file as
*** the root yf_ratelimit.py, which sheshscout.py (the Streamlit app) still
*** uses standalone, unmodified, with its original in-process rate limiter.
***
*** The two copies are DELIBERATELY DIFFERENT as of step 5:
***   root yf_ratelimit.py  -> in-process threading.Lock gate (unchanged,
***                            Streamlit app doesn't need Redis to run)
***   core/yf_ratelimit.py  -> Redis-backed gate via core/redis_client.py
***                            (this file), because once Celery runs more
***                            than one worker PROCESS, an in-process
***                            threading.Lock only coordinates threads
***                            within one of those processes -- a second
***                            worker process has its own separate lock
***                            and has no idea the first one just got
***                            hit with a 429. Redis is visible to all of
***                            them, so the shared cooldown actually works.
***
*** This file REQUIRES a reachable Redis (REDIS_URL) at call time -- not
*** at import time, so importing core.scanner still works without Redis
*** running, but actually calling fetch_stock_data() does not.

Drop-in replacement / umbrella for ALL yfinance calls in this app.

HOW IT WORKS
------------
1. curl_cffi Chrome-impersonation session  →  bypasses Yahoo's bot filters
2. Streamlit @st.cache_data               →  deduplicate identical calls (1-hr TTL)
3. Exponential back-off with jitter       →  survive transient 429s
4. In-process LRU memory cache            →  zero-network hits for repeat symbols
                                              within ONE process (see note below)
5. Redis-backed concurrency throttle      →  stay under Yahoo's rate budget,
                                              shared across every worker process

NOTE ON THE IN-PROCESS CACHE BELOW (_mem_get/_mem_set): this is a finer-grained
cache than core/scanner.py's own Redis-backed per-symbol cache (added in step
5) -- it caches individual .history()/.info/etc. calls per Ticker property,
not the whole fetch_stock_data() result. Since scanner.py's Redis cache
already prevents most duplicate top-level fetches across workers, this
inner cache staying in-process is a minor, low-risk simplification rather
than a correctness gap -- it only matters for calls that fall through
scanner.py's cache check, and even then it just costs one extra fetch,
not a rate-limit storm. Flagging it here rather than silently leaving it
unexplained.

HOW TO USE  (two-line migration per file)
-----------------------------------------
BEFORE:
    import yfinance as yf
    ticker = yf.Ticker("RELIANCE.NS")
    df     = yf.download("RELIANCE.NS", period="1y")

AFTER:
    from yf_ratelimit import safe_ticker, safe_download
    ticker = safe_ticker("RELIANCE.NS")
    df     = safe_download("RELIANCE.NS", period="1y")

Everything else (.info, .financials, .history, .balance_sheet, .cashflow,
.options, .option_chain …) works exactly the same on the returned object.
"""

from __future__ import annotations

import functools
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── optional streamlit cache ────────────────────────────────────────────────
try:
    import streamlit as st
    _HAS_ST = True
except Exception:
    _HAS_ST = False

# ── curl_cffi (preferred) → requests (fallback) ─────────────────────────────
try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL = True
except ImportError:
    import requests as _curl_requests          # type: ignore[assignment]
    _HAS_CURL = False

import yfinance as yf

try:
    from .redis_client import throttle_wait as _redis_throttle_wait
    from .redis_client import trigger_cooldown as _redis_trigger_cooldown
except ImportError:
    from redis_client import throttle_wait as _redis_throttle_wait      # running standalone
    from redis_client import trigger_cooldown as _redis_trigger_cooldown

# ────────────────────────────────────────────────────────────────────────────
# CONFIG  (tune here if needed)
# ────────────────────────────────────────────────────────────────────────────
MIN_DELAY_S      = 0.8    # minimum pause between Yahoo requests
MAX_DELAY_S      = 2.5    # maximum pause (random jitter)
MAX_RETRIES      = 3      # retry budget per call (was 5 -- see cooldown note below)
BASE_BACKOFF_S   = 3.0    # base for exponential backoff on 429
CACHE_TTL_S      = 3600   # in-process cache TTL (1 hour)
COOLDOWN_S       = 20.0   # shared pause applied to ALL threads/processes after any 429
REQUEST_TIMEOUT_S = 15.0  # hard ceiling on any single HTTP call to Yahoo -- see
                          # _make_session() below. Without this, a stalled/half-open
                          # TCP connection blocks its worker thread FOREVER. With a
                          # fixed-size ThreadPoolExecutor, enough of these pile up and
                          # every worker ends up wedged on a dead socket at once --
                          # the scan just stops advancing mid-run, at a different,
                          # seemingly arbitrary stock count each time. This is the
                          # actual root cause of scans "getting stuck" partway through
                          # (e.g. around 80 or 130 tickers) -- never about which
                          # ticker, always about how many stalled sockets happen to
                          # accumulate before every worker thread is occupied.
_CHROME_UA       = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ────────────────────────────────────────────────────────────────────────────
# RATE-LIMIT GATE  (shared across every worker PROCESS via Redis, not just
# threads within one process -- see module docstring for why that matters)
# ────────────────────────────────────────────────────────────────────────────
def _throttle():
    """Block until it's safe to make the next Yahoo request, per the
    shared Redis-backed gate in core/redis_client.py."""
    _redis_throttle_wait()


def _trigger_cooldown(seconds: float = COOLDOWN_S):
    """Called by any thread/process that hits a real 429. Pushes the
    shared cooldown deadline forward in Redis so every OTHER worker
    process's next _throttle() call also pauses -- instead of N worker
    processes independently backing off and retrying into each other,
    they all go quiet together. This is what actually stops the retry
    storm that used to cascade into an 80+ minute stall, now extended
    from "all threads in one process" to "all processes, period."
    """
    _redis_trigger_cooldown(seconds)
    logger.warning("yf_ratelimit: 429 detected -- cooling down ALL workers for %.0fs", seconds)


# ────────────────────────────────────────────────────────────────────────────
# SESSION FACTORY
# ────────────────────────────────────────────────────────────────────────────
_thread_local = threading.local()

def _make_session():
    """
    Return a curl_cffi Chrome-impersonation session, cached per WORKER
    THREAD (not per symbol -- that was the original design here, on the
    theory that a fresh session per symbol stops Yahoo tracking connection
    state across stocks). In practice, a real curl_cffi session is a live
    libcurl handle with its own TLS/connection-pool buffers, and a scan
    covering hundreds-to-thousands of symbols was creating that many of
    them. Combined with the two caches above (also previously unbounded),
    that's the actual cause of the process getting OOM-killed at a
    consistent point in every scan on Render's 512MB free instance --
    not a random hang. One session per thread (there are only
    MAX_WORKERS of them, see app/scan_runner.py) bounds this to a small
    constant no matter how large the scan is, while still rotating
    identity across the handful of worker threads rather than reusing a
    single session for literally everything.
    """
    sess = getattr(_thread_local, "session", None)
    if sess is not None:
        return sess

    if _HAS_CURL:
        sess = _curl_requests.Session(impersonate="chrome124")
    else:
        import requests as _req
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        sess = _req.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        sess.mount("https://", HTTPAdapter(max_retries=retry))
        sess.mount("http://",  HTTPAdapter(max_retries=retry))

    sess.headers.update({
        "User-Agent":      _CHROME_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })

    # ── enforce a default timeout on every request through this session ────
    # yfinance calls session.get(...)/session.request(...) internally without
    # ever passing a timeout, so a stalled connection to Yahoo just hangs the
    # calling thread indefinitely -- no exception, nothing for _with_retry's
    # try/except to catch, nothing for the ThreadPoolExecutor to notice.
    # Wrapping .request() here so ANY call path (yfinance internals included)
    # gets a real ceiling, without having to touch yfinance's own code.
    _orig_request = sess.request

    def _request_with_timeout(method, url, *args, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_S)
        return _orig_request(method, url, *args, **kwargs)

    sess.request = _request_with_timeout
    _thread_local.session = sess
    return sess


# ────────────────────────────────────────────────────────────────────────────
# IN-PROCESS MEMORY CACHE  (survives across Streamlit reruns in same process)
# ────────────────────────────────────────────────────────────────────────────
# Also previously unbounded -- holds the actual DataFrames per symbol per
# property (history, financials, balance_sheet, ...), so a long scan grew
# this right alongside _ticker_registry above. Same LRU cap treatment.
_MEM_CACHE_MAX = 1000
_mem_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_cache_lock = threading.Lock()

def _mem_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _mem_cache.get(key)
        if entry and (time.time() - entry[0]) < CACHE_TTL_S:
            _mem_cache.move_to_end(key)
            return entry[1]
    return None

def _mem_set(key: str, value: Any):
    with _cache_lock:
        _mem_cache[key] = (time.time(), value)
        _mem_cache.move_to_end(key)
        while len(_mem_cache) > _MEM_CACHE_MAX:
            _mem_cache.popitem(last=False)

def clear_cache(symbol: str | None = None):
    """Clear in-process cache.  Pass symbol to clear only that ticker."""
    with _cache_lock:
        if symbol:
            keys = [k for k in _mem_cache if k.startswith(symbol)]
            for k in keys:
                _mem_cache.pop(k, None)
        else:
            _mem_cache.clear()
    logger.info("yf_ratelimit: cache cleared%s",
                f" for {symbol}" if symbol else " (all)")


# ────────────────────────────────────────────────────────────────────────────
# RETRY DECORATOR  (wraps any callable that talks to Yahoo)
# ────────────────────────────────────────────────────────────────────────────
def _with_retry(fn, *args, **kwargs):
    """
    Call fn(*args, **kwargs) with throttle + exponential backoff on errors.
    Returns the result or raises the last exception after MAX_RETRIES.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        jitter = random.uniform(0, MAX_DELAY_S - MIN_DELAY_S)
        if attempt:
            backoff = BASE_BACKOFF_S * (2 ** (attempt - 1)) + jitter
            logger.warning("yf_ratelimit: retry %d/%d — waiting %.1fs",
                           attempt, MAX_RETRIES, backoff)
            time.sleep(backoff)
        try:
            result = fn(*args, **kwargs)
            # yf.download returns a DataFrame; empty == likely rate-limited
            if isinstance(result, pd.DataFrame) and result.empty and attempt < MAX_RETRIES - 1:
                logger.warning("yf_ratelimit: empty DataFrame on attempt %d — retrying", attempt + 1)
                last_exc = RuntimeError("Empty DataFrame returned (possible silent 429)")
                continue
            return result
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            is_rate = any(x in msg for x in ("429", "rate", "too many", "forbidden", "403"))
            if not is_rate:
                # Non-rate-limit error — don't keep retrying
                raise
            logger.warning("yf_ratelimit: rate-limit hit on attempt %d: %s", attempt + 1, exc)
            _trigger_cooldown()  # tell every other thread to back off too

    raise last_exc or RuntimeError("yf_ratelimit: all retries exhausted")


# ────────────────────────────────────────────────────────────────────────────
# PUBLIC API  ── safe_ticker() and safe_download()
# ────────────────────────────────────────────────────────────────────────────

class _CachedTicker:
    """
    Lazy, cached wrapper around yf.Ticker.  All property accesses are
    cached in-process and retried on rate-limit errors.
    """
    _PROPS = ("info", "financials", "income_stmt", "balance_sheet",
              "cashflow", "quarterly_financials", "quarterly_income_stmt",
              "quarterly_balance_sheet", "quarterly_cashflow",
              "fast_info", "dividends", "splits", "actions",
              "recommendations", "calendar", "earnings_dates",
              "options")

    def __init__(self, symbol: str):
        self._symbol  = symbol
        self._yf_obj  = None
        self._yf_lock = threading.Lock()

    # -- lazy yf.Ticker construction -----------------------------------------
    def _get_yf(self) -> yf.Ticker:
        with self._yf_lock:
            if self._yf_obj is None:
                sess = _make_session()
                self._yf_obj = yf.Ticker(self._symbol, session=sess)
        return self._yf_obj

    # -- generic cached property fetch ----------------------------------------
    def _fetch_prop(self, prop: str) -> Any:
        key = f"{self._symbol}:prop:{prop}"
        cached = _mem_get(key)
        if cached is not None:
            return cached

        def _do():
            return getattr(self._get_yf(), prop)

        result = _with_retry(_do)
        _mem_set(key, result)
        return result

    # -- expose all standard yf.Ticker properties transparently --------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._PROPS:
            return self._fetch_prop(name)
        # Pass through anything else (e.g. .ticker, .isin)
        return getattr(self._get_yf(), name)

    # -- history() — supports arbitrary kwargs --------------------------------
    def history(self, period="1mo", interval="1d", **kwargs) -> pd.DataFrame:
        key = f"{self._symbol}:history:{period}:{interval}:{sorted(kwargs.items())}"
        cached = _mem_get(key)
        if cached is not None:
            return cached

        def _do():
            return self._get_yf().history(period=period, interval=interval, **kwargs)

        result = _with_retry(_do)
        _mem_set(key, result)
        return result

    # -- option_chain() -------------------------------------------------------
    def option_chain(self, date: str | None = None) -> Any:
        key = f"{self._symbol}:option_chain:{date}"
        cached = _mem_get(key)
        if cached is not None:
            return cached

        def _do():
            return self._get_yf().option_chain(date) if date else self._get_yf().option_chain()

        result = _with_retry(_do)
        _mem_set(key, result)
        return result

    # -- repr / str -----------------------------------------------------------
    def __repr__(self):
        return f"<CachedTicker '{self._symbol}'>"

    def __str__(self):
        return self._symbol


# -- module-level Ticker cache (one object per symbol per process) -----------
# THIS WAS UNBOUNDED, AND IT'S THE REAL REASON THE PROCESS GETS OOM-KILLED AT
# A CONSISTENT STOCK COUNT (not randomly): every symbol a scan ever touches
# stays in this dict FOREVER (only a process restart clears it), and each
# entry holds a live yf.Ticker object plus everything it's cached internally
# (history, financials, balance sheet, cashflow DataFrames). A full-universe
# scan never revisits the same symbol twice in one run, so none of this
# caching does anything useful for that case -- it's pure accumulation.
# Combined with the _mem_cache below (same problem, holds the actual
# DataFrames a second time), this is what eats Render's 512MB ceiling
# roughly N stocks into every scan. Bounded with real LRU eviction now --
# capacity sized for "helps repeated lookups of the same symbol in a short
# window" (dashboard refreshes, resume flows), not "hold the whole universe."
_TICKER_REGISTRY_MAX = 200
_ticker_registry: "OrderedDict[str, _CachedTicker]" = OrderedDict()
_registry_lock   = threading.Lock()

def safe_ticker(symbol: str) -> _CachedTicker:
    """
    Drop-in for yf.Ticker(symbol).

    Returns a cached, rate-limit-aware wrapper. The same object is reused
    across calls with the same symbol within a short window; least-recently-
    used symbols are evicted once _TICKER_REGISTRY_MAX is exceeded so a
    long scan can't grow this without bound.

    Usage:
        from yf_ratelimit import safe_ticker
        t = safe_ticker("RELIANCE.NS")
        print(t.info["currentPrice"])
        df = t.history(period="1y")
    """
    with _registry_lock:
        existing = _ticker_registry.get(symbol)
        if existing is not None:
            _ticker_registry.move_to_end(symbol)
            return existing
        _ticker_registry[symbol] = _CachedTicker(symbol)
        while len(_ticker_registry) > _TICKER_REGISTRY_MAX:
            _ticker_registry.popitem(last=False)
        return _ticker_registry[symbol]


def safe_download(
    tickers,
    period: str = "1mo",
    interval: str = "1d",
    flatten: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """
    Drop-in for yf.download(tickers, ...).

    Extra args vs yf.download:
        flatten (bool): If True (default), flatten MultiIndex columns to
                        single-level "Close", "Open" … instead of
                        ("Close", "AAPL") etc.  Matches pre-0.2 behaviour.

    Usage:
        from yf_ratelimit import safe_download
        df = safe_download("RELIANCE.NS", period="1y")
        df = safe_download(["RELIANCE.NS", "TCS.NS"], start="2023-01-01")
    """
    # Build a stable cache key
    ticker_key = tickers if isinstance(tickers, str) else "|".join(sorted(tickers))
    key = f"download:{ticker_key}:{period}:{interval}:{sorted(kwargs.items())}"
    cached = _mem_get(key)
    if cached is not None:
        return cached

    def _do():
        sess = _make_session()
        return yf.download(
            tickers,
            period=period,
            interval=interval,
            session=sess,
            progress=False,
            **kwargs,
        )

    df = _with_retry(_do)

    # Flatten MultiIndex columns (yfinance >= 0.2 wraps single-ticker downloads too)
    if flatten and isinstance(df.columns, pd.MultiIndex):
        if isinstance(tickers, str) or (isinstance(tickers, (list, tuple)) and len(tickers) == 1):
            df.columns = df.columns.get_level_values(0)
        # For multi-ticker downloads keep MultiIndex — caller can handle it

    _mem_set(key, df)
    return df


# ────────────────────────────────────────────────────────────────────────────
# STREAMLIT CACHE LAYER  (optional — adds cross-rerun deduplication)
# Only activated when Streamlit is present (i.e. inside a Streamlit app)
# ────────────────────────────────────────────────────────────────────────────
if _HAS_ST:
    @st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
    def st_download(tickers, period="1mo", interval="1d", **kwargs) -> pd.DataFrame:
        """st.cache_data-backed version of safe_download.  Use this in Streamlit pages."""
        return safe_download(tickers, period=period, interval=interval, **kwargs)

    @st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
    def st_ticker_info(symbol: str) -> dict:
        """st.cache_data-backed .info fetch.  Fast for repeated Streamlit reruns."""
        return safe_ticker(symbol).info

    @st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
    def st_ticker_history(symbol: str, period="1mo", interval="1d") -> pd.DataFrame:
        """st.cache_data-backed .history fetch."""
        return safe_ticker(symbol).history(period=period, interval=interval)


# ────────────────────────────────────────────────────────────────────────────
# QUICK SELF-TEST  (run with: python yf_ratelimit.py)
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("curl_cffi available:", _HAS_CURL)

    print("\n— safe_ticker test —")
    t = safe_ticker("RELIANCE.NS")
    info = t.info
    print("currentPrice:", info.get("currentPrice") or info.get("regularMarketPrice"))

    print("\n— safe_download test —")
    df = safe_download("RELIANCE.NS", period="5d")
    print(df.tail(3))

    print("\n— cache hit test (should be instant) —")
    t2 = safe_ticker("RELIANCE.NS")
    print("Same object?", t is t2)

    print("\nAll tests passed ✓")
