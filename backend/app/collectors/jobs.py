"""Collector jobs: back up the database, then sync. Used by both the API and the CLI.

Every run that changes inventory or configuration data takes a backup first (newest
`NADIEN_BACKUP_KEEP` kept). If the backup fails the sync does not run. Utilization-only runs
skip the backup: they only add samples and run every few minutes, which would otherwise rotate
the useful pre-change backups out within hours.
"""

from collections.abc import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .. import models as m
from ..backup import BackupError, create_backup, is_backupable
from ..config import settings
from ..services.topology import discover_links
from .configs.apply import apply_config
from .configs.base import ParsedConfig
from .configs.gitweb import GitwebClient, apply_changes, list_changes
from .configs.oxidized import OxidizedClient, sync_oxidized
from .configs.rancid import FileSystem, open_from_settings, sync_rancid
from .observium.client import ObserviumClient
from .observium.rrd import RrdSource, open_source, sync_rrd
from .observium.sync import CSR_PARTS, sync_observium

CHANGES_INVENTORY = {"inventory", "neighbours"}
Progress = Callable[[str], None]


def _noop(_phase: str) -> None:
    pass


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


def run_observium(
    db: Session,
    parts: set[str],
    discover: bool = True,
    client: ObserviumClient | None = None,
    progress: Progress = _noop,
) -> dict:
    own_client = client is None
    client = client or ObserviumClient()  # fails on missing credentials before a backup slot is used
    try:
        if parts & CHANGES_INVENTORY:
            progress("backup")
            backup = backup_before(db, "observium")
        else:
            backup = {"status": "skipped", "detail": "utilization only"}
        report = sync_observium(db, client, parts, discover, progress).as_dict()
    finally:
        if own_client:
            client.close()
    return {"backup": backup, **report}


def run_observium_csr(db: Session, client: ObserviumClient | None = None, progress: Progress = _noop) -> dict:
    """The CSR Observium: cell-site routers, their ports and traffic (backup first; no LLDP, no link discovery)."""
    own_client = client is None
    client = client or ObserviumClient(base_url=settings.observium_csr_url)
    try:
        progress("backup")
        backup = backup_before(db, "observium-csr")
        report = sync_observium(db, client, CSR_PARTS, False, progress, source="csr").as_dict()
    finally:
        if own_client:
            client.close()
    return {"backup": backup, **report}


def run_rrd(db: Session, days: int = 90, source: "RrdSource | None" = None, progress: Progress = _noop) -> dict:
    """Daily peaks from Observium RRDs. No backup: like utilization-only syncs it only adds samples."""
    own = source is None
    source = source or open_source()
    try:
        return {"backup": {"status": "skipped", "detail": "utilization only"}, **sync_rrd(db, source, days, progress)}
    finally:
        if own:
            source.close()


def run_rancid(
    db: Session,
    discover: bool = True,
    opened: tuple[FileSystem, str, str] | None = None,
    progress: Progress = _noop,
) -> dict:
    fs, base, source = opened or open_from_settings()  # fail on configuration before taking a backup
    try:
        progress("backup")
        backup = backup_before(db, "rancid")
        progress("configs")
        return {"backup": backup, **sync_rancid(db, fs, base, source, discover)}
    finally:
        fs.close()


def run_gitweb(db: Session, full: bool = False, discover: bool = True, client: GitwebClient | None = None,
               progress: Progress = _noop) -> dict:
    """RANCID configs from gitweb. Lists changed files first and takes the backup only when there is
    something to write, so the daily run does not rotate useful backups out on quiet days."""
    own = client is None
    client = client or GitwebClient()  # fails on missing configuration before a backup slot is used
    try:
        progress("list")
        sync, work = list_changes(client, db, full, progress)
        if not work:
            return {"backup": {"status": "skipped", "detail": "no changes in gitweb"},
                    "source": client.base_url, "full": full, **sync.report.as_dict()}
        progress("backup")
        backup = backup_before(db, "gitweb")
        return {"backup": backup, **apply_changes(sync, work, discover)}
    finally:
        if own:
            client.close()


def run_oxidized(db: Session, discover: bool = True, client: OxidizedClient | None = None,
                 progress: Progress = _noop) -> dict:
    client = client or OxidizedClient()
    try:
        progress("backup")
        backup = backup_before(db, "oxidized")
        progress("configs")
        return {"backup": backup, **sync_oxidized(db, client, discover)}
    finally:
        client.close()


def run_config_import(db: Session, device: m.Device, cfg: ParsedConfig, discover: bool = True) -> dict:
    backup = backup_before(db, "config-import")
    result = apply_config(db, device, cfg)
    if discover:
        result["discovery"] = discover_links(db).model_dump()
    return {"backup": backup, **result}
