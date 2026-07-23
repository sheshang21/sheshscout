"""add scan_jobs.scan_type

Revision ID: c19f7b2a4e01
Revises: 7a46dc9a972a
Create Date: 2026-07-22 00:00:00.000000

Adds the column backing db.models.ScanType/ScanJob.scan_type, which lets
one job row be either the original positional/fundamentals scan or one of
the two intraday screeners ported from the standalone Streamlit scripts
(core/intraday_scanner.py). server_default='positional' means every
existing row backfills as a positional job (the only kind that existed
before this migration) with no separate UPDATE needed.

Deliberately does NOT add new tables for intraday scan results -- they
reuse scan_jobs/scan_results as-is (thresholds JSONB holds intraday
params, ScanResult.sector holds direction, ScanResult.qualified holds
"STRONG signal"). See db/models.py's ScanType docstring for the full
rationale.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c19f7b2a4e01'
down_revision: Union[str, Sequence[str], None] = '7a46dc9a972a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCAN_TYPE_ENUM = sa.Enum('positional', 'intraday_long', 'intraday_short', name='scan_type')


def upgrade() -> None:
    """Upgrade schema."""
    _SCAN_TYPE_ENUM.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'scan_jobs',
        sa.Column(
            'scan_type',
            _SCAN_TYPE_ENUM,
            nullable=False,
            server_default='positional',
        ),
    )
    op.create_index(op.f('ix_scan_jobs_scan_type'), 'scan_jobs', ['scan_type'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_scan_jobs_scan_type'), table_name='scan_jobs')
    op.drop_column('scan_jobs', 'scan_type')
    _SCAN_TYPE_ENUM.drop(op.get_bind(), checkfirst=True)
