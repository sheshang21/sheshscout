"""
core/intraday_scanner.py — Intraday Long/Short screener logic (framework-free)
================================================================================
Ported from the two standalone Streamlit scripts:
    intraday_long_screener.py   (IntradayLongScreener)
    intraday_short_screener.py  (IntradayShortScreener)

Those two files were near-identical mirror images of each other (BUY
conditions vs SHORT conditions, thresholds sign-flipped) each carrying its
own copy of get_default_stock_list / analyze_stock / calculate_rsi /
calculate_atr and ~450 lines of Streamlit UI. Streamlit is gone here — this
module doesn't know or care what's calling it, same convention as
core/scanner.py — and the fetch/scoring pipeline is unified into one
direction-parameterized module instead of two near-duplicate files.

The LONG and SHORT branches inside analyze_intraday() are kept literally
separate (rather than "unified" with a sign-flip trick) on purpose: that
keeps every condition and every score increment identical, line for line,
to what each original script did — the safest way to port scoring logic
without silently changing anyone's results.

Public surface:
    DEFAULT_PARAMS[direction]                        -> dict
    fetch_intraday_data(symbol)                       -> dict | None
    analyze_intraday(data, symbol, direction, params) -> dict | None
    calculate_rsi(prices, period=14)                  -> float | None
    calculate_atr(df, period=14)                      -> float

Split into fetch_intraday_data() / analyze_intraday() (rather than one
fetch+analyze function like the original scripts had) deliberately mirrors
core/scanner.py's fetch_stock_data() / analyze_stock() split — it's what
lets app/intraday_scan_runner.py tell "couldn't fetch this symbol" (network/
delisted/no data) apart from "fetched fine, just didn't qualify," the same
distinction app/scan_runner.py already relies on for failed_count.
"""
try:
    from .yf_ratelimit import safe_ticker as _rl_ticker   # normal package import
except ImportError:
    from yf_ratelimit import safe_ticker as _rl_ticker    # running standalone

import pandas as pd

DIRECTIONS = ("long", "short")

# Same defaults as each Streamlit script's widget `value=` kwargs, just
# collected into one dict instead of being scattered across st.slider calls.
DEFAULT_PARAMS = {
    "long": {
        "min_volume": 100000,
        "min_price": 20,
        "min_conditions": 4,
        "min_score": 50,
        "price_change_threshold": 0.0,
        "dist_threshold": 2.0,          # dist_from_low_threshold
        "trend_threshold": 2.0,
        "momentum_threshold": 0.5,
        "volume_ratio_threshold": 1.2,
        "rsi_threshold": 35,
        "atr_threshold": 1.0,
        "rsi_period": 14,
        "atr_period": 14,
        "momentum_window": 30,
        "strong_score": 70,
        "stop_loss_pct": 0.5,           # below entry
        "target_pct": 2.0,              # above entry
    },
    "short": {
        "min_volume": 100000,
        "min_price": 20,
        "min_conditions": 4,
        "min_score": 50,
        "price_change_threshold": 0.0,
        "dist_threshold": 2.0,          # dist_from_high_threshold
        "trend_threshold": -2.0,
        "momentum_threshold": -0.5,
        "volume_ratio_threshold": 1.2,
        "rsi_threshold": 65,
        "atr_threshold": 1.0,
        "rsi_period": 14,
        "atr_period": 14,
        "momentum_window": 30,
        "strong_score": 70,
        "stop_loss_pct": 0.5,           # above entry (short: stop is ABOVE)
        "target_pct": 2.0,              # below entry (short: target is BELOW)
    },
}


def calculate_rsi(prices, period=14):
    """Identical to both original scripts' calculate_rsi (pandas rolling-mean
    RSI, not core/scanner.py's numpy-diff variant — intentionally different
    algorithms, keep this one so intraday results don't shift)."""
    try:
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]
    except Exception:
        return None


def calculate_atr(df, period=14):
    """Identical to both original scripts' calculate_atr."""
    try:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean().iloc[-1]
    except Exception:
        return 0


def _merge_params(direction, overrides):
    base = dict(DEFAULT_PARAMS[direction])
    if overrides:
        base.update({k: v for k, v in overrides.items() if k in base})
    return base


def fetch_intraday_data(symbol):
    """symbol: full Yahoo ticker, e.g. 'RELIANCE.NS' / 'RELIANCE.BO'.

    Returns {'intraday': df, 'daily': df} or None if either leg came back
    empty (delisted, no trades yet, market holiday, bad symbol, ...).
    """
    try:
        stock = _rl_ticker(symbol)
        intraday = stock.history(period="1d", interval="1m")
        daily = stock.history(period="5d", interval="1d")
        if intraday is None or daily is None or intraday.empty or daily.empty:
            return None
        return {"intraday": intraday, "daily": daily}
    except Exception:
        return None


def analyze_intraday(data, symbol, direction, params=None):
    """data: dict from fetch_intraday_data(). Returns a result dict, or None
    if the symbol doesn't clear min_price/min_volume, doesn't meet
    min_conditions, or doesn't meet min_score — same "not worth storing"
    semantics as core/scanner.py's analyze_stock()."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    p = _merge_params(direction, params)

    try:
        intraday = data["intraday"]
        daily = data["daily"]

        current_price = float(intraday["Close"].iloc[-1])
        open_price = float(intraday["Open"].iloc[0])
        high_price = float(intraday["High"].max())
        low_price = float(intraday["Low"].min())
        volume = float(intraday["Volume"].sum())

        if current_price < p["min_price"] or volume < p["min_volume"]:
            return None

        price_change_pct = ((current_price - open_price) / open_price) * 100

        if len(daily) >= 2:
            recent_change = ((daily["Close"].iloc[-1] - daily["Close"].iloc[0]) / daily["Close"].iloc[0]) * 100
        else:
            recent_change = 0

        window = p["momentum_window"]
        if len(intraday) >= window * 2:
            last_n = intraday["Close"].iloc[-window:].mean()
            prev_n = intraday["Close"].iloc[-window * 2:-window].mean()
            momentum_change = ((last_n - prev_n) / prev_n) * 100
        else:
            momentum_change = 0

        avg_volume_5d = daily["Volume"].mean()
        volume_ratio = volume / avg_volume_5d if avg_volume_5d > 0 else 0

        rsi = calculate_rsi(intraday["Close"], period=p["rsi_period"])
        atr = calculate_atr(intraday, period=p["atr_period"])
        atr_pct = (atr / current_price) * 100

        conditions_met = []
        score = 0
        result = {}

        if direction == "long":
            dist_from_low = ((current_price - low_price) / low_price) * 100

            # ---- BUY conditions (matches IntradayLongScreener.analyze_stock) ----
            if price_change_pct > p["price_change_threshold"]:
                conditions_met.append("Up from open")
            elif price_change_pct >= -0.5:
                conditions_met.append("Flat/recovering")

            if dist_from_low < p["dist_threshold"]:
                conditions_met.append("Near day low / bounce zone")

            if recent_change > p["trend_threshold"]:
                conditions_met.append("5-day uptrend")

            if momentum_change > p["momentum_threshold"]:
                conditions_met.append("Positive momentum")

            if volume_ratio > p["volume_ratio_threshold"]:
                conditions_met.append("High volume")

            if rsi and rsi < p["rsi_threshold"]:
                conditions_met.append("RSI oversold")

            if atr_pct > p["atr_threshold"]:
                conditions_met.append("Good volatility")

            if len(conditions_met) < p["min_conditions"]:
                return None

            if price_change_pct > 2:
                score += 30
            elif price_change_pct > 1:
                score += 20
            elif price_change_pct > 0:
                score += 10

            if dist_from_low < 1:
                score += 20
            elif dist_from_low < 2:
                score += 10

            if recent_change > 5:
                score += 20
            elif recent_change > 2:
                score += 10

            if momentum_change > 1:
                score += 15
            elif momentum_change > 0.5:
                score += 8

            if volume_ratio > 1.5:
                score += 10
            elif volume_ratio > 1.2:
                score += 5

            if rsi and rsi < 30:
                score += 5
            elif rsi and rsi < 35:
                score += 3

            result["dist_from_low"] = dist_from_low

        else:  # short
            dist_from_high = ((high_price - current_price) / high_price) * 100

            # ---- SHORT conditions (matches IntradayShortScreener.analyze_stock) ----
            if price_change_pct < p["price_change_threshold"]:
                conditions_met.append("Down from open")
            elif price_change_pct < 0.5:
                conditions_met.append("Flat/weak")

            if dist_from_high < p["dist_threshold"]:
                conditions_met.append("Near day high")

            if recent_change < p["trend_threshold"]:
                conditions_met.append("5-day downtrend")

            if momentum_change < p["momentum_threshold"]:
                conditions_met.append("Negative momentum")

            if volume_ratio > p["volume_ratio_threshold"]:
                conditions_met.append("High volume")

            if rsi and rsi > p["rsi_threshold"]:
                conditions_met.append("RSI overbought")

            if atr_pct > p["atr_threshold"]:
                conditions_met.append("Good volatility")

            if len(conditions_met) < p["min_conditions"]:
                return None

            if price_change_pct < -2:
                score += 30
            elif price_change_pct < -1:
                score += 20
            elif price_change_pct < 0:
                score += 10

            if dist_from_high < 1:
                score += 20
            elif dist_from_high < 2:
                score += 10

            if recent_change < -5:
                score += 20
            elif recent_change < -2:
                score += 10

            if momentum_change < -1:
                score += 15
            elif momentum_change < -0.5:
                score += 8

            if volume_ratio > 1.5:
                score += 10
            elif volume_ratio > 1.2:
                score += 5

            if rsi and rsi > 70:
                score += 5
            elif rsi and rsi > 65:
                score += 3

            result["dist_from_high"] = dist_from_high

        # Original scripts applied this cut in the scan loop
        # (`if result['score'] >= screener.min_score`), not inside
        # analyze_stock() — enforced here instead so a non-qualifying
        # result is never written to scan_results at all, matching
        # core/scanner.py's analyze_stock() "return None = don't store" rule.
        if score < p["min_score"]:
            return None

        signal_strength = "STRONG" if score >= p["strong_score"] else "MODERATE" if score >= 50 else "WEAK"

        result.update({
            "symbol": symbol,
            "ticker": symbol.split(".")[0],
            "direction": direction,
            "price": current_price,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "change_pct": price_change_pct,
            "volume": volume,
            "volume_ratio": volume_ratio,
            "recent_trend": recent_change,
            "momentum": momentum_change,
            "rsi": rsi if rsi else 0,
            "atr_pct": atr_pct,
            "score": score,
            "conditions": ", ".join(conditions_met),
            "signal_strength": signal_strength,
            "stop_loss": current_price * (1 - p["stop_loss_pct"] / 100) if direction == "long"
                         else current_price * (1 + p["stop_loss_pct"] / 100),
            "target": current_price * (1 + p["target_pct"] / 100) if direction == "long"
                      else current_price * (1 - p["target_pct"] / 100),
        })
        return result

    except Exception:
        return None
