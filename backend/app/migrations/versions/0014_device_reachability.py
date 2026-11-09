"""devices: Observium reachability, uptime and last poll

Revision ID: 0014
Revises: 0013

Kept apart from devices.status (lifecycle) so the city topology can colour nodes by live state.
"""

from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reachability', sa.String(length=16), server_default='unknown', nullable=False))
        batch_op.add_column(sa.Column('uptime_s', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('last_polled_at')
        batch_op.drop_column('uptime_s')
        batch_op.drop_column('reachability')
