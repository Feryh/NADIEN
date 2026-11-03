from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from ..services.auth import (COOKIE, CurrentUser, Role, as_current, audit, current_user, hash_password,
                             needs_setup, require, session_token, user_from_token, verify_password)

# Reachable without a session: status, first-admin setup, login, logout.
public = APIRouter(prefix="/auth", tags=["Auth"])
# Behind the API guard like every other router.
account = APIRouter(prefix="/auth", tags=["Auth"])
users = APIRouter(prefix="/users", tags=["Auth"], dependencies=[Depends(require("admin"))])
audit_logs = APIRouter(prefix="/audit-logs", tags=["Auth"], dependencies=[Depends(require("admin"))])

USERNAME = r"^[A-Za-z0-9._-]{2,64}$"


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SetupIn(BaseModel):
    username: str = Field(pattern=USERNAME)
    full_name: str | None = Field(None, max_length=128)
    password: str = Field(min_length=8, max_length=256)


class UserIn(SetupIn):
    role: Role = "viewer"


class UserPatch(BaseModel):
    full_name: str | None = Field(None, max_length=128)
    role: Role | None = None
    active: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=256)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


def _user_dict(user: m.User) -> dict:
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "role": user.role,
            "active": user.active, "last_login_at": user.last_login_at, "created_at": user.created_at}


def _start_session(request: Request, response: Response, db: Session, user: m.User) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    response.set_cookie(COOKIE, session_token(db, user), max_age=int(settings.session_hours * 3600), httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")


@public.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    user = user_from_token(db, request.cookies.get(COOKIE)) if settings.auth_enabled else None
    return {"auth_enabled": settings.auth_enabled, "setup_needed": needs_setup(db),
            "user": as_current(user).__dict__ if user else None}


@public.post("/setup", status_code=201)
def setup_first_admin(payload: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Creates the first admin; refused once any user exists."""
    if not needs_setup(db):
        raise HTTPException(409, "Setup sudah dilakukan; login dengan akun yang ada.")
    user = m.User(username=payload.username, full_name=payload.full_name, password_hash=hash_password(payload.password),
                  role="admin")
    db.add(user)
    db.flush()
    _start_session(request, response, db, user)
    audit(db, user.username, "auth.setup", f"user {user.username}")
    db.commit()
    return as_current(user).__dict__


@public.post("/login")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(m.User).where(func.lower(m.User.username) == payload.username.strip().lower()))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        audit(db, payload.username.strip()[:64] or "-", "auth.login_failed", "login")
        db.commit()
        raise HTTPException(401, "Username atau password salah.")
    _start_session(request, response, db, user)
    audit(db, user.username, "auth.login", "login")
    db.commit()
    return as_current(user).__dict__


@public.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")


@account.post("/password", status_code=204)
def change_own_password(payload: PasswordIn, request: Request, response: Response, db: Session = Depends(get_db),
                        me: CurrentUser = Depends(current_user)):
    user = db.get(m.User, me.id) if me.id else None
    if user is None:
        raise HTTPException(422, "Akun ini tidak punya password (auth nonaktif).")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(422, "Password saat ini salah.")
    user.password_hash = hash_password(payload.new_password)
    _start_session(request, response, db, user)  # the old cookie stops working with the old hash
    db.commit()


@users.get("")
def list_users(db: Session = Depends(get_db)):
    return [_user_dict(u) for u in db.scalars(select(m.User).order_by(m.User.username))]


@users.post("", status_code=201)
def create_user(payload: UserIn, db: Session = Depends(get_db)):
    user = m.User(username=payload.username, full_name=payload.full_name, role=payload.role,
                  password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Username {payload.username} sudah ada.")
    return _user_dict(user)


def _active_admins(db: Session, except_id: int) -> int:
    return db.scalar(select(func.count(m.User.id)).where(m.User.role == "admin", m.User.active.is_(True),
                                                         m.User.id != except_id)) or 0


@users.patch("/{user_id}")
def update_user(user_id: int, payload: UserPatch, db: Session = Depends(get_db)):
    user = db.get(m.User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    demoted = (payload.role not in (None, "admin") or payload.active is False) and user.role == "admin"
    if demoted and not _active_admins(db, user.id):
        raise HTTPException(409, "Harus tetap ada minimal satu admin aktif.")
    for key in ("full_name", "role", "active"):
        value = getattr(payload, key)
        if value is not None:
            setattr(user, key, value)
    if payload.password:
        user.password_hash = hash_password(payload.password)
    db.commit()
    return _user_dict(user)


@audit_logs.get("")
def list_audit_logs(q: str | None = Query(None, max_length=100), limit: int = Query(200, ge=1, le=2000),
                    db: Session = Depends(get_db)):
    stmt = select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(m.AuditLog.username.ilike(like), m.AuditLog.action.ilike(like),
                              m.AuditLog.target.ilike(like)))
    return [{"id": a.id, "at": a.at, "username": a.username, "action": a.action, "target": a.target,
             "detail": a.detail} for a in db.scalars(stmt)]
