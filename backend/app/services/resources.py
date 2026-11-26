"""Resource registry: which VLANs, VC ids and IPv4 prefixes are taken, the next free ones, and reservations.

"Taken" combines what is already on the network with what is held for planned work:
- VLAN on a device: its sub-interfaces in the inventory (`<port>.<vlan>`, from Observium and RANCID) and active
  reservations on that device.
- VC / PW id between two PEs: pseudowires in either PE's RANCID config towards the other's loopback
  (config_pseudowires, refreshed by the gitweb sync) and active reservations for the pair.
- IPv4: interface addresses and loopbacks in the inventory and active reservations; pools are blocks to hand
  prefixes out of.
A reservation stays active until it is released, expires (resource_reservation_days) or shows up in the
inventory/config, when it is marked fulfilled.
"""

import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from .l2vpn_peers import pw_usage

KINDS = ("vlan", "vc_id", "ipv4")
VLAN_MIN, VLAN_MAX = 2, 4094  # VLAN 1 is the default VLAN on every vendor here
VC_ID_MAX = 4294967295
_SUB_VLAN = re.compile(r"\.(\d{1,4})$")
# Addresses that say nothing about the address plan: loopback, link-local, unset.
_IGNORED_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "169.254.0.0/16", "0.0.0.0/8")]
_PSEUDOWIRE_DIALECTS = ("vrp", "zte", "iosxr", "fiberhome")
MIN_CONFIG_LINES = 20  # fewer lines than any real PE config: a failed RANCID collection
SOURCE_WORDS = {"interface": "interface", "config": "config", "reservation": "reservasi"}


class ResourceError(ValueError):
    """Bad input: 422."""


class ResourceConflict(Exception):
    """The values are taken: 409, with what holds each one."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__("; ".join(c["message"] for c in conflicts))
        self.conflicts = conflicts


@dataclass
class Request:
    """One value to reserve."""

    kind: str
    device_id: int | None = None
    peer_device_id: int | None = None
    value: int | None = None
    prefix: str | None = None
    pool_id: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def reservation_view(row: m.ResourceReservation, hostnames: dict[int, str] | None = None) -> dict[str, Any]:
    hostnames = hostnames or {}
    return {"id": row.id, "kind": row.kind, "device_id": row.device_id, "device": hostnames.get(row.device_id or 0),
            "peer_device_id": row.peer_device_id, "peer_device": hostnames.get(row.peer_device_id or 0),
            "value": row.value, "prefix": row.prefix, "pool_id": row.pool_id, "status": row.status,
            "source": row.source, "label": row.label, "service_id": row.service_id, "change_id": row.change_id,
            "note": row.note, "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "expires_at": _aware(row.expires_at).isoformat() if row.expires_at else None,
            "settled_at": _aware(row.settled_at).isoformat() if row.settled_at else None}


def _active(kind: str):
    return (select(m.ResourceReservation)
            .where(m.ResourceReservation.kind == kind, m.ResourceReservation.status == "reserved"))


# --------------------------------------------------------------------------- what the network already uses


def _sub_vlans(db: Session, device_id: int) -> dict[int, list[str]]:
    names = db.scalars(select(m.Interface.name).where(m.Interface.device_id == device_id, m.Interface.name.like("%.%")))
    found: dict[int, list[str]] = defaultdict(list)
    for name in names:
        if (match := _SUB_VLAN.search(name)) and VLAN_MIN <= int(match.group(1)) <= VLAN_MAX:
            found[int(match.group(1))].append(name)
    return found


def _loopback(db: Session, device_id: int) -> str | None:
    return db.scalar(select(m.Device.loopback_ipv4).where(m.Device.id == device_id))


def _pair_pseudowires(db: Session, a: int, b: int) -> dict[int, list[str]]:
    """VC id -> where it is configured, from both PEs' configs towards each other's loopback."""
    loop_a, loop_b = _loopback(db, a), _loopback(db, b)
    clauses = []
    if loop_b:
        clauses.append((m.ConfigPseudowire.device_id == a) & (m.ConfigPseudowire.peer_ip == loop_b))
    if loop_a:
        clauses.append((m.ConfigPseudowire.device_id == b) & (m.ConfigPseudowire.peer_ip == loop_a))
    if not clauses:
        return {}
    hostnames = dict(db.execute(select(m.Device.id, m.Device.hostname).where(m.Device.id.in_((a, b)))).all())
    found: dict[int, list[str]] = defaultdict(list)
    for row in db.scalars(select(m.ConfigPseudowire).where(or_(*clauses))):
        found[row.vc_id].append(f"{hostnames.get(row.device_id, row.device_id)} → {row.peer_ip}")
    return found


def _interface_networks(db: Session) -> list[tuple[ipaddress.IPv4Interface, str]]:
    rows = db.execute(select(m.Interface.ipv4_address, m.Interface.name, m.Device.hostname)
                      .join(m.Device, m.Device.id == m.Interface.device_id)
                      .where(m.Interface.ipv4_address.is_not(None))).all()
    out = []
    for address, name, hostname in rows:
        try:
            iface = ipaddress.IPv4Interface(address.strip())
        except ValueError:
            continue
        if not any(iface.ip in net for net in _IGNORED_NETS):
            out.append((iface, f"{hostname} {name}"))
    for loopback, hostname in db.execute(select(m.Device.loopback_ipv4, m.Device.hostname)
                                         .where(m.Device.loopback_ipv4.is_not(None))).all():
        try:
            out.append((ipaddress.IPv4Interface(f"{loopback.strip()}/32"), f"{hostname} loopback"))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------- settling reservations


def settle(db: Session) -> None:
    """Reservations that expired, or whose value now shows in the inventory/config, stop being active."""
    now = _now()
    changed = False
    for row in db.scalars(select(m.ResourceReservation).where(m.ResourceReservation.status == "reserved")):
        if _seen(db, row):
            row.status, row.settled_at, changed = "fulfilled", now, True
        elif row.expires_at and _aware(row.expires_at) < now:
            row.status, row.settled_at, changed = "expired", now, True
    if changed:
        db.commit()


def _seen(db: Session, row: m.ResourceReservation) -> bool:
    if row.kind == "vlan" and row.device_id and row.value is not None:
        return row.value in _sub_vlans(db, row.device_id)
    if row.kind == "vc_id" and row.device_id and row.peer_device_id and row.value is not None:
        return row.value in _pair_pseudowires(db, row.device_id, row.peer_device_id)
    if row.kind == "ipv4" and row.prefix:
        return _any_host_configured(db, ipaddress.IPv4Network(row.prefix))
    return False


def _any_host_configured(db: Session, net: ipaddress.IPv4Network) -> bool:
    """An address of the block is on an interface. Blocks over 256 addresses (customer ranges handed on) are left
    for the engineer to release: one configured address does not mean the whole block is in place."""
    if net.num_addresses > 256:
        return False
    hosts = [str(ip) for ip in net] if net.prefixlen >= 31 else [str(ip) for ip in net.hosts()]
    return db.scalar(select(m.Interface.id).where(or_(*(m.Interface.ipv4_address.like(f"{h}/%") for h in hosts)))
                     .limit(1)) is not None


# --------------------------------------------------------------------------- VLAN


def vlans_in_use(db: Session, device_id: int) -> dict[int, list[dict[str, Any]]]:
    usage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for vlan, names in _sub_vlans(db, device_id).items():
        usage[vlan].append({"source": "interface", "detail": ", ".join(sorted(names)[:4])})
    for row in db.scalars(_active("vlan").where(m.ResourceReservation.device_id == device_id)):
        usage[int(row.value or 0)].append({"source": "reservation", "detail": row.label, "reservation_id": row.id})
    return dict(sorted(usage.items()))


def next_free_vlans(db: Session, device_ids: Iterable[int], count: int = 1, start: int = VLAN_MIN,
                    end: int = VLAN_MAX) -> list[int]:
    """Lowest VLANs free on every given device (both ends of a service)."""
    taken: set[int] = set()
    for device_id in device_ids:
        taken |= set(vlans_in_use(db, device_id))
    free = [vlan for vlan in range(max(start, VLAN_MIN), min(end, VLAN_MAX) + 1) if vlan not in taken]
    return free[:count]


# --------------------------------------------------------------------------- VC id


def vc_ids_in_use(db: Session, device_id: int, peer_device_id: int) -> dict[int, list[dict[str, Any]]]:
    a, b = _pair(device_id, peer_device_id)
    usage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for vc_id, where in _pair_pseudowires(db, a, b).items():
        usage[vc_id].extend({"source": "config", "detail": w} for w in where)
    for row in db.scalars(_active("vc_id").where(m.ResourceReservation.device_id == a,
                                                 m.ResourceReservation.peer_device_id == b)):
        usage[int(row.value or 0)].append({"source": "reservation", "detail": row.label, "reservation_id": row.id})
    return dict(sorted(usage.items()))


def next_free_vc_ids(db: Session, device_id: int, peer_device_id: int, wanted: list[int]) -> dict[int, int]:
    """Per wanted id (the network's habit is VC id = VLAN): itself when free, else the next free id above it."""
    taken = set(vc_ids_in_use(db, device_id, peer_device_id))
    # Free wishes keep their own id first, so a taken one never pushes a free neighbour off its value.
    out: dict[int, int] = {wish: wish for wish in wanted if wish not in taken}
    for wish in wanted:
        if wish in out:
            continue
        candidate = wish
        while candidate in taken or candidate in out.values() or candidate in wanted:
            candidate += 1
        out[wish] = candidate
    return {wish: out[wish] for wish in wanted}


# --------------------------------------------------------------------------- IPv4


def parse_prefix(value: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(value.strip(), strict=True)
    except ValueError as exc:
        raise ResourceError(f"Prefix tidak valid: {value} (contoh 10.20.0.0/30, tanpa bit host)") from exc


MAX_CLAIMED_SUBNET = 256  # addresses: a /24 or smaller


def _claimed(iface: ipaddress.IPv4Interface) -> ipaddress.IPv4Network:
    """What an interface address takes from the plan: its subnet (a /30 P2P link takes the whole /30), but only its
    own address when the mask is wider than /24, as on management ports set to /8 or /16 that would otherwise make
    every pool look full."""
    if iface.network.num_addresses <= MAX_CLAIMED_SUBNET:
        return iface.network
    return ipaddress.IPv4Network(f"{iface.ip}/32")


def ip_usage(db: Session, net: ipaddress.IPv4Network) -> list[dict[str, Any]]:
    """Everything inside or overlapping the block: interface addresses, loopbacks, active reservations."""
    out = [{"prefix": str(claimed), "address": str(iface.ip), "source": "interface", "detail": where}
           for iface, where in _interface_networks(db) if (claimed := _claimed(iface)).overlaps(net)]
    for row in db.scalars(_active("ipv4")):
        try:
            reserved = ipaddress.IPv4Network(row.prefix or "")
        except ValueError:
            continue
        if reserved.overlaps(net):
            out.append({"prefix": str(reserved), "address": None, "source": "reservation", "detail": row.label,
                        "reservation_id": row.id})
    return sorted(out, key=lambda item: ipaddress.IPv4Network(item["prefix"]))


def next_free_prefix(db: Session, pool: m.IpPool, prefix_len: int) -> ipaddress.IPv4Network | None:
    block = parse_prefix(pool.cidr)
    if not block.prefixlen <= prefix_len <= 32:
        raise ResourceError(f"Panjang prefix /{prefix_len} di luar pool {pool.cidr}")
    used = [ipaddress.IPv4Network(item["prefix"]) for item in ip_usage(db, block)]
    for candidate in block.subnets(new_prefix=prefix_len):
        if not any(candidate.overlaps(u) for u in used):
            return candidate
    return None


def pool_view(db: Session, pool: m.IpPool, with_usage: bool = False) -> dict[str, Any]:
    block = ipaddress.IPv4Network(pool.cidr)
    usage = ip_usage(db, block)
    used_addresses = len({ip for item in usage for ip in _addresses(item, block)})
    view = {"id": pool.id, "name": pool.name, "cidr": pool.cidr, "purpose": pool.purpose,
            "default_prefix_len": pool.default_prefix_len, "pop_id": pool.pop_id, "note": pool.note,
            "size": block.num_addresses, "used_addresses": used_addresses,
            "used_pct": round(100 * used_addresses / block.num_addresses, 1)}
    if with_usage:
        view["usage"] = usage
    return view


def _addresses(item: dict[str, Any], block: ipaddress.IPv4Network) -> Iterable[int]:
    net = ipaddress.IPv4Network(item["prefix"])
    if not net.overlaps(block):
        return []
    low = max(int(net.network_address), int(block.network_address))
    high = min(int(net.broadcast_address), int(block.broadcast_address))
    return range(low, high + 1)


# --------------------------------------------------------------------------- reserve / release


def _conflicts(db: Session, request: Request) -> list[dict[str, Any]]:
    if request.kind == "vlan":
        held = vlans_in_use(db, request.device_id or 0).get(request.value or 0, [])
        what = f"VLAN {request.value}"
    elif request.kind == "vc_id":
        held = vc_ids_in_use(db, request.device_id or 0, request.peer_device_id or 0).get(request.value or 0, [])
        what = f"VC id {request.value}"
    else:
        net = parse_prefix(request.prefix or "")
        held = ip_usage(db, net)
        what = f"prefix {net}"
    holders = "; ".join(f"{SOURCE_WORDS.get(h['source'], h['source'])} {h['detail']}" for h in held[:3])
    return [{"kind": request.kind, "value": request.value, "prefix": request.prefix, "held_by": held,
             "message": f"{what} sudah dipakai: {holders}"}
            ] if held else []


def _validate(db: Session, request: Request) -> Request:
    if request.kind not in KINDS:
        raise ResourceError(f"Jenis resource tidak dikenal: {request.kind}")
    if request.kind in ("vlan", "vc_id"):
        if request.device_id is None or db.get(m.Device, request.device_id) is None:
            raise ResourceError("Perangkat tidak ditemukan")
        if request.value is None:
            raise ResourceError(f"Isi nilai {request.kind}")
    if request.kind == "vlan" and not VLAN_MIN <= (request.value or 0) <= VLAN_MAX:
        raise ResourceError(f"VLAN {request.value} di luar {VLAN_MIN}–{VLAN_MAX}")
    if request.kind == "vc_id":
        if request.peer_device_id is None or db.get(m.Device, request.peer_device_id) is None:
            raise ResourceError("VC id butuh dua PE: perangkat lawan tidak ditemukan")
        if request.peer_device_id == request.device_id:
            raise ResourceError("VC id butuh dua PE yang berbeda")
        if not 1 <= (request.value or 0) <= VC_ID_MAX:
            raise ResourceError(f"VC id {request.value} di luar rentang")
        request.device_id, request.peer_device_id = _pair(request.device_id, request.peer_device_id)
    if request.kind == "ipv4":
        net = parse_prefix(request.prefix or "")
        request.prefix = str(net)
        if request.pool_id is not None:
            pool = db.get(m.IpPool, request.pool_id)
            if pool is None:
                raise ResourceError("Pool IP tidak ditemukan")
            if not net.subnet_of(ipaddress.IPv4Network(pool.cidr)):
                raise ResourceError(f"{net} tidak berada di pool {pool.name} ({pool.cidr})")
    return request


def reserve(db: Session, requests: list[Request], label: str, user: str, source: str = "manual",
            note: str | None = None, service_id: int | None = None, change_id: int | None = None,
            days: int | None = None) -> list[m.ResourceReservation]:
    """All or nothing: when any value is taken nothing is reserved and ResourceConflict lists every clash."""
    if not requests:
        raise ResourceError("Tidak ada resource yang dipesan")
    settle(db)
    requests = [_validate(db, r) for r in requests]
    _check_batch(requests)
    conflicts = [c for r in requests for c in _conflicts(db, r)]
    if conflicts:
        raise ResourceConflict(conflicts)
    expires = _now() + timedelta(days=days or settings.resource_reservation_days)
    rows = [m.ResourceReservation(kind=r.kind, device_id=r.device_id, peer_device_id=r.peer_device_id, value=r.value,
                                  prefix=r.prefix, pool_id=r.pool_id, status="reserved", source=source,
                                  label=label.strip()[:120] or "-", note=note, service_id=service_id,
                                  change_id=change_id, created_by=user, expires_at=expires)
            for r in requests]
    db.add_all(rows)
    db.commit()
    return rows


def _check_batch(requests: list[Request]) -> None:
    keys = [(r.kind, r.device_id, r.peer_device_id, r.value, r.prefix) for r in requests]
    if len(keys) != len(set(keys)):
        raise ResourceError("Nilai yang sama dipesan dua kali dalam satu permintaan")
    nets = [ipaddress.IPv4Network(r.prefix) for r in requests if r.kind == "ipv4" and r.prefix]
    for i, net in enumerate(nets):
        if any(net.overlaps(other) for other in nets[i + 1:]):
            raise ResourceError(f"Prefix {net} tumpang tindih dengan prefix lain di permintaan ini")


def release(db: Session, reservation_id: int, user: str) -> m.ResourceReservation:
    row = db.get(m.ResourceReservation, reservation_id)
    if row is None:
        raise LookupError("Reservasi tidak ditemukan")
    if row.status != "reserved":
        raise ResourceError(f"Reservasi berstatus {row.status}, tidak bisa dilepas")
    row.status, row.settled_at = "released", _now()
    row.note = ((row.note or "") + f"\nDilepas oleh {user}").strip()
    db.commit()
    return row


def list_reservations(db: Session, kind: str | None = None, status: str | None = None, device_id: int | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
    settle(db)
    query = select(m.ResourceReservation).order_by(m.ResourceReservation.created_at.desc(),
                                                   m.ResourceReservation.id.desc()).limit(limit)
    if kind:
        query = query.where(m.ResourceReservation.kind == kind)
    if status:
        query = query.where(m.ResourceReservation.status == status)
    if device_id:
        query = query.where(or_(m.ResourceReservation.device_id == device_id,
                                m.ResourceReservation.peer_device_id == device_id))
    rows = list(db.scalars(query))
    ids = {i for r in rows for i in (r.device_id, r.peer_device_id) if i}
    hostnames = dict(db.execute(select(m.Device.id, m.Device.hostname).where(m.Device.id.in_(ids))).all()) if ids else {}
    return [reservation_view(r, hostnames) for r in rows]


# --------------------------------------------------------------------------- RANCID pseudowires


def record_pseudowires(db: Session, device_id: int, text: str, vendor: str | None) -> int:
    """Replace the device's pseudowires with those in this config (called for every applied gitweb config)."""
    if vendor not in _PSEUDOWIRE_DIALECTS:
        return 0
    if len(text.splitlines()) < MIN_CONFIG_LINES:
        # RANCID sometimes commits an empty backup (header only) when collection fails: keep what the last real
        # config showed instead of forgetting every pseudowire of the box.
        return 0
    pairs = {(ip, vc) for ip, vc in pw_usage(text, vendor) if vc is not None}
    db.flush()  # rows added earlier in this session (same device in two repos) must be in the DB to be deleted
    db.query(m.ConfigPseudowire).filter(m.ConfigPseudowire.device_id == device_id).delete()
    db.add_all(m.ConfigPseudowire(device_id=device_id, peer_ip=ip, vc_id=vc) for ip, vc in sorted(pairs))
    return len(pairs)
