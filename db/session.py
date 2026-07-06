"""
db/session.py — SQLAlchemy engine & session factory.

Reads DATABASE_URL from the environment, e.g.:
    postgresql+psycopg2://stockscout:password@localhost:5432/stockscout

Nothing in this file talks to Postgres-specific features, so the same
setup works for local dev, CI, and prod — only the connection string changes.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout",
)

# pool_pre_ping avoids handing out dead connections after a Postgres restart
# or a long idle period — cheap check, saves a confusing error deep in a
# Celery task otherwise.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a session, always closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
