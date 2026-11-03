import time
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    # Per process: worker threads serving requests + a sync job + the scheduler. With N uvicorn
    # workers the DB sees up to N × (pool_size + max_overflow) connections; keep it under max_connections.
    **({} if _is_sqlite else {"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow,
                              "pool_recycle": 1800}),
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL lets the UI keep reading while a long collector sync is writing; without it a
        # large transaction locks every reader out ("database is locked" -> HTTP 500).
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


SCHEMA_LOCK_ID = 7240311  # arbitrary pg_advisory_lock key for schema setup


SCHEMA_ATTEMPTS = 10


def init_schema(bind: Engine | None = None) -> None:
    """Create missing tables, then any index missing on existing tables (create_all only indexes the
    tables it creates). Several uvicorn workers/replicas start at once: on PostgreSQL an advisory lock
    serialises them. SQLite has no such lock, so a worker that collides with another ("already exists",
    "no such table", "locked") retries in a fresh transaction; by then the winner has committed."""
    bind = bind or engine
    if bind.dialect.name != "sqlite":
        with bind.begin() as conn:
            conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_ID})")
            _create(conn)
        return
    for attempt in range(SCHEMA_ATTEMPTS):
        try:
            with bind.begin() as conn:
                _create(conn)
            return
        except OperationalError:
            if attempt == SCHEMA_ATTEMPTS - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def _create(conn) -> None:
    Base.metadata.create_all(conn)
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(conn, checkfirst=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
