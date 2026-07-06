"""
auth/service.py — signup/authenticate against the users table.

Kept separate from the FastAPI routes so this logic is testable (and
reusable — a future admin CLI or Celery task can call these directly)
without spinning up HTTP.
"""
from sqlalchemy.orm import Session

from db.models import User

from .lockout import clear_attempts, is_locked_out, record_failed_attempt
from .security import hash_password, verify_password


class EmailAlreadyRegistered(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class AccountLocked(Exception):
    pass


# A syntactically-valid but unusable argon2 hash, used to burn roughly the
# same amount of CPU time on a nonexistent-email login attempt as a real
# one — so response timing doesn't reveal whether an email is registered.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$MDAwMDAwMDAwMDAwMDAwMDAwMDAwMA$MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA"


def signup(db: Session, email: str, password: str) -> User:
    email = email.strip().lower()
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise EmailAlreadyRegistered(email)

    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    email = email.strip().lower()

    if is_locked_out(email):
        raise AccountLocked(email)

    user = db.query(User).filter(User.email == email).first()

    # Deliberately do the same amount of work, and raise the same error,
    # whether or not the email exists — don't let timing or message
    # content leak whether an email is registered.
    if user is None or not user.is_active:
        verify_password(password, _DUMMY_HASH)
        record_failed_attempt(email)
        raise InvalidCredentials(email)

    if not verify_password(password, user.password_hash):
        record_failed_attempt(email)
        raise InvalidCredentials(email)

    clear_attempts(email)
    return user
