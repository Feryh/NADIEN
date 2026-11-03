"""Digital twin (Phase 10): failure reroute, growth/upgrade what-if, service impact, scenarios, SRv6 readiness."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.twin import simulate
from app.services.twin_migration import area_devices, readiness


@pytest.fixture
def ring(db):
    """Ring A—B—C—D—A (10G each) in Jabodetabek, spur D—E to Surabaya (Jawa Timur)."""
    jkt = m.Pop(code="JKT", name="Jakarta", region="Jabodetabek")
    sby = m.Pop(code="SBY", name="Surabaya", region="Jawa Timur")
    db.add_all([jkt, sby])
    db.flush()
    dev = {}
    for name, pop in (("A", jkt), ("B", jkt), ("C", jkt), ("D", jkt), ("E", sby)):
        dev[name] = m.Device(hostname=name, pop_id=pop.id, role="pe")
        db.add(dev[name])
    db.flush()
    links = {}
    for a, b, traffic in (("A", "B", 4), ("B", "C", 1), ("C", "D", 1), ("D", "A", 1), ("D", "E", 2)):
        links[a + b] = m.Link(name=f"{a}-{b}", a_device_id=dev[a].id, b_device_id=dev[b].id, capacity_gbps=10,
                              current_gbps=traffic)
        db.add(links[a + b])
    db.commit()
    return {"dev": dev, "links": links}


def _after(result, name):
    return next(r for r in result["links"] if r["name"] == name)


def test_should_move_traffic_of_failed_link_onto_the_other_way_round_the_ring(db, ring):
    result = simulate(db, [{"type": "fail_link", "link_id": ring["links"]["AB"].id}])
    assert result["totals"]["rerouted_gbps"] == 4
    assert [_after(result, n)["after_pct"] for n in ("B-C", "C-D", "D-A")] == [50.0, 50.0, 50.0]


def test_should_count_traffic_lost_and_device_isolated_when_spur_fails(db, ring):
    result = simulate(db, [{"type": "fail_link", "link_id": ring["links"]["DE"].id}])
    assert result["totals"]["lost_unroutable_gbps"] == 2 and [i["hostname"] for i in result["isolated"]] == ["E"]


def test_should_treat_traffic_of_failed_device_as_ending_there(db, ring):
    result = simulate(db, [{"type": "fail_device", "device_id": ring["dev"]["B"].id}])
    assert result["totals"]["lost_terminating_gbps"] == 5 and result["totals"]["failed_links"] == 2


def test_should_grow_only_links_touching_the_area_when_growth_names_an_area(db, ring):
    result = simulate(db, [{"type": "growth", "pct": 100, "area": "Jawa Timur"}])
    assert [r["name"] for r in result["links"]] == ["D-E"] and _after(result, "D-E")["after_pct"] == 40.0


def test_should_lower_utilization_when_link_is_upgraded(db, ring):
    result = simulate(db, [{"type": "upgrade_link", "link_id": ring["links"]["AB"].id, "capacity_gbps": 100}])
    assert _after(result, "A-B")["after_pct"] == 4.0


def test_should_keep_spur_reachable_when_virtual_link_is_added(db, ring):
    result = simulate(db, [{"type": "add_link", "a_device_id": ring["dev"]["C"].id, "b_device_id": ring["dev"]["E"].id,
                            "capacity_gbps": 10},
                           {"type": "fail_link", "link_id": ring["links"]["DE"].id}])
    assert result["totals"]["isolated_devices"] == 0 and result["totals"]["rerouted_gbps"] == 2


def test_should_move_service_to_backup_when_primary_path_breaks(db, ring):
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    svc = m.Service(customer_id=1, circuit_id="C-1", service_type="l2vpn", bandwidth_mbps=100, status="active",
                    a_device_id=ring["dev"]["A"].id, b_device_id=ring["dev"]["B"].id)
    db.add(svc)
    db.flush()
    db.add(m.ServicePath(service_id=svc.id, role="primary", device_ids=[], hops=1, link_ids=[ring["links"]["AB"].id]))
    db.add(m.ServicePath(service_id=svc.id, role="backup", device_ids=[], hops=3,
                         link_ids=[ring["links"][k].id for k in ("DA", "CD", "BC")]))
    db.commit()
    services = simulate(db, [{"type": "fail_link", "link_id": ring["links"]["AB"].id}])["services"]
    assert services["counts"] == {"down": 0, "on_backup": 1, "unprotected": 0}


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_save_result_with_scenario_and_drop_it_when_changes_are_edited(client, ring):
    created = client.post("/api/twin/scenarios", json={
        "name": "Putus A-B", "changes": [{"type": "fail_link", "link_id": ring["links"]["AB"].id}]}).json()
    run = client.post(f"/api/twin/scenarios/{created['id']}/run").json()
    assert run["result"]["totals"]["rerouted_gbps"] == 4 and run["run_at"]
    edited = client.patch(f"/api/twin/scenarios/{created['id']}",
                          json={"changes": [{"type": "growth", "pct": 10}]}).json()
    assert edited["result"] is None and edited["run_at"] is None


def test_should_reject_unknown_link_in_scenario(client, ring):
    res = client.post("/api/twin/simulate", json={"changes": [{"type": "fail_link", "link_id": 9999}]})
    assert res.status_code == 422 and "9999" in res.json()["detail"]


def test_should_rank_areas_and_routers_by_srv6_readiness(db, ring):
    dev = ring["dev"]
    dev["A"].role, dev["A"].loopback_ipv4, dev["A"].isis_net, dev["A"].srv6_locator = "p", "1.1.1.1", "49.1", "fc00::/48"
    dev["B"].loopback_ipv4, dev["B"].isis_net = "1.1.1.2", "49.2"
    dev["E"].loopback_ipv4, dev["E"].isis_net = "1.1.1.5", "49.5"
    db.commit()
    report = readiness(db)
    assert report["totals"] == {"routers": 5, "ready": 1, "needs_config": 2, "blocked": 2}
    assert [a["area"] for a in report["areas"]] == ["Jawa Timur", "Jabodetabek"]
    assert [d["hostname"] for d in area_devices(db, "Jabodetabek")] == ["A", "B", "C", "D"]


def test_should_report_overload_above_150_pct_as_critical_when_reroute_causes_it(db, ring):
    ring["links"]["AB"].current_gbps = 15  # a 20G bundle in practice; capacity 10G here so the reroute overloads
    ring["links"]["AB"].capacity_gbps = 20
    db.commit()
    result = simulate(db, [{"type": "fail_link", "link_id": ring["links"]["AB"].id}])
    assert _after(result, "B-C")["after_pct"] == 160.0 and _after(result, "B-C")["level"] == "critical"
    assert result["link_levels"]["after"]["critical"] == 3


def test_should_flag_old_software_only_when_vendor_has_a_recommended_version(db, ring):
    curated, uncurated = m.Vendor(name="Huawei"), m.Vendor(name="ZTE")
    db.add_all([curated, uncurated])
    db.flush()
    old_hw = m.SoftwareVersion(vendor_id=curated.id, version="V7", is_recommended=False)
    new_hw = m.SoftwareVersion(vendor_id=curated.id, version="V8", is_recommended=True)
    any_zte = m.SoftwareVersion(vendor_id=uncurated.id, version="5.0", is_recommended=False)
    db.add_all([old_hw, new_hw, any_zte])
    db.flush()
    ring["dev"]["A"].vendor_id, ring["dev"]["A"].software_id = curated.id, old_hw.id
    ring["dev"]["B"].vendor_id, ring["dev"]["B"].software_id = uncurated.id, any_zte.id
    db.commit()
    assert readiness(db)["blockers"][-1] == {"label": "software bukan rekomendasi", "routers": 1}
