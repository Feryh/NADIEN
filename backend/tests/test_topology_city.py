"""Topology level=city: one entry per link record, clusters, SPOF, external POPs and the scale guard."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app
from app.services import topology_city

AREA, PROVINCE = "Jawa Tengah & DIY", "Jawa Tengah"
CITY = {"area": AREA, "province": PROVINCE, "regency": "Kab. Banyumas"}


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _device(db, pop, hostname, role, mgmt_ip=None):
    device = m.Device(hostname=hostname, pop_id=pop.id, role=role, mgmt_ip=mgmt_ip)
    db.add(device)
    db.flush()
    return device


def _link(db, a, b, a_name=None, b_name=None, capacity=10, current=2):
    interfaces = []
    for device, name in ((a, a_name), (b, b_name)):
        if name is None:
            interfaces.append(None)
            continue
        interface = m.Interface(device_id=device.id, name=name)
        db.add(interface)
        db.flush()
        interfaces.append(interface.id)
    link = m.Link(name=f"{a.hostname}-{b.hostname}", a_device_id=a.id, b_device_id=b.id, capacity_gbps=capacity,
                  current_gbps=current, a_interface_id=interfaces[0], b_interface_id=interfaces[1])
    db.add(link)
    db.flush()
    return link


@pytest.fixture
def network(db):
    """PWT (Kab. Banyumas): parallel PE links, an AGG ring, OLTs; KBM (Kab. Kebumen) one PE away."""
    pwt = m.Pop(code="PWT", name="PWT")
    kbm = m.Pop(code="KBM", name="KBM")
    db.add_all([pwt, kbm])
    db.flush()
    d = {
        "pe1": _device(db, pwt, "PWT-NDC-PE1", "pe", mgmt_ip="10.0.0.5"),
        "pe2": _device(db, pwt, "PWT-NDC-PE2", "pe", mgmt_ip="10.0.0.6"),
        "agg1": _device(db, pwt, "PWT-AGG1", "agg"),
        "agg2": _device(db, pwt, "PWT-AGG2", "agg"),
        "agg3": _device(db, pwt, "PWT-AGG3", "agg"),
        "olt1": _device(db, pwt, "PWT-ABC-OLT1", "olt"),
        "olt2": _device(db, pwt, "PWT-ABC-OLT2", "olt"),
        "kbm": _device(db, kbm, "KBM-NDC-PE1", "pe"),
    }
    links = [
        _link(db, d["pe1"], d["pe2"], "Te0/0/0/1", "Te0/0/0/1"),
        _link(db, d["pe1"], d["pe2"], "Te0/0/0/2", "Te0/0/0/2"),
        _link(db, d["agg1"], d["agg2"]),
        _link(db, d["agg2"], d["agg3"]),
        _link(db, d["agg3"], d["agg1"]),
        _link(db, d["agg1"], d["pe1"]),
        _link(db, d["olt1"], d["agg1"], "xe-0/0/1", "Te0/1"),
        _link(db, d["olt2"], d["agg2"]),
        _link(db, d["pe2"], d["kbm"], "Hu0/0/0/0", "Hu0/0/0/9", capacity=100, current=30),
    ]
    db.commit()
    return {"devices": d, "links": links}


def _city(client, **overrides):
    response = client.get("/api/topology/view", params={"level": "city", **CITY, **overrides})
    return response


def _view(client, **overrides) -> dict:
    response = _city(client, **overrides)
    assert response.status_code == 200, response.text
    return response.json()


def test_should_return_every_link_once_when_level_city(client, network, db):
    view = _view(client)
    ids = [entry["id"] for entry in view["links"]]
    assert len(ids) == len(set(ids)) == len(db.scalars(select(m.Link)).all())


def test_should_keep_parallel_links_separate_when_two_links_join_same_devices(client, network):
    d = network["devices"]
    parallel = [e for e in _view(client)["links"]
                if {e["a_device"], e["b_device"]} == {d["pe1"].hostname, d["pe2"].hostname}]
    assert len(parallel) == 2
    assert {e["a_interface"] for e in parallel} == {"Te0/0/0/1", "Te0/0/0/2"}
    assert len({(e["source"], e["target"]) for e in parallel}) == 1


def test_should_point_to_external_pop_when_link_leaves_city(client, network):
    d = network["devices"]
    entry = next(e for e in _view(client)["links"] if d["kbm"].hostname in (e["a_device"], e["b_device"]))
    assert entry["target"] == "pop:KBM"
    assert entry["b_device"] == d["kbm"].hostname
    assert entry["b_interface"] == "Hu0/0/0/9"


def test_should_list_external_pop_once_when_several_links_reach_it(client, network):
    ids = [n["id"] for n in _view(client)["nodes"]]
    assert len(ids) == len(set(ids))
    assert ids.count("pop:KBM") == 1


def test_should_group_agg_ring_when_three_agg_devices_form_cycle(client, network):
    d = network["devices"]
    view = _view(client)
    rings = [c for c in view["clusters"] if c["kind"] == "ring"]
    assert len(rings) == 1 and len(rings[0]["device_ids"]) == 3
    assert rings[0]["pop"] == "PWT"
    agg_nodes = [n for n in view["nodes"] if n.get("role") == "agg"]
    assert {n["cluster_id"] for n in agg_nodes} == {rings[0]["id"]}
    assert {n["device_id"] for n in agg_nodes} == {d[k].id for k in ("agg1", "agg2", "agg3")}


def test_should_return_422_when_regency_missing(client, network):
    response = client.get("/api/topology/view", params={"level": "city", "area": AREA, "province": PROVINCE})
    assert response.status_code == 422


def test_should_return_404_when_regency_unknown(client, network):
    assert _city(client, regency="Nowhere").status_code == 404


def test_should_return_isolated_devices_when_city_has_no_links(client, db):
    pop = m.Pop(code="KBM", name="KBM")
    db.add(pop)
    db.flush()
    _device(db, pop, "KBM-NDC-PE1", "pe")
    db.commit()
    view = _view(client, regency="Kab. Kebumen")
    assert view["links"] == []
    assert [n["label"] for n in view["nodes"]] == ["KBM-NDC-PE1"]


def test_should_summarize_access_devices_when_city_exceeds_threshold(client, network, db, monkeypatch):
    monkeypatch.setattr(topology_city, "SUMMARY_THRESHOLD", 3)
    view = _view(client)
    d = network["devices"]
    assert any(n["id"] == "summary:PWT" and n["devices"] == 2 and n["label"] == "2 akses" for n in view["nodes"])
    olt_entry = next(e for e in view["links"] if d["olt1"].hostname in (e["a_device"], e["b_device"]))
    assert olt_entry["source"] == "summary:PWT"
    assert olt_entry["a_device"] == d["olt1"].hostname
    assert len(view["links"]) == len(db.scalars(select(m.Link)).all())
    assert view["summarized"][0]["node_id"] == "summary:PWT"


def test_should_find_device_by_mgmt_ip_when_searching(client, network):
    response = client.get("/api/topology/search", params={"q": "10.0.0.5"})
    hit = response.json()["devices"][0]
    assert hit["hostname"] == "PWT-NDC-PE1"
    assert hit["mgmt_ip"] == "10.0.0.5"


def test_should_assign_kept_ring_cluster_when_device_also_in_larger_dropped_component(client, db):
    pop = m.Pop(code="PWT", name="PWT")
    db.add(pop)
    db.flush()
    agg = [_device(db, pop, f"PWT-AGG{i}", "agg") for i in (1, 2, 3)]
    pe = [_device(db, pop, f"PWT-NDC-PE{i}", "pe") for i in (1, 2, 3)]
    for a, b in ((agg[0], agg[1]), (agg[1], agg[2]), (agg[2], agg[0]), (agg[0], pe[0]), (pe[0], pe[1]),
                 (pe[1], agg[0]), (pe[1], pe[2]), (pe[2], agg[0])):
        _link(db, a, b)
    db.commit()
    view = _view(client)
    ring = next(c for c in view["clusters"] if set(c["device_ids"]) == {d.id for d in agg})
    hub = next(n for n in view["nodes"] if n["device_id"] == agg[0].id)
    assert hub["cluster_id"] == ring["id"]


def _neighbors(client, device, depth=1) -> dict:
    response = client.get("/api/topology/view", params={"level": "neighbors", "device_id": device.id, "depth": depth})
    assert response.status_code == 200, response.text
    return response.json()


def test_should_list_both_parallel_links_when_level_neighbors(client, network):
    d = network["devices"]
    view = _neighbors(client, d["pe1"])
    parallel = [e for e in view["links"] if {e["a_device"], e["b_device"]} == {d["pe1"].hostname, d["pe2"].hostname}]
    assert {e["a_interface"] for e in parallel} == {"Te0/0/0/1", "Te0/0/0/2"}
    assert {e["source"] for e in parallel} <= {f"dev:{d['pe1'].id}", f"dev:{d['pe2'].id}"}


def test_should_flag_device_spof_when_level_neighbors(client, network):
    d = network["devices"]
    nodes = {n["device_id"]: n for n in _neighbors(client, d["agg1"])["nodes"]}
    assert nodes[d["agg1"].id]["spof"] is True  # the only way to OLT1 and PE1, although it sits on a ring
    assert nodes[d["agg2"].id]["spof"] is True
    assert nodes[d["agg3"].id]["spof"] is False


def test_should_flag_link_spof_when_level_neighbors(client, network):
    d = network["devices"]
    entry = next(e for e in _neighbors(client, d["agg1"])["links"] if d["olt1"].hostname in (e["a_device"], e["b_device"]))
    assert entry["spof"] is True


def test_should_only_list_links_between_included_devices_when_level_neighbors(client, network):
    d = network["devices"]
    view = _neighbors(client, d["pe1"])
    included = {n["device_id"] for n in view["nodes"]}
    assert d["kbm"].id not in included
    assert all(not {e["a_device"], e["b_device"]} & {d["kbm"].hostname} for e in view["links"])


def test_should_keep_cluster_only_when_all_members_in_neighbors_view(client, network):
    d = network["devices"]
    full = _neighbors(client, d["agg1"])
    assert [c["kind"] for c in full["clusters"]] == ["ring"]
    node = next(n for n in full["nodes"] if n["device_id"] == d["agg1"].id)
    assert node["cluster_id"] == full["clusters"][0]["id"]
    partial = _neighbors(client, d["pe1"])  # AGG2 and AGG3 are two hops away
    assert [c["kind"] for c in partial["clusters"]] == ["pair"]  # only the PE pair is complete
    assert next(n for n in partial["nodes"] if n["device_id"] == d["agg1"].id)["cluster_id"] is None


def test_should_list_pops_of_included_devices_when_level_neighbors(client, network):
    d = network["devices"]
    view = _neighbors(client, d["pe2"])
    assert {p["code"] for p in view["pops"]} == {"PWT", "KBM"}
    pwt = next(p for p in view["pops"] if p["code"] == "PWT")
    assert pwt["device_count"] == 2  # PE1 and PE2 only
    node = next(n for n in view["nodes"] if n["device_id"] == d["kbm"].id)
    assert node["city"] == "Kab. Kebumen"
