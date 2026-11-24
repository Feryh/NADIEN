"""SNI 7657:2010 kabupaten/kota codes: used for POP codes nobody curated, never against what is known of the POP."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services import sni
from app.services.regions import place_for_pop


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_pop(db, code: str, name: str | None = None, region: str | None = None, city: str | None = None) -> m.Pop:
    pop = m.Pop(code=code, name=name or code, region=region, city=city)
    db.add(pop)
    db.commit()
    return pop


def test_should_read_the_whole_sni_table_with_capitals():
    entries = sni.table()
    assert len(entries) >= 480
    assert entries["BLN"] == sni.SniPlace("BLN", "Kab. Tanah Bumbu", "Batulicin", "Kalimantan Selatan")


def test_should_take_the_letters_of_a_numbered_pop_code_when_looking_up_sni():
    assert sni.place_for_code("SBS2").regency == "Kab. Sambas"
    assert sni.place_for_code("JKTB") is None


def test_should_fill_area_and_capital_when_code_is_only_in_sni(client, db):
    pop = add_pop(db, "BLN")
    assert client.post("/api/areas/pop-location-mapping").status_code == 200
    db.refresh(pop)
    assert (pop.region, pop.city) == ("Kalimantan", "Batulicin")


def test_should_keep_team_meaning_when_sni_gives_the_code_to_another_place(client, db):
    add_pop(db, "MGL", "Magelang - Metroyudan")
    result = client.get("/api/areas/pop-location-mapping").json()
    mapping = next(row for row in result["mappings"] if row["code"] == "MGL")
    assert "Lampung" not in (mapping["area"] or "") and mapping["city"] != "Menggala"
    assert [c["code"] for c in result["sni_conflicts"]] == ["MGL"]


def test_should_see_same_place_when_only_spelling_differs():
    from app.services.regions import sni_conflict

    assert sni_conflict("PRE", "Pare Pare", "Sulawesi") is None
    assert sni_conflict("NPM", "Padang Pariaman", "Sumatera") is None
    assert sni_conflict("KDI", "Kediri - Mojoronto", "Jawa Timur") is not None


def test_should_keep_cell_site_town_when_sni_reads_the_code_otherwise():
    place = place_for_pop("PDP", None, "PDP", area="Sumatera", hostnames=["SUM-SB-PDP-0012-ME1-ZTE"])
    assert place.regency == "Kab. Padang Pariaman"


def test_should_follow_sni_when_pop_name_confirms_it_against_the_code_table():
    place = place_for_pop("PDG", None, "Pandeglang-OLT Pandeglang", area="Jawa Barat & Banten")
    assert (place.province, place.regency) == ("Banten", "Kab. Pandeglang")


def test_should_not_use_sni_when_its_province_is_outside_the_pop_area(client, db):
    pop = add_pop(db, "SBS", region="Sulawesi")
    client.post("/api/areas/pop-location-mapping")
    db.refresh(pop)
    assert pop.city is None


def test_should_not_overwrite_a_city_the_team_already_entered(client, db):
    pop = add_pop(db, "TJG", city="Tanjung Tabalong")
    client.post("/api/areas/pop-location-mapping")
    db.refresh(pop)
    assert pop.city == "Tanjung Tabalong"


def test_should_place_pop_in_sni_regency_when_drilling_down_the_topology():
    place = place_for_pop("UNH", None, "UNH", area="Sulawesi")
    assert (place.province, place.regency, place.city) == ("Sulawesi Tenggara", "Kab. Konawe", "Unaaha")


def test_should_put_jakarta_ring_regencies_in_jabodetabek_when_area_comes_from_sni():
    from app.services.regions import area_of_regency

    assert area_of_regency("Kota Depok", "Jawa Barat") == "Jabodetabek"
    assert area_of_regency("Kab. Garut", "Jawa Barat") == "Jawa Barat & Banten"
