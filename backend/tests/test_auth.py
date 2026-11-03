"""Login, roles, built-in admin, lockout, customer scope and audit log."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.services.auth import ensure_admin, hash_password, password_problem, required_role, verify_password

PASSWORD = "rahasia123"


@pytest.fixture
def auth_on(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    app.dependency_overrides[get_db] = lambda: db
    yield
    app.dependency_overrides.clear()


def add_user(db, username: str, role: str, password: str = PASSWORD, **kw) -> m.User:
    user = m.User(username=username, role=role, password_hash=hash_password(password), **kw)
    db.add(user)
    db.commit()
    return user


def logged_in(username: str, password: str = PASSWORD) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


def test_should_verify_password_only_when_it_matches_the_hash():
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored) and not verify_password("salah", stored)


@pytest.mark.parametrize("password", ["pendek1", "tanpaangkasama", "1234567890", "xadmin12345"])
def test_should_reject_password_when_it_breaks_the_policy(password):
    assert password_problem(password, "admin") is not None


def test_should_answer_401_when_not_logged_in(auth_on):
    assert TestClient(app).get("/api/devices").status_code == 401


def test_should_create_admin_that_must_change_password_when_db_has_no_users(auth_on, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "Mulai-12345")
    ensure_admin(db)
    ensure_admin(db)  # a second worker finds the admin and does nothing
    admin = db.scalars(select(m.User)).one()
    assert (admin.username, admin.role, admin.must_change_password) == ("admin", "admin", True)


def test_should_allow_only_password_change_when_change_is_pending(auth_on, db):
    add_user(db, "admin", "admin", must_change_password=True)
    client = logged_in("admin")
    assert client.get("/api/devices").status_code == 403
    changed = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "Baru-98765"})
    assert changed.status_code == 204 and client.get("/api/devices").status_code == 200


def test_should_lock_account_when_failed_logins_reach_the_limit(auth_on, db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts", 3)
    add_user(db, "eng", "engineer")
    client = TestClient(app)
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "eng", "password": "salah"})
    response = client.post("/api/auth/login", json={"username": "eng", "password": PASSWORD})
    assert response.status_code == 429


def test_should_forbid_writes_when_role_is_viewer(auth_on, db):
    add_user(db, "view", "viewer")
    client = logged_in("view")
    assert client.get("/api/devices").status_code == 200
    assert client.post("/api/vendors", json={"name": "X"}).status_code == 403


def test_should_keep_settings_admin_only_when_user_is_engineer(auth_on, db):
    add_user(db, "eng", "engineer")
    assert logged_in("eng").put("/api/collectors/settings", json={}).status_code == 403


def test_should_record_audit_entry_with_ip_when_a_write_is_made(auth_on, db):
    add_user(db, "eng", "engineer")
    logged_in("eng").post("/api/vendors", json={"name": "VendorX"})
    entry = db.scalars(select(m.AuditLog).where(m.AuditLog.action == "POST")).one()
    assert (entry.username, entry.target, entry.ip) == ("eng", "/api/vendors", "testclient")


def test_should_export_filtered_audit_log_when_csv_is_requested(auth_on, db):
    add_user(db, "admin", "admin")
    client = logged_in("admin")
    lines = client.get("/api/audit-logs/export?action=auth.login").text.strip().splitlines()
    assert lines[0].startswith("id,at,username") and len(lines) == 2


def test_should_end_old_sessions_when_password_changes(auth_on, db):
    user = add_user(db, "eng", "engineer")
    client = logged_in("eng")
    user.password_hash = hash_password("Baru-98765")
    db.commit()
    assert client.get("/api/devices").status_code == 401


def test_should_keep_one_active_admin_when_last_admin_is_demoted(auth_on, db):
    admin = add_user(db, "admin", "admin")
    response = logged_in("admin").patch(f"/api/users/{admin.id}", json={"role": "viewer"})
    assert response.status_code == 409


def test_should_require_password_change_when_admin_creates_a_user(auth_on, db):
    add_user(db, "admin", "admin")
    created = logged_in("admin").post("/api/users", json={"username": "noc1", "password": "Awal-12345",
                                                           "role": "engineer"})
    assert created.status_code == 201 and created.json()["must_change_password"] is True


@pytest.fixture
def customers(db):
    own, other = m.Customer(name="PT Satu", account_no="C1"), m.Customer(name="PT Dua", account_no="C2")
    db.add_all([own, other])
    db.flush()
    db.add_all([m.Service(customer_id=c.id, circuit_id=f"CIR-{c.account_no}", service_type="l2vpn",
                          bandwidth_mbps=100, status="active") for c in (own, other)])
    db.commit()
    return own


def test_should_list_only_own_services_when_customer_opens_portal(auth_on, db, customers):
    add_user(db, "pt-satu", "customer", customer_id=customers.id)
    services = logged_in("pt-satu").get("/api/portal/services").json()["services"]
    assert [s["circuit_id"] for s in services] == ["CIR-C1"]


def test_should_keep_customer_out_of_staff_api_when_role_is_customer(auth_on, db, customers):
    add_user(db, "pt-satu", "customer", customer_id=customers.id)
    assert logged_in("pt-satu").get("/api/services").status_code == 403


def test_should_refuse_customer_account_when_no_customer_is_linked(auth_on, db):
    add_user(db, "admin", "admin")
    response = logged_in("admin").post("/api/users", json={"username": "cust", "password": "Awal-12345",
                                                            "role": "customer"})
    assert response.status_code == 422


@pytest.mark.parametrize("method, path, role", [
    ("GET", "/api/changes", "viewer"), ("POST", "/api/twin/simulate", "viewer"),
    ("POST", "/api/changes", "engineer"), ("POST", "/api/ai/providers", "admin"),
    ("PUT", "/api/sso-settings", "admin"), ("GET", "/api/portal/services", "customer"),
])
def test_should_map_request_to_minimum_role_when_authorizing(method, path, role):
    assert required_role(method, path) == role
