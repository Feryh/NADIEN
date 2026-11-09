"""Config-sourced links between two cities that carry no traffic are left out of the topology views."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.regions import Place
from app.services.topology_readmodel import Ctx, is_stale_cross_city

AREA, PROVINCE = "Jawa Tengah & DIY", "Jawa Tengah"
BANYUMAS = {"level": "city", "area": AREA, "province": PROVINCE, "regency": "Kab. Banyumas"}


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _device(db, pop: m.Pop, hostname: str) -> m.Device:
    device = m.Device(hostname=hostname, pop_id=pop.id, role="pe")
    db.add(device)
    db.flush()
    return device


def _link(db, a: m.Device, b: m.Device, source: str, current: float) -> int:
    link = m.Link(name=f"{a.hostname}-{b.hostname}", a_device_id=a.id, b_device_id=b.id, capacity_gbps=10,
                  current_gbps=current, discovery_source=source)
    db.add(link)
    db.flush()
    return link.id


@pytest.fixture
def network(db) -> dict[str, int]:
    """PWT (Kab. Banyumas) and KBM (Kab. Kebumen). PWT-PE1 reaches KBM-PE1 over one LLDP link; the only other
    path, through PWT-PE2, ends in a cross-city config link without traffic."""
    pwt, kbm = m.Pop(code="PWT", name="PWT"), m.Pop(code="KBM", name="KBM")
    db.add_all([pwt, kbm])
    db.flush()
    pe1, pe2 = _device(db, pwt, "PWT-NDC-PE1"), _device(db, pwt, "PWT-NDC-PE2")
    kbm1, kbm2 = _device(db, kbm, "KBM-NDC-PE1"), _device(db, kbm, "KBM-NDC-PE2")
    links = {
        "same_city_config": _link(db, pe1, pe2, "config", 0),
        "lldp_trunk": _link(db, pe1, kbm1, "lldp", 5),
        "stale_config": _link(db, pe2, kbm1, "config", 0),
        "busy_config": _link(db, pe2, kbm2, "config", 3),
        "idle_lldp": _link(db, kbm2, pe1, "lldp", 0),
    }
    db.commit()
    return links


def _view(client: TestClient, **params: str) -> dict:
    response = client.get("/api/topology/view", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _link_ids(view: dict) -> set[int]:
    return {entry["id"] for entry in view["links"]}


def test_should_hide_config_link_when_cross_city_and_no_traffic(client, network):
    assert network["stale_config"] not in _link_ids(_view(client, **BANYUMAS))


def test_should_keep_config_link_when_it_carries_traffic(client, network):
    assert network["busy_config"] in _link_ids(_view(client, **BANYUMAS))


def test_should_keep_config_link_when_both_ends_in_same_city(client, network):
    assert network["same_city_config"] in _link_ids(_view(client, **BANYUMAS))


def test_should_keep_lldp_link_when_cross_city_and_no_traffic(client, network):
    assert network["idle_lldp"] in _link_ids(_view(client, **BANYUMAS))


def test_should_show_hidden_links_when_include_hidden_is_true(client, network):
    assert network["stale_config"] in _link_ids(_view(client, **BANYUMAS, include_hidden="true"))


@pytest.mark.parametrize("params", [BANYUMAS, {"level": "area"}], ids=["city", "area"])
def test_should_report_hidden_link_count_when_links_are_hidden(client, network, params):
    assert _view(client, **params)["hidden_links"] == 1


def test_should_keep_config_link_when_one_end_city_is_unmapped(client, db, network):
    # XQZ names no known city: how far away it is cannot be told, so its idle config link stays.
    xqz = m.Pop(code="XQZ", name="XQZ")
    db.add(xqz)
    db.flush()
    pwt_pe1 = db.query(m.Device).filter_by(hostname="PWT-NDC-PE1").one()
    unknown_city = _link(db, pwt_pe1, _device(db, xqz, "XQZ-NDC-PE1"), "config", 0)
    db.commit()
    assert unknown_city in _link_ids(_view(client, **BANYUMAS))


def test_should_hide_config_link_when_same_named_regencies_are_in_different_provinces():
    places = {1: Place("Jawa Barat", "Kab. Bogor", "Bogor"), 2: Place("Jawa Tengah", "Kab. Bogor", "Bogor")}
    ctx = Ctx(pops={}, area_of_pop={1: "Jawa", 2: "Jawa"}, vendors={},
              devices={10: SimpleNamespace(pop_id=1), 20: SimpleNamespace(pop_id=2)}, place_of_pop=places)
    link = SimpleNamespace(a_device_id=10, b_device_id=20, discovery_source="config", current_gbps=0)
    assert is_stale_cross_city(link, ctx) is True


@pytest.mark.parametrize(("include_hidden", "spof"), [("false", True), ("true", False)])
def test_should_not_count_hidden_link_as_redundancy_when_computing_spof(client, network, include_hidden, spof):
    view = _view(client, **BANYUMAS, include_hidden=include_hidden)
    trunk = next(entry for entry in view["links"] if entry["id"] == network["lldp_trunk"])
    assert trunk["spof"] is spof
