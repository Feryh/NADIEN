import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.paths import build_graph, compute

#   POP AAA: A1, A2        POP BBB: B1, B2        transit: P1, P2, P3 (POP CORE)
#
#   A1 -100G- P1 -100G- B1          A1 -10G(9 used)- P3 -10G- B1
#   A2 -100G- P2 -100G- B2          P1 -10G- P2


@pytest.fixture
def net(db):
    pops = {c: m.Pop(code=c, name=c) for c in ("AAA", "BBB", "CORE")}
    db.add_all(pops.values())
    db.flush()
    names = {"A1": "AAA", "A2": "AAA", "B1": "BBB", "B2": "BBB", "P1": "CORE", "P2": "CORE", "P3": "CORE"}
    d = {h: m.Device(hostname=h, pop_id=pops[p].id, role="pe" if p != "CORE" else "p") for h, p in names.items()}
    db.add_all(d.values())
    db.flush()
    fibers = {n: m.Fiber(name=n) for n in ("F1", "F2")}
    db.add_all(fibers.values())
    db.flush()

    def link(a, b, cap, cur=0.0, lat=1.0, fiber=None, status="up"):
        lk = m.Link(name=f"{a}-{b}", a_device_id=d[a].id, b_device_id=d[b].id, capacity_gbps=cap, current_gbps=cur,
                    latency_ms=lat, fiber_id=fibers[fiber].id if fiber else None, status=status)
        db.add(lk)
        return lk

    links = {
        "A1-P1": link("A1", "P1", 100, 10, lat=1.0, fiber="F1"),
        "P1-B1": link("P1", "B1", 100, 10, lat=1.0),
        "A2-P2": link("A2", "P2", 100, 0, lat=1.5),
        "P2-B2": link("P2", "B2", 100, 0, lat=1.5),
        "P1-P2": link("P1", "P2", 10, 0, lat=0.2),
        "A1-P3": link("A1", "P3", 10, 9, lat=0.5),
        "P3-B1": link("P3", "B1", 10, 0, lat=0.5),
    }
    db.add(m.Customer(name="PT Uji", account_no="ACC-1"))
    db.commit()
    ids = {h: dev.id for h, dev in d.items()}
    return db, ids, {k: v.id for k, v in links.items()}, fibers


def test_primary_and_dual_homed_backup(net):
    db, d, _, _ = net
    r = compute(build_graph(db), {d["A1"], d["A2"]}, {d["B1"], d["B2"]}, 10, "1+1")
    assert r["primary"]["devices"] == ["A1", "P1", "B1"]  # A1-P3-B1 is faster but has no room for 10G
    assert r["primary"]["latency_ms"] == 2.0 and r["primary"]["hops"] == 2
    assert r["backup"]["devices"] == ["A2", "P2", "B2"]
    assert r["diversity"] == "node-disjoint (perangkat ujung berbeda)"


def test_ceiling_controls_path_choice(net):
    db, d, _, _ = net
    g = build_graph(db)
    # 0.5G on A1-P3 would make it 95% used: over the 80% ceiling, so the faster P3 path is skipped.
    strict = compute(g, {d["A1"]}, {d["B1"]}, 0.5, "1+1", max_util_pct=80)
    assert strict["primary"]["devices"] == ["A1", "P1", "B1"] and strict["primary"]["max_after_pct"] <= 80
    assert strict["backup"] is None and any("backup" in w for w in strict["warnings"])
    loose = compute(g, {d["A1"]}, {d["B1"]}, 0.5, "1+1", max_util_pct=100)
    assert loose["primary"]["devices"] == ["A1", "P3", "B1"]  # 1.0 ms beats 2.0 ms
    assert loose["backup"]["devices"] == ["A1", "P1", "B1"] and loose["diversity"] == "node-disjoint"


def test_srlg_shared_fiber_excluded(net):
    db, d, links, fibers = net
    db.get(m.Link, links["A2-P2"]).fiber_id = fibers["F1"].id  # same duct as A1-P1
    db.commit()
    r = compute(build_graph(db), {d["A1"], d["A2"]}, {d["B1"], d["B2"]}, 10, "1+1")
    assert r["primary"]["devices"] == ["A1", "P1", "B1"]
    assert r["backup"] is None  # the only diverse route shares fibre F1


def test_no_capacity_reports_bottlenecks(net):
    db, d, _, _ = net
    r = compute(build_graph(db), {d["A1"]}, {d["B1"]}, 200, "none")
    assert r["primary"] is None
    assert {b["from"] + "-" + b["to"] for b in r["bottlenecks"]} == {"A1-P3", "P3-B1"}  # the shortest topo path
    assert r["unconstrained_path"]["devices"] == ["A1", "P3", "B1"]


def test_down_links_and_estimated_latency(net):
    db, d, links, _ = net
    for key in ("A1-P1", "A1-P3"):
        db.get(m.Link, links[key]).status = "down"
    db.get(m.Link, links["A2-P2"]).latency_ms = None
    db.commit()
    r = compute(build_graph(db), {d["A1"], d["A2"]}, {d["B2"]}, 1, "none")
    assert r["primary"]["devices"] == ["A2", "P2", "B2"]
    assert r["primary"]["latency_estimated"] is True  # A2-P2 has no measured latency


def test_latency_estimate_uses_area_centre_when_pop_has_no_coordinates(db):
    jkt, sby = m.Pop(code="JKT", name="Jakarta"), m.Pop(code="SBY", name="Surabaya")
    db.add_all([jkt, sby])
    db.flush()
    a, b = m.Device(hostname="JKT-X", pop_id=jkt.id), m.Device(hostname="SBY-X", pop_id=sby.id)
    db.add_all([a, b])
    db.flush()
    db.add(m.Link(name="x", a_device_id=a.id, b_device_id=b.id, capacity_gbps=100, status="up"))
    db.commit()
    r = compute(build_graph(db), {a.id}, {b.id}, 1, "none")
    assert r["primary"]["latency_estimated"] is True
    assert 3 < r["primary"]["latency_ms"] < 8  # ~660 km between area centres x 1.4 x 5 us/km


def test_unknown_location_never_looks_faster(db):
    """A detour through a POP with no location must not beat a direct hop with a known estimate."""
    pops = {c: m.Pop(code=c, name=c) for c in ("JKT", "SBY", "NAP")}  # NAP: no area, no coordinates
    db.add_all(pops.values())
    db.flush()
    d = {c: m.Device(hostname=f"{c}-X", pop_id=p.id) for c, p in pops.items()}
    db.add_all(d.values())
    db.flush()
    for a, b in (("JKT", "SBY"), ("JKT", "NAP"), ("NAP", "SBY")):
        db.add(m.Link(name=f"{a}-{b}", a_device_id=d[a].id, b_device_id=d[b].id, capacity_gbps=100, status="up"))
    db.commit()
    r = compute(build_graph(db), {d["JKT"].id}, {d["SBY"].id}, 1, "none")
    assert r["primary"]["devices"] == ["JKT-X", "SBY-X"]


def test_reserve_and_release_api(net):
    db, d, links, _ = net
    app.dependency_overrides[get_db] = lambda: db
    try:
        api = TestClient(app)
        body = {"a": {"pop": "AAA"}, "b": {"pop": "bbb"}, "bandwidth_mbps": 10000, "protection": "1+1",
                "customer_id": 1, "circuit_id": "L2VPN-UJI-0001", "service_type": "l2vpn", "vlan_id": 100}
        res = api.post("/api/paths/reserve", json=body)
        assert res.status_code == 201, res.text
        sid = res.json()["service_id"]
        assert db.get(m.Link, links["A1-P1"]).reserved_gbps == 10  # primary
        assert db.get(m.Link, links["A2-P2"]).reserved_gbps == 10  # 1+1 backup reserved too
        svc = db.get(m.Service, sid)
        assert svc.status == "reserved" and svc.a_device_id == d["A1"] and svc.b_device_id == d["B1"]

        dup = api.post("/api/paths/reserve", json=body)
        assert dup.status_code == 409

        info = api.get(f"/api/paths/services/{sid}").json()
        assert [p["role"] for p in info["paths"]] == ["primary", "backup"]

        rel = api.post(f"/api/paths/services/{sid}/release").json()
        assert rel["status"] == "terminated"
        assert db.get(m.Link, links["A1-P1"]).reserved_gbps == 0 and db.get(m.Link, links["A2-P2"]).reserved_gbps == 0

        one_to_one = api.post("/api/paths/reserve", json={**body, "circuit_id": "L2VPN-UJI-0002", "protection": "1:1"})
        assert one_to_one.status_code == 201
        assert db.get(m.Link, links["A2-P2"]).reserved_gbps == 0  # 1:1 backup is computed, not reserved

        too_big = api.post("/api/paths/reserve", json={**body, "circuit_id": "X", "bandwidth_mbps": 500000})
        assert too_big.status_code == 409 and too_big.json()["detail"]["result"]["primary"] is None

        bad = api.post("/api/paths/compute", json={"a": {}, "b": {"pop": "BBB"}, "bandwidth_mbps": 1})
        assert bad.status_code == 422
    finally:
        app.dependency_overrides.clear()
