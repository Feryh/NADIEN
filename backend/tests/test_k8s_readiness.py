"""What Kubernetes relies on: probes, and CronJobs starting syncs without a login session."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app import main
from app.config import Settings, settings
from app.database import get_db
from app.main import app
from app.services.auth import required_role


def _client(db, monkeypatch, token: str | None = "job-secret") -> TestClient:
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "job_token", token)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_should_refuse_to_start_when_secret_key_is_not_a_fernet_key():
    with pytest.raises(ValidationError, match="kunci Fernet"):
        Settings(secret_key="a" * 64)


def test_should_answer_liveness_without_touching_the_database(monkeypatch):
    def broken():
        raise OperationalError("SELECT 1", {}, Exception("down"))

    monkeypatch.setattr(main, "SessionLocal", broken)
    assert TestClient(app).get("/api/health").status_code == 200


def test_should_report_not_ready_when_the_database_is_down(monkeypatch):
    def broken():
        raise OperationalError("SELECT 1", {}, Exception("down"))

    monkeypatch.setattr(main, "SessionLocal", broken)
    response = TestClient(app).get("/api/ready")
    assert response.status_code == 503 and "down" not in response.text


def test_should_reject_collector_call_when_job_token_is_wrong(db, monkeypatch):
    try:
        response = _client(db, monkeypatch).get("/api/collectors/jobs/current", headers={"Authorization": "Bearer nope"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


def test_should_reject_collector_call_when_no_job_token_is_configured(db, monkeypatch):
    try:
        response = _client(db, monkeypatch, None).get("/api/collectors/jobs/current", headers={"Authorization": "Bearer "})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


def test_should_accept_job_token_on_collectors_only(db, monkeypatch):
    headers = {"Authorization": "Bearer job-secret"}
    try:
        client = _client(db, monkeypatch)
        allowed = client.get("/api/collectors/jobs/current", headers=headers)
        elsewhere = client.get("/api/devices", headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert allowed.status_code == 200 and elsewhere.status_code == 401


def test_should_keep_collector_settings_admin_only_for_the_job_token():
    assert required_role("PUT", "/api/collectors/settings") == "admin"
