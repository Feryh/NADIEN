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


class _FakeObservium:
    """Records what a connection test would send, instead of calling the network."""
    calls: list[dict] = []

    def __init__(self, **kwargs):
        _FakeObservium.calls.append(kwargs)
        self.base_url = kwargs["base_url"].rstrip("/")  # like the real client: the server that answered
        self.failed_over = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _get(self, path, params):
        return {"count": 7}


@pytest.fixture
def fake_observium(monkeypatch):
    from app.routers import collectors

    _FakeObservium.calls = []
    monkeypatch.setattr(collectors, "ObserviumClient", _FakeObservium)
    return _FakeObservium.calls


def test_should_test_the_unsaved_url_with_the_password_typed_in_the_form(client, fake_observium):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_user": "u",
                                                            "observium_password": "old"}})
    body = client.post("/api/collectors/settings/test?target=observium",
                       json={"values": {"observium_url": "https://o2.lab:44343/", "observium_password": "new"}}).json()
    assert body["ok"] and "o2.lab" in body["detail"]
    assert (fake_observium[0]["base_url"], fake_observium[0]["password"]) == ("https://o2.lab:44343/", "new")
    assert settings.observium_url == "http://o.lab"  # testing saves nothing


def test_should_refuse_to_test_a_new_url_with_the_stored_password(client, fake_observium):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_user": "u",
                                                            "observium_password": "old"}})
    body = client.post("/api/collectors/settings/test?target=observium",
                       json={"values": {"observium_url": "http://attacker.example"}}).json()
    assert body["ok"] is False and "isi ulang" in body["detail"] and fake_observium == []


def test_should_not_send_the_stored_token_along_with_a_new_password_to_a_new_url(client, fake_observium):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1"}})
    client.post("/api/collectors/settings/test?target=observium",
                json={"values": {"observium_url": "http://o2.lab", "observium_user": "u", "observium_password": "p"}})
    assert fake_observium[0]["token"] == ""


def test_should_test_saved_settings_when_the_form_is_unchanged(client, fake_observium):
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1"}})
    client.post("/api/collectors/settings/test?target=observium")
    assert (fake_observium[0]["base_url"], fake_observium[0]["token"]) == ("http://o.lab", "t1")


def test_should_name_the_standby_when_the_connection_test_fails_over(client, monkeypatch):
    from app.routers import collectors

    class FailedOver(_FakeObservium):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.base_url, self.failed_over = kwargs["standby_url"], True

    monkeypatch.setattr(collectors, "ObserviumClient", FailedOver)
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1",
                                                            "observium_standby_url": "http://o-standby.lab"}})
    detail = client.post("/api/collectors/settings/test?target=observium").json()["detail"]
    assert detail.startswith("Terhubung ke http://o-standby.lab (standby; http://o.lab tidak menjawab)")


def test_should_report_a_non_api_answer_instead_of_failing_with_500(client, monkeypatch):
    from app.routers import collectors

    class HtmlObservium(_FakeObservium):
        def _get(self, path, params):
            raise collectors.ObserviumError("Observium /devices/ membalas text/html, bukan JSON API")

    monkeypatch.setattr(collectors, "ObserviumClient", HtmlObservium)
    client.put("/api/collectors/settings", json={"values": {"observium_url": "http://o.lab", "observium_token": "t1"}})
    resp = client.post("/api/collectors/settings/test?target=observium")
    assert resp.status_code == 200 and resp.json()["ok"] is False
