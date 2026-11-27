"""Drop the quotes a Dell config left around a device's hostname

Revision ID: 0027
Revises: 0026

Dell OS writes `hostname "JKT-CSF-CLDMGMT-01"` and the parser kept the quotes, so gitweb made a config-only twin
of the device Observium already polls. Gitweb only re-reads changed files, so the stored names are fixed here: a
quoted name nobody else has is unquoted; a twin of an existing device hands its gitweb file to that device (read
again on the next sync) and is hidden, not deleted.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, hostname FROM devices WHERE hostname LIKE '\"%\"' OR hostname LIKE '''%'''")).all()
    by_name = {name: device_id for device_id, name in bind.execute(sa.text("SELECT id, hostname FROM devices"))}
    for device_id, hostname in rows:
        name = hostname[1:-1].strip()
        if len(hostname) < 3 or hostname[0] != hostname[-1] or not name:
            continue
        twin = by_name.get(name)
        if twin is None:
            bind.execute(sa.text("UPDATE devices SET hostname = :name WHERE id = :id"), {"name": name, "id": device_id})
            by_name[name] = device_id
            continue
        bind.execute(sa.text("UPDATE gitweb_files SET device_id = :twin, created_device = :no, blob_sha = '' "
                             "WHERE device_id = :id"), {"twin": twin, "no": False, "id": device_id})
        bind.execute(sa.text("UPDATE devices SET hidden = :yes, gone_since = :now WHERE id = :id"),
                     {"yes": True, "now": datetime.now(timezone.utc), "id": device_id})


def downgrade() -> None:
    pass  # the quoted names were never wanted
