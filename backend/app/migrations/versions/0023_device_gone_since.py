"""Devices gone from the network: since when, and hidden once past the grace period

Revision ID: 0023
Revises: 0022

A decommissioned box or one Observium stopped polling kept its links (drawn down) and LLDP rows for good. Discovery
now labels such a device for lldp_max_age_days, then deletes its links and hides it. Decommissioned devices start
counting from their last poll when that is over a day old, else from this migration.
"""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("gone_since", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()))
    devices = sa.table("devices", sa.column("status", sa.String), sa.column("last_polled_at", sa.DateTime(timezone=True)),
                       sa.column("gone_since", sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    stale = devices.c.last_polled_at < now - timedelta(days=1)
    op.get_bind().execute(sa.update(devices).where(devices.c.status == "decommissioned")
                          .values(gone_since=sa.case((stale, devices.c.last_polled_at), else_=now)))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("hidden")
        batch.drop_column("gone_since")
