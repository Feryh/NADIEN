import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..schemas import IpAllocateIn, IpPoolIn, ReservationIn
from ..services import resources as res
from ..services.auth import CurrentUser, audit, current_user

router = APIRouter(prefix="/resources", tags=["Resource registry"])


def _device(db: Session, device_id: int) -> m.Device:
    device = db.get(m.Device, device_id)
    if device is None:
        raise HTTPException(404, f"Device {device_id} not found")
    return device


def _reserve(db: Session, requests: list[res.Request], payload_label: str, me: CurrentUser, **kwargs) -> list[dict]:
    try:
        rows = res.reserve(db, requests, payload_label, me.username, **kwargs)
    except res.ResourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    except res.ResourceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return [res.reservation_view(row) for row in rows]


# --------------------------------------------------------------------------- VLAN and VC id


@router.get("/vlans")
def vlans(device_id: int, db: Session = Depends(get_db)):
    """VLANs taken on a device: its sub-interfaces (inventory) and active reservations."""
    device = _device(db, device_id)
    res.settle(db)
    used = res.vlans_in_use(db, device_id)
    return {"device_id": device.id, "hostname": device.hostname, "count": len(used),
            "vlans": [{"vlan": vlan, "used_by": usage} for vlan, usage in used.items()]}


@router.get("/vlans/next")
def next_vlans(device_id: list[int] = Query(..., max_length=4), count: int = Query(1, ge=1, le=64),
               start: int = Query(res.VLAN_MIN, ge=1, le=4094), end: int = Query(res.VLAN_MAX, ge=1, le=4094),
               db: Session = Depends(get_db)):
    """Lowest VLANs free on every listed device (pass both PEs of a service)."""
    for one in device_id:
        _device(db, one)
    res.settle(db)
    return {"device_ids": device_id, "vlans": res.next_free_vlans(db, device_id, count, start, end)}


@router.get("/vc-ids")
def vc_ids(device_id: int, peer_device_id: int, wanted: list[int] = Query([], max_length=64),
           db: Session = Depends(get_db)):
    """VC ids used between two PEs (both RANCID configs and reservations) and, per wanted id (usually the VLAN),
    the id to use: itself when free, else the next free one."""
    a, b = _device(db, device_id), _device(db, peer_device_id)
    if a.id == b.id:
        raise HTTPException(422, "Pilih dua PE yang berbeda")
    res.settle(db)
    used = res.vc_ids_in_use(db, a.id, b.id)
    return {"device": a.hostname, "peer_device": b.hostname,
            "loopbacks": {a.hostname: a.loopback_ipv4, b.hostname: b.loopback_ipv4},
            "vc_ids": [{"vc_id": vc, "used_by": usage} for vc, usage in used.items()],
            "suggested": [{"wanted": w, "vc_id": v} for w, v in res.next_free_vc_ids(db, a.id, b.id, wanted).items()]}


# --------------------------------------------------------------------------- reservations


@router.get("/reservations")
def reservations(kind: str | None = Query(None, pattern="^(vlan|vc_id|ipv4)$"),
                 status: str | None = Query(None, pattern="^(reserved|fulfilled|released|expired)$"),
                 device_id: int | None = None, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    return {"reservations": res.list_reservations(db, kind, status, device_id, limit)}


@router.post("/reservations", status_code=201)
def create_reservations(payload: ReservationIn, db: Session = Depends(get_db),
                        me: CurrentUser = Depends(current_user)):
    """Hold VLANs / VC ids / prefixes for planned work. All or nothing: 409 lists every value already taken."""
    requests = [res.Request(**item.model_dump()) for item in payload.items]
    rows = _reserve(db, requests, payload.label, me, source=payload.source, note=payload.note,
                    service_id=payload.service_id, days=payload.days)
    return {"reservations": rows}


@router.post("/reservations/{reservation_id}/release")
def release_reservation(reservation_id: int, db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    try:
        row = res.release(db, reservation_id, me.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except res.ResourceError as exc:
        raise HTTPException(409, str(exc)) from exc
    return res.reservation_view(row)


# --------------------------------------------------------------------------- IPv4 pools


@router.get("/ip-pools")
def ip_pools(db: Session = Depends(get_db)):
    res.settle(db)
    pools = db.scalars(select(m.IpPool).order_by(m.IpPool.name))
    return {"pools": [res.pool_view(db, pool) for pool in pools]}


@router.post("/ip-pools", status_code=201)
def create_ip_pool(payload: IpPoolIn, db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    try:
        block = res.parse_prefix(payload.cidr)
    except res.ResourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.default_prefix_len is not None and payload.default_prefix_len < block.prefixlen:
        raise HTTPException(422, f"Prefix default /{payload.default_prefix_len} lebih besar dari pool {block}")
    if db.scalar(select(m.IpPool.id).where(m.IpPool.name == payload.name.strip())):
        raise HTTPException(409, f"Pool {payload.name} sudah ada")
    for other in db.scalars(select(m.IpPool)):
        if ipaddress.IPv4Network(other.cidr).overlaps(block):
            raise HTTPException(409, f"{block} tumpang tindih dengan pool {other.name} ({other.cidr})")
    pool = m.IpPool(name=payload.name.strip(), cidr=str(block), purpose=payload.purpose,
                    default_prefix_len=payload.default_prefix_len, pop_id=payload.pop_id, note=payload.note)
    db.add(pool)
    audit(db, me.username, "ip_pool.create", f"ip pool {pool.name}", str(block))
    db.commit()
    return res.pool_view(db, pool)


@router.get("/ip-pools/{pool_id}")
def ip_pool(pool_id: int, db: Session = Depends(get_db)):
    """The pool with every interface address, loopback and active reservation inside it."""
    pool = db.get(m.IpPool, pool_id)
    if pool is None:
        raise HTTPException(404, "Pool IP tidak ditemukan")
    res.settle(db)
    return res.pool_view(db, pool, with_usage=True)


@router.delete("/ip-pools/{pool_id}", status_code=204)
def delete_ip_pool(pool_id: int, db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    pool = db.get(m.IpPool, pool_id)
    if pool is None:
        raise HTTPException(404, "Pool IP tidak ditemukan")
    active = db.scalar(select(m.ResourceReservation.id).where(m.ResourceReservation.pool_id == pool_id,
                                                              m.ResourceReservation.status == "reserved").limit(1))
    if active:
        raise HTTPException(409, "Pool masih punya reservasi aktif: lepas dulu reservasinya")
    audit(db, me.username, "ip_pool.delete", f"ip pool {pool.name}", pool.cidr)
    db.delete(pool)
    db.commit()
    return Response(status_code=204)


@router.post("/ip-pools/{pool_id}/allocate", status_code=201)
def allocate(pool_id: int, payload: IpAllocateIn, db: Session = Depends(get_db),
             me: CurrentUser = Depends(current_user)):
    """Reserve the first free prefix of the given length in the pool."""
    pool = db.get(m.IpPool, pool_id)
    if pool is None:
        raise HTTPException(404, "Pool IP tidak ditemukan")
    prefix_len = payload.prefix_len or pool.default_prefix_len
    if prefix_len is None:
        raise HTTPException(422, "Isi panjang prefix (pool ini tidak punya default)")
    res.settle(db)
    try:
        free = res.next_free_prefix(db, pool, prefix_len)
    except res.ResourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    if free is None:
        raise HTTPException(409, f"Pool {pool.name} penuh untuk /{prefix_len}")
    rows = _reserve(db, [res.Request(kind="ipv4", prefix=str(free), pool_id=pool.id)], payload.label, me,
                    note=payload.note, days=payload.days)
    return rows[0]
