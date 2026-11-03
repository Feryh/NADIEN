"""Login, roles and the audit trail.

Passwords: scrypt (stdlib). Sessions: an HMAC-signed cookie carrying the user id, expiry and a stamp of the
password hash, so changing a password or disabling a user ends that user's sessions without a session table.
Every API router is mounted behind `authorize`: reads need viewer, writes engineer, a few admin-only areas admin.
"""

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from .collector_settings import signing_key

Role = Literal["viewer", "engineer", "approver", "admin"]
ROLE_RANK: dict[str, int] = {"viewer": 0, "engineer": 1, "approver": 2, "admin": 3}
COOKIE = "nadien_session"

# Writes that only an admin may make: credentials, providers holding API keys, users, backups.
ADMIN_WRITE_PREFIXES = ("/api/collectors/settings", "/api/ai/providers", "/api/backups")
# POSTs that change nothing (simulations, questions): viewers may use them.
READ_ONLY_POSTS = ("/api/ai/chat", "/api/twin/simulate")

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


@dataclass
class CurrentUser:
    id: int | None
    username: str
    role: str
    full_name: str | None = None

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


LOCAL_ADMIN = CurrentUser(None, "local", "admin", "Auth nonaktif")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    check = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **_SCRYPT)
    return hmac.compare_digest(check, base64.b64decode(digest))


def _stamp(user: m.User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def _signature(db: Session, payload: str) -> str:
    return hmac.new(signing_key(db), payload.encode(), hashlib.sha256).hexdigest()


def session_token(db: Session, user: m.User) -> str:
    expires = int(time.time() + settings.session_hours * 3600)
    payload = f"{user.id}.{expires}.{_stamp(user)}"
    return f"{payload}.{_signature(db, payload)}"


def user_from_token(db: Session, token: str | None) -> m.User | None:
    if not token or token.count(".") != 3:
        return None
    user_id, expires, stamp, signature = token.split(".")
    payload = f"{user_id}.{expires}.{stamp}"
    if not hmac.compare_digest(signature, _signature(db, payload)):
        return None
    if not expires.isdigit() or int(expires) < time.time() or not user_id.isdigit():
        return None
    user = db.get(m.User, int(user_id))
    if user is None or not user.active or not hmac.compare_digest(stamp, _stamp(user)):
        return None
    return user


def as_current(user: m.User) -> CurrentUser:
    return CurrentUser(user.id, user.username, user.role, user.full_name)


def needs_setup(db: Session) -> bool:
    return settings.auth_enabled and db.scalar(select(m.User.id).limit(1)) is None


def required_role(method: str, path: str) -> str:
    if method in ("GET", "HEAD", "OPTIONS") or path in READ_ONLY_POSTS:
        return "viewer"
    if path.startswith(ADMIN_WRITE_PREFIXES):
        return "admin"
    return "engineer"


def audit(db: Session, username: str, action: str, target: str, detail: str | None = None) -> None:
    """Added to the session; the caller commits."""
    db.add(m.AuditLog(at=datetime.now(timezone.utc), username=username, action=action, target=target[:512],
                      detail=detail))


def authorize(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Router-level guard for every /api router except /api/auth."""
    path = request.url.path
    if not settings.auth_enabled:
        current = LOCAL_ADMIN
    elif settings.ingest_token and path.startswith("/api/integrations") and hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {settings.ingest_token}"):
        current = CurrentUser(None, "ingest-token", "engineer")
    else:
        user = user_from_token(db, request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "Silakan login terlebih dahulu.")
        current = as_current(user)
    needed = required_role(request.method, path)
    if not current.at_least(needed):
        raise HTTPException(403, f"Butuh role {needed} untuk aksi ini (role Anda: {current.role}).")
    request.state.user = current
    if needed != "viewer":
        query = f"?{request.url.query}" if request.url.query else ""
        # Committed now, before the endpoint runs: background-job endpoints never commit this session,
        # and a refused write is worth seeing too.
        audit(db, current.username, request.method, path + query)
        db.commit()
    return current


def current_user(request: Request) -> CurrentUser:
    """The user `authorize` resolved for this request."""
    return getattr(request.state, "user", LOCAL_ADMIN)


def require(role: Role):
    def check(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.at_least(role):
            raise HTTPException(403, f"Butuh role {role} untuk aksi ini (role Anda: {user.role}).")
        return user
    return check
