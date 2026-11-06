"""DS1/DS2 devices are distribution switches: role `switch`, not a router."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.collectors.observium.parsers import guess_role
from app.routers.insights import ROUTER_ROLES
from app.services.access_ports import _direction, Far
from app.services.areas import ROLE_TIER, category_for
from app.services.segments import link_segment


def test_should_guess_switch_role_when_hostname_starts_with_ds1_or_ds2():
    assert guess_role("DS1-S6730-BKS-PDM") == "switch" and guess_role("DS2-C3750-BDG-BBU") == "switch"


@pytest.mark.parametrize("hostname", ["DS3-S5732-JKTS-TND", "DS9-UTP-EX43NDC-JKTL", "DSI-6730-JKT-OMNIDC",
                                      "DSIN1-S6320-DPKL-DAVE"])
def test_should_guess_switch_role_when_hostname_starts_with_any_ds_prefix(hostname):
    assert guess_role(hostname) == "switch"


def test_should_not_read_switch_role_from_a_ds_token_later_in_the_hostname():
    assert guess_role("JKT-DSC-PE1-ASR9K") == "pe"


def test_should_keep_switch_out_of_routers_and_in_its_own_category():
    assert "switch" not in ROUTER_ROLES and category_for("switch") == "switch"


def test_should_keep_switch_at_metro_layer_for_links_and_access_ports():
    assert ROLE_TIER["switch"] == ROLE_TIER["agg"]
    assert link_segment("pe", "switch") == "distribution"
    assert _direction("switch", Far("BKS-NBK-OLT5-HW", "olt", "lldp"), None) == "downlink"


def test_should_fix_ds_roles_and_non_ports_when_migrating_to_0009(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'n.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0008")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname, role in (("DS3-S5732-JKT", "agg"), ("DSI-6730-JKT", "pe"), ("JKT-ME1", "agg")):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, pop_locked, created_at, updated_at) "
                              "VALUES (:h, 1, :r, 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"h": hostname, "r": role})
        for name, mib, speed in (("dwdm0/2/1/0", "other", None), ("xe-0/0/1", "other", 10), ("ge-0/0/1", "ethernetCsmacd", None),
                                 ("bme0", "ethernetCsmacd", None), ("smartgroup82", "ethernetCsmacd", 20)):
            conn.execute(text("INSERT INTO interfaces (device_id, name, if_type, mib_type, speed_gbps, admin_status, oper_status, "
                              "created_at, updated_at) VALUES (3, :n, 'physical', :m, :s, 'up', 'up', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                         {"n": name, "m": mib, "s": speed})
    command.upgrade(config, "0009")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
        kinds = dict(conn.execute(text("SELECT name, if_type FROM interfaces")).all())
    assert roles == {"DS3-S5732-JKT": "switch", "DSI-6730-JKT": "switch", "JKT-ME1": "agg"}
    assert kinds == {"dwdm0/2/1/0": "virtual", "xe-0/0/1": "physical", "ge-0/0/1": "physical", "bme0": "virtual",
                     "smartgroup82": "bundle"}


def test_should_move_ds_devices_to_switch_role_when_migrating(tmp_path, monkeypatch):
    from app.config import settings

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setattr(settings, "database_url", url)  # the migration env reads the URL from settings
    config = Config("alembic.ini")
    command.upgrade(config, "0005")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO pops (code, name, tier, status, created_at, updated_at) "
                          "VALUES ('JKT', 'Jakarta', 'core', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for hostname in ("DS1-S6730-JKT-CSF", "ds2-c3750-jkt-apj", "JKT-ME1-6120HS"):
            conn.execute(text("INSERT INTO devices (hostname, pop_id, role, status, created_at, updated_at) "
                              "VALUES (:h, 1, 'agg', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"h": hostname})
    command.upgrade(config, "0006")
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT hostname, role FROM devices")).all())
    assert roles == {"DS1-S6730-JKT-CSF": "switch", "ds2-c3750-jkt-apj": "switch", "JKT-ME1-6120HS": "agg"}
