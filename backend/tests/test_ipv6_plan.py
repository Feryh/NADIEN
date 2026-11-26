"""IPv6 / SRv6 plan: register import, POP-to-city mapping, node locators, OAM addresses, saving and export."""

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services import ipv6_plan as plan_svc

HEADER = ["Region", "R", "City code", "City", "Tier", "City index", "v1 index (18 Sep)", "Infra / uSID city block",
          "First node locator", "Last node locator", "Node locators", "Residential city block",
          "Subs capacity @ /60", "CDN city /48", "Server city /48", "Internal-VRF / OAM /48"]
# Same values as the backbone team's register for these cities.
CITIES = [
    ["Jabodetabek", "0x1", "DPK", "Depok", "STANDARD", "0x44", "0x44", "2400:dc00:1440::/42", None, None, 64,
     "2402:8780:2440::/42", None, "2402:8780:b144::/48", "2402:8780:c144::/48", "2400:dc00:9144::/48"],
    ["Java Ring West", "0x2", "SMG", "Semarang", "METRO", "0x20", "0x20", "2400:dc00:2200::/40", None, None, 256,
     "2402:8780:3200::/40", None, "2402:8780:b220::/48", "2402:8780:c220::/48", "2400:dc00:9220::/48"],
    ["Java Ring West", "0x2", "PWT", "Purwokerto", "STANDARD", "0x68", "0x68", "2400:dc00:2680::/42", None, None, 64,
     "2402:8780:3680::/42", None, None, None, "2400:dc00:9268::/48"],
    ["Jabodetabek", "0x1", "PWK", "Purwakarta", "STANDARD", "0x80", "0x80", "2400:dc00:1800::/42", None, None, 64,
     "2402:8780:2800::/42", None, None, None, "2400:dc00:9180::/48"],
]


def register_bytes(rows: list[list] = CITIES) -> bytes:
    wb = openpyxl.Workbook()
    wb.active.title = "01 Block Plan"
    ws = wb.create_sheet("03 City Register")
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def network(db):
    plan_svc.import_register(db, plan_svc.parse_register(register_bytes()))
    huawei, mikrotik = m.Vendor(name="Huawei"), m.Vendor(name="Mikrotik")
    pops = {"DPK": m.Pop(code="DPK", name="DEPOK - East Depok"),
            "PWK": m.Pop(code="PWK", name="Puwokerto - Puwokerto Timur"),
            "XYZ": m.Pop(code="XYZ", name="Kota Antah")}
    db.add_all([huawei, mikrotik, *pops.values()])
    db.flush()
    devices = {}
    for hostname, role, pop, vendor in [
        ("DPK-ITC-BNG-M4", "bng", "DPK", huawei), ("PEIN1-H-DPK-CIL", "agg", "DPK", huawei),
        ("DPK-EDP-ME1-6120HS", "agg", "DPK", huawei), ("DPK-WDP-ME1-6120HS", "agg", "DPK", huawei),
        ("DPK-ABC-OLT1-HW", "olt", "DPK", huawei), ("RM1-RB2011-DPK-MTI", "pe", "DPK", mikrotik),
        ("CPE1-DPK-X", "cpe", "DPK", mikrotik), ("PWK-PWT-ME1-6120HS", "agg", "PWK", huawei),
        ("XYZ-AAA-ME1-6120HS", "agg", "XYZ", huawei),
    ]:
        devices[hostname] = m.Device(hostname=hostname, role=role, pop_id=pops[pop].id, vendor_id=vendor.id)
        db.add(devices[hostname])
    db.commit()
    return {"pops": pops, "devices": devices}


def _router(db, hostname: str) -> dict:
    return next(r for r in plan_svc.build_plan(db).routers if r["hostname"] == hostname)


# --------------------------------------------------------------------------- register


def test_should_read_index_tier_and_blocks_when_parsing_the_city_register():
    cities = {c["code"]: c for c in plan_svc.parse_register(register_bytes())}
    assert (cities["SMG"]["tier"], cities["SMG"]["city_index"], cities["SMG"]["oam48"]) == ("METRO", 0x20,
                                                                                            "2400:dc00:9220::/48")


def test_should_refuse_a_city_whose_block_does_not_match_its_region_and_index():
    bad = [CITIES[0][:7] + ["2400:dc00:1480::/42"] + CITIES[0][8:]]
    with pytest.raises(plan_svc.PlanError, match="tidak cocok"):
        plan_svc.parse_register(register_bytes(bad))


def test_should_refuse_a_file_that_is_not_an_xlsx():
    with pytest.raises(plan_svc.PlanError, match="Excel"):
        plan_svc.parse_register(b"not a workbook")


# --------------------------------------------------------------------------- POP mapping


def test_should_map_a_pop_whose_code_means_another_city_in_the_register_by_its_name(db, network):
    entries = plan_svc.pop_cities(db, {c.code: c for c in db.query(m.Ipv6City)})
    assert entries[network["pops"]["PWK"].id].city == "PWT"


def test_should_leave_a_pop_unmapped_when_neither_code_nor_name_is_in_the_register(db, network):
    entries = plan_svc.pop_cities(db, {c.code: c for c in db.query(m.Ipv6City)})
    assert entries[network["pops"]["XYZ"].id].city is None


def test_should_follow_the_planner_decision_for_a_pop(db, network):
    plan_svc.set_pop_city(db, network["pops"]["XYZ"].id, "parent", "SMG", "induk", "tester")
    assert _router(db, "XYZ-AAA-ME1-6120HS")["city"] == "SMG"


# --------------------------------------------------------------------------- locators


def test_should_give_the_depok_bng_the_register_example_locator(db, network):
    row = _router(db, "DPK-ITC-BNG-M4")
    assert (row["locator"], row["loopback6"], row["end_sid"]) == ("2400:dc00:1444::/48", "2400:dc00:1444::1/128",
                                                                  "2400:dc00:1444:e000::")


def test_should_start_access_nodes_after_the_backbone_band_in_the_city_l1_area(db, network):
    row = _router(db, "DPK-EDP-ME1-6120HS")
    assert (row["band"], row["locator"], row["isis_area"]) == ("access", "2400:dc00:1450::/48", "49.0144")


def test_should_put_pein_routers_in_the_backbone_band(db, network):
    assert _router(db, "PEIN1-H-DPK-CIL")["locator"] == "2400:dc00:1448::/48"


def test_should_address_an_olt_from_the_city_oam_block(db, network):
    row = next(r for r in plan_svc.build_plan(db).oam if r["hostname"] == "DPK-ABC-OLT1-HW")
    assert row["address"] == "2400:dc00:9144:1::1"


def test_should_give_a_mikrotik_router_an_oam_address_instead_of_a_locator(db, network):
    plan = plan_svc.build_plan(db)
    assert "RM1-RB2011-DPK-MTI" in {r["hostname"] for r in plan.oam}
    assert "CPE1-DPK-X" not in {r["hostname"] for r in plan.routers + plan.oam}


# --------------------------------------------------------------------------- saving


def test_should_keep_a_saved_index_when_a_new_device_sorts_before_it(db, network):
    plan_svc.save_plan(db, "tester")
    db.add(m.Device(hostname="DPK-AAA-ME1-6120HS", role="agg", pop_id=network["pops"]["DPK"].id,
                    vendor_id=network["devices"]["DPK-EDP-ME1-6120HS"].vendor_id))
    db.commit()
    assert (_router(db, "DPK-EDP-ME1-6120HS")["node_index"], _router(db, "DPK-AAA-ME1-6120HS")["node_index"]) == (
        0x10, 0x12)


def test_should_mark_saved_rows_proposed_and_new_rows_baru(db, network):
    plan_svc.save_plan(db, "tester")
    db.add(m.Device(hostname="DPK-ZZZ-ME1-6120HS", role="agg", pop_id=network["pops"]["DPK"].id,
                    vendor_id=network["devices"]["DPK-EDP-ME1-6120HS"].vendor_id))
    db.commit()
    assert (_router(db, "DPK-EDP-ME1-6120HS")["status"], _router(db, "DPK-ZZZ-ME1-6120HS")["status"]) == (
        "proposed", "baru")


def test_should_flag_a_city_whose_access_nodes_pass_half_of_its_access_band(db, network):
    vendor_id = network["devices"]["DPK-EDP-ME1-6120HS"].vendor_id
    db.add_all(m.Device(hostname=f"DPK-S{i:02d}-ME1-6120HS", role="agg", pop_id=network["pops"]["DPK"].id,
                        vendor_id=vendor_id) for i in range(23))
    db.commit()
    capacity = {c["code"]: c for c in plan_svc.build_plan(db).capacity}
    assert (capacity["DPK"]["access"], capacity["DPK"]["fit_50"]) == (25, "PROMOTE - needs a second city block")


# --------------------------------------------------------------------------- API


def test_should_import_the_register_from_the_request_body(client, db):
    response = client.post("/api/ipv6-plan/cities/import?filename=register.xlsx", content=register_bytes())
    assert response.status_code == 200 and response.json()["created"] == 4


def test_should_answer_422_for_a_register_without_the_city_sheet(client):
    wb = openpyxl.Workbook()
    buffer = io.BytesIO()
    wb.save(buffer)
    assert client.post("/api/ipv6-plan/cities/import", content=buffer.getvalue()).status_code == 422


def test_should_export_the_plan_as_a_workbook_with_its_sheets(client, network):
    response = client.get("/api/ipv6-plan/export")
    sheets = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True).sheetnames
    assert sheets[:4] == ["Ringkasan", "Node Locator", "OAM L2", "Kapasitas Kota"]


def test_should_confirm_a_saved_assignment(client, network):
    client.post("/api/ipv6-plan/save")
    device_id = network["devices"]["DPK-ITC-BNG-M4"].id
    response = client.patch(f"/api/ipv6-plan/assignments/{device_id}", json={"status": "confirmed"})
    assert response.json()["status"] == "confirmed"


def test_should_map_the_jakarta_pop_by_name_and_split_its_sites_by_hostname(db):
    jakarta = [["Jabodetabek", "0x1", "JKT-NDC", "Jakarta NDC", "METRO", "0x10", "0x10", "2400:dc00:1100::/40"],
               ["Jabodetabek", "0x1", "JKT-CSF", "Jakarta CSF", "METRO", "0x20", "0x20", "2400:dc00:1200::/40"]]
    plan_svc.import_register(db, plan_svc.parse_register(register_bytes(jakarta)))
    cisco, pop = m.Vendor(name="Cisco"), m.Pop(code="JKT", name="Jakarta - NDC")
    db.add_all([cisco, pop])
    db.flush()
    db.add_all([m.Device(hostname="JKT-NDC-BNG2-M4", role="bng", pop_id=pop.id, vendor_id=cisco.id),
                m.Device(hostname="P1-NCS5504-JKTS-CSF", role="p", pop_id=pop.id, vendor_id=cisco.id)])
    db.commit()
    assert {r["hostname"]: r["city"] for r in plan_svc.build_plan(db).routers} == {
        "JKT-NDC-BNG2-M4": "JKT-NDC", "P1-NCS5504-JKTS-CSF": "JKT-CSF"}
