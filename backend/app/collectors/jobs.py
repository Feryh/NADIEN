"""Collector jobs: back up the database, then sync. Used by both the API and the CLI.

Every run that changes inventory or configuration data takes a backup first (newest
`NADIEN_BACKUP_KEEP` kept). If the backup fails the sync does not run. Utilization-only runs
skip the backup: they only add samples and run every few minutes, which would otherwise rotate
the useful pre-change backups out within hours.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

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
from .observium.graph_api import GraphApiSource
from .observium.rrd import RrdSource, open_source, sync_rrd
from .observium.sync import ALL_PARTS, CSR_PARTS, SyncReport, sync_observium

CHANGES_INVENTORY = {"inventory", "neighbours"}
Progress = Callable[[str], None]
UTILIZATION_ONLY = {"status": "skipped", "detail": "utilization only"}


class _Closable(Protocol):
    def close(self) -> None: ...


Opened = TypeVar("Opened", bound=_Closable)


@contextmanager
def _opened(given: Opened | None, factory: Callable[[], Opened]) -> Iterator[Opened]:
    """Use the caller's client/source as is, or open one from settings and close it afterwards."""
    if given is not None:
        yield given
        return
    opened = factory()
    try:
        yield opened
    finally:
        opened.close()


def _csr_client() -> ObserviumClient:
    return ObserviumClient(base_url=settings.observium_csr_url)


def _csr_rrd_source() -> RrdSource:
    return GraphApiSource(_csr_client())


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
    with _opened(client, ObserviumClient) as opened:  # fails on missing credentials before a backup slot is used
        if parts & CHANGES_INVENTORY:
            progress("backup")
            backup = backup_before(db, "observium")
        else:
            backup = UTILIZATION_ONLY
        report = sync_observium(db, opened, parts, discover, progress).as_dict()
    return {"backup": backup, **report}


def run_observium_csr(db: Session, client: ObserviumClient | None = None, progress: Progress = _noop) -> dict:
    """The CSR Observium: cell-site routers, their ports and traffic (backup first; no LLDP, no link discovery)."""
    with _opened(client, _csr_client) as opened:
        progress("backup")
        backup = backup_before(db, "observium-csr")
        report = sync_observium(db, opened, CSR_PARTS, False, progress, source="csr").as_dict()
    return {"backup": backup, **report}


def run_rrd(db: Session, days: int = 90, source: "RrdSource | None" = None, progress: Progress = _noop) -> dict:
    """Daily peaks from Observium RRDs. No backup: like utilization-only syncs it only adds samples."""
    with _opened(source, open_source) as opened:
        return {"backup": UTILIZATION_ONLY, **sync_rrd(db, opened, days, progress)}


def run_rrd_csr(db: Session, days: int = 90, source: "RrdSource | None" = None, progress: Progress = _noop) -> dict:
    """Daily peaks of links only the CSR Observium can see, through its graph API. No backup: it only adds samples."""
    with _opened(source, _csr_rrd_source) as opened:
        return {"backup": UTILIZATION_ONLY, **sync_rrd(db, opened, days, progress, server="csr")}


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
    def backup() -> dict:
        progress("backup")
        return backup_before(db, "gitweb")

    with _opened(client, GitwebClient) as opened:  # fails on missing configuration before a backup slot is used
        return _gitweb_sync(db, opened, full, discover, progress, backup)


def _gitweb_sync(db: Session, client: GitwebClient, full: bool, discover: bool, progress: Progress,
                 backup: Callable[[], dict]) -> dict:
    progress("list")
    sync, work = list_changes(client, db, full, progress)
    if not work:
        return {"backup": {"status": "skipped", "detail": "no changes in gitweb"},
                "source": client.base_url, "full": full, **sync.report.as_dict()}
    taken = backup()  # reports its own phase: inside sync-all no backup is taken here
    return {"backup": taken, **apply_changes(sync, work, discover)}


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


# --------------------------------------------------------------------------- sync-all


@dataclass
class SyncAllSources:
    """Where each step of `run_all` gets its client or RRD source; tests swap in fakes."""

    observium: Callable[[], ObserviumClient] = ObserviumClient
    observium_csr: Callable[[], ObserviumClient] = _csr_client
    gitweb: Callable[[], GitwebClient] = GitwebClient
    rrd: Callable[[], RrdSource] = open_source
    rrd_csr: Callable[[], RrdSource] = _csr_rrd_source


SYNC_ALL_BACKUP_TAKEN = {"status": "skipped", "detail": "taken once at the start of sync-all"}


def _observium_configured(url: str | None) -> bool:
    """An address plus a login: without them (demo, dev) the Observium steps are skipped, not failed every day."""
    return bool(url) and bool(settings.observium_token or (settings.observium_user and settings.observium_password))


def _rrd_configured() -> bool:
    if settings.observium_rrd_source == "api":
        return _observium_configured(settings.observium_url)
    if settings.observium_rrd_source == "ssh":
        return bool(settings.observium_ssh_user)
    return True  # local rrdtool needs no access settings


class _SyncAll:
    """Runs the steps of one sync-all job; a failed step is recorded and the next one still runs."""

    def __init__(self, db: Session, progress: Progress):
        self.db = db
        self.progress = progress
        self.steps: list[dict[str, Any]] = []

    def skip(self, name: str, detail: str) -> None:
        self.steps.append({"step": name, "status": "skipped", "duration_s": 0.0, "detail": detail})

    def run(self, name: str, fn: Callable[[Progress], Any]) -> Any:
        """The step's result, or None when it failed."""
        started = time.monotonic()
        self.progress(name)
        try:
            result = fn(lambda phase: self.progress(f"{name}: {phase}"))
        # Any failure of one source (network, credentials, parser, database) must not stop the others.
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()  # the next step starts from a clean session
            self.steps.append({"step": name, "status": "failed", "duration_s": _since(started),
                               "error": f"{type(exc).__name__}: {exc}"})
            return None
        report = result.as_dict() if isinstance(result, SyncReport) else result
        self.steps.append({"step": name, "status": "ok", "duration_s": _since(started), "report": report})
        return result

    def report(self) -> dict[str, Any]:
        failed = [s["step"] for s in self.steps if s["status"] == "failed"]
        result: dict[str, Any] = {"status": "failed" if failed else "ok", "failed_steps": failed, "steps": self.steps}
        if failed:
            result["error"] = f"Langkah gagal: {', '.join(failed)}"
        return result


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)


def _observium_step(db: Session, factory: Callable[[], ObserviumClient], parts: frozenset[str],
                    source: str) -> Callable[[Progress], SyncReport]:
    def step(progress: Progress) -> SyncReport:
        with _opened(None, factory) as client:
            return sync_observium(db, client, parts, False, progress, source=source)
    return step


def _rrd_step(db: Session, factory: Callable[[], RrdSource], days: int, backfill_days: int,
              server: str) -> Callable[[Progress], dict]:
    def step(progress: Progress) -> dict:
        with _opened(None, factory) as source:
            return sync_rrd(db, source, days, progress, server=server, backfill_days=backfill_days)
    return step


def _configs_step(db: Session, factory: Callable[[], GitwebClient], full: bool) -> Callable[[Progress], dict]:
    def step(progress: Progress) -> dict:
        with _opened(None, factory) as client:
            return _gitweb_sync(db, client, full, False, progress, lambda: SYNC_ALL_BACKUP_TAKEN)
    return step


def run_all(db: Session, rrd_days: int = 2, rrd_backfill_days: int = 90, gitweb_full: bool = False,
            progress: Progress = _noop, sources: SyncAllSources | None = None) -> dict:
    """Everything in one job: one backup, Observium main + CSR, gitweb configs, one topology discovery once all
    inventory and config data is in, then RRD history (short routine window, backfill for links without history)."""
    sources = sources or SyncAllSources()
    run = _SyncAll(db, progress)
    if run.run("backup", lambda _p: backup_before(db, "sync-all")) is None:
        return run.report()  # no restore point: write nothing
    main = None
    if _observium_configured(settings.observium_url):
        main = run.run("observium", _observium_step(db, sources.observium, ALL_PARTS, "main"))
    else:
        run.skip("observium", "Observium not configured")
    csr = _observium_configured(settings.observium_csr_url)
    if csr:
        run.run("observium_csr", _observium_step(db, sources.observium_csr, CSR_PARTS, "csr"))
    else:
        run.skip("observium_csr", "Observium CSR not configured")
    if settings.gitweb_url:
        run.run("configs", _configs_step(db, sources.gitweb, gitweb_full))
    else:
        run.skip("configs", "gitweb not configured")
    # Phantom-port cleanup only with a complete list of the main Observium's ports.
    valid = main.valid_iface_ids if main is not None else None
    run.run("discovery", lambda _p: discover_links(db, valid_iface_ids=valid).model_dump())
    if _rrd_configured():
        run.run("rrd", _rrd_step(db, sources.rrd, rrd_days, rrd_backfill_days, "main"))
    else:
        run.skip("rrd", "RRD access not configured")
    if csr:
        run.run("rrd_csr", _rrd_step(db, sources.rrd_csr, rrd_days, rrd_backfill_days, "csr"))
    else:
        run.skip("rrd_csr", "Observium CSR not configured")
    return run.report()
