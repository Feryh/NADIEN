"""Daily utilization history from Observium's RRD files.

Observium keeps one RRD per port at <rrd_dir>/<observium hostname>/port-<ifIndex>.rrd with DS
INOCTETS / OUTOCTETS (bytes/s). For every link we take the port on its A side (or B side,
directions swapped, when A is not usable) and read `days` daily peaks from one of two sources
(NADIEN_OBSERVIUM_RRD_SOURCE):

- "api" (default): Observium's graph.php with format=json, same login as the REST API — see
  graph_api.py. Older days are peaks of 2-hour averages; the last 7 days are 5-minute exact.
- "ssh" / "local": the daily MAX archive (RRA:MAX:0.5:288) exported with rrdtool on the
  Observium server, over SSH or locally when NADIEN runs there:

    rrdtool xport --start end-90d --end now --step 86400 DEF:i=<f>:INOCTETS:MAX DEF:o=<f>:OUTOCTETS:MAX

Samples of each link inside the window are replaced by the daily peaks, and `current_gbps`
becomes the last full day's peak.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings

CHUNK = 250  # RRD files per remote execution
# Observium creates port RRDs with DS max 12,500,000,000 bytes/s (100 Gbps); RRD stores anything
# faster as NaN. Peaks this close to it on bigger links are probably clipped.
DS_MAX_GBPS = 100.0
_BLOCK = re.compile(r"^@@@ (\S+)$", re.M)
_ROW = re.compile(r"<row>(.*?)</row>", re.S)
_VALUE = re.compile(r"<v>\s*([^<]*?)\s*</v>")
_TIME = re.compile(r"<t>\s*(\d+)\s*</t>")


class RrdError(RuntimeError):
    pass


class Runner(Protocol):
    def run(self, script: str) -> str: ...
    def close(self) -> None: ...


class LocalRunner:
    """NADIEN running on the Observium server itself."""

    def run(self, script: str) -> str:
        try:
            result = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RrdError(f"local rrdtool run failed: {exc}") from exc
        return result.stdout

    def close(self) -> None:
        pass


class SshRunner:
    """rrdtool on the Observium server over SSH. Unknown host keys are rejected."""

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
            raise RrdError(f"SSH to {host} failed: {exc}") from exc
        except OSError as exc:
            raise RrdError(f"SSH to {host} unreachable: {exc}") from exc

    def run(self, script: str) -> str:
        stdin, stdout, _stderr = self._ssh.exec_command("bash -s", timeout=900)
        stdin.write(script)
        stdin.channel.shutdown_write()
        return stdout.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        self._ssh.close()


def open_runner() -> Runner:
    if settings.observium_rrd_source == "local":
        return LocalRunner()
    host = settings.observium_ssh_host or urlparse(settings.observium_url).hostname
    if not host or not settings.observium_ssh_user:
        raise RrdError(
            "RRD access not configured: set NADIEN_OBSERVIUM_SSH_USER (+ NADIEN_OBSERVIUM_SSH_KEY_FILE or "
            "_PASSWORD) for SSH to the Observium server, or use NADIEN_OBSERVIUM_RRD_SOURCE=api"
        )
    return SshRunner(host, settings.observium_ssh_user, settings.observium_ssh_port, settings.observium_ssh_key_file,
                     settings.observium_ssh_password, settings.observium_ssh_known_hosts)


@dataclass
class Target:
    link: m.Link
    ref: m.ObserviumPort
    swapped: bool  # the port is the B side: its in/out are the link's out/in


class RrdSource(Protocol):
    name: str
    chunk_size: int

    def usable(self, ref: m.ObserviumPort) -> bool: ...
    def fetch(self, chunk: list[Target], days: int) -> dict[str, Any]: ...
    def close(self) -> None: ...


class ScriptSource:
    """rrdtool xport of the daily MAX archive, run by a Runner (SSH or local)."""

    chunk_size = CHUNK

    def __init__(self, runner: Runner, name: str = "ssh"):
        self.runner = runner
        self.name = name

    def usable(self, ref: m.ObserviumPort) -> bool:
        return ref.if_index is not None

    def fetch(self, chunk: list[Target], days: int) -> dict[str, Any]:
        base = settings.observium_rrd_path.rstrip("/")
        files = {str(t.link.id): f"{base}/{t.ref.observium_hostname}/port-{t.ref.if_index}.rrd" for t in chunk}
        parsed = parse_output(self.runner.run(build_script(files, days)))
        if "__error__" in parsed:
            raise RrdError(f"Observium server: {parsed['__error__']}")
        return parsed

    def close(self) -> None:
        self.runner.close()


def open_source() -> RrdSource:
    """The configured history source; access problems are raised here, before a job starts."""
    if settings.observium_rrd_source == "api":
        from .client import ObserviumClient, ObserviumError
        from .graph_api import GraphApiSource

        try:
            return GraphApiSource(ObserviumClient())
        except ObserviumError as exc:
            raise RrdError(f"RRD via API not configured: {exc}") from exc
    return ScriptSource(open_runner(), settings.observium_rrd_source)


def build_script(files: dict[str, str], days: int) -> str:
    """Bash: for each `key|path`, print '@@@ key' and the xport XML (MAX archive, AVERAGE fallback)."""
    lines = "\n".join(f"{key}|{path}" for key, path in files.items())
    return f"""set -u
export LC_ALL=C
command -v rrdtool >/dev/null 2>&1 || {{ echo '@@@ __error__'; echo 'rrdtool not found'; exit 0; }}
while IFS='|' read -r key f; do
  [ -z "$key" ] && continue
  echo "@@@ $key"
  if [ ! -r "$f" ]; then echo MISSING; continue; fi
  ef=$(printf '%s' "$f" | sed 's/:/\\\\:/g')
  rrdtool xport --start end-{days}d --end now --step 86400 --maxrows {days + 2} \\
    "DEF:i=$ef:INOCTETS:MAX" "DEF:o=$ef:OUTOCTETS:MAX" XPORT:i XPORT:o 2>/dev/null \\
  || rrdtool xport --start end-{days}d --end now --step 86400 --maxrows {days + 2} \\
    "DEF:i=$ef:INOCTETS:AVERAGE" "DEF:o=$ef:OUTOCTETS:AVERAGE" XPORT:i XPORT:o 2>&1
done <<'NADIEN_RRD_LIST'
{lines}
NADIEN_RRD_LIST
"""


def parse_output(text: str) -> dict[str, list[tuple[datetime, float, float]] | str]:
    """key -> [(day, in_gbps, out_gbps)] or an error string ('MISSING', rrdtool message)."""
    results: dict[str, Any] = {}
    parts = _BLOCK.split(text)
    for key, body in zip(parts[1::2], parts[2::2]):
        body = body.strip()
        if body.startswith("MISSING"):
            results[key] = "MISSING"
            continue
        start = re.search(r"<start>\s*(\d+)\s*</start>", body)
        step = re.search(r"<step>\s*(\d+)\s*</step>", body)
        rows = _ROW.findall(body)
        if not rows or not start:
            results[key] = body[:200] or "empty output"
            continue
        samples = []
        nan_days = 0
        for i, row in enumerate(rows):
            values = _VALUE.findall(row)
            t = _TIME.search(row)
            ts = int(t.group(1)) if t else int(start.group(1)) + i * int(step.group(1) if step else 86400)
            try:
                rx, tx = (float(v) for v in values[:2])
            except ValueError:
                continue
            if rx != rx or tx != tx:  # NaN: no data — or a rate above the DS maximum, which RRD discards
                nan_days += 1
                continue
            samples.append((datetime.fromtimestamp(ts, tz=timezone.utc), rx * 8 / 1e9, tx * 8 / 1e9))
        results[key] = samples
        results[f"{key}#nan"] = nan_days
    return results


def _targets(db: Session, source: RrdSource) -> tuple[list[Target], int]:
    refs = {r.interface_id: r for r in db.scalars(select(m.ObserviumPort))}
    targets, unmapped = [], 0
    for link in db.scalars(select(m.Link)):
        for iface_id, swapped in ((link.a_interface_id, False), (link.b_interface_id, True)):
            ref = refs.get(iface_id or -1)
            if ref is not None and source.usable(ref):
                targets.append(Target(link, ref, swapped))
                break
        else:
            unmapped += 1
    return targets, unmapped


def sync_rrd(db: Session, source: RrdSource, days: int = 90,
             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    step = progress or (lambda _p: None)
    targets, unmapped = _targets(db, source)
    report: dict[str, Any] = {"source": source.name, "days": days, "links": len(targets) + unmapped,
                              "links_without_port_map": unmapped, "links_with_history": 0, "missing_files": 0,
                              "errors": [], "error_count": 0, "samples": 0, "days_without_data": 0,
                              "possibly_clipped": []}
    since = datetime.now(timezone.utc) - timedelta(days=days + 1)
    for start in range(0, len(targets), source.chunk_size):
        chunk = targets[start:start + source.chunk_size]
        step(f"rrd {start + len(chunk)}/{len(targets)}")
        parsed = source.fetch(chunk, days)
        for t in chunk:
            got = parsed.get(str(t.link.id))
            if got == "MISSING":
                report["missing_files"] += 1
                continue
            if not isinstance(got, list):
                report["error_count"] += 1
                if len(report["errors"]) < 20:
                    report["errors"].append(f"{t.link.name}: {got or 'no output'}")
                continue
            nan_days = int(parsed.get(f"{t.link.id}#nan", 0) or 0)
            report["days_without_data"] += nan_days
            peak = max((max(rx, tx) for _, rx, tx in got), default=0.0)
            if (t.link.capacity_gbps or 0) > DS_MAX_GBPS and (nan_days or peak >= DS_MAX_GBPS * 0.97):
                if len(report["possibly_clipped"]) < 50:
                    report["possibly_clipped"].append(
                        f"{t.link.name}: {t.link.capacity_gbps:g}G link, peak {peak:.1f}G, {nan_days} hari tanpa data")
            if not got:
                continue
            db.execute(delete(m.UtilizationSample).where(m.UtilizationSample.link_id == t.link.id,
                                                         m.UtilizationSample.ts >= since))
            for ts, rx, tx in got:
                in_g, out_g = (tx, rx) if t.swapped else (rx, tx)
                db.add(m.UtilizationSample(link_id=t.link.id, ts=ts, in_gbps=round(in_g, 4), out_gbps=round(out_g, 4)))
            # A bucket stamped within the last 24 h is still filling; the last complete day is the
            # planning "current" (fall back to the partial one only when it's all there is).
            full_days = [s for s in got if s[0] <= datetime.now(timezone.utc) - timedelta(days=1)] or got
            last = full_days[-1]
            t.link.current_gbps = round(max(last[1], last[2]), 3)
            report["links_with_history"] += 1
            report["samples"] += len(got)
        db.commit()
    return report
