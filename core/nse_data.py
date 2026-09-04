"""
core/nse_data.py — Direct NSE data source for intraday scanning. NO yfinance.
================================================================================
IMPORTANT CAVEAT, READ FIRST: NSE's website blocks traffic from datacenter/
cloud IP ranges (Render, AWS, GCP, ...) at the network/WAF (Akamai
bot-manager) level, independent of cookies, headers, or request shape. If
this module is failing with persistent 403s on quote-equity and/or
"Expecting value: line 1 column 1" JSON-decode errors on historical data
(a 200 status with an HTML challenge page instead of JSON) even after
session refreshes, that IS the block -- there is no cookie dance that
routes around it. This module still has a circuit breaker (see
NSE_BREAKER_THRESHOLD below) so a block fails fast instead of retrying
every symbol into the same wall, but it cannot make a blocked IP work.
Reliable NSE-direct access from a cloud host generally needs either a
residential/rotating proxy in front of these calls, or switching to a
real market-data vendor with an API key (Zerodha Kite Connect, Upstox,
Angel One, Truedata, etc.) instead of scraping the public NSE site.

Scope (deliberately narrow): this module ONLY covers what
core/intraday_scanner.py needs -- live quote + today's price series + last
few days' daily OHLCV for NSE-listed symbols. It does NOT attempt to
replace yfinance for BSE, and it does NOT attempt to replace yfinance for
fundamentals (income statement / balance sheet) -- NSE's public site has no
free endpoint for those; the positional scanner (core/scanner.py) keeps
using yfinance for both NSE and BSE, unchanged, for now.

WHY A SEPARATE MODULE, NOT A BRANCH INSIDE core/yf_ratelimit.py:
NSE's data model doesn't line up with Yahoo's at all -- different auth
(cookie handshake, not curl_cffi impersonation + crumb), different shapes
(a live quote object + a raw tick/price chart, not OHLCV candles), and a
different, much stricter tolerance for request rate. Bolting that onto the
Yahoo-shaped rate limiter would make both harder to reason about. This file
owns its own session, its own throttle, its own retry policy -- it shares
NOTHING with core/yf_ratelimit.py or core/redis_client.py's Yahoo-facing
cooldown. A block on one source can never pause the other.

NSE ENDPOINTS USED (public, unauthenticated, but require a same-session
cookie obtained by first hitting the homepage -- NSE rejects direct API
hits with no referer/cookie):
    GET /                                        -> sets cookies
    GET /api/quote-equity?symbol=SYM              -> live snapshot:
                                                      lastPrice, open,
                                                      dayHigh/dayLow,
                                                      previousClose,
                                                      totalTradedVolume
    GET /api/chart-databyindex?index=SYMEQN       -> today's raw price
                                                      ticks: [[epoch_ms,
                                                      price], ...], no
                                                      OHLC/volume per tick
    GET /api/historical/cm/equity?symbol=SYM&...  -> daily OHLCV history

DATA-SHAPE MAPPING vs core/intraday_scanner.fetch_intraday_data()'s
{'intraday': df, 'daily': df} contract, so analyze_intraday() in that file
needs ZERO changes to consume either source:
  - 'daily'    : built directly from the historical/cm/equity daily bars.
                 Real OHLCV, same as yfinance's history(period="5d").
  - 'intraday' : analyze_intraday() only ever reads Open[0], High.max(),
                 Low.min(), Close (full series, for RSI + momentum window),
                 and Volume.sum() (i.e. TOTAL day volume, not a per-minute
                 series -- it never looks at per-row volume). So:
                   * current/open/high/low price and total volume come
                     straight from quote-equity (more accurate than
                     deriving them from the tick chart, and one extra call
                     saved).
                   * the Close-series/RSI/ATR/momentum inputs are built by
                     resampling chart-databyindex's raw ticks into 1-minute
                     OHLC bars ourselves (NSE's chart updates every few
                     seconds during market hours, so a 1-min groupby gives
                     a real, if volume-less, OHLC candle series).
                   * Volume is placed only on the LAST row (as the day
                     total) rather than spread per-minute -- Volume.sum()
                     still comes out correct, and nothing else reads
                     Volume row-by-row.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{BASE_URL}/get-quotes/equity",
}

# ── config (own env vars -- deliberately NOT shared with YF_* ones, this
# source has nothing to do with Yahoo and shouldn't be tuned by the same
# knobs) ──────────────────────────────────────────────────────────────────
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


NSE_MIN_DELAY_S = _env_float("NSE_MIN_DELAY_S", 0.6)
NSE_MAX_RETRIES = _env_int("NSE_MAX_RETRIES", 3)
NSE_REQUEST_TIMEOUT_S = _env_float("NSE_REQUEST_TIMEOUT_S", 10.0)
NSE_COOKIE_TTL_S = _env_int("NSE_COOKIE_TTL_S", 240)  # NSE's cookies go stale
                     # well before a full scan finishes; refresh proactively
                     # rather than waiting to get 401s.

# ── session + cookie handshake (one per process, refreshed on TTL/failure) ─
_session_lock = threading.Lock()
_session: requests.Session | None = None
_session_ts = 0.0

# ── simple in-process throttle (single-process web server here, same
# caveat app/scan_runner.py's docstring already flags for the Yahoo path --
# if this ever moves to multiple worker processes, this needs a Redis gate
# too, same shape as core/redis_client.py's) ───────────────────────────────
_throttle_lock = threading.Lock()
_last_request_ts = 0.0


def _throttle():
    global _last_request_ts
    with _throttle_lock:
        wait = NSE_MIN_DELAY_S - (time.time() - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.time()


# ── circuit breaker ──────────────────────────────────────────────────────
# If NSE is blocking this host at the network/WAF level (very common for
# datacenter/cloud IPs -- Render, AWS, GCP, etc. -- independent of cookies
# or request shape), retrying per-symbol just hammers a closed door for
# the rest of the scan: every symbol pays 3 attempts x session-refresh x
# backoff before giving up, for a block a session refresh can never fix.
# Once we've seen enough consecutive failures to conclude this, stop
# calling NSE at all for a cooldown window and fail fast -- callers using
# data_source="auto" fall back to yfinance immediately instead of waiting
# out a doomed retry ladder every single time; callers using "nse" get a
# clean, fast None instead of the same wall of retries per symbol.
NSE_BREAKER_THRESHOLD = _env_int("NSE_BREAKER_THRESHOLD", 5)  # consecutive
                          # failures (across ALL symbols, this process)
                          # before tripping
NSE_BREAKER_COOLDOWN_S = _env_float("NSE_BREAKER_COOLDOWN_S", 120.0)

_breaker_lock = threading.Lock()
_consecutive_failures = 0
_breaker_open_until = 0.0


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.time() < _breaker_open_until


def _breaker_note_failure():
    global _consecutive_failures, _breaker_open_until
    with _breaker_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= NSE_BREAKER_THRESHOLD:
            _breaker_open_until = time.time() + NSE_BREAKER_COOLDOWN_S
            logger.warning(
                "nse_data: %d consecutive failures -- this almost always means NSE is "
                "blocking this server's IP at the network/WAF level (common for "
                "datacenter hosts like Render/AWS/GCP), NOT a stale-cookie problem a "
                "retry can fix. Stopping NSE calls for %.0fs so the rest of this scan "
                "doesn't hammer a closed door one symbol at a time.",
                _consecutive_failures, NSE_BREAKER_COOLDOWN_S,
            )


def _breaker_note_success():
    global _consecutive_failures, _breaker_open_until
    with _breaker_lock:
        _consecutive_failures = 0
        _breaker_open_until = 0.0


def _get_session(force: bool = False) -> requests.Session:
    global _session, _session_ts
    with _session_lock:
        if (not force and _session is not None
                and (time.time() - _session_ts) < NSE_COOKIE_TTL_S):
            return _session
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        # Homepage visit is what actually sets the cookies the /api/* calls
        # need -- hitting /api/quote-equity cold, with no prior cookie,
        # reliably 401s. NOTE: if NSE is blocking this IP at the WAF level
        # (see _breaker_note_failure above), this homepage visit itself may
        # come back as a challenge page rather than a real 200 -- refreshing
        # the session doesn't route around an IP-level block, only a truly
        # stale/expired cookie on an otherwise-allowed IP.
        sess.get(BASE_URL, timeout=NSE_REQUEST_TIMEOUT_S)
        _session = sess
        _session_ts = time.time()
        return sess


def _get(path: str, params: dict) -> dict | list | None:
    """GET an NSE /api/ endpoint with throttle + retry.

    Distinguishes two failure shapes that used to be handled identically
    (both just "retry with a fresh session"), because only one of them
    actually responds to that:
      - HTTP 401/403: could genuinely be a stale/expired cookie on an
        otherwise-allowed IP -- worth one session refresh + retry.
      - Non-JSON 200 response (a WAF/bot-check challenge page returned
        WITH a 200 status): a session refresh does nothing for this, it's
        an IP-level decision, not a cookie one. Retrying still happens
        (transient WAF checks do sometimes clear), but does NOT force a
        session refresh each time, and counts toward the circuit breaker
        so a persistent block doesn't repeat this dance for every symbol.
    """
    if _breaker_is_open():
        return None

    last_exc = None
    for attempt in range(NSE_MAX_RETRIES):
        _throttle()
        if attempt:
            backoff = 1.5 * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
            time.sleep(backoff)
        try:
            sess = _get_session(force=(attempt > 0))
            resp = sess.get(f"{BASE_URL}{path}", params=params,
                             timeout=NSE_REQUEST_TIMEOUT_S)
            if resp.status_code in (401, 403):
                last_exc = RuntimeError(f"NSE {resp.status_code}")
                logger.warning("nse_data: %s on %s, refreshing session (attempt %d/%d)",
                                resp.status_code, path, attempt + 1, NSE_MAX_RETRIES)
                continue
            resp.raise_for_status()
            try:
                result = resp.json()
            except ValueError:
                # 200 status but not JSON -- almost always a WAF challenge
                # page, not a data problem. Don't force a session refresh
                # for this: refreshing cookies doesn't get past an IP-level
                # block, it just burns another handshake round-trip.
                last_exc = RuntimeError("NSE returned a non-JSON 200 response "
                                         "(likely a bot-check/challenge page, not a data issue)")
                logger.warning("nse_data: non-JSON response from %s (attempt %d/%d) -- "
                                "likely blocked at the network level, not retrying with a "
                                "fresh session", path, attempt + 1, NSE_MAX_RETRIES)
                continue
            _breaker_note_success()
            return result
        except Exception as exc:  # noqa: BLE001 -- broad on purpose, this is a best-effort feed
            last_exc = exc
            logger.warning("nse_data: request failed on %s (attempt %d/%d): %s",
                            path, attempt + 1, NSE_MAX_RETRIES, exc)
    logger.warning("nse_data: giving up on %s after %d attempts: %s",
                    path, NSE_MAX_RETRIES, last_exc)
    _breaker_note_failure()
    return None


# ────────────────────────────────────────────────────────────────────────────
# PUBLIC DATA FETCHERS
# ────────────────────────────────────────────────────────────────────────────
def fetch_quote(nse_symbol: str) -> dict | None:
    """nse_symbol: bare NSE symbol, no .NS suffix (e.g. 'RELIANCE')."""
    data = _get("/api/quote-equity", {"symbol": nse_symbol})
    if not data:
        return None
    try:
        price_info = data["priceInfo"]
        return {
            "last_price": float(price_info["lastPrice"]),
            "open": float(price_info["open"]),
            "day_high": float(price_info["intraDayHighLow"]["max"]),
            "day_low": float(price_info["intraDayHighLow"]["min"]),
            "prev_close": float(price_info["previousClose"]),
            "total_volume": float(
                data.get("marketDeptOrderBook", {})
                    .get("tradeInfo", {})
                    .get("totalTradedVolume", 0) or 0
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("nse_data: unexpected quote-equity shape for %s: %s", nse_symbol, exc)
        return None


def fetch_intraday_ticks(nse_symbol: str) -> pd.DataFrame | None:
    """Today's raw (timestamp, price) ticks, resampled into 1-minute OHLC
    bars. Volume-less by construction (NSE's free chart feed doesn't carry
    per-tick volume) -- callers needing total day volume should use
    fetch_quote()'s total_volume instead of summing this frame's Volume."""
    data = _get("/api/chart-databyindex", {"index": f"{nse_symbol}EQN"})
    if not data or "grapthData" not in data or not data["grapthData"]:
        return None
    try:
        ticks = pd.DataFrame(data["grapthData"], columns=["ts", "price"])
        ticks["ts"] = pd.to_datetime(ticks["ts"], unit="ms")
        ticks = ticks.set_index("ts").sort_index()
        bars = ticks["price"].resample("1min").ohlc()
        bars = bars.dropna(how="all")
        if bars.empty:
            return None
        bars.columns = ["Open", "High", "Low", "Close"]
        bars["Volume"] = 0.0
        return bars
    except Exception as exc:  # noqa: BLE001
        logger.warning("nse_data: failed to build intraday bars for %s: %s", nse_symbol, exc)
        return None


def fetch_daily_history(nse_symbol: str, days: int = 5) -> pd.DataFrame | None:
    """Last `days` daily OHLCV bars, real volume included."""
    to_date = datetime.now()
    # Ask for a wider window than `days` calendar days so weekends/holidays
    # don't leave us short -- trim to the last `days` rows afterward.
    from_date = to_date - timedelta(days=days * 3 + 5)
    params = {
        "symbol": nse_symbol,
        "series": '["EQ"]',
        "from": from_date.strftime("%d-%m-%Y"),
        "to": to_date.strftime("%d-%m-%Y"),
    }
    data = _get("/api/historical/cm/equity", params)
    if not data or "data" not in data or not data["data"]:
        return None
    try:
        rows = data["data"]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["CH_TIMESTAMP"])
        df = df.sort_values("date").set_index("date")
        out = pd.DataFrame({
            "Open": df["CH_OPENING_PRICE"].astype(float),
            "High": df["CH_TRADE_HIGH_PRICE"].astype(float),
            "Low": df["CH_TRADE_LOW_PRICE"].astype(float),
            "Close": df["CH_CLOSING_PRICE"].astype(float),
            "Volume": df["CH_TOT_TRADED_QTY"].astype(float),
        })
        return out.tail(days)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("nse_data: unexpected historical shape for %s: %s", nse_symbol, exc)
        return None


def fetch_intraday_data_nse(symbol: str) -> dict[str, Any] | None:
    """Drop-in match for core/intraday_scanner.fetch_intraday_data()'s
    {'intraday': df, 'daily': df} contract, sourced entirely from NSE --
    no yfinance import anywhere in this module.

    symbol: full suffixed symbol as used elsewhere in this app, e.g.
    'RELIANCE.NS'. Only .NS is supported; call with a bare NSE symbol or a
    .BO symbol is a programming error in the caller, not a data problem,
    so this raises rather than silently returning None for those.
    """
    if not symbol.endswith(".NS"):
        raise ValueError(f"fetch_intraday_data_nse only supports .NS symbols, got {symbol!r}")
    nse_symbol = symbol[:-3]

    quote = fetch_quote(nse_symbol)
    daily = fetch_daily_history(nse_symbol, days=5)
    if quote is None or daily is None or daily.empty:
        return None

    bars = fetch_intraday_ticks(nse_symbol)
    if bars is None or bars.empty:
        # Fall back to a single synthetic bar built from the quote snapshot
        # so RSI/momentum just come out as "not enough data" (None/0, both
        # handled gracefully by analyze_intraday()) instead of failing the
        # whole fetch -- a live quote with a temporarily-unavailable chart
        # feed shouldn't be treated the same as a genuinely dead symbol.
        bars = pd.DataFrame({
            "Open": [quote["open"]], "High": [quote["day_high"]],
            "Low": [quote["day_low"]], "Close": [quote["last_price"]],
            "Volume": [0.0],
        }, index=[pd.Timestamp.now()])
    else:
        # Overwrite the last bar's Close with the live quote price (freshest
        # data point) and anchor Open/High/Low to the quote snapshot, which
        # reflects the FULL day's range -- the resampled chart only reflects
        # whatever the chart feed happened to return, which can lag.
        bars.iloc[-1, bars.columns.get_loc("Close")] = quote["last_price"]

    # Put the day's TOTAL volume on the last row only -- analyze_intraday()
    # sums the whole Volume column, and total_volume from quote-equity IS
    # the day total already, so putting it anywhere else would double- or
    # under-count.
    bars["Volume"] = 0.0
    bars.iloc[-1, bars.columns.get_loc("Volume")] = quote["total_volume"]
    # First row's Open should be the day's actual open, not just the first
    # resampled bar's open (which is whenever the chart feed's first tick
    # landed, not necessarily 09:15).
    bars.iloc[0, bars.columns.get_loc("Open")] = quote["open"]
    # Make sure day high/low from the authoritative quote snapshot are
    # represented somewhere in the series, since analyze_intraday() takes
    # High.max()/Low.min() over the whole frame.
    bars.iloc[-1, bars.columns.get_loc("High")] = max(
        quote["day_high"], bars.iloc[-1]["High"]
    )
    bars.iloc[-1, bars.columns.get_loc("Low")] = min(
        quote["day_low"], bars.iloc[-1]["Low"]
    )

    return {"intraday": bars, "daily": daily}
