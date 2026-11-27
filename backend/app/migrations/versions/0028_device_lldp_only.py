"""Devices known only from neighbours' LLDP

Revision ID: 0028
Revises: 0027

GTO-TLB-OLT1-HW is not polled by Observium, yet GTO-KTT-BNG1-M4's Eth-Trunk5 and two OLTs see it over LLDP. Discovery
now creates such boxes (collectors/lldp_only.py) and flags them here, so their links are drawn and a hand approval
across them yields to what LLDP sees.
"""

import sqlalchemy as sa
from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("lldp_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("lldp_only")
