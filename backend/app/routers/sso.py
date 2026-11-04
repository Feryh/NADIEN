from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services import oidc
from ..services.auth import CurrentUser, Role, audit, current_user, require
from ..services.collector_settings import encrypt_secret
from .auth import start_session

# Browser redirects of the OIDC login: reachable without a session.
public = APIRouter(prefix="/auth/sso", tags=["Auth"])
settings_router = APIRouter(prefix="/sso-settings", tags=["Auth"], dependencies=[Depends(require("admin"))])

transport: httpx.BaseTransport | None = None  # tests swap in a mock identity provider
STATE_PATH = "/api/auth/sso"


def _to_login(message: str) -> RedirectResponse:
    response = RedirectResponse(f"/?sso_error={quote(message)}", status_code=303)
    response.delete_cookie(oidc.STATE_COOKIE, path=STATE_PATH)
    return response


@public.get("/login")
def sso_login(request: Request, db: Session = Depends(get_db)):
    try:
        sso = oidc.configured(db.get(m.SsoSettings, 1))
        url, state_cookie = oidc.start(db, sso, transport)
    except oidc.SsoError as exc:
        return _to_login(str(exc))
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(oidc.STATE_COOKIE, state_cookie, max_age=oidc.STATE_TTL_S, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path=STATE_PATH)
    return response


@public.get("/callback")
def sso_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                 error_description: str | None = None, db: Session = Depends(get_db)):
    if error:
        return _to_login(f"Identity provider: {error_description or error}")
    try:
        sso = oidc.configured(db.get(m.SsoSettings, 1))
        if not code or not state:
            raise oidc.SsoError("Callback SSO tanpa code/state.")
        nonce, verifier = oidc.read_state(db, request.cookies.get(oidc.STATE_COOKIE), state)
        token = oidc.exchange(db, sso, code, verifier, transport)
        claims = oidc.verify_id_token(token, oidc.discover(sso.issuer_url, transport), sso.client_id, nonce, transport)
        user = oidc.user_for(db, sso, claims)
    except oidc.SsoError as exc:
        audit(db, "-", "auth.sso_failed", "sso", str(exc), request)
        db.commit()
        return _to_login(str(exc))
    response = RedirectResponse("/", status_code=303)
    start_session(request, response, db, user)
    response.delete_cookie(oidc.STATE_COOKIE, path=STATE_PATH)
    audit(db, user.username, "auth.sso_login", "sso", f"role {user.role}", request)
    db.commit()
    return response


class SsoIn(BaseModel):
    enabled: bool = False
    display_name: str = Field("SSO", min_length=1, max_length=64)
    issuer_url: HttpUrl | None = None
    client_id: str | None = Field(None, max_length=256)
    client_secret: str | None = Field(None, max_length=1024)  # None keeps the saved one, "" clears it
    scopes: str = Field("openid profile email", max_length=256)
    redirect_uri: HttpUrl | None = None
    role_claim: str = Field("groups", min_length=1, max_length=64)
    role_mapping: dict[str, Role] = Field(default_factory=dict)
    default_role: Role | None = None
    auto_create: bool = True
    allowed_domains: str | None = Field(None, max_length=512)


class TestIn(BaseModel):
    issuer_url: HttpUrl


def _settings_dict(sso: m.SsoSettings | None) -> dict:
    if sso is None:
        sso = m.SsoSettings(enabled=False, display_name="SSO", scopes="openid profile email", role_claim="groups",
                            role_mapping={}, auto_create=True)
    return {"enabled": sso.enabled, "display_name": sso.display_name, "issuer_url": sso.issuer_url,
            "client_id": sso.client_id, "has_client_secret": bool(sso.client_secret_encrypted), "scopes": sso.scopes,
            "redirect_uri": sso.redirect_uri, "role_claim": sso.role_claim, "role_mapping": sso.role_mapping or {},
            "default_role": sso.default_role, "auto_create": sso.auto_create, "allowed_domains": sso.allowed_domains}


@settings_router.get("")
def get_sso_settings(db: Session = Depends(get_db)):
    return _settings_dict(db.get(m.SsoSettings, 1))


@settings_router.put("")
def put_sso_settings(payload: SsoIn, request: Request, db: Session = Depends(get_db),
                     me: CurrentUser = Depends(current_user)):
    if payload.default_role == "customer" or "customer" in payload.role_mapping.values():
        raise HTTPException(422, "SSO hanya untuk staf; akun customer dibuat sebagai user lokal.")
    if payload.enabled and not (payload.issuer_url and payload.client_id and payload.redirect_uri):
        raise HTTPException(422, "Isi issuer URL, client ID, dan redirect URI sebelum mengaktifkan SSO.")
    sso = db.get(m.SsoSettings, 1) or m.SsoSettings(id=1)
    for key in ("enabled", "display_name", "client_id", "scopes", "role_claim", "role_mapping", "default_role",
                "auto_create", "allowed_domains"):
        setattr(sso, key, getattr(payload, key))
    sso.issuer_url = str(payload.issuer_url).rstrip("/") if payload.issuer_url else None
    sso.redirect_uri = str(payload.redirect_uri) if payload.redirect_uri else None
    if payload.client_secret is not None:
        sso.client_secret_encrypted = encrypt_secret(db, payload.client_secret) if payload.client_secret else None
    db.add(sso)
    audit(db, me.username, "sso.update", "sso settings", f"enabled={sso.enabled} issuer={sso.issuer_url}", request)
    db.commit()
    return _settings_dict(sso)


@settings_router.post("/test")
def test_sso_settings(payload: TestIn):
    """Reads the provider's discovery document: proves the issuer URL is right and reachable."""
    try:
        provider = oidc.discover(str(payload.issuer_url), transport)
    except oidc.SsoError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Discovery OK", "issuer": provider.issuer,
            "authorization_endpoint": provider.authorization_endpoint, "token_endpoint": provider.token_endpoint}
