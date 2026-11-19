"""PEIN devices aggregate access at the metro layer: role `agg`, unless the hostname also says SPE."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.collectors.observium.parsers import guess_role
from app.services.areas import category_for


def test_should_file_a_pein_device_under_metro_aggregation():
    assert guess_role("PEIN1-H-JKTS-IDT") == "agg" and category_for(guess_role("PEIN2-H-JKTP-GR9")) == "metro"


def test_should_keep_a_pein_device_a_pe_when_the_hostname_also_says_spe():
    assert guess_role("PEIN1-SPE1-JKT-NDC") == "pe"


def test_should_move_pein_pes_to_agg_and_keep_spe_ones_when_migrating_to_0017(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'p.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0016")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname in ("PEIN1-H-JKTS-IDT", "PEIN1-SPE1-JKT-NDC", "BTM-NIX-SPE1-X8", "JKT-PEINX-PE1-M8"):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, pop_locked, created_at, updated_at) "
                              "VALUES (:h, 1, 'pe', 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"h": hostname})
    command.upgrade(config, "0017")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
    assert roles == {"PEIN1-H-JKTS-IDT": "agg", "PEIN1-SPE1-JKT-NDC": "pe", "BTM-NIX-SPE1-X8": "pe",
                     "JKT-PEINX-PE1-M8": "pe"}
