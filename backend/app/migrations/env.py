"""Alembic environment. The API passes its open connection (config.attributes["connection"]);
the CLI connects with NADIEN_DATABASE_URL."""

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (registers tables)
from app.config import settings
from app.database import Base, include_in_migrations

target_metadata = Base.metadata


def _run(connection) -> None:
    # SQLite cannot ALTER most things in place: batch mode rebuilds the table.
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite", compare_type=True,
                      include_object=include_in_migrations)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if context.is_offline_mode():
    # `alembic upgrade head --sql`: print the DDL for review by a DBA instead of running it.
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    _run(connection)
else:
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        _run(conn)
        conn.commit()
