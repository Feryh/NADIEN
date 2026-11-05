"""node positions arranged by hand on each topology view (area, province, regency, POP, devices)

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'topology_layouts',
        sa.Column('view_key', sa.String(length=255), nullable=False),
        sa.Column('positions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint('view_key'),
    )


def downgrade() -> None:
    op.drop_table('topology_layouts')
