from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services import cache
from ..services.twin import TwinError, simulate
from ..services.twin_migration import area_devices, readiness

router = APIRouter(prefix="/twin", tags=["Digital Twin"])


class ChangeBase(BaseModel):
    label: str | None = Field(default=None, max_length=200)  # what the page showed when it was picked


class FailLink(ChangeBase):
    type: Literal["fail_link"]
    link_id: int


class FailDevice(ChangeBase):
    type: Literal["fail_device"]
    device_id: int


class FailPop(ChangeBase):
    type: Literal["fail_pop"]
    pop_id: int


class FailFiber(ChangeBase):
    type: Literal["fail_fiber"]
    fiber_id: int


class Growth(ChangeBase):
    type: Literal["growth"]
    pct: float = Field(ge=-90, le=1000)
    area: str | None = Field(default=None, max_length=64)  # None = whole network


class UpgradeLink(ChangeBase):
    type: Literal["upgrade_link"]
    link_id: int
    capacity_gbps: float = Field(gt=0, le=100000)


class AddLink(ChangeBase):
    type: Literal["add_link"]
    a_device_id: int
    b_device_id: int
    capacity_gbps: float = Field(gt=0, le=100000)
    name: str | None = Field(default=None, max_length=128)


Change = Annotated[FailLink | FailDevice | FailPop | FailFiber | Growth | UpgradeLink | AddLink,
                   Field(discriminator="type")]


class SimulateIn(BaseModel):
    changes: list[Change] = Field(min_length=1, max_length=200)


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    changes: list[Change] = Field(default_factory=list, max_length=200)


class ScenarioPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    changes: list[Change] | None = Field(default=None, max_length=200)


def _run(db: Session, changes: list[dict]) -> dict:
    try:
        return simulate(db, changes)
    except TwinError as exc:
        raise HTTPException(422, str(exc)) from exc


def _scenario(db: Session, scenario_id: int) -> m.TwinScenario:
    scenario = db.get(m.TwinScenario, scenario_id)
    if scenario is None:
        raise HTTPException(404, "Scenario not found")
    return scenario


def _summary(s: m.TwinScenario) -> dict:
    totals = (s.result or {}).get("totals")
    return {"id": s.id, "name": s.name, "description": s.description, "changes": len(s.changes or []),
            "run_at": s.run_at.isoformat() if s.run_at else None, "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "totals": totals, "services": (s.result or {}).get("services", {}).get("counts")}


def _commit(db: Session, name: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Nama skenario '{name}' sudah dipakai") from exc


@router.post("/simulate")
def simulate_changes(payload: SimulateIn, db: Session = Depends(get_db)):
    """Simulate without saving."""
    return _run(db, [c.model_dump() for c in payload.changes])


@router.get("/scenarios")
def list_scenarios(db: Session = Depends(get_db)):
    return [_summary(s) for s in db.scalars(select(m.TwinScenario).order_by(m.TwinScenario.updated_at.desc()))]


@router.post("/scenarios", status_code=201)
def create_scenario(payload: ScenarioIn, db: Session = Depends(get_db)):
    scenario = m.TwinScenario(name=payload.name, description=payload.description,
                              changes=[c.model_dump() for c in payload.changes])
    db.add(scenario)
    _commit(db, payload.name)
    return {**_summary(scenario), "changes": scenario.changes, "result": None}


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: int, db: Session = Depends(get_db)):
    s = _scenario(db, scenario_id)
    return {**_summary(s), "changes": s.changes, "result": s.result}


@router.patch("/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, payload: ScenarioPatch, db: Session = Depends(get_db)):
    s = _scenario(db, scenario_id)
    values = payload.model_dump(exclude_unset=True)
    if "changes" in values:
        s.changes = values.pop("changes") or []
        s.result, s.run_at = None, None  # an old result would no longer describe these changes
    for key, value in values.items():
        setattr(s, key, value)
    _commit(db, s.name)
    return {**_summary(s), "changes": s.changes, "result": s.result}


@router.delete("/scenarios/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int, db: Session = Depends(get_db)):
    db.delete(_scenario(db, scenario_id))
    db.commit()
    return Response(status_code=204)


@router.post("/scenarios/{scenario_id}/run")
def run_scenario(scenario_id: int, db: Session = Depends(get_db)):
    """Simulate on today's network and keep the result with the scenario."""
    s = _scenario(db, scenario_id)
    if not s.changes:
        raise HTTPException(422, "Skenario belum punya perubahan")
    s.result = _run(db, s.changes)
    s.run_at = datetime.now(timezone.utc)
    db.commit()
    return {**_summary(s), "changes": s.changes, "result": s.result}


@router.get("/links")
def link_options(device_id: int | None = None, q: str | None = Query(None, min_length=2, max_length=64),
                 limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)):
    """Links to pick for a change: those of one device, or matching a name / hostname."""
    a, b = m.Device.__table__.alias("a"), m.Device.__table__.alias("b")
    stmt = (select(m.Link.id, m.Link.name, m.Link.capacity_gbps, m.Link.current_gbps, m.Link.status, m.Link.fiber_id,
                   a.c.hostname.label("a_device"), b.c.hostname.label("b_device"))
            .join(a, a.c.id == m.Link.a_device_id).join(b, b.c.id == m.Link.b_device_id))
    if device_id is not None:
        stmt = stmt.where(or_(m.Link.a_device_id == device_id, m.Link.b_device_id == device_id))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(m.Link.name.ilike(like), a.c.hostname.ilike(like), b.c.hostname.ilike(like)))
    rows = db.execute(stmt.order_by(m.Link.capacity_gbps.desc()).limit(limit)).all()
    return [{"link_id": r.id, "name": r.name, "a_device": r.a_device, "b_device": r.b_device,
             "capacity_gbps": r.capacity_gbps, "utilization_pct": round(r.current_gbps / r.capacity_gbps * 100, 1)
             if r.capacity_gbps else None, "status": r.status, "fiber_id": r.fiber_id} for r in rows]


@router.get("/migration/srv6")
def srv6_readiness(db: Session = Depends(get_db)):
    return cache.cached(("twin", "srv6"), lambda: readiness(db))


@router.get("/migration/srv6/devices")
def srv6_devices(area: str = Query(..., min_length=1, max_length=64), db: Session = Depends(get_db)):
    return area_devices(db, area)
