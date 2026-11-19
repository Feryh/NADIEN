"""routers named CPE get their own role

Revision ID: 0016
Revises: 0015

Customer-premises routers (TNG-BJJ-CPE1-SRX300, CPE1-ATN910-MDN-USU, …) were filed as PE because the hostname
parser had no CPE role. Router roles (p/pe/bng/dci) whose hostname carries a CPE token move to `cpe`; switches
such as DS2-CPE-… keep their role. Downgrade puts them back to `pe`.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

ROUTER_ROLES = ("p", "pe", "bng", "dci")
_TOKEN_SPLIT = re.compile(r"[-_.\s{}:]+")


def _names_cpe(hostname: str) -> bool:
    return any(token.rstrip("0123456789") == "CPE" for token in _TOKEN_SPLIT.split(hostname.upper()))


def upgrade() -> None:
    bind = op.get_bind()
    devices = sa.table("devices", sa.column("id", sa.Integer), sa.column("hostname", sa.String),
                       sa.column("role", sa.String))
    rows = bind.execute(sa.select(devices.c.id, devices.c.hostname)
                        .where(devices.c.role.in_(ROUTER_ROLES), sa.func.upper(devices.c.hostname).like("%CPE%")))
    ids = [row.id for row in rows if _names_cpe(row.hostname)]
    if ids:
        bind.execute(devices.update().where(devices.c.id.in_(ids)).values(role="cpe"))


def downgrade() -> None:
    devices = sa.table("devices", sa.column("role", sa.String))
    op.get_bind().execute(devices.update().where(devices.c.role == "cpe").values(role="pe"))
