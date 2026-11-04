"""MoP generator (Phase 6): vendor dialects, content per section, PDF export."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app
from app.services.mop import MopMeta, build_mop
from app.services.mop_pdf import latin1
from app.services.vendors import dialect_for
from tests.test_validation import net  # noqa: F401  (shared A — P — B network fixture)


@pytest.mark.parametrize("vendor, os_family, hostname, expected", [
    ("Huawei", None, "JKT-PE1", "vrp"),
    ("ZTE", "ZXROS", "", "zte"),
    ("Cisco", "IOS-XR", "", "iosxr"),
    ("Cisco", "IOS", "", None),
    ("FiberHome", None, "", "fiberhome"),
    (None, None, "SBY-ASR9K-PE1", "iosxr"),
])
def test_should_pick_cli_dialect_when_vendor_or_hostname_is_known(vendor, os_family, hostname, expected):
    assert dialect_for(vendor, os_family, hostname) == expected


@pytest.fixture
def service(db, net):  # noqa: F811
    huawei = db.scalar(select(m.Vendor).where(m.Vendor.name == "Huawei"))
    for name in ("A-PE1", "B-PE1"):
        net["dev"][name].vendor_id = huawei.id
    db.add(m.Customer(id=1, name="PT Contoh", account_no="A1"))
    service =m.Service(customer_id=1, circuit_id="L2-JKT/SBY-001", service_type="l2vpn", bandwidth_mbps=1000, vlan_id=300,
                        a_device_id=net["dev"]["A-PE1"].id, a_interface="ge-0/0/1", b_device_id=net["dev"]["B-PE1"].id,
                        b_interface="ge-0/0/1", status="reserved")
    db.add(service)
    db.flush()
    db.add(m.ServicePath(service_id=service.id, role="primary", device_ids=[], hops=2, reserved_gbps=1,
                         link_ids=[net["links"]["ap"].id, net["links"]["pb"].id]))
    db.commit()
    return service


def _section(doc: dict, prefix: str) -> dict:
    return next(s for s in doc["sections"] if s["title"].startswith(prefix))


def test_should_write_precheck_in_device_dialect_when_vendor_is_huawei(db, service):
    doc = build_mop(db, service, MopMeta())
    commands = [c for step in _section(doc, "7.")["steps"] for c in step["commands"]]
    assert "display interface ge-0/0/1" in commands


def test_should_mark_draft_when_validation_fails(db, net):  # noqa: F811
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    bare = m.Service(customer_id=1, circuit_id="BAD-1", service_type="l2vpn", bandwidth_mbps=500, status="reserved")
    db.add(bare)
    db.commit()
    assert build_mop(db, bare, MopMeta())["draft"] is True


def test_should_put_generated_config_before_commit_when_config_is_given(db, service):
    doc = build_mop(db, service, MopMeta(), configs={"A": {"config": ["interface ge-0/0/1.300"], "rollback": []}})
    first = _section(doc, "8.")["steps"][0]["commands"]
    assert first[0] == "interface ge-0/0/1.300" and first[-2:] == ["commit", "return"]


def test_should_warn_about_shared_port_only_when_another_service_uses_it(db, service):
    def has_note() -> bool:
        return any("dipakai bersama" in p for p in _section(build_mop(db, service, MopMeta()), "5.")["paragraphs"])

    assert not has_note()
    db.add(m.Service(customer_id=1, circuit_id="OTHER-1", service_type="l2vpn", bandwidth_mbps=100, vlan_id=301,
                     a_device_id=service.a_device_id, a_interface="ge-0/0/1", status="active"))
    db.commit()
    assert has_note()


def test_should_keep_text_printable_when_symbols_are_outside_latin1():
    assert latin1("JKT ↔ SBY — ≤ 15 menit 中") == "JKT <-> SBY - <= 15 menit ?"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_download_pdf_named_after_circuit(client, service):
    res = client.get(f"/api/mop/services/{service.id}/pdf", params={"number": "MOP-01", "executor": "Budi"})
    assert res.status_code == 200
    assert res.content.startswith(b"%PDF")
    assert 'filename="MoP-L2-JKT_SBY-001.pdf"' in res.headers["content-disposition"]


def test_should_return_404_when_service_is_missing(client, db):
    assert client.get("/api/mop/services/999").status_code == 404
