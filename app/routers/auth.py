import base64
import hashlib
import hmac
import os
import secrets as _secrets
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from auth.security import hash_password
from auth.service import AccountLocked, EmailAlreadyRegistered, InvalidCredentials, authenticate, signup
from auth.sessions import SESSION_TTL_SECONDS, create_session, delete_session
from db.models import User
from db.session import get_db

from ..deps import SESSION_COOKIE_NAME, get_current_user
from ..schemas import LoginRequest, SignupRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

# Cookies need Secure=True in prod (HTTPS only) but that blocks the cookie
# entirely over plain http during local dev — toggle via env, default safe (True).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"

# SameSite=lax is right when frontend and API share one origin (the Caddy
# setup in Caddyfile/docker-compose.yml: one domain, Caddy routes by path).
# It's WRONG if frontend and API are deployed as two different origins/
# subdomains -- e.g. Render's Static Site + Web Service, which land on two
# different *.onrender.com subdomains. Browsers treat those as different
# "sites" (onrender.com is on the public suffix list), so a Lax cookie
# never gets attached to the API calls the frontend makes. In that case,
# set COOKIE_SAMESITE=none (also requires Secure=True, i.e. real HTTPS,
# which Render provides by default -- this combination will NOT work over
# plain http, only https).
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "lax").lower()


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,       # not readable from JS — mitigates XSS token theft
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,  # "lax" for same-origin deploys, "none" for split frontend/API origins
        path="/",
    )


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup_endpoint(payload: SignupRequest, response: Response, db: Session = Depends(get_db)):
    try:
        user = signup(db, payload.email, payload.password)
    except EmailAlreadyRegistered:
        # Same 400 whether the email exists or the password is weak-ish —
        # no strong reason to hide this one at signup time (unlike login),
        # since an attacker can already probe registration via the signup
        # form itself. Kept simple.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    session_id = create_session(str(user.id))
    _set_session_cookie(response, session_id)
    return user


@router.post("/login", response_model=UserOut)
def login_endpoint(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    try:
        user = authenticate(db, payload.email, payload.password)
    except AccountLocked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
        )
    except InvalidCredentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    session_id = create_session(str(user.id))
    _set_session_cookie(response, session_id)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_endpoint(
    response: Response,
    ss_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    # Read the raw cookie (not via get_current_user — logout should succeed
    # even if the session already expired) and clear it both ends.
    if ss_session:
        delete_session(ss_session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return None


@router.get("/me", response_model=UserOut)
def me_endpoint(current_user: User = Depends(get_current_user)):
    return current_user


# ── SSO from SheshAnalysis ─────────────────────────────────────────────
# Real integration, not just a nav link: a SheshAnalysis user clicking
# "Stock Scout" arrives here already authenticated, no second login.
#
# Two separate apps, two separate user tables (SheshScout has its own
# schema -- scan_jobs/scan_results are keyed off *its* users.id, and it's
# a Postgres+Redis+Celery-shaped app, not something that can share a
# users table with Flask/SQLite without a much bigger migration). So
# instead of merging databases, SheshAnalysis vouches for the person: it
# signs a short-lived token (HMAC-SHA256, shared secret, 60s window) with
# their email, and this endpoint trusts that signature, finds-or-creates
# a matching SheshScout account by email, and logs them in exactly the
# way a password login would (same create_session/_set_session_cookie
# path). No password is ever involved on this side for an SSO arrival --
# they already proved who they are on SheshAnalysis.
SSO_SHARED_SECRET = os.environ.get("SSO_SHARED_SECRET", "")


def _verify_sso_token(token: str) -> str:
    """Returns the verified email, or raises HTTPException."""
    if not SSO_SHARED_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="SSO is not configured on this service")
    try:
        encoded, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
        email, expiry_str = payload.rsplit("|", 1)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Malformed SSO token")

    expected_sig = hmac.new(SSO_SHARED_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid SSO token")

    try:
        expiry = int(expiry_str)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Malformed SSO token")
    if expiry < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="SSO link expired — go back to SheshAnalysis and click Stock Scout again")

    return email.strip().lower()


@router.get("/sso")
def sso_login(token: str, db: Session = Depends(get_db)):
    email = _verify_sso_token(token)

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Auto-provision. This person already proved who they are on
        # SheshAnalysis; give them an unusable random password since
        # they'll only ever arrive here via SSO, never a password login.
        user = User(email=email, password_hash=hash_password(_secrets.token_urlsafe(32)))
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Account disabled")

    session_id = create_session(str(user.id))
    # Relative redirect -- in production Caddy serves the built React
    # frontend and this API on the same origin (see Caddyfile / main.py's
    # docstring), so "/" lands back on the dashboard, already logged in.
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, session_id)
    return response
