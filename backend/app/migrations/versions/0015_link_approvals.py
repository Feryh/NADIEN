"""link_approvals: links confirmed by hand where LLDP sees a box outside the inventory

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('link_approvals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('a_interface_id', sa.Integer(), nullable=False),
    sa.Column('b_interface_id', sa.Integer(), nullable=False),
    sa.Column('approved_by', sa.String(length=64), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['a_interface_id'], ['interfaces.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['b_interface_id'], ['interfaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('a_interface_id', 'b_interface_id')
    )
    with op.batch_alter_table('link_approvals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_link_approvals_a_interface_id'), ['a_interface_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_link_approvals_b_interface_id'), ['b_interface_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('link_approvals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_link_approvals_b_interface_id'))
        batch_op.drop_index(batch_op.f('ix_link_approvals_a_interface_id'))

    op.drop_table('link_approvals')
