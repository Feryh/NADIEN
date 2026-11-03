"""OIDC login against a mock identity provider that signs real RS256 ID tokens."""

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.main import app
from app.routers import sso as sso_router
from app.services import oidc
from tests.test_auth import auth_on  # noqa: F401  (fixture)

ISSUER = "https://idp.test/realms/noc"
CLIENT_ID = "nadien"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwk() -> dict:
    numbers = KEY.public_key().public_numbers()
    return {"kty": "RSA", "kid": "k1", "alg": "RS256", "n": _b64(numbers.n.to_bytes(256, "big")),
            "e": _b64(numbers.e.to_bytes(3, "big"))}


def id_token(**claims) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
    body = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u-123", "exp": int(time.time()) + 300, "iat": int(time.time()),
            "email": "budi@example.co.id", "name": "Budi", "preferred_username": "budi", "groups": ["noc-approver"],
            **claims}
    payload = _b64(json.dumps(body).encode())
    signature = KEY.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


@pytest.fixture
def idp(auth_on, db, monkeypatch):  # noqa: F811
    """Mock provider; `issued["claims"]` overrides the ID token claims, the nonce is echoed from the login."""
    issued: dict = {"claims": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/certs"})
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [_jwk()]})
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"id_token": id_token(**{"nonce": issued["nonce"], **issued["claims"]})})
        return httpx.Response(404)

    monkeypatch.setattr(sso_router, "transport", httpx.MockTransport(handler))
    monkeypatch.setattr(oidc, "_cache", {})
    db.add(m.SsoSettings(id=1, enabled=True, display_name="Keycloak", issuer_url=ISSUER, client_id=CLIENT_ID,
                         scopes="openid profile email", redirect_uri="http://testserver/api/auth/sso/callback",
                         role_claim="groups", role_mapping={"noc-approver": "approver", "noc-view": "viewer"},
                         default_role=None, auto_create=True))
    db.commit()
    return issued


def sso_login(issued: dict, state_override: str | None = None) -> tuple[TestClient, httpx.Response]:
    client = TestClient(app, follow_redirects=False)
    start = client.get("/api/auth/sso/login")
    params = parse_qs(urlparse(start.headers["location"]).query)
    issued["nonce"] = params["nonce"][0]
    state = state_override or params["state"][0]
    return client, client.get(f"/api/auth/sso/callback?code=abc&state={state}")


def test_should_send_pkce_challenge_when_login_starts(idp):
    params = parse_qs(urlparse(TestClient(app, follow_redirects=False).get("/api/auth/sso/login")
                               .headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"] and params["client_id"] == [CLIENT_ID]


def test_should_create_user_with_mapped_role_when_id_token_is_valid(idp, db):
    client, callback = sso_login(idp)
    assert callback.status_code == 303 and callback.headers["location"] == "/"
    me = client.get("/api/auth/status").json()["user"]
    assert (me["username"], me["role"], me["auth_source"]) == ("budi", "approver", "oidc")


def test_should_refuse_login_when_nonce_does_not_match(idp, db):
    idp["claims"] = {"nonce": "lain"}
    _, callback = sso_login(idp)
    assert "sso_error" in callback.headers["location"] and db.scalar(select(m.User.id)) is None


def test_should_refuse_login_when_token_is_for_another_client(idp, db):
    idp["claims"] = {"aud": "aplikasi-lain"}
    _, callback = sso_login(idp)
    assert "sso_error" in callback.headers["location"]


def test_should_refuse_callback_when_state_is_not_this_browsers(idp, db):
    _, callback = sso_login(idp, state_override="state-palsu")
    assert "sso_error" in callback.headers["location"]


def test_should_refuse_login_when_no_group_maps_and_no_default_role(idp, db):
    idp["claims"] = {"groups": ["finance"]}
    _, callback = sso_login(idp)
    assert "sso_error" in callback.headers["location"] and db.scalar(select(m.User.id)) is None


def test_should_pick_highest_role_when_several_groups_map(idp, db):
    sso = db.get(m.SsoSettings, 1)
    assert oidc.role_for(sso, {"groups": ["noc-view", "noc-approver"]}) == "approver"
