"""
core/scanner.py — Stock Scout core scanning logic (framework-free)
====================================================================
Pulled out of sheshscout.py so it can be called from anywhere: a Celery
task, a FastAPI endpoint, a one-off script, or (still) a Streamlit app.
No Streamlit import here — this module doesn't know or care what's
calling it.

Public surface (what the rest of the app should import):
    fetch_stock_data(symbol)                        -> dict | None
    analyze_stock(data, min_market_cap, thresholds)  -> dict | None
    fetch_live_price(symbol)                        -> float | None
    to_jsonable(value)                               -> JSON-safe value
    SECTOR_MAP                                       -> dict

Everything prefixed with `_` is an internal helper.

NOTE ON STATE (updated as of step 5 -- Celery + Redis wiring):
  Three pieces of state used to be process-local, file-based, or both:
    - the scan checkpoint         -- REMOVED. Superseded by scan_jobs +
      scan_results in Postgres (step 4): resuming means "diff the
      universe against existing results," not "read a checkpoint file."
    - the in-memory data cache    -- NOW Redis-backed (cache_get_stock/
      cache_set_stock, via core/redis_client.py), shared across every
      Celery worker process, not just threads in one process.
    - the dead-symbol blacklist   -- STILL file-based (_is_known_dead /
      _mark_dead_symbol). This works across processes IF they share a
      filesystem (e.g. same host/volume), but isn't as robust as the
      Postgres dead_symbols table already sitting unused in db/models.py.
      Deliberately left alone in step 5 to keep this step scoped to what
      was asked (rate limiter + cache) -- flagged here as a known,
      easy-to-do-later gap rather than silently leaving it unexplained.

  The Yahoo-facing rate limiter (MIN_DELAY_S gate + 429 cooldown) also
  moved to Redis as of step 5 -- see core/yf_ratelimit.py's docstring,
  which is a DIFFERENT FILE from the root yf_ratelimit.py that
  sheshscout.py still uses standalone, unmodified.
"""

# ── yfinance rate-limit shim ─────────────────────────────────────
try:
    from .yf_ratelimit import safe_ticker as _rl_ticker   # normal package import
except ImportError:
    from yf_ratelimit import safe_ticker as _rl_ticker    # running scanner.py standalone
import threading


class _YFShim:
    """Thin shim so existing yf.Ticker() calls use the rate-limit-safe wrapper."""
    @staticmethod
    def Ticker(symbol, **_):
        return _rl_ticker(symbol)


yf = _YFShim()
# ── end shim ──────────────────────────────────────────────────────

import pandas as pd
import numpy as np
import math
from datetime import datetime
import warnings
import time
import logging
import json
import os

# NOTE: the disk-based scan checkpoint that used to live here (matching
# sheshscout.py's own checkpoint, still present there and still needed
# by the standalone Streamlit app) was removed from THIS copy in step 5.
# It's dead code here: nothing in app/scan_runner.py or the API calls it,
# since resuming a scan now means "diff the job's universe against
# scan_results already in Postgres," not "read a checkpoint file."


# ── Known-dead symbol cache (delisted / not-found) ─────────────────
# A delisted symbol still triggers yf_ratelimit's full internal retry ladder
# (up to 5 attempts x growing backoff) on every fresh scan/restart, which is
# what stalled the app long enough for Streamlit's health check to time out.
#
# CAUTION: yfinance returns an empty history() DataFrame both for a truly
# delisted symbol AND for a symbol that got caught in a shared rate-limit
# burst -- there's no way to tell those apart from a single empty result.
# So we require TWO empty results, at least an hour apart, before treating a
# symbol as dead. A one-off rate-limit burst (many symbols empty at once,
# all within seconds of each other) will not trigger a blacklist; only a
# symbol that's *still* empty on a later, separate scan will.
# Entries expire after 30 days in case a halted stock relists.
DEAD_SYMBOLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dead_symbols.json")
_DEAD_SYMBOLS_TTL = 30 * 24 * 3600
_DEAD_STRIKE_MIN_GAP = 3600      # seconds between strikes for them to count separately
_DEAD_STRIKE_THRESHOLD = 2       # strikes needed before a symbol is treated as dead
_DEAD_SYMBOLS_CACHE = None       # symbol -> list of strike timestamps
_DEAD_SYMBOLS_LOCK = threading.Lock()


def _load_dead_symbols():
    try:
        with open(DEAD_SYMBOLS_PATH, "r") as f:
            data = json.load(f)
        now = time.time()
        cleaned = {}
        for s, strikes in data.items():
            strikes = [t for t in strikes if now - t < _DEAD_SYMBOLS_TTL]
            if strikes:
                cleaned[s] = strikes
        return cleaned
    except Exception:
        return {}


def _is_known_dead(symbol):
    global _DEAD_SYMBOLS_CACHE
    with _DEAD_SYMBOLS_LOCK:
        if _DEAD_SYMBOLS_CACHE is None:
            _DEAD_SYMBOLS_CACHE = _load_dead_symbols()
        return len(_DEAD_SYMBOLS_CACHE.get(symbol, [])) >= _DEAD_STRIKE_THRESHOLD


def _mark_dead_symbol(symbol):
    global _DEAD_SYMBOLS_CACHE
    with _DEAD_SYMBOLS_LOCK:
        if _DEAD_SYMBOLS_CACHE is None:
            _DEAD_SYMBOLS_CACHE = _load_dead_symbols()
        strikes = _DEAD_SYMBOLS_CACHE.get(symbol, [])
        now = time.time()
        if not strikes or (now - strikes[-1]) >= _DEAD_STRIKE_MIN_GAP:
            strikes.append(now)
            _DEAD_SYMBOLS_CACHE[symbol] = strikes
            try:
                with open(DEAD_SYMBOLS_PATH, "w") as f:
                    json.dump(_DEAD_SYMBOLS_CACHE, f)
            except Exception:
                pass


def _clear_dead_symbols():
    global _DEAD_SYMBOLS_CACHE
    with _DEAD_SYMBOLS_LOCK:
        _DEAD_SYMBOLS_CACHE = {}
        try:
            os.remove(DEAD_SYMBOLS_PATH)
        except Exception:
            pass


# Configure logging to show warnings but not info
warnings.filterwarnings('ignore')
logging.getLogger('yfinance').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

# Comprehensive Stock Universe - 200+ NSE Stocks

SECTOR_MAP = {
    'RELIANCE': 'Energy', 'TCS': 'IT', 'HDFCBANK': 'Banking', 'INFY': 'IT', 'ICICIBANK': 'Banking',
    'HINDUNILVR': 'FMCG', 'ITC': 'FMCG', 'SBIN': 'Banking', 'BHARTIARTL': 'Telecom', 'KOTAKBANK': 'Banking',
    'LT': 'Infrastructure', 'AXISBANK': 'Banking', 'ASIANPAINT': 'Paints', 'MARUTI': 'Auto', 'HCLTECH': 'IT',
    'BAJFINANCE': 'NBFC', 'WIPRO': 'IT', 'SUNPHARMA': 'Pharma', 'TITAN': 'Consumer', 'ULTRACEMCO': 'Cement',
    'NESTLEIND': 'FMCG', 'ONGC': 'Energy', 'TATAMOTORS': 'Auto', 'NTPC': 'Power', 'POWERGRID': 'Power',
    'JSWSTEEL': 'Metals', 'M&M': 'Auto', 'TECHM': 'IT', 'ADANIENT': 'Conglomerate', 'ADANIPORTS': 'Infrastructure'
}

# ── Shared stock-data cache (Redis-backed as of step 5 — see module docstring) ──
try:
    from .redis_client import cache_get_stock, cache_set_stock
except ImportError:
    from redis_client import cache_get_stock, cache_set_stock  # running standalone


def to_jsonable(value):
    """Recursively convert numpy/pandas scalar types (and NaN) to plain
    Python so a result dict can round-trip through JSON -- needed both
    for the Redis cache below and for storing ScanResult.raw_result in
    Postgres (app/scan_runner.py imports this rather than redefining it).
    Plain numpy arrays (closes/highs/lows/volumes) become lists here;
    see _restore_arrays() for the reverse conversion on cache reads.
    """
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array
        return value.tolist()
    if hasattr(value, "item"):  # numpy scalar (float64, int64, bool_, ...)
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


_ARRAY_FIELDS = ("closes", "highs", "lows", "volumes")


def _restore_arrays(cached: dict) -> dict:
    """Reverse of to_jsonable() for the array fields specifically -- a
    cache HIT off Redis comes back as plain lists (JSON has no array
    type), but analyze_stock() and the indicator functions expect real
    numpy arrays (slicing, np.mean, etc.), so restore them on the way out.
    """
    restored = dict(cached)
    for field in _ARRAY_FIELDS:
        if restored.get(field) is not None:
            restored[field] = np.array(restored[field])
    return restored


# ── Global concurrency gate ──────────────────────────────────────────────────
# Controls how many workers are actually hitting Yahoo at the same moment.
# Each stock needs ~3 HTTP calls; 6 workers × 3 calls = 18 concurrent connections,
# well under Yahoo's ~20–30 limit. Adjust _YF_SEMAPHORE_COUNT at runtime via sidebar.
_YF_SEMAPHORE_COUNT = 6
_YF_SEMAPHORE = threading.Semaphore(_YF_SEMAPHORE_COUNT)

_RETRY_MAX = 3
_RETRY_INITIAL_DELAY = 2   # seconds — longer base so backoff gives Yahoo breathing room


def bulletproof_fetch(func, *args, max_retries=None, initial_delay=None, **kwargs):
    """Single-shot wrapper around a Yahoo-calling function.

    IMPORTANT: yf_ratelimit.py's _CachedTicker already retries every individual
    Yahoo call up to MAX_RETRIES times with its own exponential backoff before
    raising. Retrying the *whole* fetch_stock_data() call again here on top of
    that used to multiply delays (outer_retries x inner_retries) while holding
    a worker slot the entire time -- that compounding was what caused scans to
    stall for 80+ minutes under real rate-limiting. So: call once, catch, and
    bail. The semaphore is only held for the single attempt, never across a
    sleep/backoff, so a slow/stuck symbol can't starve the other workers.
    """
    with _YF_SEMAPHORE:
        try:
            return func(*args, **kwargs)
        except Exception:
            return None


def fetch_stock_data(symbol):
    """Fetch data from Yahoo Finance using only 2 HTTP calls per stock.

    Call map (was 6, now 2):
      CALL 1 — ticker.history()         → OHLCV for all technicals
      CALL 2 — ticker.get_financials()  → annual income stmt (revenue, margins)
               ticker.quarterly_income_stmt is a cached sub-slice of the same endpoint
               ticker.balance_sheet     → fetched once, reused for cash + historical
      fast_info                         → zero extra HTTP call (uses cached history metadata)

    ticker.info is intentionally NOT called — it is the single most throttled
    Yahoo endpoint and returns the same fundamentals we derive below from financials.

    Symbol must already include .NS or .BO suffix.
    """
    # ── Known-dead short-circuit (no network call at all) ────────
    if _is_known_dead(symbol):
        return None

    # ── Cache check (300s TTL, shared across every worker process via Redis) ──
    cached = cache_get_stock(symbol)
    if cached is not None:
        return _restore_arrays(cached)

    try:
        ticker = yf.Ticker(symbol)

        # ── CALL 1: Price history ────────────────────────────────
        hist = ticker.history(period="3mo", interval="1d")
        if hist.empty:
            _mark_dead_symbol(symbol)
            return None

        closes  = hist['Close'].values
        highs   = hist['High'].values
        lows    = hist['Low'].values
        volumes = hist['Volume'].values

        price      = closes[-1]
        prev_close = closes[-2] if len(closes) > 1 else price
        change     = ((price - prev_close) / prev_close) * 100

        # ── Market cap via fast_info (no extra HTTP call) ────────
        fi         = ticker.fast_info
        market_cap = getattr(fi, 'market_cap', None) or 0

        # ── CALL 2a: Annual income statement ─────────────────────
        # Fetched ONCE and reused for: latest_fy_revenue + historical revenues
        annual_inc = None
        try:
            annual_inc = ticker.income_stmt if hasattr(ticker, 'income_stmt') else ticker.financials
        except Exception:
            pass

        # ── CALL 2b: Annual balance sheet ────────────────────────
        # Fetched ONCE and reused for: total_cash + historical cash
        annual_bs = None
        try:
            annual_bs = ticker.balance_sheet
        except Exception:
            pass

        # ── CALL 2c: Quarterly income stmt ───────────────────────
        # Yahoo returns this from the same underlying financials endpoint;
        # yfinance caches it on the Ticker object so no duplicate round-trip.
        q_inc = None
        try:
            q_inc = ticker.quarterly_income_stmt if hasattr(ticker, 'quarterly_income_stmt') else ticker.quarterly_financials
        except Exception:
            pass

        # ── Derive fundamentals from statements (no ticker.info) ─

        # Latest FY revenue
        latest_fy_revenue = 0
        if annual_inc is not None and not annual_inc.empty:
            if 'Total Revenue' in annual_inc.index:
                v = annual_inc.loc['Total Revenue'].iloc[0]
                latest_fy_revenue = 0 if pd.isna(v) else v

        # Total cash from most recent balance sheet column
        total_cash = 0
        if annual_bs is not None and not annual_bs.empty:
            for cash_key in ('Cash And Cash Equivalents',
                             'Cash Cash Equivalents And Short Term Investments',
                             'Cash And Short Term Investments'):
                if cash_key in annual_bs.index:
                    v = annual_bs.loc[cash_key].iloc[0]
                    total_cash = 0 if pd.isna(v) else v
                    break

        # Profit margin from latest annual income stmt
        profit_margin = None
        if annual_inc is not None and not annual_inc.empty:
            try:
                rev = annual_inc.loc['Total Revenue'].iloc[0] if 'Total Revenue' in annual_inc.index else None
                net = annual_inc.loc['Net Income'].iloc[0]     if 'Net Income'    in annual_inc.index else None
                if rev and net and not pd.isna(rev) and not pd.isna(net) and rev != 0:
                    profit_margin = net / rev   # expressed as fraction, consistent with old ticker.info value
            except Exception:
                pass

        # PE ratio from fast_info (no extra call)
        pe_ratio       = getattr(fi, 'p_e_ratio', None)
        # roe / debt_to_equity not available without ticker.info — set None (not used in scoring)
        revenue_growth = None
        earnings_growth= None
        roe            = None
        debt_to_equity = None

        # QoQ / YoY growth from quarterly income stmt
        qoq_revenue_growth = yoy_revenue_growth = None
        qoq_profit_growth  = yoy_profit_growth  = None

        if q_inc is not None and not q_inc.empty:
            if 'Total Revenue' in q_inc.index:
                revenues = [r for r in q_inc.loc['Total Revenue'].values if not pd.isna(r)]
                if len(revenues) >= 2:
                    qoq_revenue_growth = ((revenues[0] - revenues[1]) / abs(revenues[1])) * 100 if revenues[1] != 0 else None
                if len(revenues) >= 4:
                    yoy_revenue_growth = ((revenues[0] - revenues[3]) / abs(revenues[3])) * 100 if revenues[3] != 0 else None

            if 'Net Income' in q_inc.index:
                profits = [p for p in q_inc.loc['Net Income'].values if not pd.isna(p)]
                if len(profits) >= 2:
                    qoq_profit_growth = ((profits[0] - profits[1]) / abs(profits[1])) * 100 if profits[1] != 0 else None
                if len(profits) >= 4:
                    yoy_profit_growth = ((profits[0] - profits[3]) / abs(profits[3])) * 100 if profits[3] != 0 else None

        # ── Ratios ───────────────────────────────────────────────
        cash_on_hand_to_mcap      = (total_cash / market_cap * 100) if market_cap > 0 and total_cash > 0 else 0
        latest_fy_revenue_to_mcap = (latest_fy_revenue / market_cap) if market_cap > 0 and latest_fy_revenue > 0 else 0

        # ── Historical financials (reuses annual_inc + annual_bs already fetched) ──
        historical_data = get_historical_financials_from_data(annual_inc, annual_bs, market_cap)

        # ── Technicals (pure numpy, no HTTP) ─────────────────────
        fii_dii_activity = detect_institutional_activity(volumes, closes)
        rsi        = calculate_rsi(closes)
        macd       = calculate_macd(closes)
        bb_position= calculate_bb_position(closes)
        vol_multiple= calculate_volume_multiple(volumes)
        trend      = detect_trend(closes)

        weekly_change      = ((closes[-1] - closes[-5])  / closes[-5])  * 100 if len(closes) >= 5  and closes[-5]  != 0 else 0
        monthly_change     = ((closes[-1] - closes[-20]) / closes[-20]) * 100 if len(closes) >= 20 and closes[-20] != 0 else 0
        three_month_change = ((closes[-1] - closes[0])   / closes[0])   * 100 if len(closes) >= 5  and closes[0]   != 0 else 0

        result = {
            'symbol': symbol,
            'price': price,
            'change': change,
            'weekly_change': weekly_change,
            'monthly_change': monthly_change,
            'three_month_change': three_month_change,
            'rsi': rsi,
            'macd': macd,
            'bb_position': bb_position,
            'vol_multiple': vol_multiple,
            'trend': trend,
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'fii_dii_score': fii_dii_activity,
            'market_cap': market_cap,
            'revenue_growth': revenue_growth,
            'profit_margin': profit_margin,
            'earnings_growth': earnings_growth,
            'pe_ratio': pe_ratio,
            'roe': roe,
            'debt_to_equity': debt_to_equity,
            'total_cash': total_cash,
            'latest_fy_revenue': latest_fy_revenue,
            'cash_on_hand_to_mcap': cash_on_hand_to_mcap,
            'latest_fy_revenue_to_mcap': latest_fy_revenue_to_mcap,
            'historical_data': historical_data,
            'qoq_revenue_growth': qoq_revenue_growth,
            'yoy_revenue_growth': yoy_revenue_growth,
            'qoq_profit_growth': qoq_profit_growth,
            'yoy_profit_growth': yoy_profit_growth
        }
        cache_set_stock(symbol, to_jsonable(result))
        return result

    except Exception as e:
        if any(kw in str(e).lower() for kw in ("delisted", "not found", "no data found")):
            _mark_dead_symbol(symbol)
        return None


def get_historical_financials_from_data(annual_inc, annual_bs, current_mcap):
    """Build 3-year historical trends from already-fetched DataFrames.
    Zero extra HTTP calls — data comes from fetch_stock_data's two calls.
    """
    historical = {'years': [], 'revenues': [], 'cash_amounts': [], 'sales_to_mcap': []}
    try:
        if annual_inc is None or annual_inc.empty:
            return historical

        years = list(annual_inc.columns[:3]) if len(annual_inc.columns) >= 3 else list(annual_inc.columns)

        for year in years:
            year_str = year.strftime('%Y') if hasattr(year, 'strftime') else str(year)
            historical['years'].append(year_str)

            # Revenue
            if 'Total Revenue' in annual_inc.index:
                v = annual_inc.loc['Total Revenue', year]
                historical['revenues'].append(0 if pd.isna(v) else v)
            else:
                historical['revenues'].append(0)

            # Cash
            cash = 0
            if annual_bs is not None and not annual_bs.empty and year in annual_bs.columns:
                for cash_key in ('Cash And Cash Equivalents',
                                 'Cash Cash Equivalents And Short Term Investments',
                                 'Cash And Short Term Investments'):
                    if cash_key in annual_bs.index:
                        v = annual_bs.loc[cash_key, year]
                        cash = 0 if pd.isna(v) else v
                        break
            historical['cash_amounts'].append(cash)

        # Sales / MCap ratio
        for revenue in historical['revenues']:
            historical['sales_to_mcap'].append(
                revenue / current_mcap if current_mcap > 0 and revenue > 0 else 0
            )

    except Exception:
        pass

    return historical


def fetch_live_price(symbol):
    """Fetch only live price for auto-refresh (non-cached)

    Symbol already has .NS or .BO suffix from file loading
    """
    try:
        # Symbol already has exchange suffix (e.g., "RELIANCE.NS" or "TCS.BO")
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
        if data is not None and not data.empty:
            return data['Close'].iloc[-1]
        return None
    except:
        return None


def detect_institutional_activity(volumes, closes):
    """Detect FII/DII activity patterns from volume and price action"""
    try:
        if len(volumes) < 20 or len(closes) < 20:
            return 0

        score = 0
        recent_days = 10

        for i in range(-recent_days, 0):
            # BULLETPROOF: Safe array access and division
            if i >= -len(volumes) and i >= -len(closes):
                vol_ratio = volumes[i] / np.mean(volumes[-60:]) if len(volumes) >= 60 else volumes[i] / np.mean(volumes)
                if vol_ratio == 0 or np.isnan(vol_ratio):
                    continue

                if i > -len(closes) and closes[i-1] != 0:
                    price_change = ((closes[i] - closes[i-1]) / closes[i-1]) * 100
                else:
                    price_change = 0

                if vol_ratio > 1.5 and price_change > 1:
                    score += 2
                elif vol_ratio > 1.2 and price_change > 0.5:
                    score += 1
                elif vol_ratio > 1.5 and price_change < -1:
                    score -= 2
                elif vol_ratio > 1.2 and price_change < -0.5:
                    score -= 1

        return score
    except:
        return 0


def calculate_rsi(prices, period=14):
    try:
        if len(prices) < period + 1:
            return 50
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    except:
        return 50


def calculate_macd(prices):
    try:
        if len(prices) < 26:
            return 0
        ema12 = calculate_ema(prices, 12)
        ema26 = calculate_ema(prices, 26)
        return ema12 - ema26
    except:
        return 0


def calculate_ema(prices, period):
    try:
        multiplier = 2 / (period + 1)
        ema = np.mean(prices[:period])
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema
    except:
        return 0


def calculate_bb_position(prices, period=20):
    try:
        if len(prices) < period:
            return 50
        recent = prices[-period:]
        sma = np.mean(recent)
        std = np.std(recent)
        upper = sma + (2 * std)
        lower = sma - (2 * std)
        current = prices[-1]
        if upper == lower:
            return 50
        position = ((current - lower) / (upper - lower)) * 100
        return max(0, min(100, position))
    except:
        return 50


def calculate_volume_multiple(volumes):
    try:
        if len(volumes) < 20:
            return 1.0
        current = volumes[-1]
        avg20 = np.mean(volumes[-20:])
        if avg20 == 0:
            return 1.0
        return current / avg20
    except:
        return 1.0


def detect_operator_activity(data):
    """Detect if stock shows signs of operator/manipulator activity"""
    try:
        closes = data['closes']
        volumes = data['volumes']
        highs = data['highs']
        lows = data['lows']

        warning_flags = []
        risk_score = 0

        if len(closes) < 20:
            return False, [], 0

        # 1. EXTREME VOLUME SPIKES - BULLETPROOF: Safe operations
        recent_vols = volumes[-10:]
        avg_vol = np.mean(volumes[-60:]) if len(volumes) >= 60 else np.mean(volumes)
        if avg_vol == 0:
            return False, [], 0

        max_recent_vol = np.max(recent_vols)

        if max_recent_vol > avg_vol * 5:
            warning_flags.append("🚨 EXTREME volume spike (>5x avg) - Possible pump")
            risk_score += 30
        elif max_recent_vol > avg_vol * 3:
            warning_flags.append("⚠️ High volume spike (>3x avg) - Monitor closely")
            risk_score += 15

        # 2. PRICE VOLATILITY
        recent_prices = closes[-10:]
        price_swings = []
        for i in range(1, len(recent_prices)):
            if recent_prices[i-1] != 0:
                swing = abs((recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1]) * 100
                price_swings.append(swing)

        avg_swing = np.mean(price_swings) if price_swings else 0
        max_swing = np.max(price_swings) if price_swings else 0

        if max_swing > 8 and avg_swing > 3:
            warning_flags.append("🚨 Extreme volatility (>8% swings) - Operator activity likely")
            risk_score += 25
        elif max_swing > 5 and avg_swing > 2:
            warning_flags.append("⚠️ High volatility - Possible manipulation")
            risk_score += 12

        # 3. CIRCUIT FILTER HITS
        circuit_hits = 0
        for i in range(-20, 0):
            if i >= -len(closes) and i > -len(closes) and closes[i-1] != 0:
                daily_change = abs((closes[i] - closes[i-1]) / closes[i-1]) * 100
                if daily_change > 9:
                    circuit_hits += 1

        if circuit_hits >= 3:
            warning_flags.append("🚨 Multiple circuit hits - Highly manipulated")
            risk_score += 30
        elif circuit_hits >= 2:
            warning_flags.append("⚠️ Circuit hits detected - High risk")
            risk_score += 15

        is_operated = risk_score >= 40

        return is_operated, warning_flags, risk_score
    except:
        return False, [], 0


def detect_trend(prices):
    try:
        if len(prices) < 5:
            return 'Sideways'
        recent = prices[-5:]
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
        if ups >= 4:
            return 'Strong Uptrend'
        elif ups >= 3:
            return 'Uptrend'
        elif ups <= 1:
            return 'Downtrend'
        else:
            return 'Sideways'
    except:
        return 'Sideways'


def analyze_stock(data, min_market_cap, thresholds=None):
    """Analyze stock with ULTRA-STRICT fundamentals criteria

    Args:
        data: Stock data dictionary
        min_market_cap: Minimum market cap filter
        thresholds: Dictionary of adjustable thresholds (optional)
    """
    try:
        if not data:
            return None

        # Default thresholds if not provided
        if thresholds is None:
            thresholds = {
                'threshold_exceptional': 180,
                'threshold_prime': 160,
                'threshold_excellent': 140,
                'threshold_strong': 120,
                'rsi_low': 32,
                'rsi_high': 38,
                'min_revenue_yoy': 20,
                'min_profit_yoy': 25
            }

        price = data['price']
        change = data['change']
        rsi = data['rsi']
        macd = data['macd']
        bb = data['bb_position']
        vol = data['vol_multiple']
        trend = data['trend']
        closes = data['closes']

        # Market cap filter (in crores) - BULLETPROOF: Safe division
        market_cap = data['market_cap'] / 10000000 if data['market_cap'] else 0

        # Skip if below minimum market cap
        if market_cap < min_market_cap:
            return None

        # OPERATOR DETECTION
        is_operated, operator_flags, operator_risk = detect_operator_activity(data)

        # Calculate additional indicators - BULLETPROOF: Safe division
        weekly_change = ((closes[-1] - closes[-5]) / closes[-5]) * 100 if len(closes) >= 5 and closes[-5] != 0 else 0
        monthly_change = ((closes[-1] - closes[-20]) / closes[-20]) * 100 if len(closes) >= 20 and closes[-20] != 0 else 0
        three_month_change = ((closes[-1] - closes[0]) / closes[0]) * 100 if len(closes) >= 5 and closes[0] != 0 else 0

        potential_rs = max(20, price * 0.10)
        potential_pct = (potential_rs / price) * 100 if price != 0 else 0

        score = 0
        criteria = []

        # CRITICAL: Operator penalty
        if is_operated:
            score -= 70
            criteria.append(f'🚨 OPERATOR DETECTED: Risk Score {operator_risk}/100 - AVOID [-70 pts]')
        elif operator_risk >= 30:
            score -= 40
            criteria.append(f'🚨 VERY HIGH RISK: Major manipulation signs (Risk: {operator_risk}/100) [-40 pts]')
        elif operator_risk >= 20:
            score -= 25
            criteria.append(f'⚠️ HIGH RISK: Manipulation signs detected (Risk: {operator_risk}/100) [-25 pts]')
        elif operator_risk >= 12:
            score -= 12
            criteria.append(f'⚠️ CAUTION: Some manipulation indicators (Risk: {operator_risk}/100) [-12 pts]')

        # 1. MARKET CAP QUALITY (15 pts) - NEW!
        if market_cap >= 50000:
            score += 15
            criteria.append(f'✅ Market Cap: Large Cap (₹{market_cap:.0f} Cr) [15 pts]')
        elif market_cap >= 20000:
            score += 12
            criteria.append(f'✅ Market Cap: Mid-Large Cap (₹{market_cap:.0f} Cr) [12 pts]')
        elif market_cap >= 10000:
            score += 10
            criteria.append(f'✅ Market Cap: Mid Cap (₹{market_cap:.0f} Cr) [10 pts]')
        elif market_cap >= 5000:
            score += 7
            criteria.append(f'⚠ Market Cap: Small-Mid Cap (₹{market_cap:.0f} Cr) [7 pts]')
        else:
            criteria.append(f'❌ Market Cap: Small Cap (₹{market_cap:.0f} Cr) [0 pts]')

        # 2. REVENUE GROWTH (25 pts) - NEW! STRICTEST CRITERIA
        yoy_rev = data['yoy_revenue_growth']
        qoq_rev = data['qoq_revenue_growth']

        if yoy_rev is not None and qoq_rev is not None:
            if yoy_rev >= 25 and qoq_rev >= 15:
                score += 25
                criteria.append(f'✅ Revenue: EXCEPTIONAL Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [25 pts]')
            elif yoy_rev >= 20 and qoq_rev >= 10:
                score += 22
                criteria.append(f'✅ Revenue: Excellent Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [22 pts]')
            elif yoy_rev >= 15 and qoq_rev >= 8:
                score += 18
                criteria.append(f'✅ Revenue: Strong Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [18 pts]')
            elif yoy_rev >= 10 and qoq_rev >= 5:
                score += 12
                criteria.append(f'⚠ Revenue: Good Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [12 pts]')
            elif yoy_rev >= 5:
                score += 5
                criteria.append(f'⚠ Revenue: Moderate Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [5 pts]')
            else:
                criteria.append(f'❌ Revenue: Weak/Negative Growth (YoY: {yoy_rev:.1f}%, QoQ: {qoq_rev:.1f}%) [0 pts]')
        elif yoy_rev is not None:
            if yoy_rev >= 20:
                score += 20
                criteria.append(f'✅ Revenue: Strong YoY Growth ({yoy_rev:.1f}%) [20 pts]')
            elif yoy_rev >= 12:
                score += 15
                criteria.append(f'✅ Revenue: Good YoY Growth ({yoy_rev:.1f}%) [15 pts]')
            elif yoy_rev >= 5:
                score += 8
                criteria.append(f'⚠ Revenue: Moderate Growth ({yoy_rev:.1f}%) [8 pts]')
            else:
                criteria.append(f'❌ Revenue: Weak Growth ({yoy_rev:.1f}%) [0 pts]')
        else:
            criteria.append(f'❌ Revenue: Data not available [0 pts]')

        # 3. PROFIT GROWTH (25 pts) - NEW! STRICTEST CRITERIA
        yoy_profit = data['yoy_profit_growth']
        qoq_profit = data['qoq_profit_growth']
        profit_margin = data['profit_margin']

        if yoy_profit is not None and qoq_profit is not None:
            if yoy_profit >= 30 and qoq_profit >= 20:
                score += 25
                criteria.append(f'✅ Profit: EXCEPTIONAL Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [25 pts]')
            elif yoy_profit >= 25 and qoq_profit >= 15:
                score += 22
                criteria.append(f'✅ Profit: Excellent Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [22 pts]')
            elif yoy_profit >= 20 and qoq_profit >= 10:
                score += 18
                criteria.append(f'✅ Profit: Strong Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [18 pts]')
            elif yoy_profit >= 12 and qoq_profit >= 6:
                score += 12
                criteria.append(f'⚠ Profit: Good Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [12 pts]')
            elif yoy_profit >= 5:
                score += 5
                criteria.append(f'⚠ Profit: Moderate Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [5 pts]')
            else:
                criteria.append(f'❌ Profit: Weak/Negative Growth (YoY: {yoy_profit:.1f}%, QoQ: {qoq_profit:.1f}%) [0 pts]')
        elif yoy_profit is not None:
            if yoy_profit >= 25:
                score += 20
                criteria.append(f'✅ Profit: Strong YoY Growth ({yoy_profit:.1f}%) [20 pts]')
            elif yoy_profit >= 15:
                score += 15
                criteria.append(f'✅ Profit: Good YoY Growth ({yoy_profit:.1f}%) [15 pts]')
            elif yoy_profit >= 8:
                score += 8
                criteria.append(f'⚠ Profit: Moderate Growth ({yoy_profit:.1f}%) [8 pts]')
            else:
                criteria.append(f'❌ Profit: Weak Growth ({yoy_profit:.1f}%) [0 pts]')
        else:
            criteria.append(f'❌ Profit: Data not available [0 pts]')

        # 4. PROFIT MARGIN (15 pts) - NEW!
        if profit_margin is not None:
            profit_margin_pct = profit_margin * 100
            if profit_margin_pct >= 20:
                score += 15
                criteria.append(f'✅ Profit Margin: Excellent ({profit_margin_pct:.1f}%) [15 pts]')
            elif profit_margin_pct >= 15:
                score += 12
                criteria.append(f'✅ Profit Margin: Very Good ({profit_margin_pct:.1f}%) [12 pts]')
            elif profit_margin_pct >= 10:
                score += 10
                criteria.append(f'✅ Profit Margin: Good ({profit_margin_pct:.1f}%) [10 pts]')
            elif profit_margin_pct >= 5:
                score += 5
                criteria.append(f'⚠ Profit Margin: Average ({profit_margin_pct:.1f}%) [5 pts]')
            else:
                criteria.append(f'❌ Profit Margin: Low ({profit_margin_pct:.1f}%) [0 pts]')
        else:
            criteria.append(f'❌ Profit Margin: Data not available [0 pts]')

        # 5. FII/DII ACTIVITY (20 pts)
        fii_score = data['fii_dii_score']
        if fii_score >= 15:
            score += 20
            criteria.append(f'✅ FII/DII: Strong Buying ({fii_score}) [20 pts]')
        elif fii_score >= 10:
            score += 15
            criteria.append(f'✅ FII/DII: Good Buying ({fii_score}) [15 pts]')
        elif fii_score >= 5:
            score += 10
            criteria.append(f'✅ FII/DII: Accumulation ({fii_score}) [10 pts]')
        elif fii_score >= 0:
            score += 5
            criteria.append(f'⚠ FII/DII: Neutral ({fii_score}) [5 pts]')
        else:
            criteria.append(f'❌ FII/DII: Selling ({fii_score}) [0 pts]')

        # 6. CONSOLIDATION (20 pts)
        if -2 <= weekly_change <= 0.3:
            score += 20
            criteria.append(f'✅ Consolidation: Perfect base ({weekly_change:+.1f}% weekly) [20 pts]')
        elif -3.5 <= weekly_change < -2:
            score += 18
            criteria.append(f'✅ Consolidation: Healthy pullback ({weekly_change:+.1f}% weekly) [18 pts]')
        elif 0.3 < weekly_change <= 1.5:
            score += 15
            criteria.append(f'✅ Consolidation: Early breakout ({weekly_change:+.1f}% weekly) [15 pts]')
        elif weekly_change > 4:
            criteria.append(f'❌ Already rallied ({weekly_change:+.1f}% weekly) [0 pts]')
        else:
            score += 5
            criteria.append(f'⚠ Consolidation: Weak ({weekly_change:+.1f}% weekly) [5 pts]')

        # 7. RSI (20 pts)
        rsi_low = thresholds['rsi_low']
        rsi_high = thresholds['rsi_high']

        if rsi_low <= rsi <= rsi_high:
            score += 20
            criteria.append(f'✅ RSI: Perfect oversold entry ({rsi:.0f}) [20 pts]')
        elif rsi_high < rsi <= rsi_high + 7:
            score += 17
            criteria.append(f'✅ RSI: Building momentum ({rsi:.0f}) [17 pts]')
        elif rsi_high + 7 < rsi <= rsi_high + 12:
            score += 12
            criteria.append(f'✅ RSI: Early momentum ({rsi:.0f}) [12 pts]')
        elif rsi_high + 12 < rsi <= rsi_high + 17:
            score += 8
            criteria.append(f'⚠ RSI: Neutral ({rsi:.0f}) [8 pts]')
        elif rsi > rsi_high + 24:
            criteria.append(f'❌ RSI: Overbought ({rsi:.0f}) [0 pts]')
        else:
            score += 5
            criteria.append(f'⚠ RSI: Moderate ({rsi:.0f}) [5 pts]')

        # 8. MACD (15 pts)
        if -1 <= macd <= 1:
            score += 15
            criteria.append(f'✅ MACD: Perfect crossover ({macd:.1f}) [15 pts]')
        elif 1 < macd <= 3:
            score += 12
            criteria.append(f'✅ MACD: Early bullish ({macd:.1f}) [12 pts]')
        elif -3 <= macd < -1:
            score += 10
            criteria.append(f'✅ MACD: About to turn ({macd:.1f}) [10 pts]')
        elif macd > 6:
            criteria.append(f'❌ MACD: Extended ({macd:.1f}) [0 pts]')
        else:
            score += 5
            criteria.append(f'⚠ MACD: Weak ({macd:.1f}) [5 pts]')

        # 9. BOLLINGER BANDS (15 pts)
        if 8 <= bb <= 20:
            score += 15
            criteria.append(f'✅ BB: Lower band bounce ({bb:.0f}%) [15 pts]')
        elif 20 < bb <= 30:
            score += 12
            criteria.append(f'✅ BB: Below middle ({bb:.0f}%) [12 pts]')
        elif 30 < bb <= 45:
            score += 8
            criteria.append(f'⚠ BB: Middle zone ({bb:.0f}%) [8 pts]')
        elif bb > 65:
            criteria.append(f'❌ BB: Upper band ({bb:.0f}%) [0 pts]')
        else:
            score += 5
            criteria.append(f'⚠ BB: Neutral ({bb:.0f}%) [5 pts]')

        # 10. VOLUME (15 pts)
        if 1.3 <= vol <= 1.8:
            score += 15
            criteria.append(f'✅ Volume: Perfect accumulation ({vol:.1f}x) [15 pts]')
        elif 1.8 < vol <= 2.2:
            score += 12
            criteria.append(f'✅ Volume: Building interest ({vol:.1f}x) [12 pts]')
        elif vol > 2.8:
            score += 5
            criteria.append(f'⚠ Volume: Too high ({vol:.1f}x) [5 pts]')
        elif 1.0 <= vol < 1.3:
            score += 7
            criteria.append(f'⚠ Volume: Average ({vol:.1f}x) [7 pts]')
        else:
            criteria.append(f'❌ Volume: Too low ({vol:.1f}x) [0 pts]')

        # 11. TODAY'S PRICE (10 pts)
        if -1.5 <= change <= 0.3:
            score += 10
            criteria.append(f'✅ Today: Perfect entry ({change:+.1f}%) [10 pts]')
        elif 0.3 < change <= 1.2:
            score += 8
            criteria.append(f'✅ Today: Early move ({change:+.1f}%) [8 pts]')
        elif -2.5 <= change < -1.5:
            score += 7
            criteria.append(f'⚠ Today: Dip ({change:+.1f}%) [7 pts]')
        elif change > 2.5:
            criteria.append(f'❌ Today: Already rallied ({change:+.1f}%) [0 pts]')
        else:
            score += 4
            criteria.append(f'⚠ Today: Moderate ({change:+.1f}%) [4 pts]')

        # 12. MONTHLY TREND (10 pts)
        if -8 <= monthly_change <= -2:
            score += 10
            criteria.append(f'✅ Monthly: Recovering from dip ({monthly_change:+.1f}%) [10 pts]')
        elif -2 < monthly_change <= 2:
            score += 8
            criteria.append(f'✅ Monthly: Base building ({monthly_change:+.1f}%) [8 pts]')
        elif 2 < monthly_change <= 6:
            score += 5
            criteria.append(f'⚠ Monthly: Moderate gain ({monthly_change:+.1f}%) [5 pts]')
        elif monthly_change > 10:
            criteria.append(f'❌ Monthly: Extended ({monthly_change:+.1f}%) [0 pts]')
        else:
            score += 3
            criteria.append(f'⚠ Monthly: Weak ({monthly_change:+.1f}%) [3 pts]')

        # 13. 3-MONTH PERFORMANCE (10 pts)
        if -15 <= three_month_change <= -5:
            score += 10
            criteria.append(f'✅ 3-Month: Perfect correction ({three_month_change:+.1f}%) [10 pts]')
        elif -5 < three_month_change <= 5:
            score += 8
            criteria.append(f'✅ 3-Month: Sideways base ({three_month_change:+.1f}%) [8 pts]')
        elif 5 < three_month_change <= 15:
            score += 5
            criteria.append(f'⚠ 3-Month: Moderate rise ({three_month_change:+.1f}%) [5 pts]')
        elif three_month_change > 25:
            criteria.append(f'❌ 3-Month: Overextended ({three_month_change:+.1f}%) [0 pts]')
        else:
            score += 3
            criteria.append(f'⚠ 3-Month: Weak ({three_month_change:+.1f}%) [3 pts]')

        # 14. UPSIDE POTENTIAL (10 pts)
        if potential_pct >= 12:
            score += 10
            criteria.append(f'✅ Upside: Excellent ({potential_pct:.1f}%) [10 pts]')
        elif potential_pct >= 10:
            score += 8
            criteria.append(f'✅ Upside: Very Good ({potential_pct:.1f}%) [8 pts]')
        elif potential_pct >= 8:
            score += 5
            criteria.append(f'⚠ Upside: Good ({potential_pct:.1f}%) [5 pts]')
        else:
            criteria.append(f'❌ Upside: Low ({potential_pct:.1f}%) [0 pts]')

        # Rating based on ULTRA-STRICT criteria with ADJUSTABLE thresholds
        threshold_exceptional = thresholds['threshold_exceptional']
        threshold_prime = thresholds['threshold_prime']
        threshold_excellent = thresholds['threshold_excellent']
        threshold_strong = thresholds['threshold_strong']

        if is_operated:
            status = '🚨 OPERATED - AVOID'
            rating = 'Operated - Avoid'
        elif score >= threshold_exceptional:
            status = '🌟 EXCEPTIONAL BUY'
            rating = 'Exceptional Buy'
        elif score >= threshold_prime:
            status = '🚀 PRIME BUY'
            rating = 'Prime Buy'
        elif score >= threshold_excellent:
            status = '💎 EXCELLENT BUY'
            rating = 'Excellent Buy'
        elif score >= threshold_strong:
            status = '✅ STRONG BUY'
            rating = 'Strong Buy'
        elif score >= 100:
            status = '👍 GOOD BUY'
            rating = 'Good Buy'
        elif score >= 80:
            status = '📋 WATCHLIST'
            rating = 'Watchlist'
        else:
            status = '❌ SKIP'
            rating = 'Skip'

        qualified = score >= threshold_excellent and not is_operated
        met_count = len([c for c in criteria if '✅' in c])

        return {
            'symbol': data['symbol'],
            'price': price,
            'change': change,
            'weekly_change': weekly_change,
            'monthly_change': monthly_change,
            'three_month_change': three_month_change,
            'potential_rs': potential_rs,
            'potential_pct': potential_pct,
            'rsi': rsi,
            'macd': macd,
            'bb': bb,
            'vol': vol,
            'trend': trend,
            'score': score,
            'qualified': qualified,
            'status': status,
            'rating': rating,
            'criteria': criteria,
            'met_count': met_count,
            'sector': SECTOR_MAP.get(data['symbol'].replace('.NS', '').replace('.BO', ''), 'Other'),
            'is_operated': is_operated,
            'operator_risk': operator_risk,
            'operator_flags': operator_flags,
            'market_cap': market_cap,
            'yoy_revenue_growth': yoy_rev,
            'qoq_revenue_growth': qoq_rev,
            'yoy_profit_growth': yoy_profit,
            'qoq_profit_growth': qoq_profit,
            'profit_margin': profit_margin * 100 if profit_margin else None,
            'total_cash': data.get('total_cash', 0),
            'latest_fy_revenue': data.get('latest_fy_revenue', 0),
            'cash_on_hand_to_mcap': data.get('cash_on_hand_to_mcap', 0),
            'latest_fy_revenue_to_mcap': data.get('latest_fy_revenue_to_mcap', 0),
            'historical_data': data.get('historical_data', {'years': [], 'revenues': [], 'cash_amounts': [], 'sales_to_mcap': []})
        }
    except Exception as e:
        # Silently return None on error
        return None
