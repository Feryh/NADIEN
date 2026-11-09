from datetime import datetime

import pytest
from sqlalchemy import select

from app import models as m
from app.collectors.configs import parse_config
from app.collectors.configs.apply import apply_config
from app.collectors.observium.client import ObserviumClient, ObserviumError
from app.collectors.observium.sync import sync_observium
from app.config import settings

from .conftest import config_text, observium_transport


def client(**kw) -> ObserviumClient:
    return ObserviumClient(base_url="http://observium.test", token="test-token", transport=observium_transport(**kw))


def links_by_pair(db) -> dict[frozenset[str], m.Link]:
    host = {d.id: d.hostname for d in db.scalars(select(m.Device))}
    return {frozenset((host[l.a_device_id], host[l.b_device_id])): l for l in db.scalars(select(m.Link))}


def test_full_sync_builds_inventory_topology_and_utilization(db):
    report = sync_observium(db, client())
    c = report.counts

    hosts = {d.hostname: d for d in db.scalars(select(m.Device))}
    assert set(hosts) == {"JKT-P-01", "SBY-P-01", "BDG-PE-01", "SMG-PE-01"}  # server skipped
    assert c["devices_created"] == 4
    assert hosts["SMG-PE-01"].status == "maintenance"  # Observium status 0
    assert hosts["JKT-P-01"].role == "p" and hosts["BDG-PE-01"].role == "pe"

    pops = {p.code: p for p in db.scalars(select(m.Pop))}
    assert set(pops) == {"JKT", "SBY", "BDG", "SMG"}
    assert pops["JKT"].latitude == pytest.approx(-6.2088)

    vendor = {v.id: v.name for v in db.scalars(select(m.Vendor))}
    assert vendor[hosts["SBY-P-01"].vendor_id] == "Juniper"
    model = db.get(m.DeviceModel, hosts["JKT-P-01"].model_id)
    assert model.name == "ASR-9906"

    ifaces = {(i.device_id, i.name): i for i in db.scalars(select(m.Interface))}
    jkt = hosts["JKT-P-01"].id
    assert (jkt, "Loopback0") not in ifaces and (jkt, "MgmtEth0/RP0/CPU0/0") not in ifaces
    assert (jkt, "HundredGigE0/0/0/9") not in ifaces  # deleted in Observium
    hu0 = ifaces[(jkt, "HundredGigE0/0/0/0")]
    assert hu0.speed_gbps == 400 and hu0.description == "TO-SBY-P-01" and hu0.ipv4_address == "10.1.0.1/30"
    assert ifaces[(jkt, "Bundle-Ether10")].if_type == "bundle"
    assert ifaces[(hosts["SBY-P-01"].id, "et-0/0/0.0")].if_type == "subinterface"

    # LLDP only (CDP row ignored); inactive neighbour and neighbour on skipped Loopback dropped.
    assert c["neighbours"] == 7
    disc = report.discovery
    assert disc["lldp_links"] == 4
    assert any("CUST-CPE-9" in u for u in disc["unresolved_neighbors"])

    links = links_by_pair(db)
    core = links[frozenset(("JKT-P-01", "SBY-P-01"))]
    assert core.capacity_gbps == 400 and core.discovery_source == "lldp"
    assert core.current_gbps == 250  # 31.25 GB/s * 8
    assert c["utilization_samples"] == 4  # SMG–SBY has a rate on the SMG side only, still counted

    samples = db.scalars(select(m.UtilizationSample).where(m.UtilizationSample.link_id == core.id)).all()
    assert len(samples) == 1 and samples[0].in_gbps == 250

    source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == "Observium"))
    assert source.last_status.startswith("ok: 4 devices")


def test_sync_is_idempotent_and_keeps_curated_fields(db):
    sync_observium(db, client())
    jkt = db.scalar(select(m.Device).where(m.Device.hostname == "JKT-P-01"))
    jkt.role = "dci"
    db.commit()

    report = sync_observium(db, client())
    assert report.counts["devices_created"] == 0 and report.counts["interfaces_created"] == 0
    assert db.get(m.Device, jkt.id).role == "dci"
    assert len(db.scalars(select(m.Link)).all()) == 4
    core = links_by_pair(db)[frozenset(("JKT-P-01", "SBY-P-01"))]
    assert len(db.scalars(select(m.UtilizationSample).where(m.UtilizationSample.link_id == core.id)).all()) == 2


def test_utilization_only_updates_rates_without_touching_inventory(db):
    sync_observium(db, client())
    n_ifaces = len(db.scalars(select(m.Interface)).all())
    report = sync_observium(db, client(port_rates={"1001": (37_500_000_000, 1)}), parts={"utilization"})
    assert report.counts["devices_created"] == 0 and report.counts.get("interfaces_updated", 0) == 0
    assert len(db.scalars(select(m.Interface)).all()) == n_ifaces
    assert links_by_pair(db)[frozenset(("JKT-P-01", "SBY-P-01"))].current_gbps == 300


def test_should_store_port_rate_on_interface_when_utilization_is_synced(db):
    sync_observium(db, client())
    sync_observium(db, client(port_rates={"1001": (37_500_000_000, 1)}), parts={"utilization"})
    hu0 = db.scalar(select(m.Interface).where(m.Interface.name == "HundredGigE0/0/0/0"))
    assert hu0.in_gbps == 300


def test_config_import_promotes_member_links_to_bundle(db):
    sync_observium(db, client())
    assert frozenset(("JKT-P-01", "BDG-PE-01")) in links_by_pair(db)
    member_links = [l for l in db.scalars(select(m.Link)) if l.link_type == "metro_e"]
    assert len(member_links) == 4  # JKT–BDG seen as two separate 100G links before configs

    hosts = {d.hostname: d for d in db.scalars(select(m.Device))}
    r1 = apply_config(db, hosts["JKT-P-01"], parse_config(config_text("iosxr.cfg"), "iosxr"))
    r2 = apply_config(db, hosts["BDG-PE-01"], parse_config(config_text("vrp.cfg"), "vrp"))
    assert r1["counts"]["bundle_members_linked"] == 2 and r2["counts"]["bundle_members_linked"] == 2
    assert hosts["JKT-P-01"].loopback_ipv4 == "10.0.0.1" and hosts["JKT-P-01"].srv6_locator == "fc00:0:1::/48"
    names = {i.name for i in db.scalars(select(m.Interface).where(m.Interface.device_id == hosts["JKT-P-01"].id))}
    assert "Loopback0" not in names and "MgmtEth0/RP0/CPU0/0" not in names  # config IPs don't create them
    assert "HundredGigE0/0/0/8" in names  # preconfigured port with a description is created

    from app.services.topology import discover_links

    result = discover_links(db)
    assert result.superseded == 2
    links = [l for l in db.scalars(select(m.Link))]
    bundle = [l for l in links if l.link_type == "bundle"]
    assert len(links) == 3 and len(bundle) == 1
    assert bundle[0].capacity_gbps == 200 and bundle[0].status == "up"


def test_missing_credentials_and_auth_failure(monkeypatch):
    monkeypatch.setattr(settings, "observium_token", None)
    monkeypatch.setattr(settings, "observium_user", None)
    monkeypatch.setattr(settings, "observium_password", None)
    with pytest.raises(ObserviumError, match="credentials missing"):
        ObserviumClient(base_url="http://observium.test")
    bad = ObserviumClient(base_url="http://observium.test", token="x", transport=observium_transport(status=401))
    with pytest.raises(ObserviumError, match="401"):
        bad.fetch("devices")


def test_pagination_collects_every_page(db):
    rows = client().fetch("ports", page_size=3)
    assert len(rows) == 16


def test_should_store_reachability_when_device_synced(db):
    sync_observium(db, client())
    hosts = {d.hostname: d for d in db.scalars(select(m.Device))}
    down = hosts["SMG-PE-01"]
    assert down.reachability == "down"
    assert down.uptime_s == 7200
    assert down.last_polled_at.replace(tzinfo=None) == datetime(2026, 11, 9, 3, 0)
    assert hosts["JKT-P-01"].reachability == "up"
    assert hosts["JKT-P-01"].uptime_s is None
