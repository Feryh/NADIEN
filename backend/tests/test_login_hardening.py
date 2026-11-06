"""Attacks on the login: guessing from one address, locking someone else out, finding valid usernames, faked IPs."""

import logging
from unittest.mock import MagicMock

import paramiko
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.routers import auth as auth_router
from app.services import collector_settings, device_cli
from app.services.auth import client_ip, ensure_admin, hash_password

PASSWORD = "rahasia123"
LOGIN = "/api/auth/login"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def add_user(db, username: str, role: str = "engineer", **kw) -> m.User:
    user = m.User(username=username, role=role, password_hash=kw.pop("password_hash", hash_password(PASSWORD)), **kw)
    db.add(user)
    db.commit()
    return user


def attempt(client, username: str, password: str = "salah", ip: str = "198.51.100.7"):
    return client.post(LOGIN, json={"username": username, "password": password}, headers={"X-Forwarded-For": ip})


def test_should_block_address_when_it_keeps_failing_across_usernames(client, monkeypatch):
    monkeypatch.setattr(settings, "login_ip_max_failures", 4)
    for number in range(4):
        assert attempt(client, f"tebak{number}").status_code == 401
    assert attempt(client, "tebak-baru").status_code == 429


def test_should_not_verify_password_when_address_is_blocked(client, db, monkeypatch):
    monkeypatch.setattr(settings, "login_ip_max_failures", 2)
    add_user(db, "eng")
    attempt(client, "a")
    attempt(client, "b")
    monkeypatch.setattr(auth_router, "verify_password", lambda *_: pytest.fail("scrypt must not run"))
    assert attempt(client, "eng", PASSWORD).status_code == 429


def test_should_let_owner_log_in_when_attacker_failed_from_another_address(client, db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts", 3)
    add_user(db, "eng")
    for _ in range(3):
        attempt(client, "eng", ip="203.0.113.9")
    assert attempt(client, "eng", ip="203.0.113.9").status_code == 429
    assert attempt(client, "eng", PASSWORD, ip="198.51.100.20").status_code == 200


def test_should_lock_account_everywhere_when_failures_reach_the_account_limit(client, db, monkeypatch):
    monkeypatch.setattr(settings, "login_account_max_attempts", 3)
    add_user(db, "eng")
    for number in range(3):
        attempt(client, "eng", ip=f"203.0.113.{number}")
    assert attempt(client, "eng", PASSWORD, ip="198.51.100.20").status_code == 429


def test_should_count_from_zero_when_login_succeeds(client, db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts", 3)
    add_user(db, "eng")
    attempt(client, "eng")
    attempt(client, "eng")
    assert attempt(client, "eng", PASSWORD).status_code == 200
    attempt(client, "eng")
    attempt(client, "eng")
    assert attempt(client, "eng", PASSWORD).status_code == 200


def test_should_lift_lock_when_admin_unlocks_the_account(client, db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts", 2)
    add_user(db, "admin", "admin")
    user = add_user(db, "eng")
    attempt(client, "eng")
    attempt(client, "eng")
    assert attempt(client, "eng", PASSWORD).status_code == 429
    admin = TestClient(app)
    assert admin.post(LOGIN, json={"username": "admin", "password": PASSWORD},
                      headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert admin.patch(f"/api/users/{user.id}", json={"unlock": True}).status_code == 200
    assert attempt(client, "eng", PASSWORD).status_code == 200


def test_should_treat_unknown_username_like_a_known_one_when_it_keeps_failing(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_attempts", 2)
    attempt(client, "tidak-ada")
    attempt(client, "tidak-ada")
    assert attempt(client, "tidak-ada").status_code == 429


@pytest.mark.parametrize("kind", ["unknown", "inactive", "sso"])
def test_should_spend_the_same_work_and_answer_when_account_cannot_log_in(client, db, monkeypatch, kind):
    if kind == "inactive":
        add_user(db, "orang", active=False)
    elif kind == "sso":
        add_user(db, "orang", auth_source="oidc", external_id="x1", password_hash="none$abc")
    expected = attempt(client, "pembanding-tidak-ada").json()
    calls = []
    real = auth_router.verify_password
    monkeypatch.setattr(auth_router, "verify_password", lambda p, s: calls.append(s) or real(p, s))
    response = attempt(client, "orang", PASSWORD)
    assert response.status_code == 401 and response.json() == expected
    assert len(calls) == 1 and calls[0].startswith("scrypt$")


def test_should_ignore_faked_address_when_client_sends_its_own_forwarded_header(client, db):
    add_user(db, "eng")
    attempt(client, "eng", ip="1.2.3.4, 198.51.100.7")
    entry = db.scalars(select(m.AuditLog).where(m.AuditLog.action == "auth.login_failed")).one()
    assert entry.ip == "198.51.100.7"


class _Request:
    def __init__(self, forwarded: str | None, peer: str = "10.0.0.1"):
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = type("Peer", (), {"host": peer})


@pytest.mark.parametrize(("hops", "forwarded", "expected"), [
    (0, "1.2.3.4", "10.0.0.1"),  # no proxy configured: the header is anybody's
    (1, "1.2.3.4, 5.6.7.8", "5.6.7.8"),
    (2, "1.2.3.4, 5.6.7.8, 10.42.0.9", "5.6.7.8"),
    (2, "5.6.7.8", "5.6.7.8"),
    (1, None, "10.0.0.1"),
])
def test_should_read_address_set_by_trusted_proxy_when_resolving_client_ip(monkeypatch, hops, forwarded, expected):
    monkeypatch.setattr(settings, "trusted_proxy_hops", hops)
    assert client_ip(_Request(forwarded)) == expected


def test_should_not_write_generated_admin_password_to_the_log(db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "admin_password", None)
    monkeypatch.setattr("app.services.auth.secrets.token_urlsafe", lambda _: "Rahasia-Sekali-1")
    with caplog.at_level(logging.WARNING):
        ensure_admin(db)
    assert "Rahasia-Sekali-1" not in caplog.text and "reset_password" in caplog.text


def test_should_sign_sessions_with_a_key_other_than_the_encryption_key(db, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "secret_key", key)
    assert collector_settings.signing_key(db) != key.encode()


def test_should_refuse_unknown_device_host_key_when_trust_on_first_use_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "device_ssh_trust_new_hosts", False)
    client = device_cli._ssh_client(tmp_path / "known_hosts")
    assert isinstance(client._policy, paramiko.RejectPolicy)


def test_should_log_fingerprint_when_a_new_device_host_key_is_recorded(tmp_path, caplog):
    known_hosts = tmp_path / "known_hosts"
    client = device_cli._ssh_client(known_hosts)
    client._transport = MagicMock()  # paramiko logs through it; no connection is made here
    key = paramiko.RSAKey.generate(2048)
    with caplog.at_level(logging.WARNING):
        client._policy.missing_host_key(client, "10.1.1.1", key)
    assert "10.1.1.1" in caplog.text and key.fingerprint in caplog.text
    assert "10.1.1.1" in known_hosts.read_text()
