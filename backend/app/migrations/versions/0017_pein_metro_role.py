"""PEIN devices are metro / aggregation

Revision ID: 0017
Revises: 0016

PEIN1-H-JKTS-IDT and the like aggregate access at the metro layer; the hostname parser filed them as PE. PE
devices whose hostname carries a PEIN token move to `agg`, unless the hostname also says SPE (those stay PE).
Downgrade puts PEIN devices filed as `agg` back to `pe`.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

_TOKEN_SPLIT = re.compile(r"[-_.\s{}:]+")


def _words(hostname: str) -> set[str]:
    return {token.rstrip("0123456789") for token in _TOKEN_SPLIT.split(hostname.upper())}


def _devices() -> sa.TableClause:
    return sa.table("devices", sa.column("id", sa.Integer), sa.column("hostname", sa.String),
                    sa.column("role", sa.String))


def _move(from_role: str, to_role: str, keep_spe: bool) -> None:
    bind = op.get_bind()
    devices = _devices()
    rows = bind.execute(sa.select(devices.c.id, devices.c.hostname)
                        .where(devices.c.role == from_role, sa.func.upper(devices.c.hostname).like("%PEIN%")))
    ids = [row.id for row in rows if "PEIN" in (words := _words(row.hostname)) and not (keep_spe and "SPE" in words)]
    if ids:
        bind.execute(devices.update().where(devices.c.id.in_(ids)).values(role=to_role))


def upgrade() -> None:
    _move("pe", "agg", keep_spe=True)


def downgrade() -> None:
    _move("agg", "pe", keep_spe=False)
