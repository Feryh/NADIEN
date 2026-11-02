"""Fetch device configurations from Oxidized's REST API and apply them to the inventory.

Observium shows configs collected by RANCID/Oxidized but its API does not expose them,
so configs are read straight from Oxidized:
  GET /nodes.json                    node list (name, group, model)
  GET /node/fetch/<group>/<name>     latest config as plain text
"""

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ...services.topology import discover_links, norm_host
from .apply import apply_config
from .detect import parse_config

# Oxidized model names -> parser hints.
OXIDIZED_MODELS = {"iosxr": "iosxr", "vrp": "vrp", "junos": "junos", "sros": "sros", "timos": "sros", "srosmd": "sros"}


class OxidizedClient:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        base_url = base_url or settings.oxidized_url
        if not base_url:
            raise RuntimeError("Oxidized URL missing: set NADIEN_OXIDIZED_URL")
        auth = (settings.oxidized_user, settings.oxidized_password) if settings.oxidized_user else None
        self._http = httpx.Client(base_url=base_url.rstrip("/"), auth=auth, timeout=60, transport=transport)

    def nodes(self) -> list[dict[str, Any]]:
        resp = self._http.get("/nodes.json")
        resp.raise_for_status()
        return resp.json()

    def fetch(self, node: dict[str, Any]) -> str:
        group = node.get("group")
        path = f"/node/fetch/{group}/{node['name']}" if group else f"/node/fetch/{node['name']}"
        resp = self._http.get(path)
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._http.close()


def os_hint(device: m.Device, vendors: dict[int, m.Vendor]) -> str | None:
    vendor = vendors.get(device.vendor_id or -1)
    return (vendor.os_family or vendor.name) if vendor else None


def sync_oxidized(db: Session, client: OxidizedClient, discover: bool = True) -> dict:
    devices = {norm_host(d.hostname): d for d in db.scalars(select(m.Device))}
    vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
    results, errors, unmatched = [], [], []
    for node in client.nodes():
        device = devices.get(norm_host(node.get("name", "")))
        if device is None:
            unmatched.append(node.get("name"))
            continue
        try:
            text = client.fetch(node)
            hint = OXIDIZED_MODELS.get(str(node.get("model", "")).lower()) or os_hint(device, vendors)
            results.append(apply_config(db, device, parse_config(text, hint)))
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{node.get('name')}: {exc}")
    report = {"applied": len(results), "results": results, "errors": errors, "unmatched_nodes": unmatched[:50]}
    if discover:
        report["discovery"] = discover_links(db).model_dump()
    return report
