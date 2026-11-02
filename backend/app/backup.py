"""Database backups taken before collector runs, with a fixed retention (newest N kept).

SQLite: online copy through the sqlite3 backup API (consistent even while the API is running).
PostgreSQL: `pg_dump --format=custom` (needs the PostgreSQL client tools on the PATH).

Files are named `nadien-<UTC timestamp>-<reason>.<db|dump>` so name order is time order.

    python -m app.backup list
    python -m app.backup create [reason]
    python -m app.backup restore <file name>   # takes a "pre-restore" backup first
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import URL, Engine, make_url

from .config import settings

NAME_RE = re.compile(r"^nadien-(\d{8}-\d{6})(?:-(\d+))?-([a-z0-9-]+)\.(db|dump)$")


class BackupError(RuntimeError):
    pass


@dataclass
class BackupInfo:
    name: str
    path: str
    size_bytes: int
    created_at: str
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _url(source: Engine | URL | str | None) -> URL:
    if source is None:
        return make_url(settings.database_url)
    if isinstance(source, Engine):
        return source.url
    return make_url(source)


def is_backupable(source: Engine | URL | str | None = None) -> bool:
    url = _url(source)
    if url.get_backend_name() == "sqlite":
        return bool(url.database) and url.database != ":memory:"
    return url.get_backend_name() == "postgresql"


def backup_dir(source: Engine | URL | str | None = None) -> Path:
    if settings.backup_dir:
        return Path(settings.backup_dir)
    url = _url(source)
    if url.get_backend_name() == "sqlite" and url.database:
        return Path(url.database).resolve().parent / "nadien-backups"
    return Path("backups").resolve()


def _slug(reason: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")[:40] or "manual"


def _info(path: Path) -> BackupInfo | None:
    match = NAME_RE.match(path.name)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    return BackupInfo(name=path.name, path=str(path), size_bytes=path.stat().st_size,
                      created_at=stamp.isoformat(), reason=match.group(3))


def _order(info: BackupInfo) -> tuple[str, int]:
    match = NAME_RE.match(info.name)
    assert match is not None
    return match.group(1), int(match.group(2) or 1)  # timestamp, then same-second sequence


def list_backups(source: Engine | URL | str | None = None) -> list[BackupInfo]:
    """Newest first."""
    directory = backup_dir(source)
    if not directory.is_dir():
        return []
    infos = [i for p in directory.iterdir() if p.is_file() and (i := _info(p))]
    return sorted(infos, key=_order, reverse=True)


def prune(source: Engine | URL | str | None = None, keep: int | None = None) -> list[str]:
    keep = settings.backup_keep if keep is None else keep
    removed = []
    for info in list_backups(source)[max(keep, 1):]:
        Path(info.path).unlink(missing_ok=True)
        removed.append(info.name)
    return removed


def _pg_env(url: URL) -> dict[str, str]:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in {
        "PGHOST": url.host, "PGPORT": url.port, "PGUSER": url.username,
        "PGPASSWORD": url.password, "PGDATABASE": url.database,
    }.items() if v is not None})
    return env


def create_backup(source: Engine | URL | str | None = None, reason: str = "manual") -> BackupInfo:
    url = _url(source)
    if not is_backupable(url):
        raise BackupError(f"Backup not supported for {url.get_backend_name()} database '{url.database}'")
    directory = backup_dir(url)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Cannot create backup directory {directory}: {exc}") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ext = "db" if url.get_backend_name() == "sqlite" else "dump"
    # Same-second runs get the next sequence number. Never reuse a pruned number: the
    # new file would sort as the oldest and be pruned right away.
    seqs = [_order(b)[1] for b in list_backups(url) if _order(b)[0] == stamp]
    seq = max(seqs) + 1 if seqs else 1
    infix = f"-{seq}" if seq > 1 else ""
    target = directory / f"nadien-{stamp}{infix}-{_slug(reason)}.{ext}"
    tmp = target.with_suffix(target.suffix + ".part")

    try:
        if ext == "db":
            if not Path(url.database).exists():
                raise BackupError(f"SQLite database not found: {url.database}")
            src = sqlite3.connect(url.database)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        else:
            pg_dump = shutil.which("pg_dump")
            if not pg_dump:
                raise BackupError("pg_dump not found: install the PostgreSQL client tools")
            result = subprocess.run([pg_dump, "--format=custom", "--no-owner", f"--file={tmp}"],
                                    env=_pg_env(url), capture_output=True, text=True, timeout=1800)
            if result.returncode != 0:
                raise BackupError(f"pg_dump failed: {result.stderr.strip()[:500]}")
        tmp.replace(target)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"Backup to {target} failed: {exc}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    prune(url)
    info = _info(target)
    assert info is not None
    return info


def restore_backup(name: str, source: Engine | URL | str | None = None) -> BackupInfo:
    """Restore a backup over the current database, after saving the current state as 'pre-restore'."""
    url = _url(source)
    match = next((b for b in list_backups(url) if b.name == name), None)
    if match is None:
        raise BackupError(f"Backup {name} not found in {backup_dir(url)}")
    safety = create_backup(url, "pre-restore")
    if url.get_backend_name() == "sqlite":
        src = sqlite3.connect(match.path)
        dst = sqlite3.connect(url.database)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    else:
        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            raise BackupError("pg_restore not found: install the PostgreSQL client tools")
        result = subprocess.run([pg_restore, "--clean", "--if-exists", "--no-owner", f"--dbname={url.database}", match.path],
                                env=_pg_env(url), capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise BackupError(f"pg_restore failed (current state saved as {safety.name}): {result.stderr.strip()[:500]}")
    return match


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "list"
    try:
        if cmd == "list":
            print(f"{backup_dir()}  (keep {settings.backup_keep})")
            for b in list_backups():
                print(f"  {b.name}  {b.size_bytes / 1e6:8.2f} MB  {b.reason}")
        elif cmd == "create":
            print(create_backup(reason=args[1] if len(args) > 1 else "manual").path)
        elif cmd == "restore" and len(args) > 1:
            print(f"restored {restore_backup(args[1]).name} (stop the API first for PostgreSQL)")
        else:
            print(__doc__)
            return 2
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
