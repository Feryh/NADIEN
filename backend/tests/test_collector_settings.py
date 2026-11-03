"""Pengaturan Collector: UI-saved connection settings over backend/.env, secrets encrypted."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import database
from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.services import collector_settings


@pytest.fixture
def client(db, monkeypatch):
    # Every test gets the original settings back, whatever the UI saved.
    for spec in collector_settings.FIELDS:
        monkeypatch.setattr(settings, spec.key, getattr(settings, spec.key))
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "settings_ui_enabled", True)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def field(body: dict, key: str) -> dict:
    return next(f for f in body["fields"] if f["key"] == key)


def test_should_store_token_encrypted_and_never_return_it_when_saved_from_ui(client, db):
    body = client.put("/api/collectors/settings", json={"values": {
        "observium_url": "http://observium.lab", "observium_token": "s3cr3t-token"}}).json()
    assert field(body, "observium_token") == {**field(body, "observium_token"), "is_set": True, "source": "ui"}
    assert "value" not in field(body, "observium_token") and "s3cr3t-token" not in str(body)
    row = db.get(m.CollectorSetting, "observium_token")
    assert row.secret and "s3cr3t-token" not in row.value
    assert settings.observium_token == "s3cr3t-token" and settings.observium_url == "http://observium.lab"


def test_should_fall_back_to_env_when_ui_value_is_cleared(client):
    env_url = settings.gitweb_url
    client.put("/api/collectors/settings", json={"values": {"gitweb_url": "http://gitweb.lab/gitweb/"}})
    assert settings.gitweb_url == "http://gitweb.lab/gitweb/"
    body = client.put("/api/collectors/settings", json={"clear": ["gitweb_url"]}).json()
    assert settings.gitweb_url == env_url and field(body, "gitweb_url")["source"] != "ui"


def test_should_keep_stored_secret_when_secret_field_is_left_empty(client):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1"}})
    client.put("/api/collectors/settings", json={"values": {"observium_token": "", "observium_include_cdp": False}})
    assert settings.observium_token == "t1" and settings.observium_include_cdp is False


def test_should_refuse_new_url_when_stored_token_is_not_entered_again(client):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1"}})
    resp = client.put("/api/collectors/settings", json={"values": {"observium_url": "http://attacker.example"}})
    assert resp.status_code == 422 and "isi ulang" in resp.json()["detail"]
    assert settings.observium_url == "http://o.lab"


def test_should_reject_bad_schedule_when_time_is_not_hhmm(client):
    resp = client.put("/api/collectors/settings", json={"values": {"gitweb_daily_at": "25h"}})
    assert resp.status_code == 422


def test_should_refuse_changes_when_settings_page_is_disabled(client, monkeypatch, db):
    monkeypatch.setattr(settings, "settings_ui_enabled", False)
    resp = client.put("/api/collectors/settings", json={"values": {"gitweb_url": "http://g.lab/"}})
    assert resp.status_code == 403 and db.scalars(select(m.CollectorSetting)).all() == []


def test_should_report_failure_without_raising_when_connection_test_fails(client):
    client.put("/api/collectors/settings", json={"values": {"gitweb_url": "http://127.0.0.1:9/gitweb/"}})
    body = client.post("/api/collectors/settings/test?target=gitweb").json()
    assert body["ok"] is False and "unreachable" in body["detail"]
