"""Login, roles and the audit trail.

Passwords: scrypt (stdlib). Sessions: an HMAC-signed cookie carrying the user id, expiry and a stamp of the
password hash, so changing a password or disabling a user ends that user's sessions without a session table.
Every API router is mounted behind `authorize`: reads need viewer, writes engineer, a few admin-only areas admin.
A `customer` user sits outside that ladder and reaches only /api/portal (its own services).
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from .collector_settings import signing_key

log = logging.getLogger(__name__)

Role = Literal["customer", "viewer", "engineer", "approver", "admin"]
STAFF_ROLES = ("viewer", "engineer", "approver", "admin")
ROLE_RANK: dict[str, int] = {"customer": -1, "viewer": 0, "engineer": 1, "approver": 2, "admin": 3}
COOKIE = "nadien_session"

# Writes that only an admin may make: credentials, providers holding API keys, users, backups, SSO.
ADMIN_WRITE_PREFIXES = ("/api/collectors/settings", "/api/ai/providers", "/api/backups", "/api/sso-settings")
# POSTs that change nothing (simulations, questions): viewers may use them.
READ_ONLY_POSTS = ("/api/ai/chat", "/api/twin/simulate")
PORTAL_PREFIX = "/api/portal"
# Reachable while a password change is still pending.
PASSWORD_CHANGE_PATHS = ("/api/auth/password",)

LOGIN_OK = "auth.login"
LOGIN_FAILED = "auth.login_failed"
LOGIN_REFUSED = "auth.login_locked"
LOGIN_UNLOCKED = "auth.login_unlocked"

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


@dataclass
class CurrentUser:
    id: int | None
    username: str
    role: str
    full_name: str | None = None
    customer_id: int | None = None
    must_change_password: bool = False
    auth_source: str = "local"

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


LOCAL_ADMIN = CurrentUser(None, "local", "admin", "Auth nonaktif")


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


_NO_LOGIN_HASH = hash_password(secrets.token_hex(32))


def unusable_password() -> str:
    """For SSO users: a hash no password matches, still unique so it can stamp sessions."""
    return "none$" + secrets.token_hex(16)


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    check = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **_SCRYPT)
    return hmac.compare_digest(check, base64.b64decode(digest))


def password_problem(password: str, username: str) -> str | None:
    """None when the password meets the policy, else the reason in the UI language."""
    if len(password) < settings.password_min_length:
        return f"Password minimal {settings.password_min_length} karakter."
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        return "Password harus berisi huruf dan angka."
    if username and username.lower() in password.lower():
        return "Password tidak boleh memuat username."
    return None


def check_password(password: str, username: str) -> None:
    problem = password_problem(password, username)
    if problem:
        raise HTTPException(422, problem)


# --------------------------------------------------------------------------- sessions


def _stamp(user: m.User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def sign(db: Session, payload: str) -> str:
    return hmac.new(signing_key(db), payload.encode(), hashlib.sha256).hexdigest()


def session_token(db: Session, user: m.User) -> str:
    expires = int(time.time() + settings.session_hours * 3600)
    payload = f"{user.id}.{expires}.{_stamp(user)}"
    return f"{payload}.{sign(db, payload)}"


def user_from_token(db: Session, token: str | None) -> m.User | None:
    if not token or token.count(".") != 3:
        return None
    user_id, expires, stamp, signature = token.split(".")
    payload = f"{user_id}.{expires}.{stamp}"
    if not hmac.compare_digest(signature, sign(db, payload)):
        return None
    if not expires.isdigit() or int(expires) < time.time() or not user_id.isdigit():
        return None
    user = db.get(m.User, int(user_id))
    if user is None or not user.active or not hmac.compare_digest(stamp, _stamp(user)):
        return None
    return user


def as_current(user: m.User) -> CurrentUser:
    return CurrentUser(user.id, user.username, user.role, user.full_name, user.customer_id,
                       user.must_change_password, user.auth_source)


# --------------------------------------------------------------------------- built-in admin & lockout


def ensure_admin(db: Session) -> None:
    """First start: create the local admin. Several workers race here; the losers see the unique violation."""
    if db.scalar(select(m.User.id).limit(1)) is not None:
        return
    password = settings.admin_password or secrets.token_urlsafe(12)
    db.add(m.User(username=settings.admin_username, full_name="Administrator", role="admin",
                  password_hash=hash_password(password), must_change_password=True))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return
    if settings.admin_password:
        log.warning("NADIEN: admin lokal '%s' dibuat dengan NADIEN_ADMIN_PASSWORD; wajib ganti saat login pertama.",
                    settings.admin_username)
    else:
        # Not the password itself: logs are kept and shipped elsewhere.
        log.warning("NADIEN: admin lokal '%s' dibuat tanpa NADIEN_ADMIN_PASSWORD. Buat password-nya dengan: "
                    "python -m app.reset_password", settings.admin_username)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def locked_for(user: m.User) -> int:
    """Minutes left on a lockout, 0 when the account may try again."""
    until = _aware(user.locked_until)
    if not until:
        return 0
    left = (until - datetime.now(timezone.utc)).total_seconds()
    return max(int(left // 60) + 1, 1) if left > 0 else 0


def record_failure(user: m.User) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.login_account_max_attempts:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=settings.login_lock_minutes)
        user.failed_logins = 0


def _lock_window_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=settings.login_lock_minutes)


def address_blocked(db: Session, ip: str | None) -> bool:
    """Too many failed or refused logins from this address. Counted from the audit log, which every worker and
    replica shares. Refused ones count too, so a blocked address stops adding audit rows."""
    if not ip:
        return False
    failures = db.scalar(select(func.count()).select_from(m.AuditLog).where(
        m.AuditLog.ip == ip, m.AuditLog.at >= _lock_window_start(),
        m.AuditLog.action.in_((LOGIN_FAILED, LOGIN_REFUSED))))
    return (failures or 0) >= settings.login_ip_max_failures


def pair_locked(db: Session, username: str, ip: str | None) -> bool:
    """Too many failures for this username from this address since the last login or unlock. Unknown usernames
    count the same way, so the answer does not reveal which ones exist."""
    if not ip:
        return False
    name = func.lower(m.AuditLog.username) == username.lower()
    since = _lock_window_start()
    reset = db.scalar(select(func.max(m.AuditLog.at)).where(
        name, m.AuditLog.at >= since, m.AuditLog.action.in_((LOGIN_OK, LOGIN_UNLOCKED))))
    failures = db.scalar(select(func.count()).select_from(m.AuditLog).where(
        name, m.AuditLog.ip == ip, m.AuditLog.action == LOGIN_FAILED, m.AuditLog.at >= (reset or since)))
    return (failures or 0) >= settings.login_max_attempts


def stored_hash_for_login(user: m.User | None) -> str:
    """What to verify the password against: a hash nothing matches for accounts that cannot log in here, so all of
    them take as long as a real check and timing does not reveal which usernames exist."""
    if user is not None and user.active and user.auth_source == "local":
        return user.password_hash
    return _NO_LOGIN_HASH


# --------------------------------------------------------------------------- authorization & audit


def required_role(method: str, path: str) -> str:
    if path.startswith(PORTAL_PREFIX):
        return "customer"
    if method in ("GET", "HEAD", "OPTIONS") or path in READ_ONLY_POSTS:
        return "viewer"
    if path.startswith(ADMIN_WRITE_PREFIXES):
        return "admin"
    return "engineer"


def client_ip(request: Request) -> str | None:
    """The address the outermost trusted proxy saw (see trusted_proxy_hops): entries to its left are client-supplied."""
    hops = settings.trusted_proxy_hops
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if hops > 0 and forwarded:
        return forwarded[-min(hops, len(forwarded))][:64]
    return request.client.host[:64] if request.client else None


def audit(db: Session, username: str, action: str, target: str, detail: str | None = None,
          request: Request | None = None) -> None:
    """Added to the session; the caller commits."""
    db.add(m.AuditLog(at=datetime.now(timezone.utc), username=username, action=action, target=target[:512],
                      detail=detail, ip=client_ip(request) if request else None))


def _check_scope(current: CurrentUser, path: str, needed: str) -> None:
    if current.must_change_password and path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(403, "Ganti password terlebih dahulu.")
    if current.role == "customer":
        if needed != "customer" and path not in PASSWORD_CHANGE_PATHS:
            raise HTTPException(403, "Akun customer hanya dapat melihat layanannya sendiri.")
        return
    if needed == "customer":
        raise HTTPException(403, "Portal ini khusus akun customer.")
    if not current.at_least(needed):
        raise HTTPException(403, f"Butuh role {needed} untuk aksi ini (role Anda: {current.role}).")


def authorize(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Router-level guard for every /api router except /api/auth's public routes."""
    path = request.url.path
    if not settings.auth_enabled:
        current = LOCAL_ADMIN
    elif settings.ingest_token and path.startswith("/api/integrations") and hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {settings.ingest_token}"):
        current = CurrentUser(None, "ingest-token", "engineer")
    elif settings.job_token and path.startswith("/api/collectors") and hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {settings.job_token}"):
        current = CurrentUser(None, "job-token", "engineer")
    else:
        user = user_from_token(db, request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "Silakan login terlebih dahulu.")
        current = as_current(user)
    needed = required_role(request.method, path)
    if settings.auth_enabled:
        _check_scope(current, path, needed)
    request.state.user = current
    if request.method not in ("GET", "HEAD", "OPTIONS") and path not in READ_ONLY_POSTS:
        # Committed now, before the endpoint runs: background-job endpoints never commit this session,
        # and a refused write is worth seeing too.
        query = f"?{request.url.query}" if request.url.query else ""
        audit(db, current.username, request.method, path + query, request=request)
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
