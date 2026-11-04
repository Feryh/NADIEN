"""POP coordinates from KML/KMZ placemarks."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.geo import KmlError, Placemark, match_pops, read_placemarks

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Owned</name>
<Placemark><name>NDC Palembang</name><Point><coordinates>104.7307702,-2.949704355,0</coordinates></Point></Placemark>
<Placemark><name>Branch Kemuning</name><Point><coordinates>104.7516555,-2.956685541,0</coordinates></Point></Placemark>
<Placemark><name>Jalur</name><LineString><coordinates>104.1,-2.1,0 104.2,-2.2,0</coordinates></LineString></Placemark>
</Folder></Document></kml>"""


def zipped(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def test_should_read_points_with_folder_when_file_is_plain_kml():
    pms = read_placemarks(KML, "pop.kml")
    assert [(p.name, p.lat, p.lon, p.folder) for p in pms] == [
        ("NDC Palembang", -2.9497044, 104.7307702, "Owned"), ("Branch Kemuning", -2.9566855, 104.7516555, "Owned")]


def test_should_open_nested_archive_when_file_is_kmz_inside_zip():
    nested = zipped("POP MTI.kmz", zipped("doc.kml", KML))
    assert len(read_placemarks(nested, "POP MTI.kmz.zip")) == 2


def test_should_refuse_kml_when_it_declares_entities():
    with pytest.raises(KmlError):
        read_placemarks(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><kml>&a;</kml>')


def pop(code: str, area: str, name: str = "", **kw) -> dict:
    return {"id": hash(code) % 1000, "code": code, "name": name or code, "city": None, "area": area,
            "latitude": None, "longitude": None, **kw}


def test_should_prefer_main_site_when_city_has_several_placemarks():
    pms = [Placemark("ODC Palembang Ilir", -2.97, 104.75), Placemark("NDC Palembang", -2.95, 104.73)]
    [proposal] = match_pops([pop("PLG", "Sumatera")], pms)
    assert proposal["placemark"] == "NDC Palembang" and proposal["confidence"] == "high"


def test_should_reject_placemark_when_it_lies_outside_the_pop_area():
    # "Padang Galak" is in Bali; POP PAD is Padang, Sumatera.
    assert match_pops([pop("PAD", "Sumatera")], [Placemark("NDC Padang Galak", -8.6578, 115.2635)]) == []
    # "ITC Kuningan" is in Jakarta; POP KNG is Kuningan, Jawa Barat.
    assert match_pops([pop("KNG", "Jawa Barat & Banten")], [Placemark("ITC Kuningan", -6.2237, 106.8259)]) == []


def test_should_find_city_in_pop_name_when_code_is_not_registered():
    pms = [Placemark("MTI Kisaran", 2.9772, 99.6262)]
    [proposal] = match_pops([pop("KIS", "Sumatera", name="Kisaran - Kota")], pms)
    assert (proposal["latitude"], proposal["longitude"]) == (2.9772, 99.6262)


def test_should_only_match_named_part_of_city_when_pop_is_a_sub_pop():
    pms = [Placemark("Bekasi Utara", -6.178, 106.996), Placemark("ODC Jakarta Garden City", -6.17, 106.96)]
    proposals = match_pops([pop("BKSU", "Jabodetabek"), pop("BKSB", "Jabodetabek"), pop("JKTS", "Jabodetabek")], pms)
    assert [(p["code"], p["placemark"], p["confidence"]) for p in proposals] == [("BKSU", "Bekasi Utara", "high")]


def test_should_use_city_in_pop_name_when_it_contradicts_the_code():
    # Code PDG reads as Padang, but the POP is "Pandeglang-OLT Pandeglang".
    pms = [Placemark("ODC Padang Utara", -0.9283, 100.3655)]
    assert match_pops([pop("PDG", "Sumatera", name="Pandeglang-OLT Pandeglang")], pms) == []


def test_should_mark_low_confidence_when_city_is_followed_by_another_place_name():
    [proposal] = match_pops([pop("JKT", "Jabodetabek")], [Placemark("ODC Jakarta Garden City", -6.17, 106.96)])
    assert proposal["confidence"] == "low"


def test_should_mark_medium_confidence_when_city_is_followed_by_a_direction():
    [proposal] = match_pops([pop("KLN", "Jawa Tengah & DIY")], [Placemark("ODC Klaten Utara", -7.69, 110.62)])
    assert proposal["confidence"] == "medium"


def test_should_mark_low_confidence_when_city_word_follows_another_word():
    [proposal] = match_pops([pop("PTI", "Jawa Tengah & DIY")], [Placemark("ODC Gunung Pati", -7.0865, 110.3654)])
    assert proposal["confidence"] == "low"


@pytest.mark.parametrize("token,place,score", [
    ("LVU", "lippo village utara", 5),
    ("SKL", "syiah kuala", 4),
    ("KKD", "kepanjenkidul", 3),
    ("GDO", "gadingrejo", 1),
    ("TLP", "bukit tinggi", 0),
    ("NDC", "ntt indonesia nexcenter", 0),  # function word: letters in order are not enough
    ("NIX", "nix surabaya", 5),
])
def test_should_score_site_abbreviation_by_how_letters_map_to_the_name(token, place, score):
    from app.services.geo import site_score

    assert site_score(token, place) == score


def device(hostname: str, site: str, anchor=(-2.95, 104.73)) -> dict:
    return {"id": hash(hostname) % 1000, "hostname": hostname, "pop": hostname[:3], "site": site, "anchor": anchor}


def test_should_match_device_site_to_nearby_placemark():
    from app.services.geo import match_devices

    pms = [Placemark("ODC Alang-Alang Lebar", -2.93, 104.70), Placemark("ODC Alang-Alang Lebar", 3.5, 98.6)]
    [proposal], ambiguous = match_devices([device("PLG-AAL-OLT1-FH", "AAL")], pms)
    assert (proposal["latitude"], proposal["confidence"], ambiguous) == (-2.93, "high", 0)


def test_should_lower_confidence_when_matching_site_is_far_from_the_pop():
    from app.services.geo import match_devices

    # KMN fits "Kedamean" letter by letter, but Kedamean (Gresik) is ~70 km from Malang.
    [proposal], _ = match_devices([device("MLG-KMN-OLT1-FH", "KMN", (-7.98, 112.63))],
                                  [Placemark("ODC Kedamean", -7.32, 112.55)])
    assert proposal["confidence"] == "low" and proposal["distance_km"] > 60


def test_should_ignore_facility_words_when_matching_site():
    from app.services.geo import match_devices

    # "Server Room Branch Ngawi" is not an abbreviation source for SRN (Sragen).
    proposals, _ = match_devices([device("SGN-SRN-BNG1-M4", "SRN", (-7.43, 111.02))],
                                 [Placemark("Server Room Branch Ngawi", -7.40, 111.45)])
    assert proposals == []


def test_should_skip_device_when_several_distant_places_fit_the_abbreviation():
    from app.services.geo import match_devices

    pms = [Placemark("ODC Tanjungpura", -6.30, 107.30), Placemark("ODC Tanjung Priok", -6.11, 106.88)]
    proposals, ambiguous = match_devices([device("KRW-TJP-BNG1-M4", "TJP", (-6.30, 107.30))], pms)
    assert proposals == [] and ambiguous == 1


def test_should_take_one_site_when_matching_names_are_the_same_place():
    from app.services.geo import match_devices

    pms = [Placemark("ODC Kramat Jati", -6.270, 106.868), Placemark("Kramat Jati", -6.272, 106.870)]
    [proposal], ambiguous = match_devices([device("JKT-KJA-OLT1-FH", "KJA", (-6.2, 106.85))], pms)
    assert ambiguous == 0 and proposal["placemark"] == "ODC Kramat Jati"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_preview_then_write_coordinates_when_importing_kmz(client, db):
    plg = m.Pop(code="PLG", name="Palembang", region="Sumatera")
    jkt = m.Pop(code="JKT", name="Jakarta", latitude=-6.2, longitude=106.8)
    db.add_all([plg, jkt])
    db.commit()
    parsed = client.post("/api/geo/placemarks?filename=pop.kmz", content=zipped("doc.kml", KML)).json()
    preview = client.post("/api/pops/coordinates/match", json={"placemarks": parsed["placemarks"]}).json()
    assert [p["code"] for p in preview["proposals"]] == ["PLG"]  # JKT already has coordinates
    assert db.get(m.Pop, plg.id).latitude is None  # preview writes nothing
    row = preview["proposals"][0]
    resp = client.put("/api/pops/coordinates", json=[{"pop_id": row["pop_id"], "latitude": row["latitude"],
                                                      "longitude": row["longitude"]}])
    assert resp.json()["updated"] == 1 and db.get(m.Pop, plg.id).latitude == -2.9497044
