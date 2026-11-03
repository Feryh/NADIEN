"""Login, roles and audit log."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.services.auth import hash_password, required_role, verify_password


@pytest.fixture
def auth_on(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    app.dependency_overrides[get_db] = lambda: db
    yield
    app.dependency_overrides.clear()


def add_user(db, username: str, role: str, password: str = "rahasia123") -> m.User:
    user = m.User(username=username, role=role, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def logged_in(username: str, password: str = "rahasia123") -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


def test_should_verify_password_only_when_it_matches_the_hash():
    stored = hash_password("rahasia123")
    assert verify_password("rahasia123", stored) and not verify_password("salah", stored)


def test_should_answer_401_when_not_logged_in(auth_on):
    assert TestClient(app).get("/api/devices").status_code == 401


def test_should_create_first_admin_once_when_no_user_exists(auth_on):
    client = TestClient(app)
    assert client.get("/api/auth/status").json()["setup_needed"] is True
    assert client.post("/api/auth/setup", json={"username": "admin", "password": "rahasia123"}).status_code == 201
    assert client.get("/api/devices").status_code == 200  # the setup call also logs in
    assert TestClient(app).post("/api/auth/setup", json={"username": "admin2", "password": "rahasia123"}).status_code == 409


def test_should_refuse_login_when_password_is_wrong(auth_on, db):
    add_user(db, "eng", "engineer")
    response = TestClient(app).post("/api/auth/login", json={"username": "eng", "password": "salah"})
    assert response.status_code == 401


def test_should_forbid_writes_when_role_is_viewer(auth_on, db):
    add_user(db, "view", "viewer")
    client = logged_in("view")
    assert client.get("/api/devices").status_code == 200
    assert client.post("/api/vendors", json={"name": "X"}).status_code == 403


def test_should_keep_settings_admin_only_when_user_is_engineer(auth_on, db):
    add_user(db, "eng", "engineer")
    assert logged_in("eng").put("/api/collectors/settings", json={}).status_code == 403


def test_should_record_audit_entry_when_a_write_is_made(auth_on, db):
    add_user(db, "eng", "engineer")
    logged_in("eng").post("/api/vendors", json={"name": "VendorX"})
    entry = db.scalars(select(m.AuditLog).where(m.AuditLog.action == "POST")).one()
    assert (entry.username, entry.target) == ("eng", "/api/vendors")


def test_should_end_old_sessions_when_password_changes(auth_on, db):
    user = add_user(db, "eng", "engineer")
    client = logged_in("eng")
    user.password_hash = hash_password("baru-12345")
    db.commit()
    assert client.get("/api/devices").status_code == 401


def test_should_keep_one_active_admin_when_last_admin_is_demoted(auth_on, db):
    admin = add_user(db, "admin", "admin")
    response = logged_in("admin").patch(f"/api/users/{admin.id}", json={"role": "viewer"})
    assert response.status_code == 409


@pytest.mark.parametrize("method, path, role", [
    ("GET", "/api/changes", "viewer"), ("POST", "/api/twin/simulate", "viewer"),
    ("POST", "/api/changes", "engineer"), ("POST", "/api/ai/providers", "admin"),
])
def test_should_map_request_to_minimum_role_when_authorizing(method, path, role):
    assert required_role(method, path) == role
