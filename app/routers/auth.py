import os

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

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
