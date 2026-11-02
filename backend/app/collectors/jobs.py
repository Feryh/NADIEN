"""Collector jobs: back up the database, then sync. Used by both the API and the CLI.

Every run that changes inventory or configuration data takes a backup first (newest
`NADIEN_BACKUP_KEEP` kept). If the backup fails the sync does not run. Utilization-only runs
skip the backup: they only add samples and run every few minutes, which would otherwise rotate
the useful pre-change backups out within hours.
"""

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .. import models as m
from ..backup import BackupError, create_backup, is_backupable
from ..config import settings
from ..services.topology import discover_links
from .configs.apply import apply_config
from .configs.base import ParsedConfig
from .configs.oxidized import OxidizedClient, sync_oxidized
from .configs.rancid import open_from_settings, sync_rancid
from .observium.client import ObserviumClient
from .observium.sync import sync_observium

CHANGES_INVENTORY = {"inventory", "neighbours"}


class JobError(RuntimeError):
    pass


def _engine(db: Session) -> Engine:
    bind = db.get_bind()
    return bind if isinstance(bind, Engine) else bind.engine


def backup_before(db: Session, reason: str) -> dict:
    if not settings.backup_before_sync:
        return {"status": "disabled"}
    engine = _engine(db)
    if not is_backupable(engine):
        return {"status": "skipped", "detail": "in-memory or unsupported database"}
    db.commit()  # make sure nothing pending is left out of the copy
    try:
        info = create_backup(engine, reason)
    except BackupError as exc:
        raise JobError(f"Backup before {reason} failed, sync not started: {exc}") from exc
    return {"status": "ok", **info.as_dict()}


def run_observium(db: Session, parts: set[str], discover: bool = True, client: ObserviumClient | None = None) -> dict:
    own_client = client is None
    client = client or ObserviumClient()  # fails on missing credentials before a backup slot is used
    try:
        backup = backup_before(db, "observium") if parts & CHANGES_INVENTORY else {"status": "skipped", "detail": "utilization only"}
        report = sync_observium(db, client, parts, discover).as_dict()
    finally:
        if own_client:
            client.close()
    return {"backup": backup, **report}


def run_rancid(db: Session, discover: bool = True) -> dict:
    fs, base, source = open_from_settings()  # fail on configuration before taking a backup
    try:
        backup = backup_before(db, "rancid")
        return {"backup": backup, **sync_rancid(db, fs, base, source, discover)}
    finally:
        fs.close()


def run_oxidized(db: Session, discover: bool = True) -> dict:
    client = OxidizedClient()
    try:
        backup = backup_before(db, "oxidized")
        return {"backup": backup, **sync_oxidized(db, client, discover)}
    finally:
        client.close()


def run_config_import(db: Session, device: m.Device, cfg: ParsedConfig, discover: bool = True) -> dict:
    backup = backup_before(db, "config-import")
    result = apply_config(db, device, cfg)
    if discover:
        result["discovery"] = discover_links(db).model_dump()
    return {"backup": backup, **result}
