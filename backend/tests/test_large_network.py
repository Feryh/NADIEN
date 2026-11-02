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
    assert diag["skipped"] == {"protocol:cdp": 1, "inactive": 1, "port_not_synced": 1}


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
