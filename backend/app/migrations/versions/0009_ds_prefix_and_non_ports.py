"""every hostname starting with DS is a switch; internal interfaces without a speed are not ports

Revision ID: 0009
Revises: 0008

0006 covered DS1/DS2 only; DS3…DS9, DSI and DSIN1 stayed agg/pe. Interfaces: management and internal ones found
after the first sync with ifType (bme0, jsrv, tap, StackPort, OpticalAmplifier, ifType ppp/bits, and ifType `other`
without a speed such as dwdm0/2/1/0) leave port capacity. The pattern is a frozen copy of parsers.VIRTUAL_NAME.
"""

import re

from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

_VIRTUAL_NAME = re.compile(
    r"^(vlan|vl\d|bvi|bdi|bdif|vbdif|vbui|supervlan|virtual|ve-group|te_tunnel|tunnel-te|pw-ether|dcn|meth|"
    r"gpon[-_]onu|nve|vsi|vxlan|bridge|dialer|mgmt_eth|qx|spi-|console|fti\d|dloopback|ipip|aux|stack|opticalamplifier|"
    r"(?:bme|vme|jsrv|vtep|dsc|cbp|esi|pip|tap|pime|pimd|mtun|mtunnel|demux|rbeb|mif|vrrp|vpls|vtnet|<?pptp|<?l2tp)"
    r"(?![a-z]))",
    re.IGNORECASE)


def _is_virtual(name: str, mib_type: str | None, speed: float | None) -> bool:
    kind = (mib_type or "").lower()
    return bool(_VIRTUAL_NAME.match(name)) or kind in ("ppp", "bits") or (kind == "other" and not speed)


def upgrade() -> None:
    op.execute("UPDATE devices SET role = 'switch' WHERE UPPER(hostname) LIKE 'DS%' AND role <> 'switch'")
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, name, mib_type, speed_gbps FROM interfaces WHERE if_type = 'physical'")).all()
    ids = [row.id for row in rows if _is_virtual(row.name, row.mib_type, row.speed_gbps)]
    update = sa.text("UPDATE interfaces SET if_type = 'virtual' WHERE id IN :ids").bindparams(sa.bindparam('ids', expanding=True))
    for start in range(0, len(ids), 500):
        bind.execute(update, {"ids": ids[start:start + 500]})
    op.execute("UPDATE interfaces SET if_type = 'bundle' WHERE if_type = 'physical' AND LOWER(name) LIKE 'smartgroup%' "
               "AND name NOT LIKE '%.%'")


def downgrade() -> None:
    # Interface types come back with the next sync or 0008's downgrade; only the roles are restored here.
    op.execute("UPDATE devices SET role = 'pe' WHERE UPPER(hostname) LIKE 'DSI%' AND role = 'switch'")
    op.execute("UPDATE devices SET role = 'agg' WHERE UPPER(hostname) LIKE 'DS%' AND UPPER(hostname) NOT LIKE 'DSI%' "
               "AND UPPER(hostname) NOT LIKE 'DS1%' AND UPPER(hostname) NOT LIKE 'DS2%' AND role = 'switch'")
