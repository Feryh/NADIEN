"""observium_ports.source: which Observium a port id belongs to (main | csr)

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('observium_ports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='main', nullable=False))


def downgrade() -> None:
    op.execute("DELETE FROM observium_ports WHERE source <> 'main'")
    with op.batch_alter_table('observium_ports', schema=None) as batch_op:
        batch_op.drop_column('source')
