import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from ..services.auth import (COOKIE, LOGIN_FAILED, LOGIN_OK, LOGIN_REFUSED, LOGIN_UNLOCKED, CurrentUser, Role,
                             address_blocked, as_current, audit, check_password, client_ip, current_user,
                             hash_password, locked_for, pair_locked, record_failure, require, session_token,
                             stored_hash_for_login, user_from_token, verify_password)

log = logging.getLogger(__name__)

# Reachable without a session: status, login, logout (and the SSO redirects in routers/sso.py).
public = APIRouter(prefix="/auth", tags=["Auth"])
# Behind the API guard like every other router.
account = APIRouter(prefix="/auth", tags=["Auth"])
users = APIRouter(prefix="/users", tags=["Auth"], dependencies=[Depends(require("admin"))])
audit_logs = APIRouter(prefix="/audit-logs", tags=["Auth"], dependencies=[Depends(require("admin"))])

USERNAME = r"^[A-Za-z0-9._@-]{2,64}$"


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class UserIn(BaseModel):
    username: str = Field(pattern=USERNAME)
    full_name: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=256)
    password: str = Field(max_length=256)
    role: Role = "viewer"
    customer_id: int | None = None


class UserPatch(BaseModel):
    full_name: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=256)
    role: Role | None = None
    customer_id: int | None = None
    active: bool | None = None
    password: str | None = Field(None, max_length=256)  # admin reset: the user must change it at next login
    unlock: bool = False


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def user_dict(user: m.User, customers: dict[int, str] | None = None) -> dict:
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "email": user.email,
            "role": user.role, "active": user.active, "auth_source": user.auth_source,
            "customer_id": user.customer_id,
            "customer": (customers or {}).get(user.customer_id) if user.customer_id else None,
            "must_change_password": user.must_change_password, "locked_minutes": locked_for(user),
            "last_login_at": user.last_login_at, "created_at": user.created_at}


def start_session(request: Request, response: Response, db: Session, user: m.User) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    user.failed_logins, user.locked_until = 0, None
    response.set_cookie(COOKIE, session_token(db, user), max_age=int(settings.session_hours * 3600), httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")


@public.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    user = user_from_token(db, request.cookies.get(COOKIE)) if settings.auth_enabled else None
    sso = db.get(m.SsoSettings, 1)
    return {"auth_enabled": settings.auth_enabled,
            "user": as_current(user).__dict__ if user else None,
            "sso": {"enabled": bool(sso and sso.enabled), "display_name": sso.display_name if sso else "SSO"},
            "password_min_length": settings.password_min_length}


@public.post("/login")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    name = payload.username.strip()
    ip = client_ip(request)
    wait = {"Retry-After": str(settings.login_lock_minutes * 60)}
    if address_blocked(db, ip):
        log.warning("login diblokir untuk alamat %s: terlalu banyak percobaan gagal", ip)
        raise HTTPException(429, "Terlalu banyak percobaan login dari alamat ini. Coba lagi nanti.", headers=wait)
    user = db.scalar(select(m.User).where(func.lower(m.User.username) == name.lower()))
    label = user.username if user is not None else name[:64] or "-"
    if (user is not None and locked_for(user)) or pair_locked(db, label, ip):
        audit(db, label, LOGIN_REFUSED, "login", request=request)
        db.commit()
        raise HTTPException(429, "Terlalu banyak percobaan gagal. Coba lagi nanti atau hubungi admin.", headers=wait)
    stored = stored_hash_for_login(user)
    if not verify_password(payload.password, stored):  # the stored hash of an account that cannot log in never matches
        if user is not None and stored == user.password_hash:
            record_failure(user)
        audit(db, label, LOGIN_FAILED, "login", request=request)
        db.commit()
        raise HTTPException(401, "Username atau password salah.")
    start_session(request, response, db, user)
    audit(db, user.username, LOGIN_OK, "login", request=request)
    db.commit()
    return as_current(user).__dict__


@public.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")


@account.post("/password", status_code=204)
def change_own_password(payload: PasswordIn, request: Request, response: Response, db: Session = Depends(get_db),
                        me: CurrentUser = Depends(current_user)):
    user = db.get(m.User, me.id) if me.id else None
    if user is None or user.auth_source != "local":
        raise HTTPException(422, "Password akun ini tidak dikelola NADIEN.")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(422, "Password saat ini salah.")
    if payload.new_password == payload.current_password:
        raise HTTPException(422, "Password baru harus berbeda dari yang lama.")
    check_password(payload.new_password, user.username)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    start_session(request, response, db, user)  # the old cookie stops working with the old hash
    audit(db, user.username, "auth.password_change", f"user {user.username}", request=request)
    db.commit()


def _customers(db: Session) -> dict[int, str]:
    return dict(db.execute(select(m.Customer.id, m.Customer.name)).tuples().all())


def _check_customer_link(db: Session, role: str, customer_id: int | None) -> None:
    if role == "customer":
        if customer_id is None:
            raise HTTPException(422, "Akun customer harus dihubungkan ke data customer.")
        if db.get(m.Customer, customer_id) is None:
            raise HTTPException(422, "Customer tidak ditemukan.")


@users.get("")
def list_users(db: Session = Depends(get_db)):
    customers = _customers(db)
    return [user_dict(u, customers) for u in db.scalars(select(m.User).order_by(m.User.username))]


@users.post("", status_code=201)
def create_user(payload: UserIn, request: Request, db: Session = Depends(get_db),
                me: CurrentUser = Depends(current_user)):
    _check_customer_link(db, payload.role, payload.customer_id)
    check_password(payload.password, payload.username)
    user = m.User(username=payload.username, full_name=payload.full_name, email=payload.email, role=payload.role,
                  customer_id=payload.customer_id if payload.role == "customer" else None,
                  password_hash=hash_password(payload.password), must_change_password=True)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Username {payload.username} sudah ada.")
    audit(db, me.username, "user.create", f"user {user.username}", f"role {user.role}", request)
    db.commit()
    return user_dict(user, _customers(db))


def _active_admins(db: Session, except_id: int) -> int:
    return db.scalar(select(func.count(m.User.id)).where(m.User.role == "admin", m.User.active.is_(True),
                                                         m.User.id != except_id)) or 0


@users.patch("/{user_id}")
def update_user(user_id: int, payload: UserPatch, request: Request, db: Session = Depends(get_db),
                me: CurrentUser = Depends(current_user)):
    user = db.get(m.User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    demoted = (payload.role not in (None, "admin") or payload.active is False) and user.role == "admin"
    if demoted and not _active_admins(db, user.id):
        raise HTTPException(409, "Harus tetap ada minimal satu admin aktif.")
    role = payload.role or user.role
    customer_id = payload.customer_id if payload.customer_id is not None else user.customer_id
    _check_customer_link(db, role, customer_id)
    changes = []
    for key in ("full_name", "email", "role", "active"):
        value = getattr(payload, key)
        if value is not None and value != getattr(user, key):
            changes.append(f"{key}: {getattr(user, key)} → {value}")
            setattr(user, key, value)
    user.customer_id = customer_id if role == "customer" else None
    if payload.password:
        if user.auth_source != "local":
            raise HTTPException(422, "Password akun SSO dikelola di identity provider.")
        check_password(payload.password, user.username)
        user.password_hash = hash_password(payload.password)
        user.must_change_password = True
        changes.append("password direset")
    if payload.unlock or payload.password:
        user.failed_logins, user.locked_until = 0, None
        audit(db, user.username, LOGIN_UNLOCKED, f"user {user.username}", f"oleh {me.username}", request)
        if payload.unlock:
            changes.append("dibuka kuncinya")
    audit(db, me.username, "user.update", f"user {user.username}", "; ".join(changes) or None, request)
    db.commit()
    return user_dict(user, _customers(db))


@users.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db),
                me: CurrentUser = Depends(current_user)):
    user = db.get(m.User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == me.id:
        raise HTTPException(409, "Tidak bisa menghapus akun sendiri.")
    if user.role == "admin" and not _active_admins(db, user.id):
        raise HTTPException(409, "Harus tetap ada minimal satu admin aktif.")
    audit(db, me.username, "user.delete", f"user {user.username}", f"role {user.role}", request)
    db.delete(user)
    db.commit()


def _audit_query(q: str | None, username: str | None, action: str | None, date_from: date | None,
                 date_to: date | None):
    stmt = select(m.AuditLog).order_by(m.AuditLog.id.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(m.AuditLog.username.ilike(like), m.AuditLog.action.ilike(like),
                              m.AuditLog.target.ilike(like), m.AuditLog.detail.ilike(like)))
    if username:
        stmt = stmt.where(m.AuditLog.username == username)
    if action:
        stmt = stmt.where(m.AuditLog.action.like(f"{action}%"))
    if date_from:
        stmt = stmt.where(m.AuditLog.at >= datetime.combine(date_from, datetime.min.time(), timezone.utc))
    if date_to:
        stmt = stmt.where(m.AuditLog.at < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), timezone.utc))
    return stmt


def _audit_dict(a: m.AuditLog) -> dict:
    return {"id": a.id, "at": a.at, "username": a.username, "action": a.action, "target": a.target,
            "detail": a.detail, "ip": a.ip}


@audit_logs.get("")
def list_audit_logs(q: str | None = Query(None, max_length=100), username: str | None = Query(None, max_length=64),
                    action: str | None = Query(None, max_length=32), date_from: date | None = None,
                    date_to: date | None = None, limit: int = Query(200, ge=1, le=2000),
                    db: Session = Depends(get_db)):
    stmt = _audit_query(q, username, action, date_from, date_to).limit(limit)
    return [_audit_dict(a) for a in db.scalars(stmt)]


@audit_logs.get("/export")
def export_audit_logs(q: str | None = Query(None, max_length=100), username: str | None = Query(None, max_length=64),
                      action: str | None = Query(None, max_length=32), date_from: date | None = None,
                      date_to: date | None = None, db: Session = Depends(get_db)):
    """CSV of the filtered log (at most 100k rows), for audits outside NADIEN."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "at", "username", "action", "target", "detail", "ip"])
    for a in db.scalars(_audit_query(q, username, action, date_from, date_to).limit(100_000)):
        writer.writerow([a.id, a.at.isoformat() if a.at else "", a.username, a.action, a.target, a.detail or "",
                         a.ip or ""])
    name = f"nadien-audit-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
