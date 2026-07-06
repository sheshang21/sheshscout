"""
tests/test_auth.py — exercises the full signup/login/logout flow against
a real Postgres + Redis (not mocked). Run with a test database configured:

    export DATABASE_URL=postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout
    export REDIS_URL=redis://localhost:6379/0
    export COOKIE_SECURE=false
    python -m alembic upgrade head   # make sure tables exist
    pytest tests/test_auth.py -v

Truncates the users table before/after — do not point this at a database
you care about.
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("COOKIE_SECURE", "false")

from app.main import app  # noqa: E402
from db.session import SessionLocal  # noqa: E402
from core.redis_client import get_redis  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    db = SessionLocal()
    db.execute(text("TRUNCATE users, scan_jobs, scan_results, dead_symbols CASCADE"))
    db.commit()
    db.close()
    get_redis().flushdb()
    yield


def test_signup_creates_session_and_returns_user():
    client = TestClient(app)
    r = client.post("/auth/signup", json={"email": "a@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 201
    assert r.json()["email"] == "a@example.com"
    assert "ss_session" in r.cookies


def test_signup_duplicate_email_rejected():
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "a@example.com", "password": "correct-horse-battery"})
    r = client.post("/auth/signup", json={"email": "a@example.com", "password": "different-pw-123"})
    assert r.status_code == 400


def test_me_requires_session():
    client = TestClient(app)
    assert client.get("/auth/me").status_code == 401


def test_login_logout_flow():
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "b@example.com", "password": "correct-horse-battery"})
    client.post("/auth/logout")

    r = client.post("/auth/login", json={"email": "b@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 200
    assert client.get("/auth/me").status_code == 200

    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_wrong_password_and_unknown_email_look_identical():
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "c@example.com", "password": "correct-horse-battery"})
    client.post("/auth/logout")

    r_wrong = client.post("/auth/login", json={"email": "c@example.com", "password": "nope"})
    r_unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": "nope"})
    assert r_wrong.status_code == r_unknown.status_code == 401
    assert r_wrong.json()["detail"] == r_unknown.json()["detail"]


def test_lockout_after_repeated_failures():
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "d@example.com", "password": "correct-horse-battery"})
    client.post("/auth/logout")

    for _ in range(5):
        client.post("/auth/login", json={"email": "d@example.com", "password": "wrong"})

    r = client.post("/auth/login", json={"email": "d@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 429
