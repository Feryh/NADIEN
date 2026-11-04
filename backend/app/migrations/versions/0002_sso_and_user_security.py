"""SSO settings, user security (customer link, forced password change, lockout, OIDC identity), audit IP

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('sso_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('display_name', sa.String(length=64), nullable=False),
    sa.Column('issuer_url', sa.String(length=512), nullable=True),
    sa.Column('client_id', sa.String(length=256), nullable=True),
    sa.Column('client_secret_encrypted', sa.Text(), nullable=True),
    sa.Column('scopes', sa.String(length=256), nullable=False),
    sa.Column('redirect_uri', sa.String(length=512), nullable=True),
    sa.Column('role_claim', sa.String(length=64), nullable=False),
    sa.Column('role_mapping', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('default_role', sa.String(length=16), nullable=True),
    sa.Column('auto_create', sa.Boolean(), nullable=False),
    sa.Column('allowed_domains', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    # Existing users get the NOT NULL defaults through server_default. SQLite rebuilds the table (batch); the
    # 0001 definition is given as copy_from because reflecting a table created earlier in the same run fails.
    with op.batch_alter_table('users', copy_from=_users_0001()) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('auth_source', sa.String(length=16), nullable=False, server_default='local'))
        batch_op.add_column(sa.Column('external_id', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('failed_logins', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_users_customer_id', 'customers', ['customer_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_users_identity', 'users', ['auth_source', 'external_id'], unique=True)
    op.add_column('audit_logs', sa.Column('ip', sa.String(length=64), nullable=True))


def _users_0001() -> sa.Table:
    return sa.Table(
        'users', sa.MetaData(),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('full_name', sa.String(length=128), nullable=True),
        sa.Column('password_hash', sa.String(length=256), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
    )


def downgrade() -> None:
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_column('ip')
    op.drop_index('ix_users_identity', table_name='users')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_customer_id', type_='foreignkey')
        batch_op.drop_column('locked_until')
        batch_op.drop_column('failed_logins')
        batch_op.drop_column('must_change_password')
        batch_op.drop_column('customer_id')
        batch_op.drop_column('external_id')
        batch_op.drop_column('auth_source')
        batch_op.drop_column('email')
    op.drop_table('sso_settings')
