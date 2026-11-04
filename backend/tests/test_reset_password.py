"""Console password reset for a local user who can no longer log in."""

from datetime import datetime, timedelta, timezone

import pytest

from app import models as m
from app.reset_password import ResetError, reset_password
from app.services.auth import hash_password, locked_for, verify_password


def add_user(db, username: str, **kw) -> m.User:
    user = m.User(username=username, role="admin", password_hash=hash_password("Lama-12345"), **kw)
    db.add(user)
    db.commit()
    return user


def test_should_unlock_and_require_change_when_admin_password_is_reset(db):
    admin = add_user(db, "admin", failed_logins=3, locked_until=datetime.now(timezone.utc) + timedelta(minutes=10))
    password = reset_password(db, "admin")
    db.refresh(admin)
    assert verify_password(password, admin.password_hash)
    assert (locked_for(admin), admin.failed_logins, admin.must_change_password) == (0, 0, True)


def test_should_refuse_reset_when_user_does_not_exist(db):
    with pytest.raises(ResetError):
        reset_password(db, "tidak-ada")


def test_should_refuse_reset_when_user_logs_in_through_sso(db):
    add_user(db, "sso.user", auth_source="oidc")
    with pytest.raises(ResetError):
        reset_password(db, "sso.user")
