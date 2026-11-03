import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services.configgen import Addressing
from ..services.mop import MopMeta, build_mop
from ..services.mop_pdf import render_mop_pdf
from .configs import addressing_params

router = APIRouter(prefix="/mop", tags=["MoP Generator"])


def _meta(
    number: str = Query("", max_length=64),
    change_ticket: str = Query("", max_length=64),
    window_start: str = Query("", max_length=32),
    window_end: str = Query("", max_length=32),
    executor: str = Query("", max_length=128),
    reviewer: str = Query("", max_length=128),
    approver: str = Query("", max_length=128),
) -> MopMeta:
    return MopMeta(number, change_ticket, window_start, window_end, executor, reviewer, approver)


def _service(db: Session, service_id: int) -> m.Service:
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    return service


@router.get("/services/{service_id}")
def mop_content(service_id: int, meta: MopMeta = Depends(_meta), addressing: Addressing = Depends(addressing_params),
                db: Session = Depends(get_db)):
    """MoP content as JSON, for the preview on the MoP Generator page."""
    return build_mop(db, _service(db, service_id), meta, addressing)


@router.get("/services/{service_id}/pdf")
def mop_pdf(service_id: int, meta: MopMeta = Depends(_meta), addressing: Addressing = Depends(addressing_params),
            db: Session = Depends(get_db)):
    service = _service(db, service_id)
    pdf = render_mop_pdf(build_mop(db, service, meta, addressing))
    filename = "MoP-" + re.sub(r"[^A-Za-z0-9._-]+", "_", service.circuit_id) + ".pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
