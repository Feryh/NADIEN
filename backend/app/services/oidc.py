"""OpenID Connect login (authorization code + PKCE) against Entra ID, Keycloak, Google Workspace, Okta, ...

The ID token is verified here: signature with the provider's JWKS (RS256/ES256), issuer, audience, expiry and
the nonce bound to this browser's login attempt. Roles come from one claim (groups, roles, ...) through the
mapping in the SSO settings; the identity provider stays the source of truth, so the role is refreshed on
every login.
"""

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .auth import ROLE_RANK, sign, unusable_password
from .collector_settings import decrypt_secret

STATE_COOKIE = "nadien_sso"
STATE_TTL_S = 600
LEEWAY_S = 120
_CACHE_TTL_S = 600
_cache: dict[str, tuple[float, Any]] = {}


class SsoError(Exception):
    """Shown to the user on the login page."""


@dataclass
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _get_json(url: str, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    cached = _cache.get(url)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SsoError(f"Identity provider tidak bisa dihubungi ({url}): {exc}") from exc
    _cache[url] = (time.time() + _CACHE_TTL_S, data)
    return data


def discover(issuer_url: str, transport: httpx.BaseTransport | None = None) -> Provider:
    data = _get_json(issuer_url.rstrip("/") + "/.well-known/openid-configuration", transport)
    try:
        return Provider(data["issuer"], data["authorization_endpoint"], data["token_endpoint"], data["jwks_uri"])
    except KeyError as exc:
        raise SsoError(f"Dokumen discovery tidak lengkap: {exc} tidak ada.") from exc


def configured(sso: m.SsoSettings | None) -> m.SsoSettings:
    if sso is None or not sso.enabled:
        raise SsoError("SSO belum diaktifkan.")
    if not (sso.issuer_url and sso.client_id and sso.redirect_uri):
        raise SsoError("Pengaturan SSO belum lengkap (issuer, client id, redirect URI).")
    return sso


# --------------------------------------------------------------------------- login start / state cookie


def start(db: Session, sso: m.SsoSettings, transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    """(authorization URL, signed state cookie value)."""
    provider = discover(sso.issuer_url, transport)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    query = urlencode({"response_type": "code", "client_id": sso.client_id, "redirect_uri": sso.redirect_uri,
                       "scope": sso.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
                       "code_challenge_method": "S256"})
    payload = f"{state}.{nonce}.{verifier}.{int(time.time()) + STATE_TTL_S}"
    return f"{provider.authorization_endpoint}?{query}", f"{payload}.{sign(db, payload)}"


def read_state(db: Session, cookie: str | None, state: str) -> tuple[str, str]:
    """(nonce, PKCE verifier) for this browser's attempt; refuses a foreign, replayed or stale callback."""
    parts = (cookie or "").split(".")
    if len(parts) != 5:
        raise SsoError("Sesi login SSO tidak ditemukan; mulai lagi dari halaman login.")
    payload = ".".join(parts[:4])
    if not secrets.compare_digest(parts[4], sign(db, payload)) or not secrets.compare_digest(parts[0], state):
        raise SsoError("State SSO tidak cocok; mulai lagi dari halaman login.")
    if not parts[3].isdigit() or int(parts[3]) < time.time():
        raise SsoError("Login SSO kedaluwarsa; mulai lagi dari halaman login.")
    return parts[1], parts[2]


# --------------------------------------------------------------------------- callback


def exchange(db: Session, sso: m.SsoSettings, code: str, verifier: str,
             transport: httpx.BaseTransport | None = None) -> str:
    provider = discover(sso.issuer_url, transport)
    secret = decrypt_secret(db, sso.client_secret_encrypted) if sso.client_secret_encrypted else None
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": sso.redirect_uri,
            "client_id": sso.client_id, "code_verifier": verifier}
    if secret:
        form["client_secret"] = secret
    try:
        with httpx.Client(transport=transport, timeout=15) as client:
            response = client.post(provider.token_endpoint, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SsoError(f"Token endpoint tidak bisa dihubungi: {exc}") from exc
    if response.status_code >= 400:
        raise SsoError(f"Identity provider menolak kode login (HTTP {response.status_code}).")
    token = response.json().get("id_token")
    if not token:
        raise SsoError("Identity provider tidak mengirim id_token (scope openid?).")
    return token


def _public_key(jwk: dict[str, Any]):
    if jwk.get("kty") == "RSA":
        return rsa.RSAPublicNumbers(int.from_bytes(_b64decode(jwk["e"]), "big"),
                                    int.from_bytes(_b64decode(jwk["n"]), "big")).public_key()
    if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
        return ec.EllipticCurvePublicNumbers(int.from_bytes(_b64decode(jwk["x"]), "big"),
                                             int.from_bytes(_b64decode(jwk["y"]), "big"), ec.SECP256R1()).public_key()
    raise SsoError(f"Jenis kunci JWKS tidak didukung: {jwk.get('kty')}")


def verify_id_token(token: str, provider: Provider, client_id: str, nonce: str,
                    transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64decode(header_b64))
        claims = json.loads(_b64decode(payload_b64))
    except ValueError as exc:
        raise SsoError("id_token tidak valid.") from exc
    alg = header.get("alg")
    if alg not in ("RS256", "ES256"):
        raise SsoError(f"Algoritma id_token tidak didukung: {alg}")
    keys = _get_json(provider.jwks_uri, transport).get("keys", [])
    jwk = next((k for k in keys if k.get("kid") == header.get("kid")), keys[0] if len(keys) == 1 else None)
    if jwk is None:
        _cache.pop(provider.jwks_uri, None)  # the provider may have rotated its keys
        raise SsoError("Kunci penanda tangan id_token tidak ditemukan di JWKS.")
    signed, signature = f"{header_b64}.{payload_b64}".encode(), _b64decode(signature_b64)
    try:
        if alg == "RS256":
            _public_key(jwk).verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            _public_key(jwk).verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise SsoError("Tanda tangan id_token tidak valid.") from exc
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    now = time.time()
    if claims.get("iss") != provider.issuer:
        raise SsoError("Issuer id_token tidak cocok.")
    if client_id not in audiences:
        raise SsoError("id_token bukan untuk aplikasi ini (aud).")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - LEEWAY_S:
        raise SsoError("id_token kedaluwarsa.")
    if claims.get("nonce") != nonce:
        raise SsoError("Nonce id_token tidak cocok.")
    if not claims.get("sub"):
        raise SsoError("id_token tanpa subject (sub).")
    return claims


def role_for(sso: m.SsoSettings, claims: dict[str, Any]) -> str | None:
    """Highest staff role among the mapped claim values; else the default role (None: refuse)."""
    value = claims.get(sso.role_claim)
    values = value if isinstance(value, list) else [value] if value else []
    mapped = [sso.role_mapping[str(v)] for v in values if str(v) in (sso.role_mapping or {})]
    mapped = [r for r in mapped if r in ROLE_RANK and r != "customer"]
    if mapped:
        return max(mapped, key=lambda r: ROLE_RANK[r])
    return sso.default_role


def _free_username(db: Session, wanted: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._@-]+", "-", wanted)[:56] or "sso-user"
    name, n = base, 1
    while db.scalar(select(m.User.id).where(m.User.username == name)):
        n += 1
        name = f"{base}-{n}"
    return name


def user_for(db: Session, sso: m.SsoSettings, claims: dict[str, Any]) -> m.User:
    email = (claims.get("email") or "").strip().lower() or None
    domains = [d.strip().lower().lstrip("@") for d in (sso.allowed_domains or "").split(",") if d.strip()]
    if domains and not (email and email.rsplit("@", 1)[-1] in domains):
        raise SsoError("Domain email akun ini tidak diizinkan untuk NADIEN.")
    role = role_for(sso, claims)
    user = db.scalar(select(m.User).where(m.User.auth_source == "oidc", m.User.external_id == str(claims["sub"])))
    if role is None:
        raise SsoError("Akun Anda belum diberi akses NADIEN (tidak ada grup/role yang cocok).")
    if user is None:
        if not sso.auto_create:
            raise SsoError("Akun SSO ini belum terdaftar di NADIEN; minta admin menambahkannya.")
        wanted = claims.get("preferred_username") or email or f"sso-{str(claims['sub'])[:12]}"
        user = m.User(username=_free_username(db, wanted), auth_source="oidc", external_id=str(claims["sub"]),
                      password_hash=unusable_password(), role=role, active=True)
        db.add(user)
    if not user.active:
        raise SsoError("Akun Anda di NADIEN dinonaktifkan.")
    user.role = role
    user.full_name = claims.get("name") or user.full_name
    user.email = email or user.email
    db.flush()
    return user
