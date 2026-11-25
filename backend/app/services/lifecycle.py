"""Devices gone from the network, and LLDP rows no poll refreshes any more.

A device is gone once it is decommissioned, or once Observium stopped polling it (its last poll over a day behind the
newest one, so a sync that has not run for a while ages nothing) while nothing polled shows it alive: no neighbour's
fresh LLDP names it, and no link of it ends on a port that is up on a polled device. Those signs do not count once a
polled replacement exists (same name, another model suffix): then the neighbours' LLDP is stale and their ports
are up towards the new box. For `lldp_max_age_days` it stays visible with a
label and its links drawn down; after that its links and LLDP rows are deleted and it is hidden, the record kept
(services, approvals and the audit trail still point at it). Polled again, it is back. LLDP rows older than that
limit are deleted too: discovery already ignores them (fresh_lldp).
Runs at the start of every discovery, so after every sync.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, aliased

from ..config import settings
from ..models import Device, Interface, Link, LldpNeighbor, UtilizationSample
from .topology import host_key, norm_host

NOT_POLLED_AFTER = timedelta(days=1)  # Observium polls every few minutes: a day behind means it stopped


@dataclass
class LifecycleResult:
    devices_gone: int = 0  # gone, still in the grace period: labelled
    devices_hidden: int = 0  # hidden by this run
    devices_back: int = 0  # polled again: label and hiding removed
    links_deleted: int = 0
    lldp_rows_deleted: int = 0


def grace() -> timedelta | None:
    return timedelta(days=settings.lldp_max_age_days) if settings.lldp_max_age_days > 0 else None


def _not_polled(device: Device, newest_poll: datetime | None) -> bool:
    polled = device.last_polled_at
    return polled is not None and newest_poll is not None and _aware(newest_poll) - _aware(polled) > NOT_POLLED_AFTER


def _gone_since(device: Device, not_polled: bool, alive: bool, now: datetime) -> datetime | None:
    polled = _aware(device.last_polled_at) if device.last_polled_at else None
    known = _aware(device.gone_since) if device.gone_since else None
    if device.status == "decommissioned":
        return known or (polled if not_polled else now)
    if not_polled and not alive:
        return min(known, polled) if known else polled
    # Polled; or never by Observium (devices from configs only); or no longer polled but still alive: not gone.
    return None


def _still_alive(db: Session, devices: list[Device], unpolled: set[int]) -> set[int]:
    """Unpolled devices the polled ones still show alive: named in a neighbour's LLDP of the last day
    (TNG-COK-ME1-6120HS, unpolled since Nov 9, its OLTs still name it), or at the far end of a link whose polled end
    is up (OLT1-C300-JKTT-KRJ: PEIN1-H-JKTT-KRJ Eth-Trunk11 up, carrying 3.5 Gbps)."""
    alive: set[int] = set()
    newest = db.scalar(select(func.max(LldpNeighbor.collected_at)))
    if newest is not None:
        named = {norm_host(n) for n in db.scalars(select(LldpNeighbor.remote_system_name)
                                                   .where(LldpNeighbor.collected_at >= newest - NOT_POLLED_AFTER)
                                                   .distinct()) if n}
        alive |= {d.id for d in devices if d.id in unpolled and norm_host(d.hostname) in named}
    far_a, far_b = aliased(Interface), aliased(Interface)
    rows = db.execute(select(Link.a_device_id, Link.b_device_id, far_a.oper_status, far_b.oper_status)
                      .join(far_a, far_a.id == Link.a_interface_id).join(far_b, far_b.id == Link.b_interface_id)
                      .where(or_(Link.a_device_id.in_(unpolled), Link.b_device_id.in_(unpolled))))
    for a_dev, b_dev, a_state, b_state in rows:
        if a_dev in unpolled and b_dev not in unpolled and b_state == "up":
            alive.add(a_dev)
        if b_dev in unpolled and a_dev not in unpolled and a_state == "up":
            alive.add(b_dev)
    return alive


def _model_stem(hostname: str) -> str | None:
    """The name without its model suffix (tng-cok-me1 for TNG-COK-ME1-6120HS); None when the last part is no model
    (no digit: a site code, as in OLT1-C300-JKTT-KRJ, where another site's OLT is not a replacement)."""
    parts = host_key(hostname).split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 and any(c.isdigit() for c in parts[-1]) else None


def _swapped(devices: list[Device], unpolled: set[int]) -> set[int]:
    """Unpolled devices whose replacement is polled: the same name with another model suffix (TNG-COK-ME1-6120HS
    swapped for TNG-COK-ME1-M8). The neighbours' LLDP keeps naming the old box and their ports stay up, now towards
    the new one, so neither shows the old box alive."""
    polled_stems = {_model_stem(d.hostname) for d in devices
                    if d.id not in unpolled and d.last_polled_at is not None and d.status != "decommissioned"}
    polled_stems.discard(None)
    return {d.id for d in devices if d.id in unpolled and _model_stem(d.hostname) in polled_stems}


def mark_gone(db: Session, now: datetime, result: LifecycleResult) -> None:
    newest_poll = db.scalar(select(func.max(Device.last_polled_at)))
    devices = db.scalars(select(Device)).all()
    unpolled = {d.id for d in devices if _not_polled(d, newest_poll)}
    alive = (_still_alive(db, devices, unpolled) - _swapped(devices, unpolled)) if unpolled else set()
    period = grace()
    for device in devices:
        since = _gone_since(device, device.id in unpolled, device.id in alive, now)
        if since is None:
            if device.gone_since is not None or device.hidden:
                result.devices_back += 1
            device.gone_since, device.hidden = None, False
            continue
        device.gone_since = since
        if period is not None and _aware(since) < now - period:
            result.devices_hidden += not device.hidden
            device.hidden = True
        else:
            result.devices_gone += 1
            device.hidden = False


def purge_hidden(db: Session, now: datetime, result: LifecycleResult) -> None:
    """Links and LLDP rows of hidden devices, and LLDP rows past the age limit."""
    hidden = select(Device.id).where(Device.hidden.is_(True))
    link_ids = select(Link.id).where(or_(Link.a_device_id.in_(hidden), Link.b_device_id.in_(hidden)))
    db.execute(delete(UtilizationSample).where(UtilizationSample.link_id.in_(link_ids)))
    result.links_deleted = db.execute(delete(Link).where(Link.id.in_(link_ids))).rowcount or 0
    stale = [LldpNeighbor.device_id.in_(hidden)]
    newest = db.scalar(select(func.max(LldpNeighbor.collected_at)))
    if newest is not None and grace() is not None:
        stale.append(LldpNeighbor.collected_at < newest - grace())
    result.lldp_rows_deleted = db.execute(delete(LldpNeighbor).where(or_(*stale))).rowcount or 0


def run_lifecycle(db: Session, now: datetime) -> LifecycleResult:
    result = LifecycleResult()
    mark_gone(db, now, result)
    db.flush()
    purge_hidden(db, now, result)
    db.flush()
    return result


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC
