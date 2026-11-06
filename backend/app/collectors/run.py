"""Command-line entry point for scheduled collection (cron / systemd timer / Celery beat).

    python -m app.collectors.run observium                     # full sync (backup first)
    python -m app.collectors.run observium --parts utilization # rates only, no backup (run every 5–15 min)
    python -m app.collectors.run observium-csr                 # cell-site routers from the CSR Observium (backup first)
    python -m app.collectors.run rrd --days 90                 # daily peaks from Observium RRDs (no backup)
    python -m app.collectors.run rancid                        # configs from Observium's RANCID archive (backup first)
    python -m app.collectors.run gitweb [--full]               # all RANCID repos via gitweb, changed files only (backup if any)
    python -m app.collectors.run oxidized                      # configs from Oxidized (backup first)
    python -m app.collectors.run config JKT-P-01 jkt-p-01.cfg [--vendor iosxr]

Credentials are read from NADIEN_OBSERVIUM_TOKEN or NADIEN_OBSERVIUM_USER/_PASSWORD (env or backend/.env).
Backups go to NADIEN_BACKUP_DIR (newest NADIEN_BACKUP_KEEP kept); see `python -m app.backup`.
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from .. import models as m
from ..database import Base, SessionLocal, engine
from ..services import collector_settings
from ..services.topology import norm_host
from . import jobs
from .configs import parse_config
from .configs.gitweb import GitwebError
from .configs.oxidized import os_hint
from .configs.rancid import RancidError
from .observium.client import ObserviumError
from .observium.rrd import RrdError
from .observium.sync import ALL_PARTS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.collectors.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    obs = sub.add_parser("observium")
    obs.add_argument("--parts", default=",".join(sorted(ALL_PARTS)))
    obs.add_argument("--no-discover", action="store_true")
    sub.add_parser("observium-csr")
    rrd = sub.add_parser("rrd")
    rrd.add_argument("--days", type=int, default=90)
    sub.add_parser("rancid")
    gitweb = sub.add_parser("gitweb")
    gitweb.add_argument("--full", action="store_true", help="re-read every file, not only changed ones")
    gitweb.add_argument("--no-discover", action="store_true")
    sub.add_parser("oxidized")
    cfg = sub.add_parser("config")
    cfg.add_argument("hostname")
    cfg.add_argument("file", type=Path)
    cfg.add_argument("--vendor")
    args = ap.parse_args(argv)

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        collector_settings.apply(db)  # settings saved in the UI apply to the CLI too
        try:
            if args.cmd == "observium":
                report = jobs.run_observium(db, set(args.parts.split(",")), not args.no_discover)
            elif args.cmd == "observium-csr":
                report = jobs.run_observium_csr(db)
            elif args.cmd == "rrd":
                report = jobs.run_rrd(db, max(7, min(args.days, 365)))
            elif args.cmd == "rancid":
                report = jobs.run_rancid(db)
            elif args.cmd == "gitweb":
                report = jobs.run_gitweb(db, args.full, not args.no_discover)
            elif args.cmd == "oxidized":
                report = jobs.run_oxidized(db)
            else:
                device = next((d for d in db.scalars(select(m.Device)) if norm_host(d.hostname) == norm_host(args.hostname)), None)
                if device is None:
                    print(f"error: {args.hostname} not in inventory", file=sys.stderr)
                    return 2
                vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
                text = args.file.read_text(encoding="utf-8", errors="replace")
                report = jobs.run_config_import(db, device, parse_config(text, args.vendor or os_hint(device, vendors)))
        except (jobs.JobError, ObserviumError, RancidError, RrdError, GitwebError, RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
