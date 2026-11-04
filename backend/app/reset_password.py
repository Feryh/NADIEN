"""Reset a local user's password from the server console, for when nobody can log in any more
(admin password lost or the account locked). Prints a temporary password that must be changed at the next login.

    python -m app.reset_password            # the built-in admin (NADIEN_ADMIN_USERNAME, default "admin")
    python -m app.reset_password <username>
"""

import secrets
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .database import SessionLocal, init_schema
from .services.auth import hash_password, password_problem


class ResetError(Exception):
    pass


def temporary_password(username: str) -> str:
    while True:
        password = secrets.token_urlsafe(12)
        if not password_problem(password, username):
            return password


def reset_password(db: Session, username: str) -> str:
    """New temporary password; also unlocks and re-enables the account, and ends its sessions (new hash)."""
    user = db.scalars(select(m.User).where(m.User.username == username)).one_or_none()
    if user is None:
        raise ResetError(f"user '{username}' tidak ditemukan")
    if user.auth_source != "local":
        raise ResetError(f"user '{username}' login lewat {user.auth_source}; password-nya diatur di sana")
    password = temporary_password(username)
    user.password_hash = hash_password(password)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    user.active = True
    db.commit()
    return password


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(__doc__)
        return 2
    username = args[0] if args else settings.admin_username
    init_schema()
    with SessionLocal() as db:
        try:
            password = reset_password(db, username)
        except ResetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"Password sementara untuk '{username}': {password}")
    print("Login dengan password ini, lalu ganti password saat diminta.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
