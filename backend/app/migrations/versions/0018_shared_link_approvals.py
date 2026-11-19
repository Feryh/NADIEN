"""link_approvals.shared: an approved link may ride a port another link already uses

Revision ID: 0018
Revises: 0017

A bundle towards a third-party NNI carries several services, one VLAN each, to different far ends. LLDP or the config
links the port to one of them; the others are approved by hand with `shared` set, and discovery keeps drawing them.
"""

from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('link_approvals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shared', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('link_approvals', schema=None) as batch_op:
        batch_op.drop_column('shared')
