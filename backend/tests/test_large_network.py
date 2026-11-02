"""Behaviour found on the first real sync: ~3.5k devices, ~300k ports, two hostname conventions."""

import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.parsers import guess_role, infer_pop
from app.collectors.observium.sync import sync_observium
from app.collectors.runner import JobBusy
from app.database import get_db
from app.main import app
from app.services.topology import discover_links

from .conftest import observium_transport

KNOWN = {"JKT", "BKS", "PBR", "BTM", "SMG", "BGR", "OLT", "PEIN"}  # last two: bad POPs from an old sync


@pytest.mark.parametrize("hostname,pop", [
    ("OLT2-MA5603T-JKT-BSG", "JKT"),
    ("OLT11-MA5800-X15-JKTS-TND", "JKTS"),  # unknown code: first 3-4 letter token after role/model
    ("PE1-ATN910-PBR-MSC", "PBR"),
    ("CGW1-NAP-BTM-NDC", "BTM"),  # known code beats an earlier unknown token
    ("DS27-UTP-EX34NDC-JKT2", "JKT"),  # JKT2 -> JKT
    ("PE1-ATN910-CIBISNINE", None),
    ("PAPP0004IT01", None),
    ("OLT2-MA5800-X15-KPG-AST", "KPG"),  # "OLT"/"PEIN" are role words even when listed as known
    ("PEIN1-H-JKTT-CPY1", "JKTT"),
])
def test_infer_pop_role_first_hostnames(hostname, pop):
    assert infer_pop(hostname, KNOWN) == pop


@pytest.mark.parametrize("hostname,role", [
    ("JKT-ABW-OLT1-HW", "olt"),
    ("OLT2-C300-BKS-ART", "olt"),
    ("IDM-IDM-BNG1-M4", "bng"),
    ("BKS-LBJ-ME1-NCS540", "agg"),
    ("DS1-S6730-BGR-BOB", "agg"),
    ("PE1-ATN910-PTT-WTR", "pe"),
    ("PEIN1-H-JKTT-CPY1", "pe"),
    ("P1-NCS5504-SMG-RMH-BALI", "p"),
    ("SW-STREAM-2A-NDC", "access"),
    ("JKT-CSF-CLDMGMT-02", "pe"),  # nothing recognisable -> default
])
def test_roles_from_hostname_tokens(hostname, role):
    assert guess_role(hostname) == role


def _transport_with(devices: dict, neighbours_key: str = "neighbours") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.removeprefix("/api/v0/").strip("/")
        if entity == "devices":
            return httpx.Response(200, json={"status": "ok", "count": len(devices), "devices": devices})
        if entity == "neighbours":
            rows = {"1": {"device_id": "1", "port_id": "11", "protocol": "lldp", "remote_hostname": "X", "remote_port": "y"}}
            return httpx.Response(200, json={"status": "ok", "count": 1, neighbours_key: rows})
        key = "addresses" if entity == "address" else entity
        return httpx.Response(200, json={"status": "ok", "count": 0, key: {}})

    return httpx.MockTransport(handler)


def test_role_first_devices_get_pop_and_unassigned_moves(db):
    devices = {
        "1": {"device_id": "1", "sysName": "JKT-ABW-OLT1-HW", "os": "vrp"},
        "2": {"device_id": "2", "sysName": "OLT2-MA5603T-JKT-BSG", "os": "vrp"},
        "3": {"device_id": "3", "sysName": "PE1-ATN910-CIBISNINE", "os": "vrp"},
    }
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_with(devices))
    report = sync_observium(db, c, {"inventory"}, discover=False)
    pops = {d.hostname: db.get(m.Pop, d.pop_id).code for d in db.scalars(select(m.Device))}
    assert pops == {"JKT-ABW-OLT1-HW": "JKT", "OLT2-MA5603T-JKT-BSG": "JKT", "PE1-ATN910-CIBISNINE": "UNASSIGNED"}
    assert report.counts["pops_inferred"] == 1 and report.counts["devices_unassigned"] == 1
    assert {d.hostname: d.role for d in db.scalars(select(m.Device))}["OLT2-MA5603T-JKT-BSG"] == "olt"


def test_unassigned_device_is_reassigned_when_pop_inferable(db):
    pop = m.Pop(code="UNASSIGNED", name="UNASSIGNED")
    db.add(pop)
    db.flush()
    db.add(m.Device(hostname="OLT2-C300-BKS-ART", pop_id=pop.id, role="olt"))
    db.add(m.Pop(code="BKS", name="Bekasi"))
    db.commit()
    devices = {"1": {"device_id": "1", "sysName": "OLT2-C300-BKS-ART", "os": "zte"}}
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_with(devices))
    report = sync_observium(db, c, {"inventory"}, discover=False)
    dev = db.scalar(select(m.Device))
    assert db.get(m.Pop, dev.pop_id).code == "BKS" and report.counts["devices_pop_assigned"] == 1


def test_neighbour_diagnostics_and_alternate_collection_key(db):
    devices = {"1": {"device_id": "1", "sysName": "JKT-P-01", "os": "iosxr"}}
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_with(devices, "neighbors"))
    report = sync_observium(db, c, {"inventory", "neighbours"}, discover=False).as_dict()
    diag = report["diagnostics"]["neighbours"]
    assert diag["fetched"] == 1  # read from "neighbors" although the docs say "neighbours"
    assert diag["skipped"] == {"port_not_synced": 1}
    assert "remote_hostname" in diag["row_fields"]
    assert report["diagnostics"]["observium_responses"]["neighbours"]["response_keys"] == ["count", "neighbors", "status"]


def test_full_fixture_diagnostics(db):
    c = ObserviumClient(base_url="http://o.test", token="t", transport=observium_transport())
    diag = sync_observium(db, c).as_dict()["diagnostics"]["neighbours"]
    assert diag["fetched"] == 10
    assert diag["protocols"] == {"lldp": 9, "cdp": 1}
    assert diag["skipped"] == {"inactive": 1, "port_not_synced": 1}  # CDP is used now
    # SBY et-0/0/1 has both an LLDP (CUST-CPE-9) and a CDP (SMG-PE-01) entry: LLDP wins.
    sby = db.scalar(select(m.Device).where(m.Device.hostname == "SBY-P-01"))
    rows = db.scalars(select(m.LldpNeighbor).where(m.LldpNeighbor.device_id == sby.id,
                                                   m.LldpNeighbor.local_interface == "et-0/0/1")).all()
    assert [r.remote_system_name for r in rows] == ["CUST-CPE-9"]


def _transport_rows(devices: dict, ports: dict, neighbours: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.removeprefix("/api/v0/").strip("/")
        data = {"devices": devices, "ports": ports, "neighbours": neighbours}.get(entity, {})
        key = {"address": "addresses", "neighbours": "entries"}.get(entity, entity)  # real Observium: "entries"
        return httpx.Response(200, json={"status": "ok", "count": len(data), key: data})

    return httpx.MockTransport(handler)


def _port(pid, did, name, speed=100000, desc=""):
    return {"port_id": pid, "device_id": did, "ifName": name, "ifHighSpeed": str(speed),
            "ifOperStatus": "up", "ifAdminStatus": "up", "ifAlias": desc}


def test_remote_resolved_by_observium_port_id_and_mgmt_ip(db):
    devices = {
        "1": {"device_id": "1", "sysName": "JKT-NDC-BNG2-M4", "os": "vrp", "ip": "10.9.0.1"},
        "2": {"device_id": "2", "sysName": "JKT-KMY-ME1-NCS540", "os": "iosxr", "ip": "10.9.0.2"},
        "3": {"device_id": "3", "sysName": "BTM-NIX-ME1-HW", "os": "vrp", "ip": "10.9.0.3"},
    }
    ports = {
        "11": _port("11", "1", "100GE0/5/1"), "12": _port("12", "1", "100GE0/5/2"),
        "21": _port("21", "2", "HundredGigE0/0/0/1"), "31": _port("31", "3", "100GE1/0/1"),
    }
    neighbours = {
        # Advertised short port name + odd system name, but Observium resolved the far port (21).
        "1": {"device_id": "1", "port_id": "11", "protocol": "lldp", "remote_hostname": "kmy-me1.lab",
              "remote_port": "Hu0/0/0/1", "remote_port_id": "21", "active": "1"},
        # No port resolution, but the management IP identifies the device.
        "2": {"device_id": "1", "port_id": "12", "protocol": "cdp", "remote_hostname": "BTM-NIX-DCGW01",
              "remote_port": "100GE1/0/1", "remote_address": "10.9.0.3", "active": "1"},
    }
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_rows(devices, ports, neighbours))
    report = sync_observium(db, c, {"inventory", "neighbours"}).as_dict()
    assert report["diagnostics"]["neighbours"]["resolved"] == {"by_observium_port_id": 1, "by_management_ip": 1}
    disc = report["discovery"]
    assert disc["lldp_links"] == 2 and disc["interfaces_created"] == 0 and disc["unresolved_neighbors"] == []
    names = {i.name for i in db.scalars(select(m.Interface))}
    assert "Hu0/0/0/1" not in names  # no duplicate port from the short LLDP name


def test_strip_domain_keeps_dotted_hostnames():
    from app.services.topology import strip_domain

    assert strip_domain("jkt-p-01.metro.net") == "jkt-p-01"
    assert strip_domain("JKT-P-01.telkom.co.id") == "JKT-P-01"
    assert strip_domain("R6.BAM.LEAF-CNDC.02") == "R6.BAM.LEAF-CNDC.02"
    assert strip_domain("10.1.2.3") == "10.1.2.3"


def test_device_stored_under_old_dot_cut_name_is_renamed(db):
    pop = m.Pop(code="UNASSIGNED", name="UNASSIGNED")
    db.add(pop)
    db.flush()
    db.add(m.Device(hostname="R6", pop_id=pop.id))
    db.commit()
    devices = {"1": {"device_id": "1", "sysName": "R6.BAM.LEAF-CNDC.02", "os": "vrp"}}
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_rows(devices, {}, {}))
    report = sync_observium(db, c, {"inventory"}, discover=False)
    assert [d.hostname for d in db.scalars(select(m.Device))] == ["R6.BAM.LEAF-CNDC.02"]
    assert report.counts["devices_renamed"] == 1


def test_stale_config_links_and_phantom_ports_are_cleaned(db):
    devices = {"1": {"device_id": "1", "sysName": "AAA-X-PE1", "os": "vrp"},
               "2": {"device_id": "2", "sysName": "AAA-Y-PE1", "os": "vrp"}}
    ports = {"11": _port("11", "1", "100GE0/0/1", desc="TO AAA-Y-PE1 100GE0/0/9"),
             "21": _port("21", "2", "100GE0/0/9", desc="TO AAA-X-PE1 100GE0/0/1"),
             "12": _port("12", "1", "100GE0/0/2"), "22": _port("22", "2", "100GE0/0/2")}
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_rows(devices, ports, {}))
    first = sync_observium(db, c, {"inventory", "neighbours"}).as_dict()["discovery"]
    assert first["config_links"] == 1  # descriptions only

    # A phantom port left by an older run (name only, unknown to Observium) with a link on it.
    x = db.scalar(select(m.Device).where(m.Device.hostname == "AAA-X-PE1"))
    y = db.scalar(select(m.Device).where(m.Device.hostname == "AAA-Y-PE1"))
    ghost = m.Interface(device_id=y.id, name="Hu0/0/2", oper_status="up")
    db.add(ghost)
    db.flush()
    x_p2 = db.scalar(select(m.Interface).where(m.Interface.device_id == x.id, m.Interface.name == "100GE0/0/2"))
    db.add(m.Link(name="old", a_device_id=x.id, a_interface_id=x_p2.id, b_device_id=y.id, b_interface_id=ghost.id,
                  discovery_source="lldp", capacity_gbps=10))
    db.commit()

    # Now LLDP sees the real adjacency on the other ports.
    neighbours = {"1": {"device_id": "1", "port_id": "12", "protocol": "lldp", "remote_hostname": "AAA-Y-PE1",
                        "remote_port": "100GE0/0/2", "remote_port_id": "22", "active": "1"}}
    c = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_rows(devices, ports, neighbours))
    second = sync_observium(db, c, {"inventory", "neighbours"}).as_dict()["discovery"]
    assert second["lldp_links"] == 1 and second["config_links"] == 0
    assert second["superseded"] == 2  # config link (pair now seen by LLDP) + link on the phantom port
    assert second["interfaces_removed"] == 1 and second["marked_down"] == 0
    links = db.scalars(select(m.Link)).all()
    assert len(links) == 1 and links[0].discovery_source == "lldp" and links[0].capacity_gbps == 100
    assert db.get(m.Interface, ghost.id) is None


def test_phantom_cleanup_never_touches_hand_made_devices(db):
    pop = m.Pop(code="LAB", name="Lab")
    db.add(pop)
    db.flush()
    dev = m.Device(hostname="LAB-SW-01", pop_id=pop.id)
    db.add(dev)
    db.flush()
    db.add(m.Interface(device_id=dev.id, name="eth9"))
    db.commit()
    discover_links(db, valid_iface_ids=set())
    assert db.scalar(select(m.Interface).where(m.Interface.name == "eth9")) is not None


def test_config_discovery_is_linear_on_a_large_network(db):
    """2,000 devices / 40,000 described ports must discover in seconds (was quadratic)."""
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    devices = [m.Device(hostname=f"JKT-S{i:04d}-ME1-HW", pop_id=pop.id) for i in range(2000)]
    db.add_all(devices)
    db.flush()
    ifaces = []
    for i, dev in enumerate(devices):
        peer = devices[(i + 1) % len(devices)]
        prev = devices[i - 1]
        ifaces.append(m.Interface(device_id=dev.id, name="Gi0/0/1", speed_gbps=10, oper_status="up",
                                  description=f"TO_{peer.hostname}_Gi0/0/2"))
        ifaces.append(m.Interface(device_id=dev.id, name="Gi0/0/2", speed_gbps=10, oper_status="up",
                                  description=f"TO {prev.hostname} Gi0/0/1"))
        ifaces.extend(m.Interface(device_id=dev.id, name=f"Gi0/1/{k}", description=f"CUST-PT-ABC-{i}-{k} 100M")
                      for k in range(18))
    db.add_all(ifaces)
    db.commit()

    started = time.monotonic()
    result = discover_links(db)
    elapsed = time.monotonic() - started
    assert result.config_links == 2000 and result.created == 2000
    assert elapsed < 60, f"discovery took {elapsed:.1f}s"


def test_background_job_and_busy(db, job_runner, monkeypatch):
    from app.routers import collectors

    monkeypatch.setattr(collectors, "ObserviumClient",
                        lambda: ObserviumClient(base_url="http://o.test", token="t", transport=observium_transport()))
    app.dependency_overrides[get_db] = lambda: db
    try:
        api = TestClient(app)
        started = api.post("/api/collectors/observium/sync")
        job = job_runner.wait(timeout=30)
    finally:
        app.dependency_overrides.clear()
    assert started.status_code == 202
    assert job["status"] == "ok", job.get("error")
    assert job["report"]["counts"]["devices_created"] == 4
    assert job["report"]["backup"]["status"] == "skipped"  # in-memory test DB

    gate = threading.Event()
    job_runner.start("slow", lambda _db, _p: (gate.wait(5), {})[1])
    with pytest.raises(JobBusy):
        job_runner.start("second", lambda _db, _p: {})
    gate.set()
    assert job_runner.wait(5)["status"] == "ok"


def test_failed_job_is_reported(job_runner):
    def boom(_db, progress):
        progress("ports")
        raise RuntimeError("observium went away")

    job_runner.start("observium", boom)
    job = job_runner.wait(5)
    assert job["status"] == "failed" and "observium went away" in job["error"] and job["phase"] == "done"
    assert job_runner.state_file.exists()
