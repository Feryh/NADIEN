from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..schemas import Ipv6AssignmentPatch, Ipv6PopDecision
from ..services import ipv6_plan as plan_svc
from ..services.auth import CurrentUser, audit, current_user
from ..services.ipv6_export import export_xlsx

router = APIRouter(prefix="/ipv6-plan", tags=["IPv6 plan"])


def _matches(row: dict, city: str | None, band: str | None, q: str | None, status: str | None) -> bool:
    if city and row["city"] != city:
        return False
    if band and row.get("band", "oam") != band:
        return False
    if status and row["status"] != status:
        return False
    return not q or q.lower() in (row["hostname"] or "").lower()


@router.get("/cities")
def cities(db: Session = Depends(get_db)):
    rows = db.scalars(select(m.Ipv6City).order_by(m.Ipv6City.region_nibble, m.Ipv6City.city_index))
    return {"cities": [{"code": c.code, "name": c.name, "region": c.region, "tier": c.tier,
                        "city_index": f"0x{c.city_index:02X}", "infra_block": c.infra_block,
                        "residential_block": c.residential_block, "oam48": c.oam48} for c in rows]}


@router.post("/cities/import")
async def import_cities(request: Request, filename: str = "", db: Session = Depends(get_db),
                        me: CurrentUser = Depends(current_user)):
    """The backbone team's register workbook as the request body (.xlsx); sheet 'City Register' is read."""
    try:
        parsed = plan_svc.parse_register(await request.body())
    except plan_svc.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = plan_svc.import_register(db, parsed)
    audit(db, me.username, "ipv6.register_import", filename or "register", f"{result['cities']} kota")
    db.commit()
    return result


@router.get("/summary")
def plan_summary(db: Session = Depends(get_db)):
    plan = plan_svc.build_plan(db)
    return {"summary": plan_svc.summary(plan), "capacity": plan.capacity}


@router.get("/routers")
def routers(city: str | None = None, band: str | None = Query(None, pattern="^(backbone|access|overflow)$"),
            q: str | None = Query(None, max_length=64), status: str | None = None, db: Session = Depends(get_db)):
    plan = plan_svc.build_plan(db)
    return {"routers": [r for r in plan.routers if _matches(r, city, band, q, status)]}


@router.get("/oam")
def oam(city: str | None = None, q: str | None = Query(None, max_length=64), status: str | None = None,
        db: Session = Depends(get_db)):
    plan = plan_svc.build_plan(db)
    return {"oam": [r for r in plan.oam if _matches(r, city, None, q, status)]}


@router.get("/pops")
def pops(only_unmapped: bool = False, db: Session = Depends(get_db)):
    plan = plan_svc.build_plan(db)
    rows = [plan_svc.pop_view(p) for p in plan.pops if p.devices and (not only_unmapped or not p.city)]
    return {"pops": rows, "unmapped_devices": plan.unmapped}


@router.put("/pops/{pop_id}")
def decide_pop(pop_id: int, payload: Ipv6PopDecision, db: Session = Depends(get_db),
               me: CurrentUser = Depends(current_user)):
    """A planner's decision for the POP: a register city (or parent city), ignore, or still to review."""
    try:
        plan_svc.set_pop_city(db, pop_id, payload.decision, payload.city_code, payload.note, me.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except plan_svc.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"pop_id": pop_id, "decision": payload.decision, "city_code": payload.city_code}


@router.delete("/pops/{pop_id}", status_code=204)
def reset_pop(pop_id: int, db: Session = Depends(get_db)):
    """Back to the automatic match."""
    plan_svc.clear_pop_city(db, pop_id)
    return Response(status_code=204)


@router.post("/save")
def save(db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    """Store the current proposal so its indexes and OAM addresses stay put when devices are added."""
    result = plan_svc.save_plan(db, me.username)
    audit(db, me.username, "ipv6.plan_save", "ipv6 plan", f"{result['written']} ditulis, {result['dropped']} dihapus")
    db.commit()
    return result


@router.patch("/assignments/{device_id}")
def set_assignment(device_id: int, payload: Ipv6AssignmentPatch, db: Session = Depends(get_db)):
    try:
        row = plan_svc.set_status(db, device_id, payload.status)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except plan_svc.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"device_id": row.device_id, "status": row.status}


@router.get("/export")
def export(db: Session = Depends(get_db)):
    data = export_xlsx(plan_svc.build_plan(db))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="ipv6-plan-nadien.xlsx"'})
