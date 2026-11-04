"""Link segments (Backbone / Distribusi / Last Mile), naming standards and the live topology feed."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app
from app.services.naming import (PortFix, check_hostname, description_script, description_status,
                                 recommended_description)
from app.services.segments import link_segment
from app.services.topology import discover_links


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("a, b, segment", [
    ("p", "pe", "backbone"), ("pe", "pe", "backbone"), ("bng", "dci", "backbone"),
    ("agg", "pe", "distribution"), ("agg", "agg", "distribution"), (None, "p", "distribution"),
    ("olt", "agg", "last_mile"), ("csr", "pe", "last_mile"), ("access", None, "last_mile"),
])
def test_should_classify_link_function_when_roles_of_both_ends_are_known(a, b, segment):
    assert link_segment(a, b) == segment


@pytest.fixture
def pop(db):
    jkt = m.Pop(code="JKT", name="Jakarta", region="Jabodetabek")
    db.add(jkt)
    db.flush()
    devices = {name: m.Device(hostname=name, pop_id=jkt.id, role=role) for name, role in
               (("JKT-NDC-P1-8201", "p"), ("JKT-NDC-PE1-ASR9K", "pe"), ("JKT-NDC-ME1-NCS540", "agg"),
                ("JKT-ABW-OLT1-HW", "olt"))}
    db.add_all(devices.values())
    db.flush()
    for name, a, b, capacity in (("bb", "JKT-NDC-P1-8201", "JKT-NDC-PE1-ASR9K", 100),
                                 ("ds", "JKT-NDC-PE1-ASR9K", "JKT-NDC-ME1-NCS540", 10),
                                 ("lm", "JKT-NDC-ME1-NCS540", "JKT-ABW-OLT1-HW", 0)):
        db.add(m.Link(name=name, a_device_id=devices[a].id, b_device_id=devices[b].id, capacity_gbps=capacity))
    db.commit()
    return devices


def test_should_split_edges_and_summary_by_segment_when_viewing_a_pop(client, pop):
    view = client.get("/api/topology/view?level=device&pop=JKT").json()
    assert sorted(e["segment"] for e in view["edges"]) == ["backbone", "distribution", "last_mile"]
    assert {k: v["links"] for k, v in view["segments"].items()} == {"backbone": 1, "distribution": 1, "last_mile": 1}


def test_should_count_link_without_capacity_as_unknown_when_summarising(client, pop):
    view = client.get("/api/topology/view?level=device&pop=JKT").json()
    assert view["segments"]["last_mile"]["capacity_unknown"] == 1


# --------------------------------------------------------------------------- naming


def test_should_accept_hostname_when_it_follows_the_pop_first_standard():
    check = check_hostname("JKT-ABW-OLT1-HW", "olt", "JKT", "Huawei")
    assert (check.convention, check.status, check.issues) == ("pop_first", "standard", [])


def test_should_flag_function_code_when_it_contradicts_the_inventory_role():
    check = check_hostname("BKS-DCI-SW1-S6750", "dci", "BKS", "Huawei")
    assert check.status == "needs_fix" and "role inventory DCI" in check.issues[0]


def test_should_suggest_pop_first_name_when_hostname_is_legacy_role_first():
    check = check_hostname("OLT4-C600-JKTT-PGD", "olt", "JKTT", "ZTE")
    assert (check.status, check.suggestion) == ("legacy", "JKTT-PGD-OLT4-C600")


def test_should_join_index_to_function_when_number_stands_alone():
    check = check_hostname("SBY-JBG-OLT-02", "olt", "SBY", "FiberHome")
    assert check.suggestion == "SBY-JBG-OLT02-FH"


def test_should_mark_non_standard_when_no_convention_matches():
    assert check_hostname("ATN905-MANDIRI-TENDEAN", "pe", "JKT", "Huawei").status == "non_standard"


def test_should_build_standard_description_with_segment_peer_port_and_capacity():
    text = recommended_description("backbone", "JKT-NDC-P1-8201", "HundredGigE0/0/0/8", 100)
    assert text == "[BB]TO-JKT-NDC-P1-8201-Hu0/0/0/8[100G]"


@pytest.mark.parametrize("description, status", [
    ("[BB]TO-JKT-NDC-P1-8201-Hu0/0/0/8[100G]", "standard"),
    ("INTERNAL:[BB]TO-JKT-NDC-P1-8201-Hu0/0/0/8 [100G]", "names_peer"),
    ("TO-DS1-OLD-NAME", "no_peer"),
    ("", "empty"),
])
def test_should_rate_port_description_against_the_peer_it_should_name(description, status):
    assert description_status(description, "JKT-NDC-P1-8201") == status


def test_should_restore_previous_description_when_rolling_back_vrp_script():
    script = description_script("vrp", [PortFix("100GE1/0/5", "[DS]TO-X-100GE0/5/0[100G]", "old text"),
                                        PortFix("100GE1/0/6", "[DS]TO-Y-100GE0/5/1[100G]", None)])
    assert script["config"][:3] == ["system-view", "interface 100GE1/0/5", " description [DS]TO-X-100GE0/5/0[100G]"]
    assert " description old text" in script["rollback"] and " undo description" in script["rollback"]
    assert script["config"][-2:] == ["commit", "return"]


def test_should_list_devices_needing_fix_when_filtering_naming_report(client, db, pop):
    db.add(m.Device(hostname="JKT-MG2-OLT-HW", pop_id=pop["JKT-NDC-P1-8201"].pop_id, role="olt"))
    db.commit()
    rows = client.get("/api/topology/naming/devices?status=needs_fix").json()["rows"]
    assert [(r["hostname"], r["suggestion"]) for r in rows] == [("JKT-MG2-OLT-HW", "JKT-MG2-OLT1-HW")]


# --------------------------------------------------------------------------- live topology


@pytest.fixture
def lldp_pair(db):
    jkt = m.Pop(code="JKT", name="Jakarta")
    db.add(jkt)
    db.flush()
    a = m.Device(hostname="JKT-NDC-PE1-ASR9K", pop_id=jkt.id, role="pe")
    b = m.Device(hostname="JKT-NDC-P1-8201", pop_id=jkt.id, role="p")
    db.add_all([a, b])
    db.flush()
    for device, port in ((a, "Hu0/0/0/1"), (b, "Hu0/0/0/2")):
        db.add(m.Interface(device_id=device.id, name=port, if_type="physical", oper_status="up", speed_gbps=100))
    db.add(m.LldpNeighbor(device_id=a.id, local_interface="Hu0/0/0/1", remote_system_name=b.hostname,
                          remote_port_id="Hu0/0/0/2"))
    db.commit()
    return a, b


def _kinds(db) -> list[str]:
    return [e.kind for e in db.scalars(select(m.TopologyEvent).order_by(m.TopologyEvent.id))]


def test_should_log_one_summary_instead_of_every_link_when_first_discovery_imports(db, lldp_pair):
    discover_links(db)
    assert _kinds(db) == ["discovery"]


def test_should_log_link_down_when_link_disappears_from_lldp(db, lldp_pair):
    discover_links(db)
    db.query(m.LldpNeighbor).delete()
    db.commit()
    discover_links(db)
    assert _kinds(db)[-1] == "link_down"


def test_should_change_signature_when_a_device_is_added(client, db, lldp_pair):
    before = client.get("/api/topology/signature").json()["signature"]
    db.add(m.Device(hostname="JKT-NDC-ME9-NCS540", pop_id=lldp_pair[0].pop_id, role="agg"))
    db.commit()
    assert client.get("/api/topology/signature").json()["signature"] != before


def test_should_report_new_device_in_change_feed_when_it_was_created_recently(client, db, lldp_pair):
    feed = client.get("/api/topology/events?kind=device_added").json()
    assert {e["title"] for e in feed} == {"JKT-NDC-PE1-ASR9K", "JKT-NDC-P1-8201"}
