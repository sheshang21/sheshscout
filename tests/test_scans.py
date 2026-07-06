"""
tests/test_scans.py — exercises the scan API end-to-end against real
Postgres + Redis, with core.scanner.fetch_stock_data mocked out (no real
Yahoo Finance calls in tests -- this only proves the API/DB/background-task
wiring, not that fetch_stock_data itself works, which core/scanner.py's own
usage already covers).

    export DATABASE_URL=postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout
    export REDIS_URL=redis://localhost:6379/0
    export COOKIE_SECURE=false
    export CELERY_ALWAYS_EAGER=true   # run tasks inline, no live worker needed for these tests
    python -m alembic upgrade head
    pytest tests/test_scans.py -v
"""
import os
from unittest.mock import patch

import numpy as np
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


def _fake_fetch(symbol):
    """Deterministic synthetic OHLCV so analyze_stock() has something real
    to score, without touching the network."""
    if symbol.startswith("DEAD"):
        return None
    closes = np.linspace(100, 115, 65)
    volumes = np.full(65, 150_000)
    return {
        "symbol": symbol, "price": closes[-1], "change": 0.5,
        "closes": closes, "highs": closes * 1.01, "lows": closes * 0.99, "volumes": volumes,
        "rsi": 35, "macd": 0.5, "bb_position": 15, "vol_multiple": 1.5,
        "trend": "Strong Uptrend", "fii_dii_score": 12,
        "market_cap": 50_000_00_00_000, "profit_margin": 0.2,
        "yoy_revenue_growth": 25, "qoq_revenue_growth": 14,
        "yoy_profit_growth": 30, "qoq_profit_growth": 18,
        "total_cash": 0, "latest_fy_revenue": 0,
        "cash_on_hand_to_mcap": 0, "latest_fy_revenue_to_mcap": 0,
        "historical_data": {"years": [], "revenues": [], "cash_amounts": [], "sales_to_mcap": []},
    }


def _signed_in_client(email="scantest@example.com"):
    client = TestClient(app)
    client.post("/auth/signup", json={"email": email, "password": "correct-horse-battery"})
    return client


def test_start_scan_runs_and_stores_results():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        r = client.post("/scans", json={"symbols": ["FAKE1.NS", "FAKE2.NS", "DEAD.NS"], "min_market_cap": 0})
        assert r.status_code == 201
        job_id = r.json()["id"]

    status = client.get(f"/scans/{job_id}").json()
    assert status["status"] == "completed"
    assert status["scanned_count"] == 3
    assert status["failed_count"] == 1  # DEAD.NS

    results = client.get(f"/scans/{job_id}/results").json()
    assert {r["symbol"] for r in results} == {"FAKE1.NS", "FAKE2.NS"}
    assert all(r["raw_result"] is None for r in results)  # detailed=False by default


def test_results_detailed_includes_raw_result():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        r = client.post("/scans", json={"symbols": ["FAKE1.NS"], "min_market_cap": 0})
        job_id = r.json()["id"]

    detailed = client.get(f"/scans/{job_id}/results", params={"detailed": True}).json()
    assert detailed[0]["raw_result"] is not None
    assert "criteria" in detailed[0]["raw_result"]


def test_resume_only_retries_unscanned_symbols():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        r = client.post("/scans", json={"symbols": ["FAKE1.NS", "DEAD.NS"], "min_market_cap": 0})
        job_id = r.json()["id"]

    assert client.get(f"/scans/{job_id}").json()["failed_count"] == 1

    # Retry, but this time DEAD.NS "comes back" (simulates a transient
    # network blip rather than a truly delisted symbol)
    def _fetch_recovered(symbol):
        return _fake_fetch("FAKE1.NS" if symbol == "DEAD.NS" else symbol)

    with patch("app.scan_runner.fetch_stock_data", side_effect=_fetch_recovered):
        client.post(f"/scans/{job_id}/resume")

    final = client.get(f"/scans/{job_id}").json()
    assert final["status"] == "completed"
    results = client.get(f"/scans/{job_id}/results").json()
    assert {r["symbol"] for r in results} == {"FAKE1.NS", "DEAD.NS"}


def test_resume_with_nothing_left_is_a_noop():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        r = client.post("/scans", json={"symbols": ["FAKE1.NS"], "min_market_cap": 0})
        job_id = r.json()["id"]

    r2 = client.post(f"/scans/{job_id}/resume")
    assert r2.json()["status"] == "completed"


def test_history_lists_jobs_newest_first():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        client.post("/scans", json={"symbols": ["FAKE1.NS"], "min_market_cap": 0})
        client.post("/scans", json={"symbols": ["FAKE2.NS"], "min_market_cap": 0})

    history = client.get("/scans").json()
    assert len(history) == 2
    assert history[0]["created_at"] >= history[1]["created_at"]


def test_ownership_is_enforced():
    client_a = _signed_in_client("a@example.com")
    client_b = _signed_in_client("b@example.com")

    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        job_id = client_a.post("/scans", json={"symbols": ["FAKE1.NS"], "min_market_cap": 0}).json()["id"]

    assert client_b.get(f"/scans/{job_id}").status_code == 404
    assert client_b.get(f"/scans/{job_id}/results").status_code == 404
    assert client_b.post(f"/scans/{job_id}/resume").status_code == 404


def test_events_stream_reaches_terminal_state():
    client = _signed_in_client()
    with patch("app.scan_runner.fetch_stock_data", side_effect=_fake_fetch):
        job_id = client.post("/scans", json={"symbols": ["FAKE1.NS"], "min_market_cap": 0}).json()["id"]

    # By the time we open the stream the background task has already
    # finished (TestClient runs it synchronously) -- confirms the SSE
    # endpoint correctly reports a terminal state as its first event
    # rather than hanging.
    with client.stream("GET", f"/scans/{job_id}/events") as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line.startswith("data:")]
        assert len(lines) >= 1
        assert '"status": "completed"' in lines[-1] or '"status":"completed"' in lines[-1]
