"""When a port's oper status last changed (Observium ifLastChange)

Revision ID: 0022
Revises: 0021

Observium keeps an LLDP neighbour on a port long after the port went down (SRG-NDC-SW1-NX93 still listed six OLTs on
ports down since August 2025). Discovery needs to tell such a port from one that went down this week. Filled by the
next Observium sync; empty until then, which discovery treats as unknown, not as old.
"""

import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("interfaces") as batch:
        batch.add_column(sa.Column("oper_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("interfaces") as batch:
        batch.drop_column("oper_changed_at")
