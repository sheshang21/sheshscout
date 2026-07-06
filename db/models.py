"""
db/models.py — the four tables from the migration plan.

    users          — accounts (auth lands in step 3, this just holds the row shape)
    scan_jobs      — one row per scan run: who ran it, what params, what status
    scan_results   — one row per (scan_job, symbol) — the analyze_stock() output
    dead_symbols   — GLOBAL, shared across every user: a delisted stock is
                     delisted for everyone, so this is not scoped per-user

Design notes:
  - UUID primary keys on users/scan_jobs/scan_results, not serial ints.
    This is a public app — sequential IDs let anyone enumerate other
    users' scan jobs by incrementing a number in the URL. UUIDs don't.
  - scan_results.raw_result is JSONB holding the full analyze_stock() dict
    (criteria list, historical_data, etc.) verbatim. The handful of columns
    alongside it (symbol, score, rating, qualified, sector) are duplicated
    out of that JSON so the common "list qualified results, sorted by
    score" queries don't need to reach into JSON at all.
  - No separate checkpoint table. The old file-based checkpoint existed so
    a scan could resume after a crash; scan_jobs + scan_results already
    give us that — "resume" means "query scan_results for this job_id,
    skip symbols already present, keep going."
  - dead_symbols stores strike_count/first/last directly as columns
    (not a JSON array of timestamps like the old file) since the only
    things ever queried are "is this symbol dead" and "expire after 30
    days of no new strikes" — plain columns are simpler and indexable.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .session import Base


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"

    id = _uuid_pk()
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    scan_jobs = relationship("ScanJob", back_populates="user", cascade="all, delete-orphan")


class ScanJobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ScanJob(Base):
    __tablename__ = "scan_jobs"

    id = _uuid_pk()
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(Enum(ScanJobStatus, name="scan_job_status"), nullable=False, default=ScanJobStatus.pending, index=True)

    # Scan parameters — kept as JSONB so new fields (e.g. a new threshold key)
    # don't need a migration; only add a real column if you need to query on it.
    universe = Column(JSONB, nullable=False)     # e.g. {"exchanges": ["NSE","BSE"], "symbols": [...]}
    thresholds = Column(JSONB, nullable=True)    # scoring thresholds used for this run
    min_market_cap = Column(Float, nullable=False, default=0)

    # Progress tracking, updated by the Celery task as it works through the list
    total_stocks = Column(Integer, nullable=False, default=0)
    scanned_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="scan_jobs")
    results = relationship("ScanResult", back_populates="scan_job", cascade="all, delete-orphan")


class ScanResult(Base):
    __tablename__ = "scan_results"
    __table_args__ = (
        # One row per symbol per job — a retry that re-fetches a symbol
        # updates the existing row instead of duplicating it.
        UniqueConstraint("scan_job_id", "symbol", name="uq_scan_results_job_symbol"),
    )

    id = _uuid_pk()
    scan_job_id = Column(UUID(as_uuid=True), ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=False, index=True)

    symbol = Column(String(32), nullable=False, index=True)
    score = Column(Float, nullable=True)
    rating = Column(String(64), nullable=True)
    qualified = Column(Boolean, nullable=False, default=False, index=True)
    sector = Column(String(64), nullable=True)

    # Full analyze_stock() output dict — criteria list, historical_data,
    # every ratio computed. This is the source of truth for the UI's
    # result detail view; the columns above are a fast-path summary.
    raw_result = Column(JSONB, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    scan_job = relationship("ScanJob", back_populates="results")


class DeadSymbol(Base):
    """Global, shared blacklist — not scoped to a user or a scan job.

    Mirrors the strike-counting logic from the old file-based version:
    a symbol needs >=2 empty-history strikes, at least an hour apart,
    before it's treated as dead. Rows older than 30 days (no new strike)
    should be treated as expired by the reader, not deleted outright —
    keep expiry as a query-time check (`last_strike_at > now() - 30 days`)
    so a relisted stock's history doesn't have to be reconstructed.
    """
    __tablename__ = "dead_symbols"

    symbol = Column(String(32), primary_key=True)
    strike_count = Column(Integer, nullable=False, default=1)
    first_strike_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_strike_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
