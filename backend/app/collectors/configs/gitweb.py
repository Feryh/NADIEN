"""RANCID repositories published through gitweb — every repo (CSR-*, METRO-*, OLT-*, BNG-*, ...).

gitweb pages used (plain HTTP GET, read-only):
    ?a=project_index                         repository list ("CSR-METRO-ZTE/.git owner")
    ?p=<repo>;a=tree;f=configs;hb=HEAD       config files with their blob hashes
    ?p=<repo>;a=blob_plain;f=router.db       RANCID device list: name;type;state
    ?p=<repo>;a=blob_plain;f=configs/<name>  the newest config of one device

Each file's blob hash is stored (`gitweb_files`), so the daily run downloads only files RANCID changed.
Precedence: Observium stays the source of truth for devices it monitors; gitweb fills the config facts
(LAG, IPs, loopback, IS-IS) and creates devices Observium does not have at all — the cell-site routers
of the CSR-* repos above all. Devices in a CSR-* repo always get role `csr`.
"""

import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ...services.areas import pop_code_for_hostname
from ...services.topology import discover_links, norm_host
from ..observium.parsers import DeviceRecord, infer_pop, role_from_hostname, short_hostname
from ..observium.sync import UNASSIGNED as UNASSIGNED_POP
from ..observium.sync import SyncReport, _Writer
from .apply import apply_config
from .base import ParsedConfig, speed_from_name
from .rancid import parse_rancid, rancid_type

Progress = Callable[[str], None]

_BLOB = re.compile(r'a=blob;f=configs/([^;"&]+);h=([0-9a-f]{40})')
# First match wins: CSR-METRO-ZTE is a cell-site repo, not a metro one.
REPO_ROLES: list[tuple[str, str]] = [
    ("CSR", "csr"), ("OLT", "olt"), ("BNG", "bng"), ("CGN", "pe"), ("BACKBONE", "p"), ("METRO", "agg"),
    ("SPE", "pe"), ("IGW", "pe"), ("ROUTER", "pe"),
]
# Parser key -> (vendor, OS family) for devices created from their config.
VENDOR_BY_PARSER = {
    "zte": ("ZTE", "ZXROS"), "fiberhome": ("FiberHome", "FOS"), "vrp": ("Huawei", "VRP"),
    "iosxr": ("Cisco", "IOS-XR"), "ios": ("Cisco", "IOS"), "junos": ("Juniper", "Junos"), "sros": ("Nokia", "SR OS"),
}
_MODEL_PATTERNS = [
    re.compile(r"^!VERSION:\s*(ZXCTN\s+\S+|ZXR10\s+\S+)", re.M),
    re.compile(r"^!VERSION:\s+(R\d{3,4}[-\w]*)\s*$", re.M),  # FiberHome CiTRANS R820-7C
    re.compile(r"^!Chassis type:\s*(\S+)", re.M),
]


class GitwebError(RuntimeError):
    pass


class GitwebClient:
    def __init__(self, base_url: str | None = None, user: str | None = None, password: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        base_url = base_url or settings.gitweb_url
        if not base_url:
            raise GitwebError("gitweb not configured: set NADIEN_GITWEB_URL (e.g. http://172.17.12.232/gitweb/)")
        user, password = user or settings.gitweb_user, password or settings.gitweb_password
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._http = httpx.Client(auth=(user, password) if user and password else None,
                                  verify=settings.gitweb_verify_tls, timeout=60, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _get(self, params: dict[str, str]) -> str:
        try:
            resp = self._http.get(self.base_url, params=params)
        except httpx.HTTPError as exc:
            raise GitwebError(f"gitweb unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise GitwebError("gitweb rejected the credentials (401)")
        if resp.status_code >= 400:
            raise GitwebError(f"gitweb {params.get('a')} {params.get('p', '')} returned HTTP {resp.status_code}")
        return resp.text

    def repos(self) -> list[str]:
        names = [line.split()[0] for line in self._get({"a": "project_index"}).splitlines() if line.strip()]
        excluded = {r.lower().removesuffix("/.git") for r in settings.gitweb_exclude_repos}
        return [n for n in names if repo_name(n) and repo_name(n).lower() not in excluded]

    def config_files(self, repo: str) -> list[tuple[str, str]]:
        """(file name, blob hash) of every file in configs/ at HEAD."""
        html = self._get({"p": repo, "a": "tree", "f": "configs", "hb": "HEAD"})
        return sorted({(unquote(name), sha) for name, sha in _BLOB.findall(html) if not name.startswith(".")})

    def router_db(self, repo: str) -> dict[str, tuple[str, str]]:
        """Lower-case device name -> (RANCID type, state)."""
        try:
            text = self._get({"p": repo, "a": "blob_plain", "f": "router.db", "hb": "HEAD"})
        except GitwebError:
            return {}
        return parse_router_db(text)

    def blob(self, repo: str, name: str) -> str:
        return self._get({"p": repo, "a": "blob_plain", "f": f"configs/{name}", "hb": "HEAD"})


def repo_name(repo: str) -> str:
    """'CSR-METRO-ZTE/.git' -> 'CSR-METRO-ZTE' ('' for the bare root '.git')."""
    return repo.strip().removesuffix(".git").rstrip("/")


def parse_router_db(text: str) -> dict[str, tuple[str, str]]:
    """RANCID router.db rows: `name;type;state` (RANCID 3) or `name:type:state` (RANCID 2)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = ";" if ";" in line else ":"
        parts = [p.strip() for p in line.split(sep)]
        if len(parts) >= 3:
            out[parts[0].lower()] = (parts[1].lower(), parts[2].lower())
    return out


def role_for_repo(repo: str) -> str | None:
    name = repo_name(repo).upper()
    return next((role for token, role in REPO_ROLES if token in name.split("-")), None)


def model_from_text(text: str) -> str | None:
    head = text[:6000]
    return next((match.group(1).strip() for p in _MODEL_PATTERNS if (match := p.search(head))), None)


@dataclass
class _Report:
    counts: Counter = field(default_factory=Counter)
    created: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    unsupported: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    repos: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"counts": dict(self.counts), "devices_created": self.created[:200],
                "unmatched": self.unmatched[:100], "unsupported_types": dict(self.unsupported),
                "errors": self.errors[:100], "repos": self.repos}


class _DeviceWriter(_Writer):
    """The Observium writer's POP/vendor/model/device rules, without preloading 300k interfaces."""

    def __init__(self, db: Session):
        self.db = db
        self.report = SyncReport(parts=["gitweb"])
        self.pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
        self.vendors = {v.name.lower(): v for v in db.scalars(select(m.Vendor))}
        self.models = {(d.vendor_id, d.name.lower()): d for d in db.scalars(select(m.DeviceModel))}
        self.software = {(s.vendor_id, s.version.lower()): s for s in db.scalars(select(m.SoftwareVersion))}
        self.devices = {norm_host(d.hostname): d for d in db.scalars(select(m.Device))}
        self.ifaces = {}


@dataclass
class _Pending:
    repo: str
    name: str
    sha: str
    record: m.GitwebFile | None
    text: str | None = None
    error: str | None = None


class _Sync:
    def __init__(self, db: Session, client: GitwebClient, full: bool, progress: Progress):
        self.db, self.client, self.full, self.progress = db, client, full, progress
        self.report = _Report()
        self.writer = _DeviceWriter(db)
        self.by_ip = {d.mgmt_ip: d for d in self.writer.devices.values() if d.mgmt_ip}
        self.known_pops = {code for code in self.writer.pops if code != UNASSIGNED_POP}
        self.files = {(f.repo, f.path): f for f in db.scalars(select(m.GitwebFile))}

    # ------------------------------------------------------------------ listing (no DB writes)
    def changed(self) -> list[tuple[str, dict[str, tuple[str, str]], list[_Pending]]]:
        work = []
        for repo in self.client.repos():
            name = repo_name(repo)
            self.progress(f"list {name}")
            files = self.client.config_files(repo)
            pending = [_Pending(name, f, sha, self.files.get((name, f))) for f, sha in files]
            if not self.full:
                pending = [p for p in pending if p.record is None or p.record.blob_sha != p.sha]
            self.report.repos[name] = {"files": len(files), "changed": len(pending)}
            self.report.counts["files"] += len(files)
            if pending:
                work.append((repo, self.client.router_db(repo), pending))
        self.report.counts["changed"] = sum(len(p) for _, _, p in work)
        return work

    # ------------------------------------------------------------------ apply
    def apply(self, work: list[tuple[str, dict[str, tuple[str, str]], list[_Pending]]]) -> None:
        for repo, router_db, pending in work:
            self.progress(f"configs {repo_name(repo)}")
            self._download(repo, pending)
            for item in pending:
                self._apply_one(repo, router_db, item)
            self.db.commit()

    def _download(self, repo: str, pending: list[_Pending]) -> None:
        def fetch(item: _Pending) -> None:
            try:
                item.text = self.client.blob(repo, item.name)
            except GitwebError as exc:
                item.error = str(exc)

        with ThreadPoolExecutor(max_workers=max(1, settings.gitweb_workers)) as pool:
            list(pool.map(fetch, pending))

    def _apply_one(self, repo: str, router_db: dict[str, tuple[str, str]], item: _Pending) -> None:
        label = f"{item.repo}/{item.name}"
        if item.error or item.text is None:
            self.report.errors.append(f"{label}: {item.error}")
            return self._remember(item, "error", None, detail=item.error)
        _, state = router_db.get(item.name.lower(), ("", "up"))
        if state not in ("up", ""):
            self.report.counts["skipped_down"] += 1
            return self._remember(item, "skipped", None, detail=f"router.db state {state}")
        try:
            cfg = parse_rancid(item.text)
        except ValueError:
            kind = rancid_type(item.text) or "unknown"
            self.report.unsupported[kind] += 1
            return self._remember(item, "unsupported", None, detail=kind)

        device, created = self._device_for(repo, item, cfg)
        if device is None:
            self.report.unmatched.append(label)
            return self._remember(item, "unmatched", None)
        self._promote_role(repo, device)
        config_only = created or (item.record is not None and item.record.created_device)
        # Devices Observium monitors keep Observium's port list; the config only fills in the gaps.
        apply_config(self.db, device, cfg, create_ports=config_only)
        if config_only:
            self._config_only_ports(device)
        self.report.counts["applied"] += 1
        self._remember(item, "applied", device, created=created)

    def _device_for(self, repo: str, item: _Pending, cfg: ParsedConfig) -> tuple[m.Device | None, bool]:
        names = [item.name, cfg.hostname or ""]
        for name in names:
            if name and (device := self.writer.devices.get(norm_host(short_hostname(name)))):
                return device, False
        if item.name in self.by_ip:
            return self.by_ip[item.name], False
        hostname = cfg.hostname or item.name
        if not settings.gitweb_create_devices or not re.search(r"[A-Za-z]", hostname):  # never name a device by IP
            return None, False
        return self._create(repo, short_hostname(hostname), item.text or "", cfg), True

    def _create(self, repo: str, hostname: str, text: str, cfg: ParsedConfig) -> m.Device:
        vendor, os_family = VENDOR_BY_PARSER.get(cfg.vendor, (None, None))
        pop_code = pop_code_for_hostname(hostname) or infer_pop(hostname, self.known_pops)
        role = "csr" if role_for_repo(repo) == "csr" else role_from_hostname(hostname) or role_for_repo(repo) or "pe"
        rec = DeviceRecord(observium_id="", hostname=hostname, pop_code=pop_code, pop_name=None, latitude=None,
                           longitude=None, city=None, vendor=vendor, os_family=os_family, model=model_from_text(text),
                           software=None, role=role, mgmt_ip=None, status="active")
        device = self.writer.device(rec)
        self.known_pops.add(pop_code or UNASSIGNED_POP)
        self.report.counts["devices_created"] += 1
        self.report.created.append(hostname)
        return device

    def _promote_role(self, repo: str, device: m.Device) -> None:
        if role_for_repo(repo) == "csr" and device.role != "csr":
            device.role = "csr"
            self.report.counts["roles_set_csr"] += 1

    def _config_only_ports(self, device: m.Device) -> None:
        """No SNMP for this device: speed from the port name; described, enabled ports count as in service."""
        for iface in self.db.scalars(select(m.Interface).where(m.Interface.device_id == device.id)):
            if iface.speed_gbps is None:
                iface.speed_gbps = speed_from_name(iface.name)
            if iface.admin_status == "up" and iface.description:
                iface.oper_status = "up"

    def _remember(self, item: _Pending, status: str, device: m.Device | None, created: bool = False,
                  detail: str | None = None) -> None:
        record = item.record
        if record is None:
            record = m.GitwebFile(repo=item.repo, path=item.name, blob_sha="", status=status, created_device=False)
            self.db.add(record)
            self.files[(item.repo, item.name)] = record
        if status != "error":  # a failed download keeps the old hash, so the next run retries it
            record.blob_sha = item.sha
        record.status = status
        record.detail = (detail or "")[:255] or None
        record.device_id = device.id if device else record.device_id
        record.created_device = record.created_device or created
        record.synced_at = datetime.now(timezone.utc)


def list_changes(client: GitwebClient, db: Session, full: bool = False, progress: Progress | None = None):
    """Listing pass only (no writes): lets the caller skip the backup when nothing changed."""
    sync = _Sync(db, client, full, progress or (lambda _p: None))
    return sync, sync.changed()


def apply_changes(sync: "_Sync", work, discover: bool = True) -> dict[str, Any]:
    sync.apply(work)
    report = {"source": sync.client.base_url, "full": sync.full, **sync.report.as_dict()}
    if discover and sync.report.counts["applied"]:
        sync.progress("discovery")
        report["discovery"] = discover_links(sync.db).model_dump()
    return report
