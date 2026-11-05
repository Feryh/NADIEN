"""Topology drill-down by province and regency: Area > Provinsi > Kab/Kota > POP."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.regions import UNMAPPED_PROVINCE, UNMAPPED_REGENCY, Place, place_for_pop


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def network(db):
    """JKT/BKS in Jabodetabek; PWT, KBM (Jawa Tengah) and YGY (DIY) in Jawa Tengah & DIY; BDG alone in Jawa Barat."""
    pops = {code: m.Pop(code=code, name=code) for code in ("JKT", "BKS", "PWT", "KBM", "YGY", "BDG")}
    db.add_all(pops.values())
    db.flush()
    devices = {code: m.Device(hostname=f"{code}-NDC-PE1", pop_id=pop.id, role="pe") for code, pop in pops.items()}
    db.add_all(devices.values())
    db.flush()
    for a, b in (("JKT", "BKS"), ("PWT", "KBM"), ("KBM", "YGY"), ("BKS", "BDG"), ("PWT", "JKT")):
        db.add(m.Link(name=f"{a}-{b}", a_device_id=devices[a].id, b_device_id=devices[b].id, capacity_gbps=10))
    db.commit()


def _view(client: TestClient, **params: str) -> dict:
    response = client.get("/api/topology/view", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _pairs(view: dict) -> set[frozenset[str]]:
    return {frozenset((e["source"], e["target"])) for e in view["edges"]}


def test_should_place_pop_in_the_regency_governing_its_city_when_code_names_a_district():
    assert place_for_pop("PWT", None, None) == Place("Jawa Tengah", "Kab. Banyumas", "Purwokerto")


def test_should_use_curated_city_written_as_regency_when_set():
    place = place_for_pop("XQZ", "Kab. Banyumas", None)
    assert (place.province, place.regency, place.city) == ("Jawa Tengah", "Kab. Banyumas", "Kab. Banyumas")


def test_should_split_jakarta_by_administrative_city_when_code_names_it():
    assert place_for_pop("JKTS", None, None).regency == "Jakarta Selatan"


def test_should_skip_curated_city_when_it_lies_outside_the_pop_area():
    # PWK (Purwakarta) curated as "Purwokerto": the area says Jawa Barat, so the code wins.
    place = place_for_pop("PWK", "Purwokerto", "Puwokerto - Puwokerto Timur", "Jawa Barat & Banten")
    assert place == Place("Jawa Barat", "Kab. Purwakarta", "Purwakarta")


def test_should_take_province_from_cell_site_hostnames_when_no_city_is_known():
    place = place_for_pop("XQZ", None, "XQZ", "Sumatera", ["SUM-LA-XQZ-0006-ME1-ZTE", "SUM-LA-XQZ-0065-ME1-ZTE"])
    assert (place.province, place.regency) == ("Lampung", UNMAPPED_REGENCY)


def test_should_name_cell_site_pop_by_its_town_when_town_code_is_known():
    place = place_for_pop("GNS", None, "GNS", "Sumatera", ["SUM-LA-GNS-0006-ME1-ZTE"])
    assert place == Place("Lampung", "Kab. Lampung Tengah", "Gunung Sugih")


def test_should_leave_province_and_regency_unmapped_when_city_is_unknown():
    place = place_for_pop("XQZ", None, None)
    assert (place.province, place.regency, place.city) == (UNMAPPED_PROVINCE, UNMAPPED_REGENCY, None)


def test_should_show_provinces_of_an_area_with_links_between_them_when_opening_an_area(client, network):
    view = _view(client, level="province", area="Jawa Tengah & DIY")
    kinds = {n["id"]: n["kind"] for n in view["nodes"]}
    jateng, diy = "province:Jawa Tengah & DIY|Jawa Tengah", "province:Jawa Tengah & DIY|DI Yogyakarta"
    assert kinds == {jateng: "province", diy: "province", "area:Jabodetabek": "external_area"}
    assert _pairs(view) == {frozenset((jateng, diy)), frozenset((jateng, "area:Jabodetabek"))}


def test_should_draw_other_provinces_of_the_area_as_external_when_opening_a_province(client, network):
    view = _view(client, level="regency", area="Jawa Tengah & DIY", province="Jawa Tengah")
    nodes = {n["label"]: n for n in view["nodes"]}
    assert nodes["Kab. Banyumas"]["kind"] == "regency" and nodes["Kab. Kebumen"]["kind"] == "regency"
    assert nodes["DI Yogyakarta"]["kind"] == "external_province"
    assert nodes["DI Yogyakarta"]["province"] == "DI Yogyakarta"  # enough to open it from the graph


def test_should_open_the_next_level_when_a_group_holds_a_single_child(client, network):
    view = _view(client, level="province", area="Jawa Barat & Banten")
    assert (view["level"], view["province"], view["regency"]) == ("pop", "Jawa Barat", "Kota Bandung")


def test_should_label_pops_by_city_and_narrow_to_one_regency(client, network):
    view = _view(client, level="pop", area="Jawa Tengah & DIY", province="Jawa Tengah", regency="Kab. Banyumas")
    pop = next(n for n in view["nodes"] if n["kind"] == "pop")
    assert (pop["label"], pop["code"]) == ("Purwokerto", "PWT")
    assert {n["kind"] for n in view["nodes"]} == {"pop", "external_regency", "external_area"}
