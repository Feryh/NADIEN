"""observium_ports.if_index is unsigned 32-bit

Revision ID: 0013
Revises: 0012

SNMP ifIndex goes up to 4294967295 and Huawei OLTs do use values above 3 billion. SQLite stores them regardless;
PostgreSQL rejects them with "integer out of range", which fails the whole Observium sync.
"""

from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('observium_ports', schema=None) as batch_op:
        batch_op.alter_column('if_index', existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True)


def downgrade() -> None:
    op.execute("UPDATE observium_ports SET if_index = NULL WHERE if_index > 2147483647")
    with op.batch_alter_table('observium_ports', schema=None) as batch_op:
        batch_op.alter_column('if_index', existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True)
