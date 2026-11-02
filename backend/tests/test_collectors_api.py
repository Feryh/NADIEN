import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app import models as m

from .conftest import config_text


@pytest.fixture
def api(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_parse_preview_writes_nothing(api, db):
    resp = api.post("/api/collectors/configs/parse?vendor=junos", content=config_text("junos.cfg"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hostname"] == "SBY-P-01" and body["bundles"]["ae30"] == ["et-0/0/2", "et-0/0/3"]
    assert db.query(m.Interface).count() == 0


def test_import_config_for_unknown_device_is_404(api):
    resp = api.post("/api/collectors/configs/NOPE-01", content=config_text("iosxr.cfg"))
    assert resp.status_code == 404


def test_import_config_applies_to_device(api, db):
    pop = m.Pop(code="SMG", name="Semarang")
    db.add(pop)
    db.flush()
    db.add(m.Device(hostname="SMG-PE-01", pop_id=pop.id))
    db.commit()
    resp = api.post("/api/collectors/configs/smg-pe-01?vendor=sros", content=config_text("sros_classic.cfg"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["bundle_members_linked"] == 2
    lag = db.query(m.Interface).filter_by(name="lag-20").one()
    assert lag.if_type == "bundle" and lag.ipv4_address == "10.1.6.1/30"


def test_empty_body_and_status_never_leaks_secrets(api, monkeypatch):
    assert api.post("/api/collectors/configs/parse", content=b"  ").status_code == 422
    from app.config import settings

    monkeypatch.setattr(settings, "observium_token", "super-secret-token")
    body = api.get("/api/collectors/status").json()
    assert body["observium"]["configured"] is True and body["observium"]["auth"] == "token"
    assert "super-secret-token" not in str(body)


def test_observium_sync_reports_missing_credentials(api, monkeypatch):
    from app.config import settings

    for attr in ("observium_token", "observium_user", "observium_password"):
        monkeypatch.setattr(settings, attr, None)
    resp = api.post("/api/collectors/observium/sync")
    assert resp.status_code == 502 and "credentials missing" in resp.json()["detail"]
