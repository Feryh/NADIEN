"""Read device configs from the RANCID archive that Observium displays.

RANCID keeps the latest config of each device as a plain file named after the router.db entry:
    <base>/[var/]<group>/configs/<hostname-or-ip>
with a header such as `!RANCID-CONTENT-TYPE: cisco-xr` / `#RANCID-CONTENT-TYPE: juniper`.
Every directory named `configs` under the base (a few levels deep) is collected, so both the
classic `var/<group>/configs` layout and a flat `<group>/configs` layout work. If the base has no
`configs` directory it is treated as one itself.

Access is read-only, either from a local/mounted path or over SFTP to the Observium server.
"""

import posixpath
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ...services.topology import discover_links, norm_host
from .apply import apply_config
from .base import ParsedConfig
from .detect import parse_config
from .oxidized import os_hint

CONTENT_TYPE = re.compile(r"^[!#]\s*RANCID-CONTENT-TYPE:\s*(\S+)", re.M)
# RANCID device types -> parser keys. Anything else falls back to detecting from the text.
RANCID_TYPES = [
    (r"xr", "iosxr"),
    (r"juniper|junos", "junos"),
    (r"^(huawei|vrp)", "vrp"),
    (r"sros|sr-os|alu|nokia|timos", "sros"),
    (r"^zte", "zte"),
    (r"^fiberhomemetro$", "fiberhome"),
    (r"^(cisco|ios|nxos)", "ios"),
]
# OLT shelves: GPON/board configs without router interfaces. Text detection would misread them
# (a Huawei OLT has `sysname` like VRP), so they are reported as unsupported instead of parsed.
UNSUPPORTED_TYPES = {"huaweiolt", "fiberhome", "zteolt", "fiberhomeolt"}
SKIP_FILES = {"router.db", ".cvsignore"}
SKIP_DIRS = {"CVS", ".git", ".svn", "logs", "old", "bin", "etc", "lib", "share"}
MAX_DEPTH = 4
MAX_FILE_BYTES = 10_000_000


class RancidError(RuntimeError):
    pass


def rancid_hint(text: str) -> str | None:
    match = CONTENT_TYPE.search(text[:4000])
    if not match:
        return None
    kind = match.group(1).lower()
    if kind in UNSUPPORTED_TYPES:
        return None
    return next((parser for pattern, parser in RANCID_TYPES if re.search(pattern, kind)), None)


def parse_rancid(text: str, fallback_hint: str | None = None) -> ParsedConfig:
    """Parse a RANCID file; raises ValueError for OLT types and text no parser recognises."""
    if rancid_type(text) in UNSUPPORTED_TYPES:
        raise ValueError(f"RANCID type {rancid_type(text)} is not a router config")
    return parse_config(text, rancid_hint(text) or fallback_hint)


def rancid_type(text: str) -> str | None:
    match = CONTENT_TYPE.search(text[:4000])
    return match.group(1).lower() if match else None


@dataclass
class RancidFile:
    name: str  # router.db name: hostname, FQDN or IP
    group: str | None
    path: str


class FileSystem(Protocol):
    def listdir(self, path: str) -> list[tuple[str, bool]]: ...  # (name, is_dir)
    def read(self, path: str) -> str: ...
    def join(self, *parts: str) -> str: ...


class LocalFS:
    def listdir(self, path: str) -> list[tuple[str, bool]]:
        p = Path(path)
        if not p.is_dir():
            raise RancidError(f"RANCID path not found: {path}")
        return [(c.name, c.is_dir()) for c in p.iterdir()]

    def read(self, path: str) -> str:
        p = Path(path)
        if p.stat().st_size > MAX_FILE_BYTES:
            raise RancidError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        return p.read_text(encoding="utf-8", errors="replace")

    def join(self, *parts: str) -> str:
        return str(Path(*parts))

    def close(self) -> None:
        pass


class SftpFS:
    """SFTP to the Observium server. Unknown host keys are rejected (no silent trust-on-first-use)."""

    def __init__(self, host: str, user: str, port: int = 22, key_file: str | None = None,
                 password: str | None = None, known_hosts: str | None = None):
        import paramiko

        self._ssh = paramiko.SSHClient()
        self._ssh.load_system_host_keys()
        if known_hosts:
            self._ssh.load_host_keys(known_hosts)
        self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self._ssh.connect(host, port=port, username=user, key_filename=key_file, password=password,
                              allow_agent=False, look_for_keys=not (key_file or password), timeout=20)
        except paramiko.SSHException as exc:
            raise RancidError(f"SSH to {host} failed: {exc}") from exc
        except OSError as exc:
            raise RancidError(f"SSH to {host} unreachable: {exc}") from exc
        self._sftp = self._ssh.open_sftp()

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        try:
            return [(a.filename, stat.S_ISDIR(a.st_mode or 0)) for a in self._sftp.listdir_attr(path)]
        except OSError as exc:
            raise RancidError(f"Cannot list {path} on the RANCID server: {exc}") from exc

    def read(self, path: str) -> str:
        if (self._sftp.stat(path).st_size or 0) > MAX_FILE_BYTES:
            raise RancidError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        with self._sftp.open(path, "r") as fh:
            return fh.read().decode("utf-8", errors="replace")

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def close(self) -> None:
        self._sftp.close()
        self._ssh.close()


def find_files(fs: FileSystem, base: str) -> list[RancidFile]:
    config_dirs: list[tuple[str, str | None]] = []
    stack = [(base, 0)]
    while stack:
        path, depth = stack.pop()
        for name, is_dir in fs.listdir(path):
            if not is_dir or name.startswith(".") or name in SKIP_DIRS:
                continue
            child = fs.join(path, name)
            if name == "configs":
                config_dirs.append((child, re.split(r"[\\/]", path.rstrip("/\\"))[-1] or None))
            elif depth < MAX_DEPTH:
                stack.append((child, depth + 1))
    if not config_dirs:
        config_dirs = [(base, None)]
    files = []
    for directory, group in config_dirs:
        for name, is_dir in fs.listdir(directory):
            if not is_dir and not name.startswith(".") and name not in SKIP_FILES and not name.endswith((",v", "~")):
                files.append(RancidFile(name=name, group=group, path=fs.join(directory, name)))
    return sorted(files, key=lambda f: (f.group or "", f.name))


def open_from_settings() -> tuple[FileSystem, str, str]:
    """(filesystem, base path, human-readable source) from NADIEN_RANCID_* settings."""
    if settings.rancid_path:
        return LocalFS(), settings.rancid_path, settings.rancid_path
    if settings.rancid_ssh_host and settings.rancid_ssh_user:
        fs = SftpFS(settings.rancid_ssh_host, settings.rancid_ssh_user, settings.rancid_ssh_port,
                    settings.rancid_ssh_key_file, settings.rancid_ssh_password, settings.rancid_ssh_known_hosts)
        return fs, settings.rancid_remote_path, f"sftp://{settings.rancid_ssh_host}{settings.rancid_remote_path}"
    raise RancidError(
        "RANCID not configured: set NADIEN_RANCID_PATH (local/mounted) or "
        "NADIEN_RANCID_SSH_HOST + NADIEN_RANCID_SSH_USER (+ key file)"
    )


def _device_index(db: Session) -> tuple[dict[str, m.Device], dict[str, m.Device]]:
    devices = db.scalars(select(m.Device)).all()
    by_host = {norm_host(d.hostname): d for d in devices}
    by_ip = {d.mgmt_ip: d for d in devices if d.mgmt_ip}
    return by_host, by_ip


def iter_configs(fs: FileSystem, base: str) -> Iterator[tuple[RancidFile, str]]:
    for f in find_files(fs, base):
        yield f, fs.read(f.path)


def sync_rancid(db: Session, fs: FileSystem, base: str, source: str = "", discover: bool = True) -> dict:
    by_host, by_ip = _device_index(db)
    vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
    applied, unmatched, unsupported, errors = [], [], [], []
    files = find_files(fs, base)
    for f in files:
        # router.db entries may be the short name, the FQDN or the management IP.
        device = by_host.get(norm_host(f.name)) or by_ip.get(f.name)
        if device is None:
            unmatched.append(f.name)
            continue
        try:
            text = fs.read(f.path)
            try:
                cfg = parse_rancid(text, os_hint(device, vendors))
            except ValueError:
                unsupported.append(f"{f.name} (RANCID type: {rancid_type(text) or 'unknown'})")
                continue
            result = apply_config(db, device, cfg)
            applied.append({"file": f.name, "group": f.group, "device": device.hostname, "vendor": cfg.vendor,
                            "counts": result["counts"], "warnings": result["warnings"]})
        except RancidError as exc:
            errors.append(f"{f.name}: {exc}")
    report = {
        "source": source or base,
        "files": len(files),
        "applied": len(applied),
        "results": applied,
        "unmatched_files": unmatched[:100],
        "unmatched_count": len(unmatched),
        "unsupported": unsupported[:100],
        "errors": errors[:100],
    }
    if discover:
        report["discovery"] = discover_links(db).model_dump()
    return report
