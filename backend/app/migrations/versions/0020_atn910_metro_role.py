"""ATN910 routers are metro / aggregation

Revision ID: 0020
Revises: 0019

Huawei ATN910 routers (PE1-ATN910C-TJB-KMN, PE1-ATN910D-IOH-BANJARMASIN) aggregate access at the metro layer; the
hostname parser filed most of them as PE from their PE1 token. PE devices with an ATN910 token in the hostname move
to `agg`. CPE1-ATN910-… stays a CPE: those are customer-premises routers. Downgrade puts them back to `pe`, except
the PEIN ones, which were `agg` before.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

_TOKEN_SPLIT = re.compile(r"[-_.\s]+")


def _tokens(hostname: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(hostname.upper()) if t]


def _is_atn910(hostname: str) -> bool:
    return any(token.startswith("ATN910") for token in _tokens(hostname))


def _move(from_role: str, to_role: str, skip_tokens: set[str]) -> None:
    bind = op.get_bind()
    devices = sa.table("devices", sa.column("id", sa.Integer), sa.column("hostname", sa.String),
                       sa.column("role", sa.String))
    rows = bind.execute(sa.select(devices.c.id, devices.c.hostname)
                        .where(devices.c.role == from_role, sa.func.upper(devices.c.hostname).like("%ATN910%")))
    ids = [row.id for row in rows if _is_atn910(row.hostname)
           and not skip_tokens & {t.rstrip("0123456789") for t in _tokens(row.hostname)}]
    if ids:
        bind.execute(devices.update().where(devices.c.id.in_(ids)).values(role=to_role))


def upgrade() -> None:
    _move("pe", "agg", skip_tokens={"CPE"})


def downgrade() -> None:
    _move("agg", "pe", skip_tokens={"PEIN", "ME", "AGG", "CS"})
