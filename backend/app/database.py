import importlib.util
import threading
from contextlib import contextmanager
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import Connection, Engine, create_engine, event, inspect
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


MIGRATIONS = Path(__file__).parent / "migrations"
_MIGRATE_LOCK = threading.Lock()  # Alembic keeps module-level state while it runs: one run per process
BASELINE = "0001"  # the schema create_all built before migrations existed
# Tables a database extension creates by itself (the postgis image adds spatial_ref_sys, topology.layer, tiger.*
# to every database): not ours, so autogenerate must not drop them and they do not make a database "pre-migration".
EXTENSION_TABLES = frozenset({"spatial_ref_sys"})


def extension_tables(conn: Connection) -> set[str]:
    """EXTENSION_TABLES plus, on PostgreSQL, every table an installed extension owns."""
    if conn.dialect.name != "postgresql":
        return set(EXTENSION_TABLES)
    owned = conn.exec_driver_sql(
        "SELECT c.relname FROM pg_depend d JOIN pg_class c ON c.oid = d.objid "
        "WHERE d.classid = 'pg_class'::regclass AND d.deptype = 'e'").scalars()
    return set(EXTENSION_TABLES) | set(owned)


def migration_filter(conn: Connection) -> Callable[..., bool]:
    """Alembic `include_object` hook that leaves extension tables alone."""
    skip = extension_tables(conn)

    def include(obj, name: str | None, type_: str, reflected: bool, compare_to) -> bool:
        return not (type_ == "table" and reflected and compare_to is None and name in skip)

    return include


def init_schema(bind: Engine | None = None) -> None:
    """Bring the database to the latest migration. Several uvicorn workers/replicas start at once: on
    PostgreSQL an advisory lock serialises them. SQLite has no such lock, so a worker that collides with
    another ("already exists", "no such table", "locked") retries in a fresh transaction; by then the winner
    has committed."""
    bind = bind or engine
    if bind.dialect.name != "sqlite":
        with _MIGRATE_LOCK, bind.begin() as conn:
            conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_ID})")
            _migrate(conn)
        return
    for attempt in range(SCHEMA_ATTEMPTS):
        try:
            with _MIGRATE_LOCK, bind.begin() as conn:
                _migrate(conn)
            return
        # CommandError: another process stamped/upgraded between our look at the tables and our stamp.
        except (OperationalError, CommandError):
            if attempt == SCHEMA_ATTEMPTS - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def alembic_config(conn: Connection | None = None) -> AlembicConfig:
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = conn
    return config


def _migrate(conn: Connection) -> None:
    from . import models  # noqa: F401  (late: models imports Base from here)

    config = alembic_config(conn)
    tables = set(inspect(conn).get_table_names()) - extension_tables(conn)
    if tables and "alembic_version" not in tables:
        # A database from before migrations (create_all): replay the baseline for what it lacks, then record
        # it as being at the baseline so later migrations alter it like any other.
        _complete_baseline(conn, tables)
        command.stamp(config, BASELINE)
    command.upgrade(config, "head")


class _BaselineOps:
    """`op` for replaying the baseline on a pre-migration database: existing tables are kept as they are,
    only their missing indexes (create_all skipped indexes on tables that already existed) are added."""

    def __init__(self, conn: Connection, existing: set[str]):
        self._ops = Operations(MigrationContext.configure(conn))
        self._conn, self._existing = conn, existing

    def create_table(self, name: str, *columns, **kw):
        if name not in self._existing:
            return self._ops.create_table(name, *columns, **kw)

    def create_index(self, name: str, table: str, columns, **kw):
        if table not in self._existing or name not in {i["name"] for i in inspect(self._conn).get_indexes(table)}:
            return self._ops.create_index(name, table, columns, **kw)

    @contextmanager
    def batch_alter_table(self, table: str, **kw):
        # The baseline only creates indexes inside batch blocks; done directly, no table rebuild needed.
        yield _BaselineBatch(self, table)

    @staticmethod
    def f(name: str) -> str:
        return name


class _BaselineBatch:
    def __init__(self, ops: _BaselineOps, table: str):
        self._ops, self._table = ops, table

    def create_index(self, name: str, columns, **kw):
        return self._ops.create_index(name, self._table, columns, **kw)

    f = staticmethod(_BaselineOps.f)


def _complete_baseline(conn: Connection, existing: set[str]) -> None:
    path = next((MIGRATIONS / "versions").glob(f"{BASELINE}_*.py"))
    spec = importlib.util.spec_from_file_location("nadien_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = _BaselineOps(conn, existing)
    module.upgrade()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
