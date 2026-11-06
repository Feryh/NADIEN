"""Second Observium for cell-site routers: only CSRs come in, and the two servers do not overwrite each other."""

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.routers import collectors as collectors_router
from app.collectors.configs.gitweb import _Sync
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.rrd import _targets, sync_rrd
from app.collectors.observium.sync import CSR_PARTS, sync_observium
from app.database import get_db
from app.main import app

CSR = "sum-la-gns-0006-me1-zte"


def _client(devices: dict, ports: dict) -> ObserviumClient:
    rows = {"devices": devices, "ports": ports}

    def handler(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.removeprefix("/api/v0/").strip("/")
        key = "addresses" if entity == "address" else entity
        data = rows.get(key, {})
        query = parse_qs(request.url.query.decode())
        items = list(data.items())
        if "pagesize" in query:
            size, page = int(query["pagesize"][0]), int(query.get("pageno", ["1"])[0])
            items = items[(page - 1) * size : page * size]
        return httpx.Response(200, json={"status": "ok", "count": len(data), key: dict(items)})

    return ObserviumClient(base_url="http://o.test", token="t", transport=httpx.MockTransport(handler))


def _port(port_id: str, device_id: str, name: str) -> dict:
    return {"port_id": port_id, "device_id": device_id, "ifName": name, "ifType": "ethernetCsmacd", "ifHighSpeed": 10000,
            "ifAdminStatus": "up", "ifOperStatus": "up", "ifIndex": 7, "ifInOctets_rate": "125000000", "ifOutOctets_rate": "0"}


def _csr_sync(db) -> None:
    devices = {"1": {"device_id": "1", "sysName": CSR, "hostname": CSR, "os": "zxr10"},
               "2": {"device_id": "2", "sysName": "tng-bsd-bm-compute-09", "hostname": "bm9", "os": "linux", "type": "network"}}
    ports = {"5": _port("5", "1", "xgei-1/1/0/1"), "6": _port("6", "2", "eth0")}
    sync_observium(db, _client(devices, ports), CSR_PARTS, False, source="csr")


def test_should_take_only_cell_site_routers_from_the_csr_observium(db):
    _csr_sync(db)
    assert [d.hostname for d in db.scalars(select(m.Device))] == [CSR.upper()]
    assert db.scalar(select(m.Device)).role == "csr"


def test_should_keep_csr_port_map_when_the_main_observium_syncs(db):
    _csr_sync(db)
    devices = {"1": {"device_id": "1", "sysName": "JKT-PE1", "hostname": "JKT-PE1", "os": "iosxr"}}
    # Same port id as the CSR's port: ids of the two servers overlap.
    sync_observium(db, _client(devices, {"5": _port("5", "1", "Hu0/0/0/0")}), {"inventory"}, False)
    refs = {(r.source, r.observium_port_id) for r in db.scalars(select(m.ObserviumPort))}
    assert refs == {("csr", 5), ("main", 5)}


def test_should_not_ask_main_rrd_history_for_a_csr_port(db):
    _csr_sync(db)
    iface = db.scalar(select(m.Interface))
    db.add(m.Link(name="x", a_device_id=iface.device_id, a_interface_id=iface.id, b_device_id=iface.device_id))
    db.commit()
    targets, unmapped = _targets(db, SimpleNamespace(usable=lambda ref: True))
    assert targets == [] and unmapped == 1


def _link_between(db, a: m.Interface | None, b: m.Interface | None) -> m.Link:
    device = (a or b).device_id
    link = m.Link(name=f"l{a and a.id}-{b and b.id}", a_device_id=device, a_interface_id=a.id if a else None,
                  b_device_id=device, b_interface_id=b.id if b else None)
    db.add(link)
    return link


def test_should_read_csr_history_only_for_links_the_main_server_cannot_see(db):
    _csr_sync(db)
    csr_port = db.scalar(select(m.Interface))
    main_port = m.Interface(device_id=csr_port.device_id, name="Hu0/0/0/0")
    plain_port = m.Interface(device_id=csr_port.device_id, name="Hu0/0/0/1")
    db.add_all([main_port, plain_port])
    db.flush()
    db.add(m.ObserviumPort(interface_id=main_port.id, observium_port_id=5, observium_hostname="pe", source="main"))
    only_csr = _link_between(db, plain_port, csr_port)
    _link_between(db, main_port, csr_port)  # the main server serves this one
    _link_between(db, main_port, plain_port)
    db.commit()
    targets, unmapped = _targets(db, SimpleNamespace(usable=lambda ref: True), "csr")
    assert [(t.link.id, t.swapped, t.ref.source) for t in targets] == [(only_csr.id, True, "csr")] and unmapped == 0


def test_should_store_csr_daily_peaks_on_the_link_through_the_csr_port(db):
    _csr_sync(db)
    port = db.scalar(select(m.Interface))
    link = _link_between(db, port, None)
    db.commit()
    day = datetime(2026, 11, 1, tzinfo=timezone.utc)
    source = SimpleNamespace(name="api", chunk_size=10, usable=lambda ref: True,
                             fetch=lambda chunk, days: {str(link.id): [(day, 0.5, 0.2)]})
    report = sync_rrd(db, source, 90, server="csr")
    assert report["links_with_history"] == 1 and link.current_gbps == 0.5


def test_should_keep_observium_status_when_gitweb_rereads_a_csr_config(db):
    _csr_sync(db)
    device = db.scalar(select(m.Device))
    reported = db.scalar(select(m.Interface))
    reported.oper_status, reported.description = "down", "to OLT"
    config_port = m.Interface(device_id=device.id, name="xgei-1/1/0/9", oper_status="down", description="from config")
    db.add(config_port)
    db.commit()
    _Sync._config_only_ports(SimpleNamespace(db=db), device)
    assert reported.oper_status == "down" and config_port.oper_status == "up"


def test_should_count_csr_ports_in_port_capacity(db):
    _csr_sync(db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        body = TestClient(app).get("/api/inventory/ports/summary", params={"category": "csr"}).json()
    finally:
        app.dependency_overrides.clear()
    assert body["totals"]["total"] == 1 and body["by_speed"][0]["speed"] == "10G"


def test_should_test_the_csr_observium_address_when_asked(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, base_url=None):
            calls.append(base_url)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def _get(self, *_):
            return {"count": 3}

    monkeypatch.setattr("app.routers.collectors.ObserviumClient", Fake)
    body = collectors_router.test_connection(target="observium_csr")
    assert body["ok"] is True and calls == [settings.observium_csr_url]
