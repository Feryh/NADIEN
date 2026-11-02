"""Command-line entry point for scheduled collection (cron / systemd timer / Celery beat).

    python -m app.collectors.run observium                     # full sync
    python -m app.collectors.run observium --parts utilization # rates only (run every 5–15 min)
    python -m app.collectors.run rancid                        # configs from Observium's RANCID archive
    python -m app.collectors.run oxidized                      # configs from Oxidized
    python -m app.collectors.run config JKT-P-01 jkt-p-01.cfg [--vendor iosxr]

Credentials are read from NADIEN_OBSERVIUM_TOKEN or NADIEN_OBSERVIUM_USER/_PASSWORD (env or backend/.env).
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from .. import models as m
from ..database import Base, SessionLocal, engine
from ..services.topology import discover_links, norm_host
from .configs import parse_config
from .configs.apply import apply_config
from .configs.oxidized import OxidizedClient, os_hint, sync_oxidized
from .configs.rancid import RancidError, open_from_settings, sync_rancid
from .observium.client import ObserviumClient, ObserviumError
from .observium.sync import ALL_PARTS, sync_observium


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.collectors.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    obs = sub.add_parser("observium")
    obs.add_argument("--parts", default=",".join(sorted(ALL_PARTS)))
    obs.add_argument("--no-discover", action="store_true")
    sub.add_parser("rancid")
    sub.add_parser("oxidized")
    cfg = sub.add_parser("config")
    cfg.add_argument("hostname")
    cfg.add_argument("file", type=Path)
    cfg.add_argument("--vendor")
    args = ap.parse_args(argv)

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if args.cmd == "observium":
            try:
                with ObserviumClient() as client:
                    report = sync_observium(db, client, set(args.parts.split(",")), not args.no_discover).as_dict()
            except ObserviumError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        elif args.cmd == "rancid":
            try:
                fs, base, source = open_from_settings()
            except RancidError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            try:
                report = sync_rancid(db, fs, base, source)
            finally:
                fs.close()
        elif args.cmd == "oxidized":
            client = OxidizedClient()
            try:
                report = sync_oxidized(db, client)
            finally:
                client.close()
        else:
            device = next((d for d in db.scalars(select(m.Device)) if norm_host(d.hostname) == norm_host(args.hostname)), None)
            if device is None:
                print(f"error: {args.hostname} not in inventory", file=sys.stderr)
                return 2
            vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
            parsed = parse_config(args.file.read_text(encoding="utf-8", errors="replace"), args.vendor or os_hint(device, vendors))
            report = apply_config(db, device, parsed)
            report["discovery"] = discover_links(db).model_dump()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
