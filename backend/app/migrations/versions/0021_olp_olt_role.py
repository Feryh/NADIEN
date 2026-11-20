"""OLP devices (OLT Pole) are OLTs

Revision ID: 0021
Revises: 0020

Pole-mounted OLTs are named with an OLP token (JKT-MTH-OLP4-FH, JMB-MLP-OLP1-ZTE). The hostname parser did not know
the token, so they defaulted to PE. PE devices with an OLP token move to `olt`; downgrade puts them back.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

_TOKEN_SPLIT = re.compile(r"[-_.\s]+")


def _move(from_role: str, to_role: str) -> None:
    bind = op.get_bind()
    devices = sa.table("devices", sa.column("id", sa.Integer), sa.column("hostname", sa.String),
                       sa.column("role", sa.String))
    rows = bind.execute(sa.select(devices.c.id, devices.c.hostname)
                        .where(devices.c.role == from_role, sa.func.upper(devices.c.hostname).like("%OLP%")))
    ids = [row.id for row in rows
           if "OLP" in {t.rstrip("0123456789") for t in _TOKEN_SPLIT.split(row.hostname.upper())}]
    if ids:
        bind.execute(devices.update().where(devices.c.id.in_(ids)).values(role=to_role))


def upgrade() -> None:
    _move("pe", "olt")


def downgrade() -> None:
    _move("olt", "pe")
