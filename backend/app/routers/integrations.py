"""Containers for data owned by other platforms (fiber, DWDM, ODF, patch panel).

Two ways in:
- push: the external platform (or an ETL job) POSTs records to /ingest/{entity}
- pull: an IntegrationSource connector fetches them on /sources/{id}/sync (connectors TBD)
Both upsert on (source_system, external_id), so re-syncing is idempotent.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from ..crud import crud_router
from ..database import get_db

router = APIRouter(prefix="/integrations", tags=["Integrations"])
router.include_router(
    crud_router("/sources", m.IntegrationSource, s.IntegrationSourceCreate, s.IntegrationSourceRead, "Integrations")
)

EXTERNAL_ENTITIES: dict[str, tuple[type, type]] = {
    "dwdm_nodes": (m.DwdmNode, s.DwdmNodeCreate),
    "odfs": (m.Odf, s.OdfCreate),
    "patch_panels": (m.PatchPanel, s.PatchPanelCreate),
    "fibers": (m.Fiber, s.FiberCreate),
    "dwdm_wavelengths": (m.DwdmWavelength, s.DwdmWavelengthCreate),
}


def upsert_external(db: Session, entity: str, source_system: str, records: list[dict[str, Any]]) -> s.IngestResult:
    if entity not in EXTERNAL_ENTITIES:
        raise HTTPException(404, f"Unknown entity '{entity}'. Use one of: {', '.join(EXTERNAL_ENTITIES)}")
    model, schema = EXTERNAL_ENTITIES[entity]
    now = datetime.now(timezone.utc)
    created = updated = 0
    for raw in records:
        data = schema.model_validate({**raw, "source_system": source_system}).model_dump()
        if not data.get("external_id"):
            raise HTTPException(422, f"Every record needs an external_id (got {raw!r})")
        data["synced_at"] = now
        obj = db.scalar(
            select(model).where(model.source_system == source_system, model.external_id == data["external_id"])
        )
        if obj is None:
            db.add(model(**data))
            created += 1
        else:
            for key, value in data.items():
                setattr(obj, key, value)
            updated += 1
    db.commit()
    return s.IngestResult(entity=entity, created=created, updated=updated)


@router.post("/ingest/{entity}", response_model=s.IngestResult)
def ingest(entity: str, source_system: str, records: list[dict[str, Any]], db: Session = Depends(get_db)):
    """Upsert records from an external platform into one of the external containers."""
    return upsert_external(db, entity, source_system, records)


@router.post("/sources/{source_id}/sync", response_model=s.IntegrationSourceRead)
def sync_source(source_id: int, db: Session = Depends(get_db)):
    source = db.get(m.IntegrationSource, source_id)
    if source is None:
        raise HTTPException(404, "Integration source not found")
    source.last_sync_at = datetime.now(timezone.utc)
    if not source.enabled:
        source.last_status = "skipped: source disabled"
    else:
        # Connector per system_type plugs in here (REST/GraphQL client -> upsert_external).
        source.last_status = f"not implemented: no connector for system_type '{source.system_type}' yet"
    db.commit()
    db.refresh(source)
    return source
