"""Boxes the network shows over LLDP that Observium does not poll.

GTO-KTT-BNG1-M4 Eth-Trunk5 (4x10G) goes to GTO-TLB-OLT1-HW, which Observium lacks; its four members, GTO-TLB-OLT2-HW
and GTO-LBT-OLT1-HW all name it in LLDP. Without the box the trunk had no link, and an approval drew it onto a cell
site behind the OLTs as a 10G link carrying the whole 40G uplink's traffic. Such a neighbour becomes a device when
its name follows the network's convention at a site the inventory already has (GTO-TLB): carriers' boxes
(JK-KYB-04-PE1-CSFCB1), servers' NICs and outside domains stay out. Runs at the start of every discovery.
"""

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..services.areas import pop_code_for_hostname
from ..services.topology import _HOSTNAME_SHAPED, _MAC, fresh_lldp, host_key, norm_host
from .observium.parsers import DeviceRecord, infer_pop, role_from_hostname, short_hostname
from .observium.sync import UNASSIGNED as UNASSIGNED_POP

OWN_DOMAINS = ("myrepublic.net", "myrepublic.co.id")


def lldp_only_candidate(name: str, sites: set[tuple[str, ...]]) -> bool:
    """A name of ours: hostname-shaped, no other company's domain, its first two parts a site the inventory has."""
    name = name.strip()
    if not name or re.search(r"\s", name) or _MAC.fullmatch(re.sub(r"[:.\-]", "", name.lower())):
        return False
    if "." in name and re.search(r"\.[a-z]{2,}$", name.lower()) and not name.lower().endswith(OWN_DOMAINS):
        return False
    short = norm_host(name)
    if _HOSTNAME_SHAPED.fullmatch(short) is None:
        return False
    parts = host_key(short).split("-")
    return len(parts) >= 3 and tuple(parts[:2]) in sites


def sync_lldp_only_devices(db: Session) -> int:
    """Create the LLDP-only devices fresh LLDP names, drop the flag of those Observium now polls and remove the ones
    no neighbour names any more (once no link is left on them). Returns how many were created."""
    devices = list(db.scalars(select(m.Device)))
    for device in devices:
        if device.lldp_only and device.last_polled_at is not None:
            device.lldp_only = False  # Observium polls it now: an ordinary device
    known = {norm_host(d.hostname) for d in devices}
    named = {norm_host(n) for n in db.scalars(select(fresh_lldp(db).subquery().c.remote_system_name).distinct()) if n}
    _remove_unnamed(db, [d for d in devices if d.lldp_only and norm_host(d.hostname) not in named])
    if not settings.lldp_create_devices:
        return 0
    sites = {tuple(host_key(d.hostname).split("-")[:2]) for d in devices if not d.lldp_only}
    pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
    created = 0
    for name in sorted(named):
        hostname = short_hostname(name)
        if norm_host(hostname) in known or not lldp_only_candidate(name, sites):
            continue
        db.add(m.Device(hostname=hostname, pop_id=_pop(db, pops, hostname).id,
                        role=role_from_hostname(hostname) or "switch", lldp_only=True))
        known.add(norm_host(hostname))
        created += 1
    db.flush()
    return created


def _pop(db: Session, pops: dict[str, m.Pop], hostname: str) -> m.Pop:
    code = (pop_code_for_hostname(hostname) or infer_pop(hostname, set(pops) - {UNASSIGNED_POP}) or UNASSIGNED_POP).upper()
    pop = pops.get(code)
    if pop is None:
        pop = pops[code] = m.Pop(code=code, name=code)
        db.add(pop)
        db.flush()
    return pop


def _remove_unnamed(db: Session, stale: list[m.Device]) -> None:
    """An LLDP-only box no neighbour names any more is gone; it is deleted once discovery took its links away."""
    for device in stale:
        linked = db.scalar(select(m.Link.id).where(or_(m.Link.a_device_id == device.id, m.Link.b_device_id == device.id)).limit(1))
        if linked is None:
            db.delete(device)
