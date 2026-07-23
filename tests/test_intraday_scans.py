"""
tests/test_intraday_scans.py — exercises the intraday-scans API end-to-end
against real Postgres + Redis, with core.intraday_scanner.fetch_intraday_data
mocked out (no real Yahoo Finance calls -- same reasoning as
tests/test_scans.py, which this mirrors 1:1 so the two pipelines get the
same coverage: create/run, resume, history, ownership, SSE).

    export DATABASE_URL=postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout
    export REDIS_URL=redis://localhost:6379/0
    export COOKIE_SECURE=false
    export CELERY_ALWAYS_EAGER=true
    python -m alembic upgrade head
    pytest tests/test_intraday_scans.py -v
"""
import os
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("CELERY_ALWAYS_EAGER", "true")

from app.main import app  # noqa: E402
from core.redis_client import get_redis  # noqa: E402
from db.session import SessionLocal  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    db = SessionLocal()
    db.execute(text("TRUNCATE users, scan_jobs, scan_results, dead_symbols CASCADE"))
    db.commit()
    db.close()
    get_redis().flushdb()
    yield


def _bullish_ohlc():
    """Synthetic 1m/5d OHLCV that clears every LONG condition (up from
    open, near day low is irrelevant once it's rallied, 5-day uptrend,
    positive momentum, high volume, RSI oversold-ish, decent ATR) so
    analyze_intraday(..., 'long') scores comfortably above min_score."""
    idx = pd.date_range("2026-07-22 09:15", periods=120, freq="1min")
    close = [100.0] * 30 + list(pd.Series(range(90)).apply(lambda i: 100 + i * 0.15))
    open_ = [c - 0.05 for c in close]
    high = [c + 0.1 for c in close]
    low = [c - 0.3 for c in close]
    volume = [50_000] * 120
    intraday = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)

    didx = pd.date_range("2026-07-17", periods=5, freq="1D")
    dclose = [90, 93, 96, 98, 100]
    daily = pd.DataFrame(
        {"Open": dclose, "High": [c + 1 for c in dclose], "Low": [c - 1 for c in dclose],
         "Close": dclose, "Volume": [400_000] * 5},
        index=didx,
    )
    return {"intraday": intraday, "daily": daily}


def _fake_fetch_intraday(symbol):
    if symbol.startswith("DEAD"):
        return None
    return _bullish_ohlc()


def _signed_in_client(email="intradaytest@example.com"):
    client = TestClient(app)
    client.post("/auth/signup", json={"email": email, "password": "correct-horse-battery"})
    return client


def test_start_intraday_scan_runs_and_stores_results():
    client = _signed_in_client()
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        r = client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1", "FAKE2", "DEAD"]})
        assert r.status_code == 201
        job_id = r.json()["id"]
        assert r.json()["scan_type"] == "intraday_long"

    status = client.get(f"/intraday-scans/{job_id}").json()
    assert status["status"] == "completed"
    assert status["scanned_count"] == 3
    assert status["failed_count"] == 1  # DEAD.NS

    results = client.get(f"/intraday-scans/{job_id}/results").json()
    assert {r["symbol"] for r in results} == {"FAKE1.NS", "FAKE2.NS"}
    for row in results:
        assert row["sector"] == "long"  # repurposed to hold direction
        assert row["rating"] in ("STRONG", "MODERATE", "WEAK")


def test_short_direction_uses_short_scan_type_and_symbol_normalization():
    client = _signed_in_client()
    # bearish mirror of _bullish_ohlc via a simple negation-friendly fixture
    idx = pd.date_range("2026-07-22 09:15", periods=120, freq="1min")
    close = [100.0] * 30 + list(pd.Series(range(90)).apply(lambda i: 100 - i * 0.15))
    open_ = [c + 0.05 for c in close]
    high = [c + 0.3 for c in close]
    low = [c - 0.1 for c in close]
    volume = [50_000] * 120
    intraday = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)
    didx = pd.date_range("2026-07-17", periods=5, freq="1D")
    dclose = [110, 107, 104, 102, 100]
    daily = pd.DataFrame(
        {"Open": dclose, "High": [c + 1 for c in dclose], "Low": [c - 1 for c in dclose],
         "Close": dclose, "Volume": [400_000] * 5},
        index=didx,
    )
    bearish = {"intraday": intraday, "daily": daily}

    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=lambda s: bearish):
        # bare ticker (no .NS/.BO) must get normalized to .NS
        r = client.post("/intraday-scans", json={"direction": "short", "symbols": ["fake3"]})
        job_id = r.json()["id"]
        assert r.json()["scan_type"] == "intraday_short"

    results = client.get(f"/intraday-scans/{job_id}/results").json()
    assert results[0]["symbol"] == "FAKE3.NS"
    assert results[0]["sector"] == "short"


def test_results_detailed_includes_raw_result():
    client = _signed_in_client()
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        r = client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1"]})
        job_id = r.json()["id"]

    detailed = client.get(f"/intraday-scans/{job_id}/results", params={"detailed": True}).json()
    assert detailed[0]["raw_result"] is not None
    assert "conditions" in detailed[0]["raw_result"]
    assert "stop_loss" in detailed[0]["raw_result"]


def test_resume_only_retries_unscanned_symbols():
    client = _signed_in_client()
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        r = client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1", "DEAD"]})
        job_id = r.json()["id"]

    assert client.get(f"/intraday-scans/{job_id}").json()["failed_count"] == 1

    def _fetch_recovered(symbol):
        return _fake_fetch_intraday("FAKE1.NS" if symbol == "DEAD.NS" else symbol)

    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fetch_recovered):
        client.post(f"/intraday-scans/{job_id}/resume")

    final = client.get(f"/intraday-scans/{job_id}").json()
    assert final["status"] == "completed"
    results = client.get(f"/intraday-scans/{job_id}/results").json()
    assert {r["symbol"] for r in results} == {"FAKE1.NS", "DEAD.NS"}


def test_history_only_shows_intraday_jobs_scoped_from_positional():
    """A positional /scans job and an intraday /intraday-scans job created
    by the same user must not leak into each other's history endpoint --
    proves the scan_type filtering in both routers actually isolates them
    even though they share one scan_jobs table."""
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", return_value=None):
        client.post("/scans", json={"symbols": ["POSITIONAL1.NS"], "min_market_cap": 0})
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1"]})

    positional_history = client.get("/scans").json()
    intraday_history = client.get("/intraday-scans").json()

    assert len(positional_history) == 1
    assert positional_history[0]["scan_type"] == "positional"
    assert len(intraday_history) == 1
    assert intraday_history[0]["scan_type"] == "intraday_long"


def test_ownership_is_enforced():
    client_a = _signed_in_client("ia@example.com")
    client_b = _signed_in_client("ib@example.com")

    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        job_id = client_a.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1"]}).json()["id"]

    assert client_b.get(f"/intraday-scans/{job_id}").status_code == 404
    assert client_b.get(f"/intraday-scans/{job_id}/results").status_code == 404
    assert client_b.post(f"/intraday-scans/{job_id}/resume").status_code == 404


def test_clearing_intraday_history_does_not_touch_positional_jobs():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", return_value=None):
        pos_job_id = client.post("/scans", json={"symbols": ["POSITIONAL1.NS"], "min_market_cap": 0}).json()["id"]
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1"]})

    assert client.delete("/intraday-scans").status_code == 204

    assert client.get("/intraday-scans").json() == []
    assert client.get(f"/scans/{pos_job_id}").status_code == 200  # untouched


def test_events_stream_reaches_terminal_state():
    client = _signed_in_client()
    with patch("app.intraday_scan_runner.fetch_intraday_data", side_effect=_fake_fetch_intraday):
        job_id = client.post("/intraday-scans", json={"direction": "long", "symbols": ["FAKE1"]}).json()["id"]

    with client.stream("GET", f"/intraday-scans/{job_id}/events") as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line.startswith("data:")]
        assert len(lines) >= 1
        assert '"status": "completed"' in lines[-1] or '"status":"completed"' in lines[-1]
