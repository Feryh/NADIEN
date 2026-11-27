"""Keep the quoted-hostname twins hidden

Revision ID: 0029
Revises: 0028

0027 hid "JKT-CSF-CLDMGMT-01" (a config-only twin of JKT-CSF-CLDMGMT-01), but lifecycle treats a device Observium never
polled as present and showed it again on the next discovery. Decommissioned long ago, it stays hidden and draws no
link.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None

LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    names = {name for (name,) in bind.execute(sa.text("SELECT hostname FROM devices"))}
    rows = bind.execute(sa.text("SELECT id, hostname FROM devices WHERE hostname LIKE '\"%\"' OR hostname LIKE '''%'''")).all()
    for device_id, hostname in rows:
        if len(hostname) > 2 and hostname[0] == hostname[-1] and hostname[1:-1].strip() in names:
            bind.execute(sa.text("UPDATE devices SET status = 'decommissioned', hidden = :yes, gone_since = :at WHERE id = :id"),
                         {"yes": True, "at": LONG_AGO, "id": device_id})


def downgrade() -> None:
    pass
