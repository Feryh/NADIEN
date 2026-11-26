"""Carrier NNI ports planned as their own capacity objects (services/nni.py, /api/capacity/nnis)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.collectors.observium import rrd
from app.database import get_db
from app.main import app
from app.services import nni


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _port(db, device, name, speed, description="", if_type="physical", rate=None):
    port = m.Interface(device_id=device.id, name=name, if_type=if_type, speed_gbps=speed, oper_status="up",
                       description=description, in_gbps=rate, out_gbps=rate / 3 if rate else None)
    db.add(port)
    db.flush()
    return port


def _link(db, a_port, b_port, capacity, current=0.2):
    link = m.Link(name=f"{a_port.name}<>{b_port.name}", a_device_id=a_port.device_id, a_interface_id=a_port.id,
                  b_device_id=b_port.device_id, b_interface_id=b_port.id, capacity_gbps=capacity, current_gbps=current,
                  status="up", discovery_source="approved")
    db.add(link)
    db.flush()
    return link


@pytest.fixture
def hub(db):
    """PLG-NIX-BNG2-M6K-8SP smartgroup84 (100G, 'NNI to XLS') to SUM-BB-SGL-0226-ME1-ZTE's 1G uplink."""
    pop = m.Pop(code="PLG", name="Palembang")
    db.add(pop)
    db.flush()
    devices = {h: m.Device(hostname=h, pop_id=pop.id) for h in (
        "PLG-NIX-BNG2-M6K-8SP", "SUM-BB-SGL-0226-ME1-ZTE", "SUM-SS-PLG-0100-ME1-ZTE", "PLG-NIX-P1-8201")}
    db.add_all(devices.values())
    db.flush()
    nni_port = _port(db, devices["PLG-NIX-BNG2-M6K-8SP"], "smartgroup84", 100, "NNI to XLS", "bundle", rate=6.0)
    site = _port(db, devices["SUM-BB-SGL-0226-ME1-ZTE"], "xgei-1/1/0/9", 1, "TO_UPLINK_XLS", rate=0.19)
    _link(db, nni_port, site, 1)
    db.commit()
    return {"devices": devices, "nni": nni_port, "site": site}


def test_should_list_a_port_described_as_nni_with_the_site_behind_it(db, hub):
    ports = nni.nni_ports(db)
    assert list(ports) == [hub["nni"].id]
    assert (ports[hub["nni"].id].kind, ports[hub["nni"].id].links[0]["far_device"]) == ("nni", "SUM-BB-SGL-0226-ME1-ZTE")


def test_should_not_list_a_bundle_whose_links_all_reach_one_box(db, hub):
    d = hub["devices"]
    lag = _port(db, d["PLG-NIX-P1-8201"], "Bundle-Ether5", 40, "LACP to OLT", "bundle")
    olt_a = _port(db, d["SUM-SS-PLG-0100-ME1-ZTE"], "xgei-1/1/0/1", 10)
    olt_b = _port(db, d["SUM-SS-PLG-0100-ME1-ZTE"], "xgei-1/1/0/2", 10)
    _link(db, lag, olt_a, 10)
    _link(db, lag, olt_b, 10)
    db.commit()
    assert lag.id not in nni.nni_ports(db)


def test_should_list_a_shared_port_that_reaches_two_boxes_as_shared(db, hub):
    d = hub["devices"]
    trunk = _port(db, d["PLG-NIX-P1-8201"], "Bundle-Ether9", 100, "to switches", "bundle")
    _link(db, trunk, _port(db, d["SUM-SS-PLG-0100-ME1-ZTE"], "xgei-1/1/0/3", 10), 10)
    _link(db, trunk, _port(db, d["SUM-BB-SGL-0226-ME1-ZTE"], "xgei-1/1/0/4", 10), 10)
    db.commit()
    assert nni.nni_ports(db)[trunk.id].kind == "shared"


def test_should_record_live_samples_only_for_nni_ports(db, hub):
    count = nni.record_live(db, {hub["nni"].id: (6.0, 2.0), hub["site"].id: (0.2, 0.1)}, datetime.now(timezone.utc))
    db.commit()
    assert count == 1 and db.query(m.PortSample).one().interface_id == hub["nni"].id


def test_should_plan_the_nni_on_its_own_capacity_and_history(db, hub):
    start = datetime.now(timezone.utc) - timedelta(days=40)
    nni.replace_history(db, hub["nni"].id, [(start + timedelta(days=d), 4.0 + d * 0.1, 1.0) for d in range(40)], start)
    db.commit()
    (row,) = nni.nni_capacity(db, start - timedelta(days=1))
    assert (row["capacity_gbps"], round(row["current_gbps"], 1), row["growth_gbps_per_month"] > 0) == (100, 7.9, True)


def test_should_mark_the_site_link_with_the_nni_it_rides(client, hub):
    (link,) = client.get("/api/capacity/links").json()
    assert (link["capacity_gbps"], link["via_nni"]) == (1, "PLG-NIX-BNG2-M6K-8SP smartgroup84")


def test_should_serve_nni_rows_from_the_capacity_api(client, hub):
    (row,) = client.get("/api/capacity/nnis").json()
    assert (row["device"], row["port"], row["kind"], len(row["sites"])) == ("PLG-NIX-BNG2-M6K-8SP", "smartgroup84",
                                                                           "nni", 1)


class _FakeSource:
    name, chunk_size = "fake", 10

    def __init__(self, days_back: int):
        self.days_back = days_back

    def usable(self, ref) -> bool:
        return True

    def fetch(self, chunk, days):
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return {str(t.link.id): [(now - timedelta(days=d), 5.0, 1.0) for d in range(self.days_back, 0, -1)]
                for t in chunk}


def test_should_import_daily_peaks_of_an_nni_port_from_rrd(db, hub):
    db.add(m.ObserviumPort(interface_id=hub["nni"].id, source="main", observium_port_id=1, if_index=84,
                           observium_hostname="plg-nix-bng2"))
    db.commit()
    report: dict = {}
    assert rrd._sync_nni_ports(db, _FakeSource(30), "main", 2, 90, report) == 1
    assert db.query(m.PortSample).filter_by(interface_id=hub["nni"].id).count() == 30
