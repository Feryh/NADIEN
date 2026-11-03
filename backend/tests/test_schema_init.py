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
