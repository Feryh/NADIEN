"""interfaces.mib_type (Observium ifType) and if_type 'virtual' for logical interfaces kept as physical

Revision ID: 0008
Revises: 0007

Port capacity counts physical ports only. Until now "physical" was decided from the name alone, so SVIs, management
and other logical interfaces (Vlan100, dcn_eth1, MEth0/0/1, Virtual-Template1, qx1, BVI10 …) were counted. The next
Observium sync classifies by ifType; this migration fixes the existing rows by name right away. The pattern is a
frozen copy of parsers.VIRTUAL_NAME at this revision.
"""

import re

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

_VIRTUAL_NAME = re.compile(
    r"^(vlan|vl\d|bvi|bdi|bdif|vbdif|vbui|supervlan|virtual|ve-group|te_tunnel|tunnel-te|pw-ether|dcn|meth|"
    r"gpon[-_]onu|nve|vsi|vxlan|bridge|dialer|mgmt_eth|qx|spi-|console|fti\d|dloopback|ipip|tap\d|aux)",
    re.IGNORECASE)


def upgrade() -> None:
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mib_type', sa.String(length=64), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, name FROM interfaces WHERE if_type = 'physical'")).all()
    ids = [row.id for row in rows if _VIRTUAL_NAME.match(row.name)]
    update = sa.text("UPDATE interfaces SET if_type = 'virtual' WHERE id IN :ids").bindparams(sa.bindparam('ids', expanding=True))
    for start in range(0, len(ids), 500):
        bind.execute(update, {"ids": ids[start:start + 500]})


def downgrade() -> None:
    op.execute("UPDATE interfaces SET if_type = 'physical' WHERE if_type = 'virtual'")
    with op.batch_alter_table('interfaces', schema=None) as batch_op:
        batch_op.drop_column('mib_type')
