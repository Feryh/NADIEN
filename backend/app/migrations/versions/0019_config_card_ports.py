"""interfaces.config_port: card ports proven by an OLT config count in port capacity

Revision ID: 0019
Revises: 0018

Observium leaves out OLT ports that are not in service: PON ports without ONTs and uplink-board ports without an
optic (Huawei MPU `ethernet0/9/1`-`3`). The config lists them, so they are created from it and flagged here; port
capacity counts them like the ports Observium reports. PON ports already created from the config (their technology
in optic_type, no Observium port) get the flag on upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None

PON_TECHNOLOGIES = ("GPON", "XG-PON", "XGS-PON", "EPON", "10G-EPON", "XGS-PON/GPON combo")


def upgrade() -> None:
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('config_port', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute(
        sa.text("UPDATE interfaces SET config_port = :flag WHERE optic_type IN :techs "
                "AND NOT EXISTS (SELECT 1 FROM observium_ports o WHERE o.interface_id = interfaces.id)")
        .bindparams(sa.bindparam("techs", expanding=True), flag=True, techs=list(PON_TECHNOLOGIES)))


def downgrade() -> None:
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.drop_column('config_port')
