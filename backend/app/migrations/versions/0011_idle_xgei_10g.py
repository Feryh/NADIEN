"""idle ZTE xgei ports count as 10G

Revision ID: 0011
Revises: 0010

An xgei port is 10G unless a different SFP is in use; while idle, what SNMP reports says nothing about its capacity.
The parser applies this on every sync; this fixes the existing rows right away. Downgrade changes nothing: the
speeds reported before are not kept, and the next sync sets them again.
"""

from alembic import op


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""UPDATE interfaces SET speed_gbps = 10.0
                   WHERE if_type = 'physical' AND admin_status = 'up' AND oper_status = 'down'
                     AND (speed_gbps IS NULL OR speed_gbps <> 10.0)
                     AND (LOWER(name) LIKE 'xgei-%' OR LOWER(name) LIKE 'xgei\_%' ESCAPE '\')""")


def downgrade() -> None:
    pass
