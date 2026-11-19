"""Routers named CPE are customer-premises routers: role `cpe`, not PE."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.collectors.observium.parsers import guess_role
from app.services.areas import category_for
from app.services.segments import link_segment


@pytest.mark.parametrize("hostname", ["TNG-BJJ-CPE1-SRX300", "JKT-BBX-CPE-SRX300", "CPE1-ATN910-MDN-USU",
                                      "CPE1-L009-MAHESI-AGRI-KARYA{CID:6450220500183603}"])
def test_should_guess_cpe_role_when_the_hostname_has_a_cpe_token(hostname):
    assert guess_role(hostname) == "cpe"


def test_should_keep_a_ds_switch_a_switch_when_it_also_names_cpe():
    assert guess_role("DS2-CPE-6320-WIN-GSW") == "switch"


def test_should_put_cpe_in_its_own_category_at_the_last_mile():
    assert category_for("cpe") == "cpe" and link_segment("pe", "cpe") == "last_mile"


def test_should_move_only_routers_named_cpe_to_the_cpe_role_when_migrating_to_0016(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'c.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0015")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname, role in (("TNG-BJJ-CPE1-SRX300", "pe"), ("CPE1-ATN910-MDN-USU", "pe"),
                               ("DS2-CPE-6320-WIN-GSW", "switch"), ("JKT-CPEX-PE1-M8", "pe")):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, pop_locked, created_at, updated_at) "
                              "VALUES (:h, 1, :r, 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"h": hostname, "r": role})
    command.upgrade(config, "0016")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
    assert roles == {"TNG-BJJ-CPE1-SRX300": "cpe", "CPE1-ATN910-MDN-USU": "cpe", "DS2-CPE-6320-WIN-GSW": "switch",
                     "JKT-CPEX-PE1-M8": "pe"}
