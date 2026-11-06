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


@pytest.mark.parametrize("hostname,area", [
    ("OLT2-MA5800-X15-KPG-AST", "Bali & Nusa Tenggara"),  # KPG = Kupang; MA5800 is a model, not "MA" (Maluku)
    ("OLT1-C300-PBR-BKR", "Sumatera"),
    ("PE1-ATN910-KEBUMEN", "Jawa Tengah & DIY"),            # city name written out
    ("DS1-S5720-SUBANG", "Jawa Barat & Banten"),
    ("PE1-ATN910-TAMAN-SIDOARJO", "Jawa Timur"),
    ("SW-CORE-JT-01", "Jawa Tengah & DIY"),                  # 2-letter province (SNI ISO 3166-2:ID) token
    ("DS5-UTP-EX34NDC-BALI-PGL", "Bali & Nusa Tenggara"),
    ("LPG-BTH-OLT1-ZTE", "Sumatera"),
    ("PAPP0004IT01", None),
    ("PE1-JKT-SBY-TRUNK", None),                              # two areas named: ambiguous, no guess
])
def test_area_from_hostname(hostname, area):
    from app.services.areas import area_for_hostname

    assert area_for_hostname(hostname) == area


def test_pop_code_and_majority():
    from app.services.areas import area_for_hostnames, city_label, pop_code_for_hostname

    assert pop_code_for_hostname("PE1-ATN910-KEBUMEN") == "KBM" and city_label("KBM") == "Kebumen"
    assert pop_code_for_hostname("PE1-ATN910C-NUMFOR-TIMUR") is None
    assert pop_code_for_hostname("LLG-LLT-BNG1-M4") == "LLG"
    assert pop_code_for_hostname("PE1-ATN910-PADANG-SIDEMPUAN") == "PSP"  # longest city name wins
    hosts = ["LPG-A-OLT1", "LPG-B-OLT2", "LPG-C-ME1", "BR1-JKT-UPLINK"]  # one backhaul name
    assert area_for_hostnames(hosts) is None  # 3/4 = 75% < 80%
    assert area_for_hostnames(hosts[:3] + ["LPG-D-PE1", "X-JKT"]) == "Sumatera"  # 4/5 = 80%


def test_unassigned_devices_move_to_their_city(db):
    pop = m.Pop(code="UNASSIGNED", name="NDC - Jakarta")
    db.add(pop)
    db.flush()
    db.add_all([
        m.Device(hostname="PE1-ATN910-KEBUMEN", pop_id=pop.id),
        m.Device(hostname="PE1-ATN910-SUBANG", pop_id=pop.id),
        m.Device(hostname="PAPP0004IT01", pop_id=pop.id),
        m.Device(hostname="DS1-S5720-SALATIGA", pop_id=pop.id),
    ])
    db.add(m.Pop(code="SNG", name="Jawa Barat - Subang"))
    db.add(m.Pop(code="STG", name="Salatiga - Sidorejo"))  # same city as dictionary code SLT
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        res = TestClient(app).post("/api/areas/auto-assign").json()
    finally:
        app.dependency_overrides.clear()
    assert res["devices_moved"] == 3 and res["pops_created"] == ["KBM"]  # no duplicate SLT next to STG
    assert res["moved_to"]["STG"] == 1
    kbm = db.query(m.Pop).filter_by(code="KBM").one()
    assert kbm.name == "Kebumen" and kbm.region == "Jawa Tengah & DIY"
    assert db.query(m.Pop).filter_by(code="SNG").one().region == "Jawa Barat & Banten"
    assert db.query(m.Pop).filter_by(code="UNASSIGNED").one().region is None  # never given an area
    left = db.query(m.Device).filter_by(pop_id=pop.id).all()
    assert [d.hostname for d in left] == ["PAPP0004IT01"]


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
        db.flush()
        # Reported by Observium: port capacity only counts such ports.
        db.add(m.ObserviumPort(interface_id=i.id, observium_port_id=i.id, observium_hostname=dev))
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
    assert kinds["pop:BKS"] == "external_pop"
    devs = [n for n in view["nodes"] if n["kind"] == "device"]
    assert devs[0]["label"] == "JKT-ABW-P1-NCS" and devs[0]["tier"] == 0  # core first
    assert {d["site"] for d in devs} == {"site:ABW", "site:CSF"} and devs[0]["pop"] == "JKT"
    red = next(e for e in view["edges"] if e["capacity_gbps"] == 10 and e["current_gbps"] == 9)
    assert red["color"] == "red"
    assert api.get("/api/topology/view", params={"level": "device", "pop": "NOPE"}).status_code == 404
    assert api.get("/api/topology/view", params={"level": "pop"}).status_code == 422


def test_neighbor_level(net):
    api, d = net
    p1 = d["JKT-ABW-P1-NCS"].id
    one = api.get("/api/topology/view", params={"level": "neighbors", "device_id": p1}).json()
    labels = {n["label"] for n in one["nodes"]}
    assert labels == {"JKT-ABW-P1-NCS", "JKT-ABW-OLT1-HW", "BKS-LBJ-PE1-HW"}
    assert next(n for n in one["nodes"] if n["center"])["label"] == "JKT-ABW-P1-NCS"
    assert len(one["edges"]) == 2 and one["pop"] == "JKT"
    two = api.get("/api/topology/view", params={"level": "neighbors", "device_id": p1, "depth": 2}).json()
    assert {n["label"] for n in two["nodes"]} == labels | {"JKT-CSF-ME1-HW", "BDG-DGO-PE1-HW"}
    assert api.get("/api/topology/view", params={"level": "neighbors"}).status_code == 422


def test_pop_tier(net):
    api, _ = net
    view = api.get("/api/topology/view", params={"level": "pop", "area": "Jabodetabek"}).json()
    tiers = {n["code"]: n.get("tier") for n in view["nodes"] if n["kind"] == "pop"}
    assert tiers == {"JKT": 0, "BKS": 1}  # JKT has a P router, BKS only a PE


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
