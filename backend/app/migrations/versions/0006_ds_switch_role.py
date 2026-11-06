"""devices named DS1-… / DS2-… are distribution switches: role switch instead of agg

Revision ID: 0006
Revises: 0005

Hostnames such as DS1-S6730-BKS-PDM or DS2-C3750-BDG-BBU were read as metro/aggregation routers ("DS" → agg);
they are L2 switches. Only the role changes; downgrade puts them back on agg.
"""

from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

_DS_HOSTS = "(UPPER(hostname) LIKE 'DS1%' OR UPPER(hostname) LIKE 'DS2%')"


def upgrade() -> None:
    op.execute(f"UPDATE devices SET role = 'switch' WHERE {_DS_HOSTS} AND role <> 'switch'")


def downgrade() -> None:
    op.execute(f"UPDATE devices SET role = 'agg' WHERE {_DS_HOSTS} AND role = 'switch'")
