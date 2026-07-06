from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from auth.sessions import get_user_id
from db.models import User
from db.session import get_db

SESSION_COOKIE_NAME = "ss_session"


def get_current_user(
    ss_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    """Require a logged-in user. Raises 401 if the cookie is missing,
    expired, or points at a session Redis no longer has."""
    if not ss_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id = get_user_id(ss_session)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return user


def get_current_user_optional(
    ss_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User | None:
    """Same as get_current_user but returns None instead of raising —
    for endpoints that behave differently when logged in vs out, rather
    than requiring auth outright."""
    try:
        return get_current_user(ss_session=ss_session, db=db)
    except HTTPException:
        return None
