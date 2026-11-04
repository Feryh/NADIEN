"""interface traffic rate at the last utilization sync (bandwidth of ports without a link, e.g. PON ports)

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('in_gbps', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('out_gbps', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.drop_column('out_gbps')
        batch_op.drop_column('in_gbps')
