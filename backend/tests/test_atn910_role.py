"""Huawei ATN910 routers aggregate access at the metro layer: role `agg`, except CPE1-ATN910-… (customer premises)."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.collectors.observium.parsers import guess_role
from app.services.areas import category_for


def test_should_file_an_atn910_router_under_metro_aggregation():
    assert guess_role("PE1-ATN910C-TJB-KMN") == "agg" and category_for(guess_role("PE1-ATN910D-IOH-KIMA-MKS")) == "metro"


def test_should_keep_an_atn910_named_cpe_a_cpe():
    assert guess_role("CPE1-ATN910-JKT-ABC") == "cpe"


def test_should_read_the_cgw_nni_and_br_role_tokens():
    assert [guess_role(h) for h in ("CGW-ISP-JKTT-ME1", "NNI1-JKT-ME2", "BR1-JKT-ME1")] == ["pe", "pe", "pe"]


def test_should_move_atn910_pes_to_agg_and_keep_cpes_when_migrating_to_0020(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'a.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0019")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname, role in (("PE1-ATN910C-TJB-KMN", "pe"), ("CPE1-ATN910-JKT-ABC", "cpe"),
                               ("JKT-NDC-SPE1-ASR9K", "pe")):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, pop_locked, created_at, updated_at) "
                              "VALUES (:h, 1, :r, 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                         {"h": hostname, "r": role})
    command.upgrade(config, "0020")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
    assert roles == {"PE1-ATN910C-TJB-KMN": "agg", "CPE1-ATN910-JKT-ABC": "cpe", "JKT-NDC-SPE1-ASR9K": "pe"}
