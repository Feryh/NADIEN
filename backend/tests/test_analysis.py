import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.areas import area_for_pop, category_for, site_for, speed_bucket


@pytest.mark.parametrize("hostname,pop,site", [
    ("JKT-ABW-OLT1-HW", "JKT", "ABW"),
    ("OLT2-MA5603T-JKT-BSG", "JKT", "BSG"),
    ("PE1-ATN910-PTT-WTR", "PTT", "WTR"),
    ("JKT2-UTP-X", "JKT", "UTP"),
    ("PE1-ATN910-CIBISNINE", "UNASSIGNED", "-"),
])
def test_site_from_hostname(hostname, pop, site):
    assert site_for(hostname, pop) == site


def test_area_rules():
    assert area_for_pop("JKTS", None) == "Jabodetabek"  # first three letters
    assert area_for_pop("BDG", None) == "Jawa Barat & Banten"
    assert area_for_pop("BDG", "Priangan Timur") == "Priangan Timur"  # curated region wins
    assert area_for_pop("XYZ", None) == "Belum dipetakan"


def test_category_and_speed_bucket():
    assert [category_for(r) for r in ("pe", "bng", "agg", "olt", "access", None)] == [
        "router", "router", "metro", "olt", "access", "other"]
    assert [speed_bucket(s) for s in (None, 0, 0.1, 1, 10, 2.5, 100, 400)] == [
        "Unknown", "Unknown", "100M", "1G", "10G", "2.5G", "100G", "400G"]


@pytest.fixture
def net(db):
    """Two areas, three POPs; JKT has two sites."""
    pops = {c: m.Pop(code=c, name=c) for c in ("JKT", "BKS", "BDG")}
    db.add_all(pops.values())
    db.flush()
    devs = {
        "JKT-ABW-P1-NCS": ("JKT", "p"), "JKT-ABW-OLT1-HW": ("JKT", "olt"), "JKT-CSF-ME1-HW": ("JKT", "agg"),
        "BKS-LBJ-PE1-HW": ("BKS", "pe"), "BDG-DGO-PE1-HW": ("BDG", "pe"),
    }
    d = {h: m.Device(hostname=h, pop_id=pops[p].id, role=r) for h, (p, r) in devs.items()}
    db.add_all(d.values())
    db.flush()

    def port(dev, name, speed, admin="up", oper="up", if_type="physical"):
        i = m.Interface(device_id=d[dev].id, name=name, speed_gbps=speed, admin_status=admin, oper_status=oper,
                        if_type=if_type)
        db.add(i)
        return i

    p1 = port("JKT-ABW-P1-NCS", "Hu0/0/0/0", 100)
    p2 = port("JKT-ABW-P1-NCS", "Hu0/0/0/1", 100)
    port("JKT-ABW-P1-NCS", "Hu0/0/0/2", 100, oper="down")             # idle
    port("JKT-ABW-P1-NCS", "Hu0/0/0/3", 100, admin="down", oper="down")  # disabled
    port("JKT-ABW-P1-NCS", "BE10", 200, if_type="bundle")              # not counted
    o1 = port("JKT-ABW-OLT1-HW", "xe-0/0/1", 10)
    port("JKT-ABW-OLT1-HW", "xe-0/0/2", 10, oper="down")
    port("JKT-ABW-OLT1-HW", "ge-0/0/1", 1, oper="down")
    me = port("JKT-CSF-ME1-HW", "10GE1/0/1", 10)
    bk = port("BKS-LBJ-PE1-HW", "100GE1/0/1", 100)
    bd = port("BDG-DGO-PE1-HW", "100GE1/0/1", 100)
    db.flush()

    def link(a, ai, b, bi, cap, cur):
        db.add(m.Link(name="x", a_device_id=d[a].id, a_interface_id=ai.id, b_device_id=d[b].id,
                      b_interface_id=bi.id, capacity_gbps=cap, current_gbps=cur, discovery_source="lldp"))

    link("JKT-ABW-P1-NCS", p1, "JKT-ABW-OLT1-HW", o1, 10, 9)      # intra-POP, red
    link("JKT-ABW-OLT1-HW", o1, "JKT-CSF-ME1-HW", me, 10, 1)       # intra-POP, cross-site
    link("JKT-ABW-P1-NCS", p2, "BKS-LBJ-PE1-HW", bk, 100, 30)      # JKT-BKS, same area
    link("BKS-LBJ-PE1-HW", bk, "BDG-DGO-PE1-HW", bd, 100, 70)      # Jabodetabek - Jawa Barat
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), d
    app.dependency_overrides.clear()


def test_area_level(net):
    api, _ = net
    view = api.get("/api/topology/view?level=area").json()
    nodes = {n["id"]: n for n in view["nodes"]}
    assert set(nodes) == {"area:Jabodetabek", "area:Jawa Barat & Banten"}
    assert nodes["area:Jabodetabek"]["pops"] == 2 and nodes["area:Jabodetabek"]["internal"]["links"] == 3
    (edge,) = view["edges"]
    assert edge["links"] == 1 and edge["capacity_gbps"] == 100 and edge["color"] == "yellow"


def test_pop_level(net):
    api, _ = net
    view = api.get("/api/topology/view", params={"level": "pop", "area": "Jabodetabek"}).json()
    nodes = {n["id"]: n for n in view["nodes"]}
    assert nodes["pop:JKT"]["sites"] == 2 and nodes["pop:JKT"]["categories"] == {"router": 1, "olt": 1, "metro": 1}
    assert nodes["area:Jawa Barat & Banten"]["kind"] == "external_area"
    pairs = {frozenset((e["source"], e["target"])) for e in view["edges"]}
    assert frozenset(("pop:JKT", "pop:BKS")) in pairs and frozenset(("pop:BKS", "area:Jawa Barat & Banten")) in pairs


def test_device_level(net):
    api, d = net
    view = api.get("/api/topology/view", params={"level": "device", "pop": "JKT"}).json()
    kinds = {n["id"]: n["kind"] for n in view["nodes"]}
    assert kinds["site:ABW"] == "site" and kinds["site:CSF"] == "site" and kinds["pop:BKS"] == "external_pop"
    devs = [n for n in view["nodes"] if n["kind"] == "device"]
    assert devs[0]["label"] == "JKT-ABW-P1-NCS" and devs[0]["tier"] == 0  # core first
    red = next(e for e in view["edges"] if e["capacity_gbps"] == 10 and e["current_gbps"] == 9)
    assert red["color"] == "red"
    assert api.get("/api/topology/view", params={"level": "device", "pop": "NOPE"}).status_code == 404
    assert api.get("/api/topology/view", params={"level": "pop"}).status_code == 422


def test_port_summary_and_devices(net):
    api, _ = net
    s = api.get("/api/inventory/ports/summary").json()
    assert s["totals"] == {"total": 10, "used": 6, "idle": 3, "disabled": 1, "used_pct": 60.0}  # bundle excluded
    speeds = {r["speed"]: r for r in s["by_speed"]}
    assert list(speeds) == ["1G", "10G", "100G"]
    assert speeds["100G"] == {"speed": "100G", "total": 6, "used": 4, "idle": 1, "disabled": 1, "used_pct": 66.7}
    olt = next(c for c in s["by_category"] if c["category"] == "olt")
    assert (olt["devices"], olt["used"], olt["idle"]) == (1, 1, 2)

    only_olt = api.get("/api/inventory/ports/summary?category=olt").json()
    assert only_olt["totals"]["total"] == 3
    jkt = api.get("/api/inventory/ports/summary?pop=jkt").json()
    assert jkt["totals"]["total"] == 8

    devices = api.get("/api/inventory/ports/devices?sort=idle").json()["devices"]
    assert devices[0]["hostname"] == "JKT-ABW-OLT1-HW" and devices[0]["idle"] == 2 and devices[0]["site"] == "ABW"
    ten_g = api.get("/api/inventory/ports/devices?speed=10G").json()
    assert {r["hostname"] for r in ten_g["devices"]} == {"JKT-ABW-OLT1-HW", "JKT-CSF-ME1-HW"}


def test_areas_and_auto_assign(net, db):
    api, _ = net
    db.add(m.Pop(code="XYZ", name="Unknown"))
    db.commit()
    listing = api.get("/api/areas").json()
    assert listing["unmapped_pops"] == ["XYZ"]
    res = api.post("/api/areas/auto-assign").json()
    assert res["assigned"] == 3 and res["by_area"]["Jabodetabek"] == 2
    assert db.query(m.Pop).filter_by(code="BDG").one().region == "Jawa Barat & Banten"


def test_role_filter_any_of(net):
    api, _ = net
    routers = api.get("/api/devices?role=p,pe,bng,dci").json()
    assert {r["hostname"] for r in routers} == {"JKT-ABW-P1-NCS", "BKS-LBJ-PE1-HW", "BDG-DGO-PE1-HW"}
