"""POP from hostname: the first registered city code wins; function words (NAP, CGN) are never a POP."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.collectors.observium.parsers import infer_pop, parse_device
from app.database import get_db
from app.main import app
from app.services.areas import first_city_code, is_city_code

REGEX = r"^([A-Za-z]{3,4})[-_.]"


@pytest.mark.parametrize("hostname,pop", [
    ("CGW-NAP-BDG-BBU", "BDG"),
    ("CGW-NAP-BDL-KDT", "BDL"),
    ("CGW-NAP-DPS-PGL", "DPS"),
    ("CGW-NAP-DUM-NOC", "DUM"),
    ("VRR-MPLS-JKT-BSD", "JKT"),
    ("M6KS-BRAS-CGN-JKTT-PSR", "JKTT"),  # 4-letter variant kept as written
    ("JKT-ABW-OLT1-HW", "JKT"),
    ("BKT-GGP-BNG1-M4", "BKT"),
    ("SUL-SN-SGM-0078-ME1-ZTE", "SGM"),  # cell-site naming: the city token
])
def test_should_take_first_registered_city_code_when_reading_hostname(hostname, pop):
    rec = parse_device({"device_id": 1, "sysName": hostname, "os": "vrp"}, REGEX)
    assert rec.pop_code == pop


def test_should_keep_unlisted_first_token_when_hostname_has_no_registered_code():
    # LBP (Lubuk Pakam) is a real POP the dictionary does not list: still inferred, not dropped.
    assert infer_pop("OLT3-MA5800-X15-LBP-DIT", set()) == "LBP"


def test_should_never_pick_function_word_when_inferring():
    assert infer_pop("CORE-NAP-XYZQ-SITE", {"NAP"}) == "XYZQ"
    assert first_city_code("CORE-NAP-CGN-NIX") is None


def test_should_treat_four_letter_variant_as_city_when_prefix_is_registered():
    assert is_city_code("JKTU") and is_city_code("JKTS") and not is_city_code("NAP")


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def nap(db):
    bdg = m.Pop(code="BDG", name="Bandung")
    nap = m.Pop(code="NAP", name="NAP")
    lbp = m.Pop(code="LBP", name="LBP")
    db.add_all([bdg, nap, lbp])
    db.flush()
    db.add_all([m.Device(hostname="CGW-NAP-BDG-BBU", pop_id=nap.id), m.Device(hostname="CGW-NAP-BDL-KDT", pop_id=nap.id),
                m.Device(hostname="OLT3-MA5800-X15-LBP-DIT", pop_id=lbp.id, role="olt")])
    db.commit()
    return {"bdg": bdg, "nap": nap}


def test_should_only_preview_when_rehome_is_not_applied(client, nap, db):
    body = client.post("/api/pops/rehome").json()
    assert body["applied"] is False and body["devices_moved"] == 2
    assert {(mv["from"], mv["to"]) for mv in body["moves"]} == {("NAP", "BDG"), ("NAP", "BDL")}
    assert db.scalar(select(m.Device).where(m.Device.hostname == "CGW-NAP-BDG-BBU")).pop_id == nap["nap"].id


def test_should_move_devices_and_delete_emptied_pop_when_rehome_is_applied(client, nap, db):
    body = client.post("/api/pops/rehome?apply=true").json()
    assert body["pops_deleted"] == ["NAP"] and body["pops_created"] == ["BDL"]
    devices = {d.hostname: d for d in db.scalars(select(m.Device))}
    assert devices["CGW-NAP-BDG-BBU"].pop_id == nap["bdg"].id
    assert db.scalar(select(m.Pop).where(m.Pop.code == "NAP")) is None
    assert body["non_city_pops_left"] == [{"code": "LBP", "devices": 1}]  # nothing to read: left for review
