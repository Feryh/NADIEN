"""Schema setup when several API worker processes start at the same moment."""

import threading

from sqlalchemy import create_engine, inspect

from app.database import init_schema


def test_should_start_cleanly_when_four_workers_init_the_same_sqlite_db(tmp_path):
    engines = [create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30}) for _ in range(4)]
    errors: list[Exception] = []
    gate = threading.Barrier(len(engines))

    def start(engine):
        gate.wait()
        try:
            init_schema(engine)
        except Exception as exc:  # noqa: BLE001 — the test reports whatever a worker would crash on
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == []
    indexes = {i["name"] for i in inspect(engines[0]).get_indexes("links")}
    assert {"ix_links_a_device", "ix_links_b_device"} <= indexes


def test_should_add_missing_index_when_table_already_exists(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE utilization_samples (id INTEGER PRIMARY KEY, link_id INTEGER, "
                             "ts DATETIME, in_gbps FLOAT, out_gbps FLOAT)")
    init_schema(engine)
    assert "ix_util_ts" in {i["name"] for i in inspect(engine).get_indexes("utilization_samples")}


def test_should_match_models_when_empty_db_is_migrated_to_head(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    init_schema(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []  # a model change without a migration: run `alembic revision --autogenerate`


def test_should_stamp_and_upgrade_when_db_predates_migrations(tmp_path):
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    legacy = [t for t in Base.metadata.sorted_tables if t.name in ("vendors", "pops")]
    Base.metadata.create_all(engine, tables=legacy)
    init_schema(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() is not None
    assert "devices" in inspect(engine).get_table_names()


def _start_worker(url: str, gate) -> None:
    engine = create_engine(url, connect_args={"timeout": 30})
    gate.wait()  # imports done: all workers look at the legacy tables at the same moment
    init_schema(engine)


def test_should_start_cleanly_when_four_processes_migrate_a_legacy_sqlite_db(tmp_path):
    """Uvicorn workers are processes: the in-process lock does not serialise them, the retry must."""
    import multiprocessing

    from alembic import command

    from app.database import alembic_config

    url = f"sqlite:///{tmp_path / 'legacy-race.db'}"
    # Exactly what create_all built before migrations: every baseline table, no alembic_version.
    with create_engine(url).begin() as conn:
        command.upgrade(alembic_config(conn), "0001")
        conn.exec_driver_sql("DROP TABLE alembic_version")
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(4)
    workers = [context.Process(target=_start_worker, args=(url, gate)) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)
    assert [w.exitcode for w in workers] == [0, 0, 0, 0]
    with create_engine(url).connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM alembic_version").scalar() == 1
