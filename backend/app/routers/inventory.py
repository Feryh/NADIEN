from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, object_session

from .. import models as m
from .. import schemas as s
from ..collectors import runner as jobs_runner
from ..crud import crud_router
from ..database import get_db
from ..services.areas import UNMAPPED_AREA

router = APIRouter()

UNASSIGNED_POP = "UNASSIGNED"


def _pop_guard(pop: m.Pop, changes: dict[str, Any] | None) -> None:
    """UNASSIGNED is where collectors park devices whose POP the hostname does not tell; a sync
    recreates it under that code, so renaming or deleting it only produces a second copy.
    Any POP still in use cannot be deleted (SQLite does not enforce foreign keys: its devices
    would silently lose their POP)."""
    if changes is None and pop.code.upper() != UNASSIGNED_POP:
        db = object_session(pop)
        devices = db.scalar(select(func.count()).select_from(m.Device).where(m.Device.pop_id == pop.id)) or 0
        others = sum(db.scalar(select(func.count()).select_from(model).where(column == pop.id)) or 0
                     for model, column in _POP_REFERENCES if model is not m.Device)
        if devices or others:
            raise HTTPException(409, f"POP {pop.code} masih dipakai {devices} perangkat"
                                     f"{f' dan {others} data lain' if others else ''}: pindahkan dulu (edit kolom POP "
                                     f"perangkat) atau gabungkan ke POP lain")
        return
    if pop.code.upper() != UNASSIGNED_POP:
        return
    if changes is None:
        raise HTTPException(422, "POP UNASSIGNED tidak bisa dihapus: tempat parkir perangkat yang POP-nya belum diketahui")
    if str(changes.get("code", pop.code)).upper() != UNASSIGNED_POP:
        raise HTTPException(
            422, "Kode POP UNASSIGNED tidak bisa diubah: isinya perangkat dari berbagai kota. Pindahkan perangkatnya ke "
                 "POP yang benar (edit kolom POP di perangkat, atau 'Isi area otomatis' di Topology).")


# (prefix, model, create schema, read schema, tag, search field)
_ENTITIES = [
    ("/pops", m.Pop, s.PopCreate, s.PopRead, "Node Inventory", "name"),
    ("/devices", m.Device, s.DeviceCreate, s.DeviceRead, "Node Inventory", "hostname"),
    ("/dwdm-nodes", m.DwdmNode, s.DwdmNodeCreate, s.DwdmNodeRead, "External: Node", "name"),
    ("/odfs", m.Odf, s.OdfCreate, s.OdfRead, "External: Node", "name"),
    ("/patch-panels", m.PatchPanel, s.PatchPanelCreate, s.PatchPanelRead, "External: Node", "name"),
    ("/links", m.Link, s.LinkCreate, s.LinkRead, "Link Inventory", "name"),
    ("/fibers", m.Fiber, s.FiberCreate, s.FiberRead, "External: Link", "name"),
    ("/wavelengths", m.DwdmWavelength, s.DwdmWavelengthCreate, s.DwdmWavelengthRead, "External: Link", "name"),
    ("/vendors", m.Vendor, s.VendorCreate, s.VendorRead, "Device Inventory", "name"),
    ("/device-models", m.DeviceModel, s.DeviceModelCreate, s.DeviceModelRead, "Device Inventory", "name"),
    ("/software", m.SoftwareVersion, s.SoftwareVersionCreate, s.SoftwareVersionRead, "Device Inventory", "version"),
    ("/interfaces", m.Interface, s.InterfaceCreate, s.InterfaceRead, "Device Inventory", ("name", "description")),
    ("/customers", m.Customer, s.CustomerCreate, s.CustomerRead, "Customer Database", "name"),
    ("/services", m.Service, s.ServiceCreate, s.ServiceRead, "Customer Database", "circuit_id"),
]
def _device_guard(device: m.Device, changes: dict[str, Any] | None) -> None:
    """A POP picked by hand in the inventory is final: lock it so sync and the hostname rules keep it."""
    if changes and "pop_id" in changes and changes["pop_id"] != device.pop_id:
        changes["pop_locked"] = True


_GUARDS = {"/pops": _pop_guard, "/devices": _device_guard}

for prefix, model, create, read, tag, search in _ENTITIES:
    router.include_router(crud_router(prefix, model, create, read, tag, search, guard=_GUARDS.get(prefix)))


# --------------------------------------------------------------------------- bulk edit of devices


@router.patch("/devices")
def bulk_edit_devices(payload: s.DeviceBulkEditIn, db: Session = Depends(get_db)):
    """Edit several devices at once (Inventory > Router, ticked rows): move them to one POP, locked there like a
    POP picked one by one, and/or set the area of their POPs. The UNASSIGNED POP holds devices of many cities and
    gets no area: it is reported in `skipped_pops`."""
    devices = list(db.scalars(select(m.Device).where(m.Device.id.in_(payload.device_ids))))
    missing = sorted(set(payload.device_ids) - {d.id for d in devices})
    if missing:
        raise HTTPException(404, f"Perangkat tidak ditemukan: {', '.join(map(str, missing[:20]))}")
    moved = 0
    if payload.pop_id is not None:
        if db.get(m.Pop, payload.pop_id) is None:
            raise HTTPException(404, f"POP {payload.pop_id} tidak ditemukan")
        for device in devices:
            if device.pop_id != payload.pop_id:
                device.pop_id = payload.pop_id
                moved += 1
            device.pop_locked = True
    changed, skipped = [], []
    if payload.area is not None or payload.clear_area:
        area = (payload.area or "").strip()
        for pop in db.scalars(select(m.Pop).where(m.Pop.id.in_({d.pop_id for d in devices}))):
            if pop.code.upper() == UNASSIGNED_POP:
                skipped.append(pop.code)
                continue
            pop.region = None if payload.clear_area or area == UNMAPPED_AREA else area
            changed.append(pop.code)
    db.flush()
    following = db.scalar(select(func.count(m.Device.id)).join(m.Pop, m.Pop.id == m.Device.pop_id)
                          .where(m.Pop.code.in_(changed))) if changed else 0
    db.commit()
    return {"devices": len(devices), "devices_moved": moved, "pops_area_changed": sorted(changed),
            "devices_in_changed_pops": following, "skipped_pops": skipped}


# --------------------------------------------------------------------------- merge POPs

# Every column that points at a POP: all of them move to the surviving POP.
_POP_REFERENCES = [
    (m.Device, m.Device.pop_id), (m.DwdmNode, m.DwdmNode.pop_id), (m.Odf, m.Odf.pop_id),
    (m.PatchPanel, m.PatchPanel.pop_id), (m.Fiber, m.Fiber.a_pop_id), (m.Fiber, m.Fiber.b_pop_id),
    (m.Service, m.Service.a_pop_id), (m.Service, m.Service.b_pop_id),
]
# Filled on the surviving POP only where it has no value yet.
_POP_DETAILS = ("region", "city", "address", "latitude", "longitude")


class PopMergeIn(BaseModel):
    target_code: str = Field(min_length=1, max_length=32, description="POP that stays; this one is removed")


@router.post("/pops/{pop_id}/merge", tags=["Node Inventory"])
def merge_pop(pop_id: int, payload: PopMergeIn, db: Session = Depends(get_db)):
    """Merge a duplicate POP (e.g. an inferred JKTS into JKT): everything that points at it moves to
    the target POP, empty details of the target are filled from it, then it is deleted."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    source = db.get(m.Pop, pop_id)
    if source is None:
        raise HTTPException(404, f"POP {pop_id} not found")
    target = db.scalar(select(m.Pop).where(func.upper(m.Pop.code) == payload.target_code.strip().upper()))
    if target is None:
        raise HTTPException(404, f'POP "{payload.target_code}" tidak ada')
    if target.id == source.id:
        raise HTTPException(422, "POP tujuan sama dengan POP ini")
    if source.code.upper() == UNASSIGNED_POP:
        raise HTTPException(422, "UNASSIGNED tidak bisa digabung: isinya perangkat dari berbagai kota, pindahkan satu per satu")
    moved: dict[str, int] = {}
    for model, column in _POP_REFERENCES:
        count = db.execute(update(model).where(column == source.id).values({column.key: target.id})).rowcount
        if count:
            moved[f"{model.__tablename__}.{column.key}"] = count
    for attr in _POP_DETAILS:
        if getattr(target, attr) in (None, "") and getattr(source, attr) not in (None, ""):
            setattr(target, attr, getattr(source, attr))
    db.delete(source)
    db.commit()
    return {"merged": source.code, "into": target.code, "target_id": target.id, "moved": moved}
