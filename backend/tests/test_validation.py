"""Service validation (Phase 5): capacity & redundancy, MTU & ports, VLAN/VRF, device readiness."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.validation import ServiceDesign, validate


@pytest.fixture
def net(db):
    """A — P — B in a line, plus a second path A — Q — B for protection."""
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    vendor = m.Vendor(name="Huawei")
    db.add(vendor)
    db.flush()
    sw_ok = m.SoftwareVersion(vendor_id=vendor.id, version="V8", is_recommended=True)
    db.add(sw_ok)
    db.flush()
    dev = {}
    for name, role in (("A-PE1", "pe"), ("P1", "p"), ("Q1", "p"), ("B-PE1", "pe")):
        dev[name] = m.Device(hostname=name, pop_id=pop.id, role=role, loopback_ipv4="10.0.0.1", isis_net="49.0001",
                             software_id=sw_ok.id, srv6_locator="fc00::/48")
        db.add(dev[name])
    db.flush()
    ifaces = {}

    def port(device: str, name: str, mtu: int | None = 9000) -> m.Interface:
        iface = m.Interface(device_id=dev[device].id, name=name, mtu=mtu, speed_gbps=10, admin_status="up", oper_status="up")
        db.add(iface)
        db.flush()
        ifaces[(device, name)] = iface
        return iface

    links = {}
    for key, (a, b, fiber) in {"ap": ("A-PE1", "P1", 1), "pb": ("P1", "B-PE1", 2),
                               "aq": ("A-PE1", "Q1", 3), "qb": ("Q1", "B-PE1", 4)}.items():
        ia, ib = port(a, f"to-{b}"), port(b, f"to-{a}")
        if fiber is not None:
            db.add(m.Fiber(name=f"F{fiber}", id=fiber))
        links[key] = m.Link(name=f"{a}<>{b}", a_device_id=dev[a].id, a_interface_id=ia.id, b_device_id=dev[b].id,
                            b_interface_id=ib.id, capacity_gbps=10, current_gbps=2, fiber_id=fiber)
        db.add(links[key])
    port("A-PE1", "ge-0/0/1")
    port("B-PE1", "ge-0/0/1")
    db.commit()
    return {"dev": dev, "links": links, "ifaces": ifaces}


def design(net, **kw) -> ServiceDesign:
    base = dict(service_type="l2vpn", bandwidth_mbps=1000, a_device_id=net["dev"]["A-PE1"].id, a_interface="ge-0/0/1",
                b_device_id=net["dev"]["B-PE1"].id, b_interface="ge-0/0/1", vlan_id=100,
                primary_link_ids=[net["links"]["ap"].id, net["links"]["pb"].id])
    return ServiceDesign(**{**base, **kw})


def check(report: dict, code: str) -> dict:
    return next(c for g in report["groups"] for c in g["checks"] if c["code"] == code)


def test_should_pass_when_design_is_clean(db, net):
    report = validate(db, design(net))
    assert report["status"] == "pass", [c for g in report["groups"] for c in g["checks"] if c["status"] != "pass"]


def test_should_fail_capacity_when_link_exceeds_critical_after_service(db, net):
    report = validate(db, design(net, bandwidth_mbps=7500))  # (2 + 7.5) / 10 = 95 %
    assert check(report, "CAP-UTIL")["status"] == "fail" and report["status"] == "fail"


def test_should_warn_capacity_when_link_passes_warning_threshold(db, net):
    assert check(validate(db, design(net, bandwidth_mbps=5500)), "CAP-UTIL")["status"] == "warn"  # 75 %


def test_should_not_count_bandwidth_twice_when_service_is_already_reserved(db, net):
    for key in ("ap", "pb"):
        net["links"][key].reserved_gbps = 6
    db.commit()
    # 2 current + 6 reserved (this service included) = 80 % -> warning, not 140 %.
    assert check(validate(db, design(net, bandwidth_mbps=6000, already_reserved=True)), "CAP-UTIL")["status"] == "warn"


def test_should_fail_protection_when_backup_shares_fiber(db, net):
    net["links"]["aq"].fiber_id = 1  # same fiber as A–P on the primary
    db.commit()
    report = validate(db, design(net, protection="1+1", backup_link_ids=[net["links"]["aq"].id, net["links"]["qb"].id]))
    assert check(report, "CAP-SRLG")["status"] == "fail"


def test_should_fail_protection_when_no_backup(db, net):
    assert check(validate(db, design(net, protection="1:1")), "CAP-PROT")["status"] == "fail"


def test_should_fail_port_when_access_port_is_an_uplink(db, net):
    report = validate(db, design(net, a_interface="to-P1"))
    assert check(report, "PORT-A")["status"] == "fail" and "uplink" in check(report, "PORT-A")["detail"]


def test_should_fail_mtu_when_core_mtu_below_service_need(db, net):
    net["ifaces"][("P1", "to-B-PE1")].mtu = 1500
    db.commit()
    assert check(validate(db, design(net)), "MTU-CORE")["status"] == "fail"


def test_should_fail_vlan_when_same_vlan_used_on_same_port(db, net):
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    db.add(m.Service(customer_id=1, circuit_id="C-1", service_type="l2vpn", bandwidth_mbps=100, vlan_id=100,
                     a_device_id=net["dev"]["A-PE1"].id, a_interface="ge-0/0/1", status="active"))
    db.commit()
    report = validate(db, design(net))
    assert check(report, "VLAN-CONFLICT")["status"] == "fail" and "C-1" in check(report, "VLAN-CONFLICT")["items"][0]


def test_should_require_vrf_rd_rt_when_service_is_l3vpn(db, net):
    report = validate(db, design(net, service_type="l3vpn", vlan_id=200, vrf_name="CUST", route_distinguisher="65000-1"))
    assert check(report, "VRF-SET")["status"] == "fail" and check(report, "VRF-FORMAT")["status"] == "fail"


def test_should_fail_rd_when_another_vrf_uses_it(db, net):
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    db.add(m.Service(customer_id=1, circuit_id="C-2", service_type="l3vpn", bandwidth_mbps=100, vrf_name="OTHER",
                     route_distinguisher="65000:1", status="active"))
    db.commit()
    report = validate(db, design(net, service_type="l3vpn", vrf_name="CUST", route_distinguisher="65000:1",
                                 route_target="65000:1"))
    assert check(report, "VRF-RD")["status"] == "fail"


def test_should_warn_device_readiness_when_loopback_missing(db, net):
    net["dev"]["P1"].loopback_ipv4 = None
    db.commit()
    report = validate(db, design(net))
    assert check(report, "DEV-LOOPBACK")["status"] == "warn" and check(report, "DEV-LOOPBACK")["items"] == ["P1"]


def test_should_warn_software_when_vendor_has_a_recommended_version_and_device_runs_another(db, net):
    old = m.SoftwareVersion(vendor_id=net["dev"]["P1"].software_id and db.get(m.SoftwareVersion, net["dev"]["P1"].software_id).vendor_id,
                            version="V5", is_recommended=False)
    db.add(old)
    db.flush()
    net["dev"]["P1"].software_id = old.id
    db.commit()
    assert check(validate(db, design(net)), "DEV-SW")["status"] == "warn"


def test_should_only_inform_about_software_when_vendor_has_no_recommended_version(db, net):
    vendor = m.Vendor(name="ZTE")
    db.add(vendor)
    db.flush()
    unrated = m.SoftwareVersion(vendor_id=vendor.id, version="V5.10", is_recommended=False)
    db.add(unrated)
    db.flush()
    net["dev"]["P1"].software_id = unrated.id
    db.commit()
    assert check(validate(db, design(net)), "DEV-SW")["status"] == "info"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_validate_stored_service_and_list_worst_first(client, db, net):
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    ok = m.Service(customer_id=1, circuit_id="OK-1", service_type="l2vpn", bandwidth_mbps=500, vlan_id=300,
                   a_device_id=net["dev"]["A-PE1"].id, a_interface="ge-0/0/1", b_device_id=net["dev"]["B-PE1"].id,
                   b_interface="ge-0/0/1", status="reserved")
    bad = m.Service(customer_id=1, circuit_id="BAD-1", service_type="l2vpn", bandwidth_mbps=500, status="reserved")
    db.add_all([ok, bad])
    db.flush()
    db.add(m.ServicePath(service_id=ok.id, role="primary", device_ids=[], hops=2, reserved_gbps=0.5,
                         link_ids=[net["links"]["ap"].id, net["links"]["pb"].id]))
    db.commit()
    detail = client.get(f"/api/validation/services/{ok.id}").json()
    assert detail["circuit_id"] == "OK-1" and detail["status"] == "pass"
    listing = client.get("/api/validation/services").json()
    assert [s["circuit_id"] for s in listing["services"]] == ["BAD-1", "OK-1"] and listing["counts"]["fail"] == 1


def test_should_check_unsaved_design_via_api(client, net):
    body = client.post("/api/validation/check", json={
        "service_type": "l2vpn", "bandwidth_mbps": 1000, "vlan_id": 100,
        "a_device_id": net["dev"]["A-PE1"].id, "a_interface": "ge-0/0/1",
        "b_device_id": net["dev"]["B-PE1"].id, "b_interface": "ge-0/0/1",
        "primary_link_ids": [net["links"]["ap"].id, net["links"]["pb"].id]}).json()
    assert body["status"] == "pass" and [g["group"] for g in body["groups"]] == ["kapasitas", "port", "vlan", "perangkat"]
