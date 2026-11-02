import posixpath

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.collectors.configs.rancid import LocalFS, RancidError, find_files, open_from_settings, rancid_hint, sync_rancid
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.sync import sync_observium
from app.config import settings
from app.database import get_db
from app.main import app

from .conftest import config_text, observium_transport

IOS_CLASSIC = "!RANCID-CONTENT-TYPE: cisco\n!\nhostname YGY-AGG-01\ninterface TenGigabitEthernet0/1\n description x\n"


def build_archive(base):
    """Classic RANCID layout: <base>/var/<group>/configs/<router.db name>."""
    metro = base / "var" / "metro"
    access = base / "var" / "access"
    for d in (metro / "configs", metro / "CVS", access / "configs", base / "logs"):
        d.mkdir(parents=True)
    (metro / "router.db").write_text("jkt-p-01.metro.net;cisco-xr;up\n")
    (metro / "configs" / ".cvsignore").write_text("")
    (metro / "configs" / "jkt-p-01.metro.net").write_text(
        "!RANCID-CONTENT-TYPE: cisco-xr\n!\n!Chassis type: ASR-9906\n" + config_text("iosxr.cfg"))
    (metro / "configs" / "172.16.0.3").write_text("!RANCID-CONTENT-TYPE: vrp\n" + config_text("vrp.cfg"))
    (metro / "configs" / "SBY-P-01").write_text(
        "#RANCID-CONTENT-TYPE: juniper\n#\n# Chassis MX10008\n# Routing Engine 0\n" + config_text("junos.cfg"))
    (metro / "configs" / "old-router").write_text("!RANCID-CONTENT-TYPE: cisco-xr\nhostname OLD\n")
    (access / "configs" / "YGY-AGG-01").write_text(IOS_CLASSIC)
    (base / "logs" / "metro.20261102").write_text("log")


@pytest.fixture
def inventory(db):
    sync_observium(db, ObserviumClient(base_url="http://observium.test", token="t", transport=observium_transport()))
    smg = db.scalar(select(m.Device).where(m.Device.hostname == "SMG-PE-01"))
    db.add(m.Device(hostname="YGY-AGG-01", pop_id=smg.pop_id))
    db.commit()
    return db


def test_rancid_hint():
    assert rancid_hint("!RANCID-CONTENT-TYPE: cisco-xr\n") == "iosxr"
    assert rancid_hint("#RANCID-CONTENT-TYPE: juniper\n") == "junos"
    assert rancid_hint("!RANCID-CONTENT-TYPE: vrp\n") == "vrp"
    assert rancid_hint("#RANCID-CONTENT-TYPE: sros\n") == "sros"
    assert rancid_hint("!RANCID-CONTENT-TYPE: cisco\n") is None
    assert rancid_hint("hostname X\n") is None


def test_find_files_skips_rancid_housekeeping(tmp_path):
    build_archive(tmp_path)
    files = find_files(LocalFS(), str(tmp_path))
    assert sorted(f.name for f in files) == ["172.16.0.3", "SBY-P-01", "YGY-AGG-01", "jkt-p-01.metro.net", "old-router"]
    assert {f.group for f in files} == {"metro", "access"}


def test_flat_configs_directory(tmp_path):
    (tmp_path / "SBY-P-01").write_text(config_text("junos.cfg"))
    assert [f.name for f in find_files(LocalFS(), str(tmp_path))] == ["SBY-P-01"]


def test_find_files_over_posix_paths():
    """Same walk SFTP uses: forward-slash paths, (name, is_dir) listings."""
    tree = {
        "/opt/observium/rancid": [("metro", True), ("README", False)],
        "/opt/observium/rancid/metro": [("configs", True), ("router.db", False)],
        "/opt/observium/rancid/metro/configs": [("JKT-P-01", False), ("JKT-P-01,v", False)],
    }

    class FakeFS:
        def listdir(self, path):
            return tree.get(path, [])

        def read(self, path):
            return ""

        def join(self, *parts):
            return posixpath.join(*parts)

    files = find_files(FakeFS(), "/opt/observium/rancid")
    assert [(f.name, f.group, f.path) for f in files] == [
        ("JKT-P-01", "metro", "/opt/observium/rancid/metro/configs/JKT-P-01")
    ]


def test_sync_rancid_applies_configs(inventory, tmp_path):
    db = inventory
    build_archive(tmp_path)
    report = sync_rancid(db, LocalFS(), str(tmp_path))

    assert report["files"] == 5 and report["applied"] == 3
    assert report["unmatched_files"] == ["old-router"]
    assert report["unsupported"] == ["YGY-AGG-01 (RANCID type: cisco)"]
    assert {r["device"] for r in report["results"]} == {"JKT-P-01", "BDG-PE-01", "SBY-P-01"}  # BDG via mgmt IP

    hosts = {d.hostname: d for d in db.scalars(select(m.Device))}
    assert hosts["JKT-P-01"].loopback_ipv4 == "10.0.0.1"
    assert hosts["SBY-P-01"].srv6_locator == "fc00:0:4::/48"
    assert hosts["BDG-PE-01"].isis_net == "49.0001.0100.0000.0004.00"

    # LAG membership from configs turns the two JKT–BDG member links into one bundle link.
    assert report["discovery"]["superseded"] == 2
    bundles = [l for l in db.scalars(select(m.Link)) if l.link_type == "bundle"]
    assert len(bundles) == 1 and bundles[0].capacity_gbps == 200


def test_not_configured(monkeypatch, db):
    for attr in ("rancid_path", "rancid_ssh_host", "rancid_ssh_user"):
        monkeypatch.setattr(settings, attr, None)
    with pytest.raises(RancidError, match="not configured"):
        open_from_settings()
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).post("/api/collectors/configs/rancid/sync")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400 and "NADIEN_RANCID_PATH" in resp.json()["detail"]


def test_endpoint_runs_in_background(monkeypatch, inventory, job_runner, tmp_path):
    build_archive(tmp_path)
    monkeypatch.setattr(settings, "rancid_path", str(tmp_path))
    app.dependency_overrides[get_db] = lambda: inventory
    try:
        client = TestClient(app)
        status = client.get("/api/collectors/status").json()
        resp = client.post("/api/collectors/configs/rancid/sync")
        job = job_runner.wait(timeout=30)
        polled = client.get("/api/collectors/jobs/current").json()
    finally:
        app.dependency_overrides.clear()
    assert status["rancid"]["configured"] is True
    assert resp.status_code == 202 and resp.json()["status"] == "running"
    assert job["status"] == "ok" and job["report"]["applied"] == 3, job.get("error")
    assert polled["id"] == job["id"] and polled["phase"] == "done"
