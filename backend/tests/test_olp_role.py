"""OLP devices are OLT Poles (pole-mounted OLTs): role `olt`."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.collectors.observium.parsers import guess_role
from app.services.areas import category_for


def test_should_file_an_olp_device_under_olt():
    assert guess_role("JKT-MTH-OLP4-FH") == "olt" and category_for(guess_role("JMB-MLP-OLP1-ZTE")) == "olt"


def test_should_move_olp_pes_to_olt_when_migrating_to_0021(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'o.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0020")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname in ("JKT-MTH-OLP4-FH", "JKT-OLPX-PE1-M8"):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, pop_locked, created_at, updated_at) "
                              "VALUES (:h, 1, 'pe', 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"h": hostname})
    command.upgrade(config, "0021")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
    assert roles == {"JKT-MTH-OLP4-FH": "olt", "JKT-OLPX-PE1-M8": "pe"}
