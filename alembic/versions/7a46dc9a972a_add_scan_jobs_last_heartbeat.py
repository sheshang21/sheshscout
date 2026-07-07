"""add scan_jobs.last_heartbeat

Revision ID: 7a46dc9a972a
Revises: 8117e4fbfb25
Create Date: 2026-07-08 00:00:00.000000

scan_runner.py and app/routers/scans.py have been writing/reading
job.last_heartbeat and job.is_stale since the orphaned-scan-recovery
feature was added, but the column backing last_heartbeat (is_stale is a
computed @property, not a column) was never actually migrated in. This
adds it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7a46dc9a972a'
down_revision: Union[str, Sequence[str], None] = '8117e4fbfb25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'scan_jobs',
        sa.Column('last_heartbeat', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scan_jobs', 'last_heartbeat')
