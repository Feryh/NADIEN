"""topology events: links added, down and up again (live topology and change log)

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('topology_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=True),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['link_id'], ['links.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('topology_events', schema=None) as batch_op:
        batch_op.create_index('ix_topology_events_at', ['at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('topology_events', schema=None) as batch_op:
        batch_op.drop_index('ix_topology_events_at')

    op.drop_table('topology_events')
